#!/bin/bash
# nginx-setup.sh — HTTPS reverse proxy para ROK VPN Portal
# Expone el portal en HTTPS puerto 8447 (ya abierto en nftables + IONOS).
# Puerto 443 permanece ocupado por wstunnel.
#
# Uso: bash nginx-setup.sh
# Ejecutar como root en el VPS tras instalar el portal con portal-setup.sh.
set -euo pipefail

PORTAL_PORT="8080"
HTTPS_PORT="8447"
CERT_DIR="/etc/ssl/rok-vpn"
NGINX_CONF="/etc/nginx/sites-available/rok-portal"
VPS_IP="74.208.44.254"

echo "==> ROK VPN Portal — configuración HTTPS (nginx en :${HTTPS_PORT})"

# ── 1. Instalar nginx ─────────────────────────────────────────────────────────
echo "[1/5] Instalando nginx..."
apt-get update -q
apt-get install -y -q nginx

# ── 2. Certificado autofirmado ────────────────────────────────────────────────
echo "[2/5] Generando certificado autofirmado..."
mkdir -p "$CERT_DIR"
openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout "${CERT_DIR}/rok-portal.key" \
  -out    "${CERT_DIR}/rok-portal.crt" \
  -subj   "/CN=${VPS_IP}/O=ROK VPN/C=ES"
chmod 600 "${CERT_DIR}/rok-portal.key"
echo "    Certificado en ${CERT_DIR}/ (válido 10 años)"
echo ""
echo "    NOTA: Los navegadores mostrarán advertencia por ser autofirmado."
echo "    Para certificado de confianza con dominio propio:"
echo "      apt install certbot python3-certbot-nginx"
echo "      certbot --nginx -d tu.dominio.com"
echo ""

# ── 3. Configuración nginx ────────────────────────────────────────────────────
echo "[3/5] Creando configuración nginx..."
cat > "$NGINX_CONF" << EOF
server {
    listen ${HTTPS_PORT} ssl;
    server_name ${VPS_IP};

    ssl_certificate     ${CERT_DIR}/rok-portal.crt;
    ssl_certificate_key ${CERT_DIR}/rok-portal.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # Cabeceras de seguridad mínimas para panel admin
    add_header X-Frame-Options DENY;
    add_header X-Content-Type-Options nosniff;
    add_header Referrer-Policy strict-origin-when-cross-origin;

    # La landing y las pantallas móviles llevan las imágenes y el runtime
    # embebidos en base64 y rondan los 2,3 MB cada una. Sin gzip cada visita
    # descarga ese peso completo; comprimidas bajan a unos pocos cientos de KB.
    gzip              on;
    gzip_types        text/html text/css application/javascript application/json image/svg+xml;
    gzip_min_length   1024;
    gzip_comp_level   6;
    gzip_proxied      any;
    gzip_vary         on;

    # La imagen de Open Graph la piden los scrapers de WhatsApp e iMessage en
    # cada compartición; se sirve desde disco sin pasar por gunicorn.
    location /static/ {
        alias       /opt/rok-vpn/portal/static/;
        expires     7d;
        add_header  Cache-Control "public, max-age=604800";
        access_log  off;
    }

    location / {
        proxy_pass         http://127.0.0.1:${PORTAL_PORT};
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        # ProxyFix del portal lee la última IP de esta cabecera para aplicar el
        # límite por cliente. nginx la reescribe siempre, así que un cliente no
        # puede falsear la suya enviando su propio X-Forwarded-For.
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;
        proxy_read_timeout 30s;
    }
}
EOF

ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/rok-portal

# Eliminar el site default si interfiere
if [ -L /etc/nginx/sites-enabled/default ]; then
  rm /etc/nginx/sites-enabled/default
  echo "    Site 'default' de nginx desactivado."
fi

# ── 4. Firewall — abrir TCP 8447 ──────────────────────────────────────────────
echo "[4/5] Verificando regla nftables para TCP ${HTTPS_PORT}..."
if ! nft list ruleset | grep -q "tcp dport ${HTTPS_PORT}"; then
  nft add rule inet filter input tcp dport "${HTTPS_PORT}" accept comment '"rok-portal-https"'
  echo "    Regla añadida."
else
  echo "    Regla ya existe."
fi

if [ -f /etc/nftables.conf ]; then
  nft list ruleset > /etc/nftables.conf
  echo "    nftables.conf actualizado."
fi

# ── 5. Activar nginx ──────────────────────────────────────────────────────────
echo "[5/5] Activando nginx..."
nginx -t
systemctl enable nginx
systemctl restart nginx
sleep 1

if systemctl is-active --quiet nginx; then
  echo ""
  echo "  ✓ Portal disponible en https://${VPS_IP}:${HTTPS_PORT}/login"
  echo "  ✓ HTTP en http://${VPS_IP}:${PORTAL_PORT}/login (solo acceso local)"
  echo ""
  echo "  El navegador mostrará advertencia por certificado autofirmado."
  echo "  Acepta la excepción de seguridad para continuar."
else
  echo "  ✗ nginx no arrancó. Revisa: journalctl -u nginx -n 30"
  exit 1
fi

echo ""
echo "==> nginx configurado."
echo "    Logs: journalctl -u nginx -f"
echo "    Renovar cert: openssl x509 -in ${CERT_DIR}/rok-portal.crt -noout -dates"
