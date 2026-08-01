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

# Generar clave de sesión Flask persistente. Sin esto, cada worker de gunicorn
# usaría una clave distinta y las sesiones se romperían entre peticiones.
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
if grep -q "ROK_SECRET_KEY=REPLACE_ME_AT_INSTALL" "$SERVICE_FILE"; then
  SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  JWT_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  sed -i "s|ROK_SECRET_KEY=REPLACE_ME_AT_INSTALL\b|ROK_SECRET_KEY=${SECRET_KEY}|" "$SERVICE_FILE"
  sed -i "s|ROK_JWT_SECRET=REPLACE_ME_AT_INSTALL_JWT|ROK_JWT_SECRET=${JWT_KEY}|" "$SERVICE_FILE"
  chmod 600 "$SERVICE_FILE"
  echo "    Claves de sesión y JWT generadas."
else
  echo "    Claves ya presentes, se conservan."
fi

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# ── 7. Verificar contraseña de portal ────────────────────────────────────────
echo "[7/7] Verificando configuración..."
if grep -q "ROK_PORTAL_PASSWORD=changeme" "$SERVICE_FILE"; then
  echo ""
  echo "  ╔══════════════════════════════════════════════════════════════╗"
  echo "  ║  ATENCIÓN: La contraseña del portal es 'changeme'.          ║"
  echo "  ║  Edita /etc/systemd/system/${SERVICE_NAME}.service         ║"
  echo "  ║  y cambia ROK_PORTAL_PASSWORD antes de iniciar el servicio. ║"
  echo "  ╚══════════════════════════════════════════════════════════════╝"
  echo ""
  echo "Después de cambiar la contraseña, ejecuta:"
  echo "  systemctl daemon-reload && systemctl start $SERVICE_NAME"
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
