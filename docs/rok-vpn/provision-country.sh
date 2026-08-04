#!/bin/bash
# ROK VPN — Aprovisiona un VPS nuevo como servidor de país.
#
# Se ejecuta como root EN EL VPS NUEVO (Alemania, Japón, Brasil...).
# Instala WireGuard + wstunnel + rok-agent y al final imprime el comando
# para dar de alta el país en el portal central.
#
# Uso:
#   ./provision-country.sh --code DE --subnet 10.101.0 --portal-ip 74.208.44.254
#
# El --subnet debe ser ÚNICO por país. Convención sugerida:
#   10.100.0 = US Nueva York (portal)   10.104.0 = MX
#   10.101.0 = DE            10.105.0 = BR      10.106.0 = AR
#   10.102.0 = GB            10.107.0 = JP      10.108.0 = SG
#   10.103.0 = CA            ...
set -euo pipefail

WSTUNNEL_VERSION="10.6.2"   # misma versión validada end-to-end en el VPS original
WG_PORT=1194
WST_PORT=443
AGENT_PORT=8081

CODE=""; SUBNET=""; PORTAL_IP=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --code)      CODE="$2";      shift 2 ;;
    --subnet)    SUBNET="$2";    shift 2 ;;
    --portal-ip) PORTAL_IP="$2"; shift 2 ;;
    *) echo "Argumento desconocido: $1"; exit 1 ;;
  esac
done

if [[ -z "$CODE" || -z "$SUBNET" || -z "$PORTAL_IP" ]]; then
  echo "Uso: $0 --code DE --subnet 10.101.0 --portal-ip 74.208.44.254" >&2
  exit 1
fi

if [[ ! "$SUBNET" =~ ^10\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
  echo "El subnet debe tener la forma 10.X.Y (sin el último octeto)" >&2
  exit 1
fi

PUBLIC_IP=$(curl -fsS --max-time 10 https://api.ipify.org)
echo "==> Aprovisionando $CODE en $PUBLIC_IP con subnet ${SUBNET}.0/24"

# ── 1. Paquetes ────────────────────────────────────────────────────────────────
echo "[1/6] Instalando paquetes..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq wireguard wireguard-tools python3-venv python3-pip \
                       nftables curl wget >/dev/null

# ── 2. WireGuard ───────────────────────────────────────────────────────────────
echo "[2/6] Configurando WireGuard..."
umask 077
mkdir -p /etc/wireguard
if [[ ! -f /etc/wireguard/server_private.key ]]; then
  wg genkey | tee /etc/wireguard/server_private.key | wg pubkey > /etc/wireguard/server_public.key
fi
SERVER_PRIV=$(cat /etc/wireguard/server_private.key)
SERVER_PUB=$(cat /etc/wireguard/server_public.key)
WAN_IF=$(ip -4 route show default | awk '{print $5; exit}')

cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
Address = ${SUBNET}.1/24
ListenPort = ${WG_PORT}
PrivateKey = ${SERVER_PRIV}
PostUp   = nft add table ip rokvpn 2>/dev/null || true
PostUp   = nft add chain ip rokvpn postrouting { type nat hook postrouting priority 100 \; } 2>/dev/null || true
PostUp   = nft add rule ip rokvpn postrouting oifname "${WAN_IF}" ip saddr ${SUBNET}.0/24 masquerade
PostDown = nft delete table ip rokvpn 2>/dev/null || true
EOF
chmod 600 /etc/wireguard/wg0.conf

sysctl -qw net.ipv4.ip_forward=1
grep -q "^net.ipv4.ip_forward=1" /etc/sysctl.conf || echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf

systemctl enable --now wg-quick@wg0 >/dev/null 2>&1 || systemctl restart wg-quick@wg0

# ── 3. wstunnel ────────────────────────────────────────────────────────────────
echo "[3/6] Instalando wstunnel v${WSTUNNEL_VERSION}..."
case "$(uname -m)" in
  x86_64)        ARCH_TAG="amd64" ;;
  aarch64|arm64) ARCH_TAG="arm64" ;;
  *) echo "Arquitectura no soportada: $(uname -m)"; exit 1 ;;
esac
wget -q "https://github.com/erebe/wstunnel/releases/download/v${WSTUNNEL_VERSION}/wstunnel_${WSTUNNEL_VERSION}_linux_${ARCH_TAG}.tar.gz" \
     -O /tmp/wstunnel.tar.gz
tar -xzf /tmp/wstunnel.tar.gz -C /tmp/
mv -f /tmp/wstunnel /usr/local/bin/wstunnel
chmod +x /usr/local/bin/wstunnel
rm -f /tmp/wstunnel.tar.gz

# Mismo nombre de unidad que el VPS original para que la flota sea homogénea.
cat > /etc/systemd/system/wstunnel-server.service <<EOF
[Unit]
Description=wstunnel — WireGuard over WebSocket
Documentation=https://github.com/erebe/wstunnel
After=network-online.target wg-quick@wg0.service
Wants=network-online.target wg-quick@wg0.service

[Service]
Type=simple
ExecStart=/usr/local/bin/wstunnel server --restrict-to '127.0.0.1:${WG_PORT}' ws://0.0.0.0:${WST_PORT}
Restart=always
RestartSec=5
User=root
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

# ── 4. Agente ──────────────────────────────────────────────────────────────────
echo "[4/6] Instalando rok-agent..."
mkdir -p /opt/rok-vpn/agent
if [[ -f "$(dirname "$0")/portal/agent/agent.py" ]]; then
  cp "$(dirname "$0")/portal/agent/agent.py" /opt/rok-vpn/agent/agent.py
elif [[ ! -f /opt/rok-vpn/agent/agent.py ]]; then
  echo "ERROR: falta agent.py. Copia portal/agent/agent.py a /opt/rok-vpn/agent/" >&2
  exit 1
fi

python3 -m venv /opt/rok-vpn/agent/venv
/opt/rok-vpn/agent/venv/bin/pip install --quiet --upgrade pip
/opt/rok-vpn/agent/venv/bin/pip install --quiet "flask>=3.0" "gunicorn>=21.2"

AGENT_TOKEN=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)

cat > /etc/systemd/system/rok-agent.service <<EOF
[Unit]
Description=ROK VPN Node Agent
After=network-online.target wg-quick@wg0.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/rok-vpn/agent
Environment="ROK_AGENT_TOKEN=${AGENT_TOKEN}"
Environment="ROK_WG_IFACE=wg0"
Environment="ROK_AGENT_PORT=${AGENT_PORT}"
ExecStart=/opt/rok-vpn/agent/venv/bin/gunicorn --bind 0.0.0.0:${AGENT_PORT} \\
  --workers 1 --timeout 30 --access-logfile - --error-logfile - agent:app
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF
chmod 600 /etc/systemd/system/rok-agent.service

# ── 5. Firewall ────────────────────────────────────────────────────────────────
# El puerto del agente SÓLO se abre para la IP del portal central.
echo "[5/6] Configurando firewall..."
nft list table inet filter >/dev/null 2>&1 || nft add table inet filter
nft list chain inet filter input >/dev/null 2>&1 || \
  nft 'add chain inet filter input { type filter hook input priority 0; }'
nft list ruleset | grep -q "udp dport ${WG_PORT} accept" || \
  nft add rule inet filter input udp dport ${WG_PORT} accept
nft list ruleset | grep -q "tcp dport ${WST_PORT} accept" || \
  nft add rule inet filter input tcp dport ${WST_PORT} accept
nft list ruleset | grep -q "ip saddr ${PORTAL_IP} tcp dport ${AGENT_PORT} accept" || \
  nft add rule inet filter input ip saddr ${PORTAL_IP} tcp dport ${AGENT_PORT} accept
nft list ruleset > /etc/nftables.conf
systemctl enable nftables >/dev/null 2>&1 || true

# ── 6. Arranque ────────────────────────────────────────────────────────────────
echo "[6/6] Arrancando servicios..."
systemctl daemon-reload
systemctl enable --now wstunnel-server rok-agent >/dev/null 2>&1
sleep 2
systemctl is-active --quiet wg-quick@wg0     || { echo "FALLO: wg-quick@wg0"; exit 1; }
systemctl is-active --quiet wstunnel-server  || { echo "FALLO: wstunnel-server"; exit 1; }
systemctl is-active --quiet rok-agent        || { echo "FALLO: rok-agent"; exit 1; }

cat <<EOF

════════════════════════════════════════════════════════════════════════
  ✅ $CODE aprovisionado — $PUBLIC_IP
════════════════════════════════════════════════════════════════════════

  WireGuard  UDP ${WG_PORT}   subnet ${SUBNET}.0/24
  wstunnel   TCP ${WST_PORT}
  rok-agent  TCP ${AGENT_PORT}   (sólo desde ${PORTAL_IP})
  clave pública: ${SERVER_PUB}

  ── ÚLTIMO PASO: dar de alta el país en el portal ──────────────────────

  Desde una sesión admin del portal, con <ID> = el id de este país
  (consúltalo con  GET /admin/servers ):

  curl -X POST https://<tu-dominio>/admin/servers/<ID> \\
    -H 'Content-Type: application/json' \\
    -b cookies.txt \\
    -d '{
      "endpoint_host": "${PUBLIC_IP}",
      "agent_url":     "http://${PUBLIC_IP}:${AGENT_PORT}",
      "agent_token":   "${AGENT_TOKEN}",
      "wg_subnet":     "${SUBNET}",
      "active":        1
    }'

  ⚠ El token de arriba se muestra UNA sola vez. Guárdalo en tu gestor de
    contraseñas — no lo pegues en git ni en ningún archivo del repo.

════════════════════════════════════════════════════════════════════════
EOF
