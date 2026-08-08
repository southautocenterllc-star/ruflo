"""Smoke test del portal ROK VPN — sin tocar WireGuard real."""
import os
import subprocess
import sys
import tempfile

tmp = tempfile.mkdtemp()
os.environ["ROK_DB_PATH"]       = os.path.join(tmp, "peers.db")
os.environ["ROK_PEERS_DIR"]     = os.path.join(tmp, "peers")
os.environ["ROK_PORTAL_PASSWORD"] = "test-pass-smoke"
os.environ["ROK_SECRET_KEY"]    = "a" * 64
# Sin endpoint no se marca ningún servidor como local y el catálogo se queda
# sin servidor por defecto, así que las altas de peer fallan. El portal ya no
# trae un IP de producción por defecto, de modo que el test debe fijar el suyo
# (rango de documentación RFC 5737, nunca enrutable).
os.environ["ROK_VPS_ENDPOINT"]  = "198.51.100.10:1194"
# El test repite POST /login muchas veces y chocaría con el límite de 5/min.
# El límite se cubre aparte, en test-ratelimit.py.
os.environ["ROK_RATELIMIT_ENABLED"] = "0"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Stub de las llamadas a wg / wg-quick ──────────────────────────────────────
def _keygen():
    n = 0
    while True:
        n += 1
        yield f"fakeKey{n:03d}".ljust(43, "0").encode() + b"=\n"


FAKE_KEYS = _keygen()
CALLS = []


def fake_check_output(cmd, **kw):
    CALLS.append(cmd)
    if cmd[:2] == ["wg", "genkey"] or cmd[:2] == ["wg", "pubkey"]:
        return next(FAKE_KEYS)
    if cmd[:3] == ["wg", "show", "wg0"]:
        return b"sErverPubKey0000000000000000000000000000000=\n"
    raise AssertionError(f"llamada inesperada: {cmd}")


def fake_check_call(cmd, **kw):
    CALLS.append(cmd)
    return 0


subprocess.check_output = fake_check_output
subprocess.check_call = fake_check_call

import app as portal  # noqa: E402

portal.app.config["TESTING"] = False
c = portal.app.test_client()

failures = []


def check(label, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f"  — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


# 1. health
r = c.get("/health")
check("GET /health → 200 {'status':'ok'}",
      r.status_code == 200 and r.get_json() == {"status": "ok"}, r.data[:120])

# 2. login page renderiza
r = c.get("/login")
check("GET /login → 200 y renderiza form",
      r.status_code == 200 and b'name="password"' in r.data, r.status_code)

# 3. /admin sin sesión redirige a login
r = c.get("/admin")
check("GET /admin sin sesión → redirect /login",
      r.status_code == 302 and "/login" in r.headers.get("Location", ""),
      f"{r.status_code} {r.headers.get('Location')}")

# 4. login con contraseña incorrecta
r = c.post("/login", data={"password": "wrong"}, follow_redirects=True)
check("POST /login contraseña incorrecta → sigue en login",
      b'name="password"' in r.data and "Incorrect password" in r.data.decode())

# 5. login correcto
r = c.post("/login", data={"password": "test-pass-smoke"})
check("POST /login correcto → redirect /admin",
      r.status_code == 302 and "/admin" in r.headers.get("Location", ""),
      f"{r.status_code} {r.headers.get('Location')}")

# 6. admin vacío
r = c.get("/admin")
check("GET /admin con sesión → 200, tabla vacía",
      r.status_code == 200 and "Sin clientes" in r.data.decode(), r.status_code)

# 7. crear peer
r = c.post("/admin/add", data={"name": "smoke-1", "email": "a@b.c"},
           follow_redirects=True)
body = r.data.decode()
check("POST /admin/add → peer creado con IP 10.100.0.2",
      "smoke-1" in body and "10.100.0.2" in body, body[-400:])

# 8. wg set fue llamado con allowed-ips correcto
wg_set = [x for x in CALLS if x[:2] == ["wg", "set"]]
check("wg set wg0 peer ... allowed-ips 10.100.0.2/32",
      any("allowed-ips" in x and "10.100.0.2/32" in x for x in wg_set), wg_set)

# 9. wg-quick save fue llamado
check("wg-quick save wg0 tras alta",
      ["wg-quick", "save", "wg0"] in CALLS, CALLS)

# 8b. conf files saved to PEERS_DIR
peers_dir = os.environ["ROK_PEERS_DIR"]
check("rok-vpn-smoke-1.conf guardado en PEERS_DIR",
      os.path.isfile(os.path.join(peers_dir, "rok-vpn-smoke-1.conf")))
check("rok-vpn-smoke-1-wstunnel.conf guardado en PEERS_DIR",
      os.path.isfile(os.path.join(peers_dir, "rok-vpn-smoke-1-wstunnel.conf")))

# 10. nombre duplicado rechazado
r = c.post("/admin/add", data={"name": "smoke-1"}, follow_redirects=True)
check("POST /admin/add nombre duplicado → error",
      "already exists" in r.data.decode())

# 11. obtener token de la BD
with portal.get_db() as conn:
    row = conn.execute(
        "SELECT download_token, ip_address FROM peers WHERE name='smoke-1'"
    ).fetchone()
token = row["download_token"]
check("Token de descarga generado", bool(token) and len(token) > 30, token)

# 12. página de descarga pública (sin sesión)
c2 = portal.app.test_client()  # cliente nuevo, sin sesión admin
r = c2.get(f"/download/{token}")
check("GET /download/<token> sin sesión → 200",
      r.status_code == 200, r.status_code)

# 13. descarga .conf directa
r = c2.get(f"/download/{token}/config")
cfg = r.data.decode()
check("GET /download/<token>/config → .conf válido",
      r.status_code == 200
      and "[Interface]" in cfg
      and "Address = 10.100.0.2/24" in cfg
      and "Endpoint = 198.51.100.10:1194" in cfg
      and "AllowedIPs = 0.0.0.0/0, ::/0" in cfg,
      cfg)

# 14. descarga .conf wstunnel
r = c2.get(f"/download/{token}/wstunnel-config")
cfgw = r.data.decode()
check("GET /download/<token>/wstunnel-config → endpoint 127.0.0.1:51820",
      r.status_code == 200 and "Endpoint = 127.0.0.1:51820" in cfgw, cfgw)

# 15. QR PNG
r = c2.get(f"/download/{token}/qr.png")
check("GET /download/<token>/qr.png → PNG",
      r.status_code == 200 and r.data[:8] == b"\x89PNG\r\n\x1a\n",
      f"{r.status_code} {r.data[:16]!r}")

# 16. token inválido → 404
r = c2.get("/download/tokenqueNoExiste123/config")
check("GET /download/<token-inválido>/config → 404",
      r.status_code == 404, r.status_code)

# 16b. la PÁGINA de descarga con token inválido renderiza 404.html
r = c2.get("/download/tokenqueNoExiste123")
check("GET /download/<token-inválido> → 404 renderiza página",
      r.status_code == 404 and "Invalid link" in r.data.decode(),
      f"{r.status_code} {r.data[:200]!r}")

# 17. revocar sin sesión → redirect a login (no ejecuta)
r = c2.post("/admin/revoke/smoke-1")
check("POST /admin/revoke sin sesión → redirect login",
      r.status_code == 302 and "/login" in r.headers.get("Location", ""),
      f"{r.status_code} {r.headers.get('Location')}")

# 18. revocar con sesión
r = c.post("/admin/revoke/smoke-1", follow_redirects=True)
check("POST /admin/revoke con sesión → revocado",
      "revoked" in r.data.decode())

# 18b. conf files deleted from PEERS_DIR after revoke
check("rok-vpn-smoke-1.conf borrado tras revocación",
      not os.path.isfile(os.path.join(peers_dir, "rok-vpn-smoke-1.conf")))
check("rok-vpn-smoke-1-wstunnel.conf borrado tras revocación",
      not os.path.isfile(os.path.join(peers_dir, "rok-vpn-smoke-1-wstunnel.conf")))

# 19. wg set ... remove fue llamado
check("wg set wg0 peer ... remove",
      any(x[:2] == ["wg", "set"] and "remove" in x for x in CALLS),
      [x for x in CALLS if "remove" in x])

# 20. token revocado ya no descarga
r = c2.get(f"/download/{token}/config")
check("Token revocado → 404 en descarga", r.status_code == 404, r.status_code)

# 21. reutilización de IP tras revocar
r = c.post("/admin/add", data={"name": "smoke-2"}, follow_redirects=True)
check("IP 10.100.0.2 reutilizada tras revocación",
      "10.100.0.2" in r.data.decode(), r.data.decode()[-300:])

# 22. re-alta con el nombre de un peer revocado (caso "perdió el dispositivo")
r = c.post("/admin/add", data={"name": "smoke-1"}, follow_redirects=True)
body = r.data.decode()
check("Re-alta con nombre revocado → permitida",
      "Ya existe" not in body and "smoke-1" in body, body[-400:])

# 23. el histórico conserva la fila revocada
with portal.get_db() as conn:
    rows = conn.execute(
        "SELECT revoked FROM peers WHERE name='smoke-1' ORDER BY id"
    ).fetchall()
check("Histórico: 2 filas smoke-1 (1 revocada + 1 activa)",
      len(rows) == 2 and rows[0]["revoked"] == 1 and rows[1]["revoked"] == 0,
      [dict(x) for x in rows])

# 24. duplicado entre ACTIVOS sigue rechazado
r = c.post("/admin/add", data={"name": "smoke-1"}, follow_redirects=True)
check("Duplicado entre activos → sigue rechazado",
      "already exists" in r.data.decode())

# 25. logout
r = c.get("/logout")
r = c.get("/admin")
check("Logout invalida la sesión",
      r.status_code == 302 and "/login" in r.headers.get("Location", ""))

print()
if failures:
    print(f"❌ {len(failures)} FALLO(S): {failures}")
    sys.exit(1)
print("✅ 30/30 PASS")
