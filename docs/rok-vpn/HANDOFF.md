# ROK VPN — Handoff para nueva sesión

## Estado actual (2026-08-01)

**Entrega 2 y Entrega 3 completadas al 100%.**

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

## Entrega 3 — Portal Web Flask (COMPLETO)

### Instalación en VPS
- Portal en: `/opt/rok-vpn/portal/`
- Archivos: `app.py`, `portal-setup.sh`, `rok-portal.service`, `templates/`
- Venv en: `/opt/rok-vpn/portal/venv/`
- DB SQLite en: `/opt/rok-vpn/portal/peers.db`
- Configs de peers en: `/opt/rok-vpn/peers/`

### Servicio
```bash
systemctl status rok-portal      # ver estado
systemctl restart rok-portal     # reiniciar
journalctl -u rok-portal -f      # logs en vivo
```

### Acceso
- URL admin: `http://74.208.44.254:8080/login`
- Puerto 8080 abierto en: nftables del VPS + IONOS Firewall Policy "My firewall policy"

### Variables de entorno (en /etc/systemd/system/rok-portal.service)
- `ROK_PORTAL_PASSWORD` — contraseña del admin (configurada al instalar)
- `ROK_SECRET_KEY` — generada automáticamente por portal-setup.sh
- `ROK_VPS_ENDPOINT` — `74.208.44.254:1194`
- `ROK_WG_IFACE` — `wg0`

---

## Prueba E2E completada (E3-T17)

1. ✅ Portal carga en `http://74.208.44.254:8080/login`
2. ✅ Login con contraseña configurada
3. ✅ Peer `juanpachanga` creado → IP `10.100.0.2` asignada
4. ✅ Config descargado como `rok-vpn-juanpachanga.conf`
5. ✅ VPN conectada con `sudo wg-quick up vpntest`
6. ✅ IP pública verificada: `74.208.44.254` ← tráfico saliendo por VPS
7. ✅ Peer revocado desde el admin

---

## Firewall IONOS

- Panel: `cloud.ionos.com` → Servers & Cloud → Network → Firewall Policies → "My firewall policy"
- Puertos abiertos: TCP 22, 80, 443, 8080, 8447
- VPS asignado: `My VPS (74.208.44.254)`

---

## Archivos en el repo

```
ruflo/docs/rok-vpn/
├── portal/
│   ├── app.py                  # Flask app principal
│   ├── portal-setup.sh         # Script de instalación
│   ├── rok-portal.service      # Systemd unit
│   └── templates/
│       ├── login.html
│       ├── admin.html
│       ├── download.html
│       └── 404.html
├── README.md
├── runbook-e3.md
└── HANDOFF.md                  # este archivo
```

---

## Pendiente / Próximas entregas

- Nada pendiente de E2 ni E3.
- Si hay Entrega 4, partir desde aquí.
