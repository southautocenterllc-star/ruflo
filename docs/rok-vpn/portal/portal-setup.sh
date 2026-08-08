#!/usr/bin/env bash
# portal-setup.sh — Instala ROK VPN Portal en el VPS
# Ejecutar como root desde el directorio que contiene los archivos del portal.
# Uso: bash portal-setup.sh
set -euo pipefail

PORTAL_DIR="/opt/rok-vpn/portal"
DB_DIR="/opt/rok-vpn/db"
PEERS_DIR="/opt/rok-vpn/peers"
SERVICE_NAME="rok-portal"
PORTAL_PORT="8080"

echo "==> ROK VPN Portal — instalación"

# ── 1. Dependencias del sistema ───────────────────────────────────────────────
echo "[1/7] Instalando dependencias del sistema..."
apt-get update -q
apt-get install -y -q python3-pip python3-venv

# ── 2. Directorios ────────────────────────────────────────────────────────────
echo "[2/7] Creando directorios..."
mkdir -p "$PORTAL_DIR/templates" "$DB_DIR" "$PEERS_DIR"

# ── 3. Copiar archivos del portal ─────────────────────────────────────────────
echo "[3/7] Copiando archivos..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cp "$SCRIPT_DIR/app.py"                        "$PORTAL_DIR/app.py"
cp "$SCRIPT_DIR/templates/login.html"          "$PORTAL_DIR/templates/login.html"
cp "$SCRIPT_DIR/templates/admin.html"          "$PORTAL_DIR/templates/admin.html"
cp "$SCRIPT_DIR/templates/download.html"       "$PORTAL_DIR/templates/download.html"
cp "$SCRIPT_DIR/templates/404.html"            "$PORTAL_DIR/templates/404.html"

# ── 4. Entorno virtual Python ─────────────────────────────────────────────────
echo "[4/7] Creando entorno virtual..."
python3 -m venv "$PORTAL_DIR/venv"
"$PORTAL_DIR/venv/bin/pip" install --upgrade pip -q
"$PORTAL_DIR/venv/bin/pip" install flask "qrcode[pil]" gunicorn bcrypt PyJWT -q

# ── 5. Firewall — abrir TCP 8080 ──────────────────────────────────────────────
echo "[5/7] Abriendo TCP $PORTAL_PORT en nftables..."
if ! nft list ruleset | grep -q "tcp dport $PORTAL_PORT accept"; then
  nft add rule inet filter input tcp dport "$PORTAL_PORT" accept
  echo "    Regla añadida."
else
  echo "    Regla ya existe."
fi

# Persistir reglas nftables si existe el archivo de configuración
if [ -f /etc/nftables.conf ]; then
  nft list ruleset > /etc/nftables.conf
  echo "    nftables.conf actualizado."
fi

# ── 6. Systemd service ────────────────────────────────────────────────────────
echo "[6/7] Instalando servicio systemd..."
cp "$SCRIPT_DIR/rok-portal.service" "/etc/systemd/system/${SERVICE_NAME}.service"

# ── Configuración y secretos ─────────────────────────────────────────────────
# Los secretos van a /etc/rok-vpn/portal.env, NO a la unidad de systemd: la
# unidad se versiona en git y `systemctl show` la deja leer a cualquier usuario
# del sistema. El EnvironmentFile queda 0600 y sólo root lo ve.
ENV_DIR="/etc/rok-vpn"
ENV_FILE="${ENV_DIR}/portal.env"
mkdir -p "$ENV_DIR"
chmod 700 "$ENV_DIR"

if [ ! -f "$ENV_FILE" ]; then
  # Sin una clave de sesión fija cada worker de gunicorn usaría la suya y las
  # sesiones se romperían entre peticiones.
  SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  JWT_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  AGENT_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  PORTAL_PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"

  cat > "$ENV_FILE" << ENVEOF
# Generado por portal-setup.sh — NO subir a git.
ROK_SECRET_KEY=${SECRET_KEY}
ROK_JWT_SECRET=${JWT_KEY}
ROK_JWT_EXP_DAYS=30
ROK_PORTAL_PASSWORD=${PORTAL_PW}
ROK_AGENT_TOKEN=${AGENT_TOKEN}
ROK_AGENT_TIMEOUT=10
ROK_DB_PATH=/opt/rok-vpn/db/peers.db
ROK_PEERS_DIR=/opt/rok-vpn/peers
ROK_WG_IFACE=wg0
ROK_WG_SUBNET=10.100.0
ROK_DNS=10.100.0.1
ROK_PORTAL_PORT=8080

# ── Rellenar a mano antes de arrancar ───────────────────────────────────────
# Endpoint WireGuard publico de ESTE VPS, en formato host:puerto.
# Sin el, el catalogo se queda sin servidor local y no se pueden dar altas.
ROK_VPS_ENDPOINT=

# Dominio publico con https:// y sin barra final. Necesario para que la vista
# previa de WhatsApp e iMessage resuelva la imagen de Open Graph.
ROK_SITE_URL=

# Square. Dejar en sandbox hasta tener las credenciales de produccion.
ROK_SQUARE_ENV=sandbox
ROK_SQUARE_TOKEN=
ROK_SQUARE_LOCATION_ID=
ENVEOF

  chmod 600 "$ENV_FILE"
  echo "    Secretos generados en ${ENV_FILE} (0600)."
  echo ""
  echo "    Contrasena del panel admin: ${PORTAL_PW}"
  echo "    Guardala ahora; no se vuelve a mostrar."
  echo ""
else
  echo "    ${ENV_FILE} ya existe, se conserva."
fi

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# ── 7. Verificar contraseña de portal ────────────────────────────────────────
echo "[7/7] Verificando configuración..."
if ! grep -q '^ROK_VPS_ENDPOINT=.\+' "$ENV_FILE"; then
  echo ""
  echo "  ATENCION: falta ROK_VPS_ENDPOINT en ${ENV_FILE}."
  echo "  Sin el, el catalogo no tiene servidor local y no se pueden dar altas."
  echo ""
  echo "  Edita ${ENV_FILE}, rellena ROK_VPS_ENDPOINT y ROK_SITE_URL, y ejecuta:"
  echo "    systemctl start $SERVICE_NAME"
else
  systemctl start "$SERVICE_NAME"
  sleep 2
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo ""
    echo "  ✓ Portal activo en http://$(hostname -I | awk '{print $1}'):$PORTAL_PORT"
  else
    echo "  ✗ El servicio no arrancó. Revisa: journalctl -u $SERVICE_NAME -n 30"
  fi
fi

echo ""
echo "==> Instalación completada."
echo "    Logs: journalctl -u $SERVICE_NAME -f"
echo "    Estado: systemctl status $SERVICE_NAME"
