# Entrega 3 — Portal Web ROK VPN: Runbook de Deploy y Prueba

## Resumen

Portal Flask que permite al admin crear y revocar peers WireGuard.
Cada cliente recibe un enlace único para descargar su `.conf` y código QR.

**Stack:** Flask + SQLite + qrcode/Pillow + Gunicorn · Puerto 8080 (443 ocupado por wstunnel)

---

## 1. Prerequisitos en el VPS

| Requisito | Verificación |
|-----------|-------------|
| WireGuard activo | `wg show wg0` |
| `wg-quick` disponible | `which wg-quick` |
| Python 3.10+ | `python3 --version` |
| Root o sudo | `whoami` |

---

## 2. Transferir archivos al VPS

Desde tu laptop, en la carpeta `docs/rok-vpn/portal/` del repo:

```bash
# Crear directorio temporal en el VPS
ssh root@74.208.44.254 "mkdir -p /tmp/rok-portal/templates"

# Copiar archivos
scp portal/app.py                      root@74.208.44.254:/tmp/rok-portal/
scp portal/portal-setup.sh             root@74.208.44.254:/tmp/rok-portal/
scp portal/rok-portal.service          root@74.208.44.254:/tmp/rok-portal/
scp portal/templates/login.html        root@74.208.44.254:/tmp/rok-portal/templates/
scp portal/templates/admin.html        root@74.208.44.254:/tmp/rok-portal/templates/
scp portal/templates/download.html     root@74.208.44.254:/tmp/rok-portal/templates/
```

---

## 3. Instalar en el VPS

```bash
ssh root@74.208.44.254

# Cambiar la contraseña ANTES de instalar
nano /tmp/rok-portal/rok-portal.service
# → Cambia ROK_PORTAL_PASSWORD=changeme por tu contraseña real

# Ejecutar instalación
cd /tmp/rok-portal
bash portal-setup.sh
```

El script:
1. Instala `python3-pip python3-venv`
2. Crea `/opt/rok-vpn/portal/` con entorno virtual
3. Instala `flask qrcode[pil] gunicorn`
4. Abre TCP 8080 en nftables
5. Instala y activa `rok-portal.service`

---

## 4. Verificar servicio

```bash
systemctl status rok-portal
journalctl -u rok-portal -n 30
curl -s http://localhost:8080/health
```

Respuesta esperada de health: `{"status": "ok"}`

---

## 5. Cambiar contraseña (si ya está instalado)

```bash
# Editar el servicio
nano /etc/systemd/system/rok-portal.service
# → Cambiar ROK_PORTAL_PASSWORD=...

systemctl daemon-reload
systemctl restart rok-portal
```

---

## 6. Prueba end-to-end

### 6.1 Abrir el panel admin
```
http://74.208.44.254:8080/login
```
Ingresar la contraseña configurada en `ROK_PORTAL_PASSWORD`.

### 6.2 Crear un peer de prueba
- Nombre: `test-e3`
- Email: `test@rok.vpn` (opcional)
- Clic en **Crear cliente**

Verificar en el VPS:
```bash
wg show wg0
# Debe aparecer el nuevo peer con IP 10.100.0.X
```

Verificar en SQLite:
```bash
sqlite3 /opt/rok-vpn/db/peers.db "SELECT name, ip_address, revoked FROM peers;"
```

### 6.3 Descargar config y conectar
1. Copiar el enlace de descarga del admin panel
2. Abrir en navegador → página de descarga con QR + botones
3. Descargar `rok-vpn-test-e3.conf`
4. Importar en WireGuard y activar
5. Verificar IP pública:
```bash
curl https://api.ipify.org
# Debe mostrar 74.208.44.254
```

### 6.4 Revocar el peer de prueba
- En admin panel → botón **Revocar** → confirmar
- Verificar que desaparece de `wg show wg0`
- El enlace de descarga debe mostrar error 404

---

## 7. Gestión del servicio

```bash
# Ver logs en tiempo real
journalctl -u rok-portal -f

# Reiniciar
systemctl restart rok-portal

# Detener
systemctl stop rok-portal

# Ver peers actuales
sqlite3 /opt/rok-vpn/db/peers.db \
  "SELECT name, ip_address, created_at, revoked FROM peers ORDER BY id;"

# Backup de la base de datos
cp /opt/rok-vpn/db/peers.db /opt/rok-vpn/db/peers.db.bak.$(date +%Y%m%d)
```

---

## 8. Arquitectura de archivos

```
/opt/rok-vpn/
├── portal/
│   ├── venv/              # Entorno virtual Python
│   ├── app.py             # Aplicación Flask
│   └── templates/
│       ├── login.html
│       ├── admin.html
│       └── download.html
├── db/
│   └── peers.db           # SQLite — tabla peers
└── peers/                 # Directorio reservado para futuros archivos .conf
```

---

## 9. Variables de entorno (rok-portal.service)

| Variable | Default | Descripción |
|----------|---------|-------------|
| `ROK_PORTAL_PASSWORD` | `changeme` | Contraseña del panel admin |
| `ROK_SECRET_KEY` | auto | Clave Flask para sesiones |
| `ROK_DB_PATH` | `/opt/rok-vpn/db/peers.db` | Ruta SQLite |
| `ROK_PEERS_DIR` | `/opt/rok-vpn/peers` | Directorio de peers |
| `ROK_VPS_ENDPOINT` | `74.208.44.254:1194` | Endpoint WireGuard público |
| `ROK_WG_IFACE` | `wg0` | Interfaz WireGuard |
| `ROK_WG_SUBNET` | `10.100.0` | Primeros tres octetos |
| `ROK_DNS` | `10.100.0.1` | DNS para clientes |
| `ROK_PORTAL_PORT` | `8080` | Puerto de escucha |

---

## 10. Rutas del portal

| Ruta | Acceso | Descripción |
|------|--------|-------------|
| `/` | público | Redirige a `/admin` |
| `/login` | público | Login admin |
| `/logout` | admin | Cerrar sesión |
| `/admin` | admin | Dashboard — lista de peers |
| `/admin/add` | admin POST | Crear peer |
| `/admin/revoke/<name>` | admin POST | Revocar peer |
| `/download/<token>` | público (token) | Página descarga cliente |
| `/download/<token>/config` | público (token) | `.conf` conexión directa |
| `/download/<token>/wstunnel-config` | público (token) | `.conf` via wstunnel |
| `/download/<token>/qr.png` | público (token) | QR de la config directa |
| `/health` | público | Health check JSON |
