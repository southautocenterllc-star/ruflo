#!/bin/bash
# wstunnel server setup for ROK VPN
# Run as root on VPS (74.208.44.254)
set -euo pipefail

WSTUNNEL_VERSION="10.6.2"   # versión validada end-to-end en Task 10
WG_PORT=1194
WST_PORT=443

echo "[1/5] Downloading wstunnel v${WSTUNNEL_VERSION}..."
ARCH=$(uname -m)
case "$ARCH" in
  x86_64) ARCH_TAG="amd64" ;;
  aarch64|arm64) ARCH_TAG="arm64" ;;
  *) echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

DOWNLOAD_URL="https://github.com/erebe/wstunnel/releases/download/v${WSTUNNEL_VERSION}/wstunnel_${WSTUNNEL_VERSION}_linux_${ARCH_TAG}.tar.gz"

wget -q "$DOWNLOAD_URL" -O /tmp/wstunnel.tar.gz || {
  # Try alternative URL format
  DOWNLOAD_URL="https://github.com/erebe/wstunnel/releases/download/v${WSTUNNEL_VERSION}/wstunnel_${WSTUNNEL_VERSION}_linux_amd64.tar.gz"
  wget -q "$DOWNLOAD_URL" -O /tmp/wstunnel.tar.gz
}

tar -xzf /tmp/wstunnel.tar.gz -C /tmp/
mv /tmp/wstunnel /usr/local/bin/wstunnel
chmod +x /usr/local/bin/wstunnel
echo "wstunnel $(/usr/local/bin/wstunnel --version 2>&1 | head -1)"

echo "[2/5] Adding TCP ${WST_PORT} to nftables..."
# Add rule if not already present
nft list ruleset | grep -q "tcp dport ${WST_PORT}" || \
  nft add rule inet filter input tcp dport "${WST_PORT}" accept comment '"wstunnel"'

# Persist: append to the current nftables.conf if not already there
grep -q "wstunnel" /etc/nftables.conf 2>/dev/null || {
  echo "# wstunnel TCP ${WST_PORT} (added by rok-vpn setup)" >> /etc/nftables.conf
}

echo "[3/5] Creating systemd service..."
cat > /etc/systemd/system/wstunnel-server.service << EOF
[Unit]
Description=wstunnel — WireGuard over WebSocket
Documentation=https://github.com/erebe/wstunnel
After=network-online.target wg-quick@wg0.service
Wants=network-online.target wg-quick@wg0.service

[Service]
Type=simple
ExecStartPre=/bin/bash -c "nft list ruleset | grep -q 'tcp dport ${WST_PORT}' || nft add rule inet filter input tcp dport ${WST_PORT} accept comment 'wstunnel'"
ExecStart=/usr/local/bin/wstunnel server --restrict-to '127.0.0.1:${WG_PORT}' ws://0.0.0.0:${WST_PORT}
Restart=always
RestartSec=5
User=root
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

echo "[4/5] Enabling and starting wstunnel-server..."
systemctl daemon-reload
systemctl enable wstunnel-server
systemctl restart wstunnel-server
sleep 2

echo "[5/5] Status check..."
systemctl status wstunnel-server --no-pager -l
echo ""
ss -tlnp | grep ':443' || echo "WARNING: nothing listening on TCP 443"
echo ""
echo "Done! VPS wstunnel server is up."
echo "WireGuard wg0 status:"
wg show wg0 2>/dev/null || echo "(wg0 not up — run: wg-quick up wg0)"
