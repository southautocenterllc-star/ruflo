# ROK VPN — Handoff para nueva sesión

## Estado actual (2026-08-01)

**Entrega 2 y Entrega 3 completadas.** Este documento se corrigió el 2026-08-01
para que coincida con lo que hay realmente en el repo y en el VPS; la versión
anterior describía rutas y archivos que no existían (ver «Correcciones» al final).

---

## VPS

| Campo | Valor |
|-------|-------|
| IP | `74.208.44.254` |
| Usuario | `root` |
| OS | Ubuntu |
| Acceso | `ssh root@74.208.44.254` |

---

## Entrega 2 — Bootstrap WireGuard (COMPLETO)

- WireGuard corriendo en interfaz `wg0`, puerto UDP `1194`
- Subred VPN: `10.100.0.0/24`, gateway `10.100.0.1`
- Scripts en `/opt/rok-vpn/`: `add-peer.sh`, `revoke-peer.sh`, `list-peers.sh`
- nftables activo con reglas para 22, 80, 443, 1194, 8080, 8447
- DNS interno con unbound en `10.100.0.1`

---

## wstunnel — bypass de bloqueo UDP (COMPLETO, Task 10)

Esta pieza es **infraestructura crítica** y condiciona al resto del sistema:
es la razón por la que el portal vive en el 8080 y no en el 443.

- Muchos ISP domésticos bloquean **todo** UDP saliente hacia la IP del VPS
  (probados y bloqueados: UDP 51820, 443, 1194, 53). Sin wstunnel, esos
  clientes no levantan el túnel.
- `wstunnel` encapsula el UDP de WireGuard dentro de WebSocket sobre **TCP 443**,
  así que el tráfico parece HTTPS y atraviesa el filtro.

```
[cliente:rok0] → UDP 51820 → [wstunnel client] → WS TCP 443
                → [wstunnel server VPS] → UDP 1194 → [wg0]
```

- **El VPS escucha en `ws://` (sin TLS)**, no en `wss://`. Los clientes deben
  conectar con `ws://74.208.44.254:443`.
- Servicios systemd: `wstunnel-server` (VPS) y `wstunnel-client` (cliente).
- Versión validada end-to-end: **v10.6.2** (los scripts de setup la fijan).
- Detalle completo de la prueba: `TASK10-RESULT.md`.

---

## Entrega 3 — Portal Web Flask (COMPLETO)

### Instalación en VPS
- Portal en: `/opt/rok-vpn/portal/`
- Venv en: `/opt/rok-vpn/portal/venv/`
- DB SQLite en: `/opt/rok-vpn/db/peers.db`
- Las configs de los clientes **se generan al vuelo en memoria**, no se guardan
  en disco. `ROK_PEERS_DIR` (`/opt/rok-vpn/peers/`) está declarado en el unit y
  en `app.py` pero hoy no se usa; las claves privadas viven en la tabla `peers`
  de SQLite.

### Servicio
```bash
systemctl status rok-portal      # ver estado
systemctl restart rok-portal     # reiniciar
journalctl -u rok-portal -f      # logs en vivo
```

### Acceso
- URL admin: `http://74.208.44.254:8080/login`
- Puerto 8080 abierto en: nftables del VPS + IONOS Firewall Policy "My firewall policy"

### Rutas del portal
| Ruta | Uso |
|------|-----|
| `/login`, `/logout` | Sesión de admin |
| `/admin` | Alta y revocación de clientes |
| `/download/<token>` | Página del cliente (sin login, sólo con el token) |
| `/download/<token>/config` | `.conf` con endpoint directo `74.208.44.254:1194` |
| `/download/<token>/wstunnel-config` | `.conf` con endpoint `127.0.0.1:51820` para wstunnel |
| `/download/<token>/qr.png` | QR de la config directa |
| `/health` | `{"status": "ok"}` |

El cliente que esté detrás de un ISP que bloquee UDP necesita la
**wstunnel-config**, no la directa.

### Variables de entorno (en /etc/systemd/system/rok-portal.service)
| Variable | Valor |
|----------|-------|
| `ROK_PORTAL_PASSWORD` | contraseña del admin (se configura al instalar) |
| `ROK_SECRET_KEY` | la genera `portal-setup.sh` en la instalación |
| `ROK_DB_PATH` | `/opt/rok-vpn/db/peers.db` |
| `ROK_PEERS_DIR` | `/opt/rok-vpn/peers` (declarada, sin uso actual) |
| `ROK_VPS_ENDPOINT` | `74.208.44.254:1194` |
| `ROK_WG_IFACE` | `wg0` |
| `ROK_WG_SUBNET` | `10.100.0` |
| `ROK_DNS` | `10.100.0.1` |
| `ROK_PORTAL_PORT` | `8080` |

---

## Prueba E2E completada (E3-T17)

1. ✅ Portal carga en `http://74.208.44.254:8080/login`
2. ✅ Login con contraseña configurada
3. ✅ Peer `juanpachanga` creado → IP `10.100.0.2` asignada
4. ✅ Config descargado como `rok-vpn-juanpachanga.conf`
5. ✅ VPN conectada con `sudo wg-quick up vpntest`
6. ✅ IP pública verificada: `74.208.44.254` ← tráfico saliendo por VPS
7. ✅ Peer revocado desde el admin

> Esta prueba usó la config **directa** (UDP 1194), así que se hizo desde una red
> que no filtra UDP. La ruta con wstunnel se validó por separado en Task 10.

---

## Smoke test del portal

`portal/smoke-test.py` ejerce el portal completo con las llamadas a `wg` y
`wg-quick` simuladas — no toca WireGuard real, así que se puede correr en
cualquier máquina.

```bash
python3 -m venv /tmp/rokvenv
/tmp/rokvenv/bin/pip install flask "qrcode[pil]"
cd docs/rok-vpn/portal && /tmp/rokvenv/bin/python smoke-test.py
```

Estado actual: **26/26 PASS**. Correrlo antes de cada despliegue.

---

## Firewall IONOS

- Panel: `cloud.ionos.com` → Servers & Cloud → Network → Firewall Policies → "My firewall policy"
- Puertos abiertos: TCP 22, 80, 443, 8080, 8447
- VPS asignado: `My VPS (74.208.44.254)`

> Ojo: la policy lista sólo TCP. El acceso **directo** por UDP 1194 depende de
> que ese puerto esté permitido; si no lo está, la única vía operativa para los
> clientes es la config de wstunnel (TCP 443). Verificar en el panel antes de
> prometerle a un cliente la config directa.

---

## Archivos en el repo

```
docs/rok-vpn/
├── portal/
│   ├── app.py                  # Flask app principal
│   ├── portal-setup.sh         # Script de instalación en el VPS
│   ├── rok-portal.service      # Systemd unit
│   ├── smoke-test.py           # 26 casos, sin WireGuard real
│   └── templates/
│       ├── login.html
│       ├── admin.html
│       ├── download.html
│       └── 404.html
├── wstunnel-vps-setup.sh       # Instala wstunnel server en el VPS
├── wstunnel-laptop-setup.sh    # Instala wstunnel client en el cliente
├── ENTREGA3-RUNBOOK.md         # Runbook de deploy y prueba de E3
├── TASK10-RESULT.md            # Resultado de la prueba E2E con wstunnel
└── HANDOFF.md                  # este archivo
```

---

## Correcciones aplicadas el 2026-08-01

Antes de arrancar Entrega 4 se auditó la documentación contra el código. Se
corrigieron seis desviaciones:

1. **Ruta de la DB.** Este handoff decía `/opt/rok-vpn/portal/peers.db`; la real
   es `/opt/rok-vpn/db/peers.db` (`rok-portal.service` + `portal-setup.sh`).
2. **Configs de peers.** Se afirmaba que se guardaban en `/opt/rok-vpn/peers/`.
   No se guardan: se generan en memoria por petición.
3. **Listado de archivos.** Mencionaba `README.md` y `runbook-e3.md`, que no
   existen, y omitía los scripts de wstunnel, `TASK10-RESULT.md`, `smoke-test.py`
   y `404.html`.
4. **wstunnel ausente.** No se mencionaba en todo el documento, pese a ser lo que
   ocupa el 443 y la única vía para clientes con UDP bloqueado.
5. **Versión de wstunnel.** Los dos scripts de setup fijaban `10.1.8`, pero la
   versión probada end-to-end (y la que pide la página de descarga) es `10.6.2`.
   Se alinearon los scripts a `10.6.2`.
6. **`wss://` en la página de descarga.** Las instrucciones al cliente decían
   `wss://74.208.44.254:443`, pero el servidor arranca con `ws://`. Conectar por
   `wss://` falla en el handshake TLS. Corregido a `ws://`.

Además, el runbook de E3 omitía `templates/404.html` en la lista de `scp`.
Como `portal-setup.sh` corre con `set -euo pipefail` y hace `cp` de ese archivo,
seguir el runbook al pie de la letra **abortaba la instalación**. Corregido.

---

## Pendiente / Próximas entregas

- Nada pendiente de E2 ni E3.
- Para Entrega 4, partir desde aquí. Puntos que conviene decidir pronto:
  - Confirmar en IONOS si UDP 1194 está realmente abierto, y si no, considerar
    servir sólo la config de wstunnel.
  - `ROK_PEERS_DIR` está declarada sin uso: darle uso o eliminarla.
  - El portal va por HTTP plano en el 8080 y las claves privadas se guardan en
    SQLite sin cifrar; ambas cosas son aceptables para una demo interna pero
    conviene revisarlas antes de abrirlo a clientes reales.
