#!/bin/bash
# nginx-setup.sh — HTTPS reverse proxy para ROK VPN Portal
#
# Uso básico (self-signed, para pruebas):
#   bash nginx-setup.sh
#
# Uso con dominio real y Let's Encrypt:
#   DOMAIN=rokvpn.com bash nginx-setup.sh
#
# El puerto 443 está ocupado por wstunnel (VPN bypass) así que nginx
# expone HTTPS en el 8447 (ya abierto en nftables + IONOS).
# El portal también se sirve en HTTP puro (80) para que la landing
# sea accesible en https://rokvpn.com sin numero de puerto.
#
# Ejecutar como root en el VPS.
set -euo pipefail

PORTAL_PORT="8080"
HTTPS_PORT="8447"
CERT_DIR="/etc/ssl/rok-vpn"
NGINX_CONF="/etc/nginx/sites-available/rok-portal"
VPS_IP="74.208.44.254"

# Si se pasa DOMAIN se usa Let's Encrypt; si no, certificado autofirmado.
DOMAIN="${DOMAIN:-}"
SERVER_NAME="${DOMAIN:-$VPS_IP}"
WEBROOT="/var/www/certbot"

echo "==> ROK VPN Portal — configuración nginx"
[ -n "$DOMAIN" ] && echo "    Modo dominio: $DOMAIN (Let's Encrypt)" \
                 || echo "    Modo IP: $VPS_IP (self-signed)"

# ── 1. Instalar nginx (+ certbot si hay dominio) ──────────────────────────────
echo "[1/5] Instalando nginx..."
apt-get update -q
apt-get install -y -q nginx

if [ -n "$DOMAIN" ]; then
  echo "    Instalando certbot..."
  apt-get install -y -q certbot python3-certbot-nginx
fi

# ── 2. Certificado ────────────────────────────────────────────────────────────
echo "[2/5] Obteniendo certificado..."

if [ -n "$DOMAIN" ]; then
  # certbot usa un directorio webroot efímero en puerto 80.
  # Para eso necesitamos un nginx mínimo corriendo en 80 ANTES del challenge.
  mkdir -p "$WEBROOT"

  # Config temporal de nginx sólo para el challenge (http en 80)
  cat > "$NGINX_CONF" << TMPEOF
server {
    listen 80;
    server_name ${DOMAIN} www.${DOMAIN};
    root ${WEBROOT};
    location /.well-known/acme-challenge/ { }
    location / { return 301 https://\$host:${HTTPS_PORT}\$request_uri; }
}
TMPEOF

  # Abrir puerto 80 en nftables si hace falta
  if ! nft list ruleset | grep -q "tcp dport 80 accept"; then
    nft add rule inet filter input tcp dport 80 accept
    echo "    Puerto 80 abierto en nftables."
  fi

  ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/rok-portal
  rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
  nginx -t
  systemctl reload nginx 2>/dev/null || systemctl start nginx

  echo "    Solicitando certificado para ${DOMAIN} y www.${DOMAIN}..."
  certbot certonly --webroot -w "$WEBROOT" \
    -d "$DOMAIN" -d "www.${DOMAIN}" \
    --non-interactive --agree-tos --email "admin@${DOMAIN}" \
    --no-eff-email

  SSL_CERT="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
  SSL_KEY="/etc/letsencrypt/live/${DOMAIN}/privkey.pem"
  echo "    Certificado Let's Encrypt obtenido."

  # Auto-renovación (Let's Encrypt caduca en 90 días)
  if ! crontab -l 2>/dev/null | grep -q certbot; then
    (crontab -l 2>/dev/null; echo "0 3 * * * certbot renew --quiet && systemctl reload nginx") | crontab -
    echo "    Cron de renovación añadido (diario a las 03:00)."
  fi

else
  # Self-signed para pruebas con IP desnuda
  mkdir -p "$CERT_DIR"
  openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
    -keyout "${CERT_DIR}/rok-portal.key" \
    -out    "${CERT_DIR}/rok-portal.crt" \
    -subj   "/CN=${VPS_IP}/O=ROK VPN/C=US"
  chmod 600 "${CERT_DIR}/rok-portal.key"
  SSL_CERT="${CERT_DIR}/rok-portal.crt"
  SSL_KEY="${CERT_DIR}/rok-portal.key"
  echo "    Certificado autofirmado en ${CERT_DIR}/ (válido 10 años)"
  echo ""
  echo "    NOTA: El navegador mostrará advertencia. Acepta la excepción."
  echo "    Para certificado válido ejecuta: DOMAIN=rokvpn.com bash nginx-setup.sh"
fi

# ── 3. Configuración nginx final ──────────────────────────────────────────────
echo "[3/5] Creando configuración nginx..."
cat > "$NGINX_CONF" << EOF
# ── HTTP (80) — landing pública ────────────────────────────────────────────
server {
    listen 80;
    server_name ${SERVER_NAME} www.${SERVER_NAME:-};

    # La landing es pública y no necesita HTTPS; gzip es crucial porque pesa ~2,3 MB.
    gzip              on;
    gzip_types        text/html text/css application/javascript application/json image/svg+xml;
    gzip_min_length   1024;
    gzip_comp_level   6;
    gzip_proxied      any;
    gzip_vary         on;

    # Sirve static/ directamente desde disco (OG image, futuros assets)
    location /static/ {
        alias      /opt/rok-vpn/portal/static/;
        expires    7d;
        add_header Cache-Control "public, max-age=604800";
        access_log off;
    }

    # Todo lo demás pasa a gunicorn
    location / {
        proxy_pass         http://127.0.0.1:${PORTAL_PORT};
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto http;
        proxy_read_timeout 30s;
    }
}

# ── HTTPS (${HTTPS_PORT}) — panel admin y API ─────────────────────────────────────
server {
    listen ${HTTPS_PORT} ssl;
    server_name ${SERVER_NAME} www.${SERVER_NAME:-};

    ssl_certificate     ${SSL_CERT};
    ssl_certificate_key ${SSL_KEY};
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    add_header X-Frame-Options DENY;
    add_header X-Content-Type-Options nosniff;
    add_header Referrer-Policy strict-origin-when-cross-origin;
    add_header Strict-Transport-Security "max-age=31536000" always;

    gzip              on;
    gzip_types        text/html text/css application/javascript application/json image/svg+xml;
    gzip_min_length   1024;
    gzip_comp_level   6;
    gzip_proxied      any;
    gzip_vary         on;

    location /static/ {
        alias      /opt/rok-vpn/portal/static/;
        expires    7d;
        add_header Cache-Control "public, max-age=604800";
        access_log off;
    }

    location / {
        proxy_pass         http://127.0.0.1:${PORTAL_PORT};
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        # ProxyFix del portal lee esta cabecera; nginx la reescribe para que
        # un cliente no pueda falsear su IP enviando su propio X-Forwarded-For.
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;
        proxy_read_timeout 30s;
    }
}
EOF

ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/rok-portal
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

# ── 4. Firewall ────────────────────────────────────────────────────────────────
echo "[4/5] Verificando reglas nftables..."
for PORT in 80 "$HTTPS_PORT"; do
  if ! nft list ruleset | grep -q "tcp dport ${PORT}"; then
    nft add rule inet filter input tcp dport "${PORT}" accept
    echo "    Puerto ${PORT} abierto."
  else
    echo "    Puerto ${PORT}: ya abierto."
  fi
done

if [ -f /etc/nftables.conf ]; then
  nft list ruleset > /etc/nftables.conf
  echo "    nftables.conf actualizado."
fi

# ── 5. Activar nginx ───────────────────────────────────────────────────────────
echo "[5/5] Activando nginx..."
nginx -t
systemctl enable nginx
systemctl restart nginx
sleep 1

if systemctl is-active --quiet nginx; then
  echo ""
  if [ -n "$DOMAIN" ]; then
    echo "  ✓ Landing pública: http://${DOMAIN}"
    echo "  ✓ Panel admin:     https://${DOMAIN}:${HTTPS_PORT}/login"
    echo "  ✓ API móvil:       https://${DOMAIN}:${HTTPS_PORT}/api/v1/"
  else
    echo "  ✓ Portal (self-signed): https://${VPS_IP}:${HTTPS_PORT}/login"
    echo "  ✓ HTTP:                 http://${VPS_IP}/login"
  fi
else
  echo "  ✗ nginx no arrancó. Revisa: journalctl -u nginx -n 50"
  exit 1
fi

echo ""
echo "==> nginx configurado."
echo "    Logs:   journalctl -u nginx -f"
echo "    Config: ${NGINX_CONF}"
[ -n "$DOMAIN" ] && echo "    Cert:   /etc/letsencrypt/live/${DOMAIN}/"
