#!/bin/bash
# nginx-setup.sh — HTTPS reverse proxy with Let's Encrypt for ROK VPN Portal
#
# After running this script:
#   https://rokvpn.com/ → ROK VPN Portal (trusted cert, works on Safari iOS)
#   wstunnel moves from TCP 443 → TCP 8443
#
# Prerequisites:
#   - DNS A record for rokvpn.com must point to this VPS (74.208.44.254)
#   - Portal running: systemctl status rok-portal
#   - wstunnel-vps-setup.sh already installed (will be restarted on new port)
#
# Run as root: bash nginx-setup.sh
# Idempotent: safe to re-run.
set -euo pipefail

DOMAIN="${ROK_DOMAIN:-rokvpn.com}"
ADMIN_EMAIL="${ROK_ADMIN_EMAIL:-admin@rokvpn.com}"
PORTAL_PORT="8080"
NGINX_CONF="/etc/nginx/sites-available/rok-portal"
CERT_PATH="/etc/letsencrypt/live/${DOMAIN}"

echo "==> ROK VPN Portal — HTTPS setup for ${DOMAIN}"

# ── 1. Install nginx and certbot ──────────────────────────────────────────────
echo "[1/5] Installing nginx and certbot..."
apt-get update -q
apt-get install -y -q nginx certbot python3-certbot-nginx

# ── 2. Free port 443 so certbot can run ───────────────────────────────────────
echo "[2/5] Freeing port 443 for ACME challenge..."
# wstunnel was on 443; stop it before certbot needs that port
systemctl stop wstunnel-server 2>/dev/null || true

# ── 3. Temporary HTTP config for ACME challenge ───────────────────────────────
echo "[3/5] Obtaining Let's Encrypt certificate..."

# Minimal config so nginx can serve the ACME webroot challenge on port 80
cat > "$NGINX_CONF" << NGINX_TMP
server {
    listen 80;
    server_name ${DOMAIN} www.${DOMAIN};

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        proxy_pass http://127.0.0.1:${PORTAL_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
NGINX_TMP

ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/rok-portal
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
mkdir -p /var/www/certbot
nginx -t
systemctl enable nginx
systemctl restart nginx

if [ -f "${CERT_PATH}/fullchain.pem" ]; then
    echo "    Certificate already exists — skipping issuance."
else
    certbot certonly \
        --webroot \
        --webroot-path /var/www/certbot \
        --domain "${DOMAIN}" \
        --email "${ADMIN_EMAIL}" \
        --agree-tos \
        --non-interactive \
        --no-eff-email \
    || {
        echo ""
        echo "    Webroot challenge failed. Trying standalone (stops nginx briefly)..."
        systemctl stop nginx
        certbot certonly \
            --standalone \
            --domain "${DOMAIN}" \
            --email "${ADMIN_EMAIL}" \
            --agree-tos \
            --non-interactive \
            --no-eff-email \
        || {
            echo ""
            echo "ERROR: Could not obtain certificate for ${DOMAIN}."
            echo "Check that:"
            echo "  1. DNS: ${DOMAIN} → $(curl -s https://api.ipify.org 2>/dev/null || echo '<this VPS IP>')"
            echo "  2. Port 80 is open in the firewall"
            echo "  3. Try manually: certbot certonly --standalone -d ${DOMAIN}"
            exit 1
        }
        systemctl start nginx
    }
fi

# ── 4. Full HTTPS nginx config ─────────────────────────────────────────────────
echo "[4/5] Writing production HTTPS config..."

cat > "$NGINX_CONF" << NGINX_CONF
# ROK VPN Portal — nginx
# HTTP → HTTPS redirect + ACME challenge passthrough
server {
    listen 80;
    server_name ${DOMAIN} www.${DOMAIN};

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

# HTTPS portal
server {
    listen 443 ssl;
    server_name ${DOMAIN} www.${DOMAIN};

    ssl_certificate     ${CERT_PATH}/fullchain.pem;
    ssl_certificate_key ${CERT_PATH}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:!aNULL:!MD5;
    ssl_prefer_server_ciphers off;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 10m;

    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options DENY;
    add_header X-Content-Type-Options nosniff;
    add_header X-XSS-Protection "1; mode=block";
    add_header Referrer-Policy "strict-origin-when-cross-origin";

    # Gzip compression
    gzip              on;
    gzip_types        text/html text/css application/javascript application/json image/svg+xml;
    gzip_min_length   1024;
    gzip_comp_level   6;
    gzip_proxied      any;
    gzip_vary         on;

    # Static files served directly from disk (OG image, etc.)
    location /static/ {
        alias      /opt/rok-vpn/portal/static/;
        expires    7d;
        add_header Cache-Control "public, max-age=604800";
        access_log off;
    }

    # ROK Portal (Flask/Gunicorn on 8080)
    location / {
        proxy_pass         http://127.0.0.1:${PORTAL_PORT};
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
    }
}
NGINX_CONF

nginx -t
systemctl reload nginx

# ── 5. Auto-renewal and wstunnel restart ──────────────────────────────────────
echo "[5/5] Configuring cert renewal and restarting wstunnel..."

# Renewal hook: reload nginx after cert renewal
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh << 'HOOK'
#!/bin/bash
systemctl reload nginx 2>/dev/null || true
HOOK
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

# Enable renewal timer (systemd) or cron fallback
systemctl enable certbot.timer 2>/dev/null || \
    { (crontab -l 2>/dev/null | grep -v certbot; \
       echo "0 3 * * * certbot renew --quiet --post-hook 'systemctl reload nginx'") | crontab -; }

# Restart wstunnel on port 8443 (run wstunnel-vps-setup.sh first to set WST_PORT=8443)
systemctl start wstunnel-server 2>/dev/null || true

# Open 80/443 in nftables if not already open
for PORT in 80 443; do
    if ! nft list ruleset 2>/dev/null | grep -q "tcp dport ${PORT}"; then
        nft add rule inet filter input tcp dport "${PORT}" accept 2>/dev/null || true
    fi
done
if [ -f /etc/nftables.conf ]; then
    nft list ruleset > /etc/nftables.conf 2>/dev/null || true
fi

echo ""
echo "==> Setup complete!"
echo ""
echo "  Portal: https://${DOMAIN}/"
echo "  Admin:  https://${DOMAIN}/login"
echo "  wstunnel: TCP 8443 (update client scripts — see wstunnel-vps-setup.sh)"
echo ""
EXPIRY=$(certbot certificates 2>/dev/null | grep "Expiry Date" | head -1 | awk '{print $3, $4, $5}' || echo "run: certbot certificates")
echo "  Cert expiry: ${EXPIRY}"
echo "  Auto-renewal: enabled"
echo ""
echo "  Verify: curl -sv https://${DOMAIN}/login 2>&1 | grep -E 'SSL|subject|issuer|HTTP'"
echo "  Logs:   journalctl -u nginx -f"
