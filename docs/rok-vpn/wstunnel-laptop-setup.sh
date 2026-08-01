#!/bin/bash
# wstunnel client setup for ROK VPN laptop
# Run as root on laptop (requires sudo)
set -euo pipefail

WSTUNNEL_VERSION="10.6.2"   # versión validada end-to-end en Task 10
VPS_IP="74.208.44.254"
VPS_WST_PORT=443       # wstunnel WebSocket port on VPS
LOCAL_UDP_PORT=51820   # local port wstunnel listens on (WireGuard will point here)
WG_IFACE="rok0"
WG_CONF="/etc/wireguard/rok0.conf"

echo "[1/5] Downloading wstunnel v${WSTUNNEL_VERSION}..."
ARCH=$(uname -m)
case "$ARCH" in
  x86_64) ARCH_TAG="amd64" ;;
  aarch64|arm64) ARCH_TAG="arm64" ;;
  *) echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

DOWNLOAD_URL="https://github.com/erebe/wstunnel/releases/download/v${WSTUNNEL_VERSION}/wstunnel_${WSTUNNEL_VERSION}_linux_${ARCH_TAG}.tar.gz"
wget -q "$DOWNLOAD_URL" -O /tmp/wstunnel.tar.gz
tar -xzf /tmp/wstunnel.tar.gz -C /tmp/
mv /tmp/wstunnel /usr/local/bin/wstunnel
chmod +x /usr/local/bin/wstunnel
echo "wstunnel $(/usr/local/bin/wstunnel --version 2>&1 | head -1)"

echo "[2/5] Bringing down rok0 if up..."
wg-quick down "${WG_IFACE}" 2>/dev/null || true

echo "[3/5] Updating rok0.conf endpoint to use wstunnel local port..."
# Backup original
cp "${WG_CONF}" "${WG_CONF}.bak.$(date +%Y%m%d%H%M%S)"

# Change endpoint from VPS_IP:1194 to 127.0.0.1:LOCAL_UDP_PORT
sed -i "s|Endpoint = .*|Endpoint = 127.0.0.1:${LOCAL_UDP_PORT}|" "${WG_CONF}"
echo "Updated Endpoint in ${WG_CONF}:"
grep "Endpoint" "${WG_CONF}"

echo "[4/5] Creating wstunnel-client systemd service..."
cat > /etc/systemd/system/wstunnel-client.service << EOF
[Unit]
Description=wstunnel client — WireGuard over WebSocket to ROK VPN
Documentation=https://github.com/erebe/wstunnel
After=network-online.target
Wants=network-online.target
Before=wg-quick@rok0.service

[Service]
Type=simple
ExecStart=/usr/local/bin/wstunnel client \\
  -L 'udp://127.0.0.1:${LOCAL_UDP_PORT}:127.0.0.1:1194?timeout_sec=0' \\
  ws://${VPS_IP}:${VPS_WST_PORT}
Restart=always
RestartSec=5
User=root
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable wstunnel-client

echo "[5/5] Starting wstunnel-client and then rok0..."
systemctl start wstunnel-client
sleep 2
systemctl status wstunnel-client --no-pager

echo ""
echo "Starting rok0 tunnel..."
wg-quick up rok0
sleep 2
wg show rok0

echo ""
echo "Testing tunnel (ping VPS inner IP)..."
ping -c 4 10.100.0.1
