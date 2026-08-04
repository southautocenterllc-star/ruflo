"""ROK VPN — Registro de servidores multi-país.

Cada país es un VPS independiente corriendo WireGuard + wstunnel + rok-agent.
El portal central guarda el catálogo aquí y habla con cada servidor remoto
por HTTP contra su agente (`agent/agent.py`) usando un token compartido.

El servidor local (donde corre este portal) se marca con is_local = 1 y se
opera con los comandos `wg` directamente, sin pasar por HTTP.
"""
import json
import os
import urllib.error
import urllib.request

AGENT_TIMEOUT = int(os.environ.get("ROK_AGENT_TIMEOUT", "10"))

# ── Catálogo de países ─────────────────────────────────────────────────────────
# Este es el catálogo completo que se muestra en la app, la web y el desktop.
# Un país aparece como disponible sólo cuando su VPS está provisionado
# (endpoint_host poblado + active = 1). El resto se muestra como "próximamente".
#
# Campos: code, country, city, region, tier_required
#   tier_required: 'free' = accesible a todos | 'paid' = sólo plan pago
SERVER_CATALOG = [
    # ── North America ──────────────────────────────────────────────────────────
    {"code": "US", "country": "Estados Unidos", "city": "Nueva York",   "region": "north_america", "tier_required": "free"},
    {"code": "US", "country": "Estados Unidos", "city": "Los Ángeles",  "region": "north_america", "tier_required": "paid"},
    {"code": "CA", "country": "Canadá",         "city": "Toronto",      "region": "north_america", "tier_required": "paid"},
    {"code": "MX", "country": "México",         "city": "Ciudad de México", "region": "north_america", "tier_required": "paid"},

    # ── South America ──────────────────────────────────────────────────────────
    {"code": "BR", "country": "Brasil",         "city": "São Paulo",    "region": "south_america", "tier_required": "paid"},
    {"code": "AR", "country": "Argentina",      "city": "Buenos Aires", "region": "south_america", "tier_required": "paid"},
    {"code": "CL", "country": "Chile",          "city": "Santiago",     "region": "south_america", "tier_required": "paid"},
    {"code": "CO", "country": "Colombia",       "city": "Bogotá",       "region": "south_america", "tier_required": "paid"},

    # ── Europe ─────────────────────────────────────────────────────────────────
    {"code": "GB", "country": "Reino Unido",    "city": "Londres",      "region": "europe", "tier_required": "paid"},
    {"code": "DE", "country": "Alemania",       "city": "Fráncfort",    "region": "europe", "tier_required": "paid"},
    {"code": "FR", "country": "Francia",        "city": "París",        "region": "europe", "tier_required": "paid"},
    {"code": "ES", "country": "España",         "city": "Madrid",       "region": "europe", "tier_required": "paid"},
    {"code": "NL", "country": "Países Bajos",   "city": "Ámsterdam",    "region": "europe", "tier_required": "paid"},
    {"code": "CH", "country": "Suiza",          "city": "Zúrich",       "region": "europe", "tier_required": "paid"},

    # ── Asia ───────────────────────────────────────────────────────────────────
    {"code": "JP", "country": "Japón",          "city": "Tokio",        "region": "asia", "tier_required": "paid"},
    {"code": "SG", "country": "Singapur",       "city": "Singapur",     "region": "asia", "tier_required": "paid"},
    {"code": "KR", "country": "Corea del Sur",  "city": "Seúl",         "region": "asia", "tier_required": "paid"},
    {"code": "IN", "country": "India",          "city": "Bombay",       "region": "asia", "tier_required": "paid"},
    {"code": "HK", "country": "Hong Kong",      "city": "Hong Kong",    "region": "asia", "tier_required": "paid"},
    {"code": "AE", "country": "Emiratos Árabes","city": "Dubái",        "region": "asia", "tier_required": "paid"},
]

REGION_LABELS = {
    "north_america": "Norteamérica",
    "south_america": "Sudamérica",
    "europe":        "Europa",
    "asia":          "Asia",
}

REGION_ORDER = ["north_america", "south_america", "europe", "asia"]


def flag_emoji(country_code: str) -> str:
    """Convierte ISO 3166-1 alpha-2 en emoji de bandera (🇺🇸, 🇩🇪...).

    Funciona nativo en iOS, Android, macOS y Windows 11 sin assets ni librerías.
    La web y el desktop pueden usar el mismo `code` con flag-icons si prefieren SVG.
    """
    code = (country_code or "").upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code)


# ── Esquema y seed ─────────────────────────────────────────────────────────────
def init_schema(conn) -> None:
    """Crea la tabla `servers` y añade `server_id` a `peers` si falta."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS servers (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            code           TEXT    NOT NULL,
            country        TEXT    NOT NULL,
            city           TEXT    NOT NULL,
            region         TEXT    NOT NULL,
            tier_required  TEXT    NOT NULL DEFAULT 'paid',
            endpoint_host  TEXT,
            wg_port        INTEGER NOT NULL DEFAULT 1194,
            ws_port        INTEGER NOT NULL DEFAULT 443,
            wg_public_key  TEXT,
            wg_subnet      TEXT    NOT NULL DEFAULT '10.100.0',
            agent_url      TEXT,
            agent_token    TEXT,
            is_local       INTEGER NOT NULL DEFAULT 0,
            active         INTEGER NOT NULL DEFAULT 0,
            load_percent   INTEGER NOT NULL DEFAULT 0,
            sort_order     INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_servers_code_city
            ON servers(code, city)
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(peers)")}
    if "server_id" not in cols:
        conn.execute("ALTER TABLE peers ADD COLUMN server_id INTEGER REFERENCES servers(id)")


def seed_catalog(conn) -> None:
    """Inserta el catálogo. Idempotente: no toca filas ya provisionadas."""
    for order, entry in enumerate(SERVER_CATALOG):
        conn.execute(
            "INSERT OR IGNORE INTO servers (code,country,city,region,tier_required,sort_order)"
            " VALUES (?,?,?,?,?,?)",
            (entry["code"], entry["country"], entry["city"],
             entry["region"], entry["tier_required"], order),
        )


def ensure_local_server(conn, endpoint: str, wg_subnet: str) -> None:
    """Marca el VPS que corre este portal como servidor local activo.

    `endpoint` llega como "host:puerto" desde ROK_VPS_ENDPOINT.
    """
    host, _, port = endpoint.partition(":")
    if not host:
        return
    row = conn.execute("SELECT id FROM servers WHERE is_local = 1").fetchone()
    if row:
        conn.execute(
            "UPDATE servers SET endpoint_host = ?, wg_port = ?, wg_subnet = ?, active = 1"
            " WHERE id = ?",
            (host, int(port or 1194), wg_subnet, row["id"]),
        )
        return
    # Por defecto el servidor local es el primero del catálogo (US / Nueva York).
    conn.execute(
        "UPDATE servers SET endpoint_host = ?, wg_port = ?, wg_subnet = ?,"
        " is_local = 1, active = 1"
        " WHERE id = (SELECT MIN(id) FROM servers)",
        (host, int(port or 1194), wg_subnet),
    )


# ── Consultas ──────────────────────────────────────────────────────────────────
def list_servers(conn, tier: str = "free"):
    """Devuelve el catálogo agrupado por región, listo para serializar.

    Cada servidor trae `available`: True sólo si está provisionado, activo y
    el tier del usuario alcanza. Los no provisionados salen con
    status = 'coming_soon' para que la app los muestre en gris.
    """
    rows = conn.execute(
        "SELECT * FROM servers ORDER BY sort_order, id"
    ).fetchall()

    grouped = {region: [] for region in REGION_ORDER}
    for row in rows:
        provisioned = bool(row["active"] and row["endpoint_host"])
        tier_ok = tier == "paid" or row["tier_required"] == "free"
        if provisioned:
            status = "online" if tier_ok else "locked"
        else:
            status = "coming_soon"
        grouped.setdefault(row["region"], []).append({
            "id":            row["id"],
            "code":          row["code"],
            "flag":          flag_emoji(row["code"]),
            "country":       row["country"],
            "city":          row["city"],
            "region":        row["region"],
            "tier_required": row["tier_required"],
            "load_percent":  row["load_percent"],
            "status":        status,
            "available":     provisioned and tier_ok,
        })

    return [
        {
            "region":  region,
            "label":   REGION_LABELS[region],
            "servers": grouped.get(region, []),
        }
        for region in REGION_ORDER
        if grouped.get(region)
    ]


def get_server(conn, server_id: int):
    return conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()


def default_server(conn):
    """Servidor por defecto: el local, o el primero activo del catálogo."""
    row = conn.execute(
        "SELECT * FROM servers WHERE is_local = 1 AND active = 1"
    ).fetchone()
    if row:
        return row
    return conn.execute(
        "SELECT * FROM servers WHERE active = 1 AND endpoint_host IS NOT NULL"
        " ORDER BY sort_order, id LIMIT 1"
    ).fetchone()


def allocate_ip(conn, server) -> str:
    """Asigna la siguiente IP libre dentro del subnet de ESE servidor.

    Cada país tiene su propio espacio de direcciones, así que las IPs se
    reutilizan entre servidores sin colisionar.
    """
    subnet = server["wg_subnet"]
    rows = conn.execute(
        "SELECT ip_address FROM peers WHERE revoked = 0 AND server_id = ?",
        (server["id"],),
    ).fetchall()
    used = {r["ip_address"] for r in rows}
    for last_octet in range(2, 255):
        candidate = f"{subnet}.{last_octet}"
        if candidate not in used:
            return candidate
    raise RuntimeError(f"Sin IPs disponibles en {server['country']} ({subnet}.0/24)")


# ── Cliente del agente remoto ──────────────────────────────────────────────────
class AgentError(RuntimeError):
    """El agente remoto no pudo aplicar el cambio."""


def _agent_call(server, path: str, payload: dict) -> dict:
    if not server["agent_url"] or not server["agent_token"]:
        raise AgentError(f"Servidor {server['country']} sin agente configurado")
    req = urllib.request.Request(
        f"{server['agent_url'].rstrip('/')}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {server['agent_token']}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=AGENT_TIMEOUT) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = (exc.read() or b"").decode()[:200]
        raise AgentError(f"Agente {server['country']} respondió {exc.code}: {detail}") from exc
    except Exception as exc:
        raise AgentError(f"No se pudo contactar el agente de {server['country']}: {exc}") from exc


def remote_add_peer(server, public_key: str, ip: str) -> None:
    _agent_call(server, "/peers", {"public_key": public_key, "ip": ip})


def remote_remove_peer(server, public_key: str) -> None:
    _agent_call(server, "/peers/remove", {"public_key": public_key})


def remote_public_key(server) -> str:
    """Lee la clave pública WireGuard del servidor remoto y la cachea en DB."""
    result = _agent_call(server, "/pubkey", {})
    key = result.get("public_key", "")
    if not key:
        raise AgentError(f"Agente {server['country']} no devolvió clave pública")
    return key
