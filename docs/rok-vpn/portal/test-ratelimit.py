"""Test del límite de peticiones del portal ROK VPN.

El smoke test desactiva el limitador porque repite los mismos endpoints muchas
veces; este archivo lo cubre aparte, con el limitador activo.

Lo que más importa aquí es el caso detrás de nginx: sin ProxyFix, `remote_addr`
es siempre 127.0.0.1 y todos los clientes caen en el mismo cubo, de modo que
cinco intentos de login en total dejarían fuera al resto del mundo.
"""
import os
import sys
import tempfile

tmp = tempfile.mkdtemp()
os.environ["ROK_DB_PATH"]         = os.path.join(tmp, "peers.db")
os.environ["ROK_PEERS_DIR"]       = os.path.join(tmp, "peers")
os.environ["ROK_PORTAL_PASSWORD"] = "test-pass-rl"
os.environ["ROK_SECRET_KEY"]      = "b" * 64
os.environ["ROK_VPS_ENDPOINT"]    = "198.51.100.10:1194"
os.environ.pop("ROK_RATELIMIT_ENABLED", None)   # límite ACTIVO

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as portal  # noqa: E402

failures = []


def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  — {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


def hit(client, path, ip, n, **kw):
    """Lanza n peticiones simulando que llegan desde `ip` a través de nginx."""
    return [
        client.post(path, environ_base={"REMOTE_ADDR": "127.0.0.1"},
                    headers={"X-Forwarded-For": ip}, **kw).status_code
        for _ in range(n)
    ]


check("limitador activo", portal.limiter.enabled)

# ── Un solo cliente agota su cuota ────────────────────────────────────────────
c = portal.app.test_client()
codes = hit(c, "/api/v1/login", "198.51.100.20", 8,
            json={"email": "a@b.c", "password": "x"})
check("mismo cliente: corta tras 5 intentos",
      codes[:5] == [401] * 5 and codes[5:] == [429] * 3, codes)

# ── Clientes distintos NO se afectan entre sí ─────────────────────────────────
# Sin ProxyFix esto devolvía 429 a partir del sexto y bloqueaba a gente inocente.
c = portal.app.test_client()
codes = [
    c.post("/api/v1/login", environ_base={"REMOTE_ADDR": "127.0.0.1"},
           headers={"X-Forwarded-For": f"203.0.113.{i}"},
           json={"email": f"u{i}@b.c", "password": "x"}).status_code
    for i in range(8)
]
check("clientes distintos: ninguno bloqueado por otro",
      429 not in codes, codes)

# ── El 429 trae un mensaje claro, no el HTML por defecto de Flask ────────────
c = portal.app.test_client()
last = None
for _ in range(8):
    last = c.post("/api/v1/login", environ_base={"REMOTE_ADDR": "127.0.0.1"},
                  headers={"X-Forwarded-For": "198.51.100.30"},
                  json={"email": "a@b.c", "password": "x"})
check("429 responde JSON con mensaje",
      last.status_code == 429
      and last.is_json
      and "Too many requests" in last.get_json().get("error", ""),
      f"{last.status_code} {last.data[:120]!r}")

# ── Registro y login de admin también limitados a 5/min ──────────────────────
c = portal.app.test_client()
codes = hit(c, "/api/v1/register", "198.51.100.40", 7,
            json={"email": "x@y.z", "password": "corta"})
check("/api/v1/register limitado a 5/min", codes.count(429) == 2, codes)

c = portal.app.test_client()
codes = hit(c, "/login", "198.51.100.50", 7, data={"password": "mala"})
check("/login (admin) limitado a 5/min", codes.count(429) == 2, codes)

# ── Los endpoints normales usan el límite general, mucho más alto ────────────
c = portal.app.test_client()
codes = [
    c.get("/api/v1/servers", environ_base={"REMOTE_ADDR": "127.0.0.1"},
          headers={"X-Forwarded-For": "198.51.100.60"}).status_code
    for _ in range(20)
]
check("endpoint normal aguanta 20 peticiones seguidas",
      codes == [200] * 20, set(codes))

print()
if failures:
    print(f"❌ {len(failures)} FALLO(S): {failures}")
    sys.exit(1)
print("✅ Todos los límites se comportan como se espera")
