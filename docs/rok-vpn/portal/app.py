"""ROK VPN Portal — Flask admin + public user API + Square subscriptions."""
import io
import os
import secrets
import sqlite3
import subprocess
from datetime import datetime, timezone, timedelta
from functools import wraps

import bcrypt
import jwt
import qrcode
from flask import (Flask, Response, flash, jsonify, redirect,
                   render_template, request, send_file, session, url_for)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

import servers as srv

app = Flask(__name__)
app.secret_key = os.environ.get("ROK_SECRET_KEY") or secrets.token_hex(32)

# Detrás de nginx toda petición llega desde 127.0.0.1, así que sin esto el
# limitador mete a todos los clientes en el mismo cubo: cinco intentos de login
# en total dejarían fuera al resto del mundo. ProxyFix hace que remote_addr sea
# la IP real del cliente. x_for=1 confía en exactamente un salto — el nuestro;
# confiar en más dejaría que el cliente falsee su IP añadiendo cabeceras.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["100 per minute"],
    storage_uri="memory://",
    # Los tests recorren los mismos endpoints muchas veces seguidas y chocarían
    # con el límite de 5/min de /login. Se desactiva sólo cuando se pide
    # explícitamente; en producción la variable no existe y el límite queda activo.
    enabled=os.environ.get("ROK_RATELIMIT_ENABLED", "1") != "0",
)

# ── Config from environment ────────────────────────────────────────────────────
DB_PATH      = os.environ.get("ROK_DB_PATH",        "/opt/rok-vpn/db/peers.db")
PEERS_DIR    = os.environ.get("ROK_PEERS_DIR",      "/opt/rok-vpn/peers")
VPS_ENDPOINT = os.environ.get("ROK_VPS_ENDPOINT",   "")
WG_IFACE     = os.environ.get("ROK_WG_IFACE",       "wg0")
WG_SUBNET    = os.environ.get("ROK_WG_SUBNET",      "10.100.0")
DNS          = os.environ.get("ROK_DNS",             "10.100.0.1")
PORTAL_PASS  = os.environ.get("ROK_PORTAL_PASSWORD", "")
JWT_SECRET   = os.environ.get("ROK_JWT_SECRET",     secrets.token_hex(32))
JWT_EXP_DAYS = int(os.environ.get("ROK_JWT_EXP_DAYS", "30"))
SQUARE_TOKEN       = os.environ.get("ROK_SQUARE_TOKEN", "")
SQUARE_ENV         = os.environ.get("ROK_SQUARE_ENV",   "sandbox")
SQUARE_LOCATION_ID = os.environ.get("ROK_SQUARE_LOCATION_ID", "")
# Base absoluta para las etiquetas Open Graph. Los scrapers de WhatsApp y
# iMessage no resuelven rutas relativas ni siguen redirecciones http→https,
# así que en producción esto debe apuntar al dominio final con https://.
SITE_URL = os.environ.get("ROK_SITE_URL", "").rstrip("/")

FREE_DEVICE_LIMIT = 1
PAID_DEVICE_LIMIT = 5
PLAN_PRICES = {
    "monthly": {"amount": 499,  "currency": "USD", "label": "$4.99/mo"},
    "annual":  {"amount": 2999, "currency": "USD", "label": "$29.99/yr"},
}


# ── Database ───────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS peers (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT    NOT NULL,
                email          TEXT,
                public_key     TEXT    NOT NULL,
                private_key    TEXT    NOT NULL,
                ip_address     TEXT    NOT NULL,
                download_token TEXT    UNIQUE NOT NULL,
                created_at     TEXT    NOT NULL,
                revoked_at     TEXT,
                revoked        INTEGER DEFAULT 0,
                user_id        INTEGER REFERENCES users(id)
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_peers_name_active
                ON peers(name) WHERE revoked = 0
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                email               TEXT    UNIQUE NOT NULL,
                password_hash       TEXT    NOT NULL,
                tier                TEXT    NOT NULL DEFAULT 'free',
                square_customer_id  TEXT,
                created_at          TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id               INTEGER NOT NULL REFERENCES users(id),
                plan                  TEXT    NOT NULL,
                status                TEXT    NOT NULL DEFAULT 'active',
                square_payment_id     TEXT,
                started_at            TEXT    NOT NULL,
                expires_at            TEXT
            )
        """)
        srv.init_schema(conn)
        srv.seed_catalog(conn)
        srv.ensure_local_server(conn, VPS_ENDPOINT, WG_SUBNET)


init_db()
os.makedirs(PEERS_DIR, exist_ok=True)


# ── WireGuard helpers ──────────────────────────────────────────────────────────
def wg_genkey():
    priv = subprocess.check_output(["wg", "genkey"]).decode().strip()
    pub  = subprocess.check_output(["wg", "pubkey"], input=priv.encode()).decode().strip()
    return priv, pub


def wg_get_server_pubkey():
    return subprocess.check_output(
        ["wg", "show", WG_IFACE, "public-key"]
    ).decode().strip()


def wg_add_peer(public_key: str, ip: str):
    subprocess.check_call(["wg", "set", WG_IFACE, "peer", public_key, "allowed-ips", f"{ip}/32"])
    subprocess.check_call(["wg-quick", "save", WG_IFACE])


def wg_remove_peer(public_key: str):
    subprocess.check_call(["wg", "set", WG_IFACE, "peer", public_key, "remove"])
    subprocess.check_call(["wg-quick", "save", WG_IFACE])


# ── Despacho local / remoto ────────────────────────────────────────────────────
# El servidor donde corre este portal se opera con comandos `wg` locales.
# El resto de países se operan por HTTP contra su agente (`agent/agent.py`).
def server_add_peer(server, public_key: str, ip: str) -> None:
    if server["is_local"]:
        wg_add_peer(public_key, ip)
    else:
        srv.remote_add_peer(server, public_key, ip)


def server_remove_peer(server, public_key: str) -> None:
    if server["is_local"]:
        wg_remove_peer(public_key)
    else:
        srv.remote_remove_peer(server, public_key)


def server_public_key(server) -> str:
    """Clave pública WireGuard del servidor, cacheada en DB para los remotos."""
    if server["is_local"]:
        return wg_get_server_pubkey()
    if server["wg_public_key"]:
        return server["wg_public_key"]
    key = srv.remote_public_key(server)
    with get_db() as conn:
        conn.execute("UPDATE servers SET wg_public_key = ? WHERE id = ?",
                     (key, server["id"]))
    return key


def server_endpoint(server) -> str:
    return f"{server['endpoint_host']}:{server['wg_port']}"


def resolve_server(conn, server_id, tier: str):
    """Valida el servidor pedido contra el tier del usuario.

    Devuelve (row, None) si está permitido, o (None, mensaje_error).
    Sin server_id, cae al servidor por defecto.
    """
    if server_id is None:
        server = srv.default_server(conn)
        if not server:
            return None, "No servers available"
        return server, None
    server = srv.get_server(conn, server_id)
    if not server:
        return None, "Server not found"
    if not (server["active"] and server["endpoint_host"]):
        return None, f"{server['country']} is not available yet"
    if tier != "paid" and server["tier_required"] == "paid":
        return None, f"{server['country']} requires a paid plan"
    return server, None


# ── Config builders ────────────────────────────────────────────────────────────
def build_config(priv: str, ip: str, server_pubkey: str,
                 endpoint: str, dns: str = DNS) -> str:
    return (
        f"[Interface]\nPrivateKey = {priv}\nAddress = {ip}/24\nDNS = {dns}\n\n"
        f"[Peer]\nPublicKey = {server_pubkey}\nEndpoint = {endpoint}\n"
        f"AllowedIPs = 0.0.0.0/0, ::/0\nPersistentKeepalive = 25\n"
    )


def build_wstunnel_config(priv: str, ip: str, server_pubkey: str, dns: str = DNS) -> str:
    return build_config(priv, ip, server_pubkey, endpoint="127.0.0.1:51820", dns=dns)


def _save_peer_configs(name: str, priv: str, ip: str) -> None:
    try:
        server_pubkey = wg_get_server_pubkey()
        pairs = [
            (f"rok-vpn-{name}.conf",         build_config(priv, ip, server_pubkey, VPS_ENDPOINT)),
            (f"rok-vpn-{name}-wstunnel.conf", build_wstunnel_config(priv, ip, server_pubkey)),
        ]
        for fname, content in pairs:
            with open(os.path.join(PEERS_DIR, fname), "w") as fh:
                fh.write(content)
    except Exception as exc:
        app.logger.warning("No se pudo guardar config en PEERS_DIR: %s", exc)


def _delete_peer_configs(name: str) -> None:
    for fname in (f"rok-vpn-{name}.conf", f"rok-vpn-{name}-wstunnel.conf"):
        path = os.path.join(PEERS_DIR, fname)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception as exc:
            app.logger.warning("No se pudo borrar %s: %s", path, exc)


def generate_qr_png(config_text: str) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(config_text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _provision_peer(name: str, server, user_id: int = None):
    """Crea el peer WireGuard en `server`, lo guarda en DB. Devuelve dict del peer."""
    priv, pub = wg_genkey()
    token     = secrets.token_urlsafe(32)
    now       = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        ip = srv.allocate_ip(conn, server)
    server_add_peer(server, pub, ip)
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO peers (name,email,public_key,private_key,ip_address,"
                "download_token,created_at,user_id,server_id) VALUES (?,NULL,?,?,?,?,?,?,?)",
                (name, pub, priv, ip, token, now, user_id, server["id"]),
            )
            row = conn.execute(
                "SELECT * FROM peers WHERE download_token = ?", (token,)
            ).fetchone()
    except Exception:
        # No dejar el peer huérfano en WireGuard si falla el INSERT.
        try:
            server_remove_peer(server, pub)
        except Exception:
            pass
        raise
    if server["is_local"]:
        _save_peer_configs(name, priv, ip)
    return dict(row)


# ── JWT helpers ────────────────────────────────────────────────────────────────
def _make_token(user_id: int) -> str:
    payload = {
        # RFC 7519 exige que `sub` sea string. PyJWT >= 2.10 lo valida al
        # decodificar y rechaza los enteros con InvalidSubjectError.
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXP_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def _decode_token(token: str) -> dict:
    payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    try:
        payload["sub"] = int(payload["sub"])
    except (TypeError, ValueError) as exc:
        raise jwt.InvalidTokenError("sub inválido") from exc
    return payload


def require_user(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify(error="Unauthorized"), 401
        try:
            payload = _decode_token(auth[7:])
        except jwt.PyJWTError:
            return jsonify(error="Invalid or expired token"), 401
        with get_db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE id = ?", (payload["sub"],)
            ).fetchone()
        if not user:
            return jsonify(error="User not found"), 401
        return f(dict(user), *args, **kwargs)
    return wrapper


# ── Admin auth decorator ───────────────────────────────────────────────────────
def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


# ── Routes: admin auth ─────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def login():
    if request.method == "POST":
        if request.form.get("password") == PORTAL_PASS:
            session["admin"] = True
            return redirect(url_for("admin"))
        flash("Incorrect password", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


# ── Routes: admin ──────────────────────────────────────────────────────────────
@app.route("/admin")
@require_admin
def admin():
    with get_db() as conn:
        peers = conn.execute("SELECT * FROM peers ORDER BY created_at DESC").fetchall()
    return render_template("admin.html", peers=peers)


@app.route("/admin/add", methods=["POST"])
@require_admin
def admin_add():
    name  = request.form.get("name",  "").strip()
    email = request.form.get("email", "").strip()
    if not name:
        flash("Name is required", "danger")
        return redirect(url_for("admin"))
    with get_db() as conn:
        server = srv.default_server(conn)
    if not server:
        flash("No active servers", "danger")
        return redirect(url_for("admin"))
    priv, pub = wg_genkey()
    token     = secrets.token_urlsafe(32)
    now       = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        ip = srv.allocate_ip(conn, server)
    try:
        server_add_peer(server, pub, ip)
    except (subprocess.CalledProcessError, srv.AgentError) as exc:
        flash(f"Failed to add WireGuard peer: {exc}", "danger")
        return redirect(url_for("admin"))
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO peers (name,email,public_key,private_key,ip_address,"
                "download_token,created_at,server_id) VALUES (?,?,?,?,?,?,?,?)",
                (name, email, pub, priv, ip, token, now, server["id"]),
            )
        except sqlite3.IntegrityError:
            server_remove_peer(server, pub)
            flash(f"A client named \u00ab{name}\u00bb already exists", "danger")
            return redirect(url_for("admin"))
    _save_peer_configs(name, priv, ip)
    flash(f"Client \u00ab{name}\u00bb created \u2014 IP {ip}", "success")
    return redirect(url_for("admin"))


@app.route("/admin/servers")
@require_admin
def admin_servers():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,code,country,city,region,tier_required,endpoint_host,"
            "       wg_port,ws_port,agent_url,is_local,active,"
            "       (SELECT COUNT(*) FROM peers WHERE peers.server_id = servers.id"
            "        AND peers.revoked = 0) AS peers"
            "  FROM servers ORDER BY sort_order, id"
        ).fetchall()
    return jsonify(servers=[
        dict(r, flag=srv.flag_emoji(r["code"]), agent_token_set=bool(r["agent_url"]))
        for r in rows
    ])


@app.route("/admin/servers/<int:server_id>", methods=["POST"])
@require_admin
def admin_server_update(server_id: int):
    """Da de alta (o actualiza) un país ya provisionado.

    Body JSON: endpoint_host, agent_url, agent_token, wg_port?, ws_port?,
               wg_subnet?, active?
    El token del agente se guarda tal cual: nunca se devuelve por la API.
    """
    data = request.get_json(silent=True) or {}
    with get_db() as conn:
        if not srv.get_server(conn, server_id):
            return jsonify(error="Server not found"), 404
        fields, values = [], []
        for key in ("endpoint_host", "agent_url", "agent_token", "wg_subnet"):
            if key in data:
                fields.append(f"{key} = ?")
                values.append((data[key] or "").strip() or None)
        for key in ("wg_port", "ws_port", "active", "load_percent"):
            if key in data:
                fields.append(f"{key} = ?")
                values.append(int(data[key]))
        if not fields:
            return jsonify(error="Nothing to update"), 400
        values.append(server_id)
        conn.execute(f"UPDATE servers SET {', '.join(fields)} WHERE id = ?", values)
        server = srv.get_server(conn, server_id)

    # Verifica que el agente responde y cachea su clave pública.
    warning = None
    if server["active"] and not server["is_local"]:
        try:
            key = srv.remote_public_key(server)
            with get_db() as conn:
                conn.execute("UPDATE servers SET wg_public_key = ? WHERE id = ?",
                             (key, server_id))
        except srv.AgentError as exc:
            warning = str(exc)

    return jsonify(ok=True, id=server_id, country=server["country"], warning=warning)


@app.route("/admin/revoke/<name>", methods=["POST"])
@require_admin
def admin_revoke(name: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM peers WHERE name = ? AND revoked = 0", (name,)
        ).fetchone()
    if not row:
        flash("Client not found or already revoked", "warning")
        return redirect(url_for("admin"))
    try:
        wg_remove_peer(row["public_key"])
    except subprocess.CalledProcessError as exc:
        flash(f"Failed to revoke in WireGuard: {exc}", "danger")
        return redirect(url_for("admin"))
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE peers SET revoked = 1, revoked_at = ? WHERE id = ?",
                     (now, row["id"]))
    _delete_peer_configs(name)
    flash(f"Client \u00ab{name}\u00bb revoked", "success")
    return redirect(url_for("admin"))


# ── Routes: client download (token-gated) ─────────────────────────────────────
def _get_active_peer(token: str):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM peers WHERE download_token = ? AND revoked = 0", (token,)
        ).fetchone()


def _peer_server(peer):
    """Servidor donde vive el peer; cae al por defecto para peers antiguos."""
    with get_db() as conn:
        if peer["server_id"]:
            server = srv.get_server(conn, peer["server_id"])
            if server:
                return server
        return srv.default_server(conn)


@app.route("/download/<token>")
def download_page(token: str):
    peer = _get_active_peer(token)
    if not peer:
        return render_template("404.html"), 404
    return render_template("download.html", peer=peer, token=token)


@app.route("/download/<token>/config")
def download_config(token: str):
    peer = _get_active_peer(token)
    if not peer:
        return "Enlace inválido o revocado", 404
    server = _peer_server(peer)
    cfg = build_config(peer["private_key"], peer["ip_address"],
                       server_public_key(server), server_endpoint(server))
    return send_file(io.BytesIO(cfg.encode()), mimetype="text/plain",
                     as_attachment=True, download_name=f"rok-vpn-{peer['name']}.conf")


@app.route("/download/<token>/wstunnel-config")
def download_wstunnel_config(token: str):
    peer = _get_active_peer(token)
    if not peer:
        return "Enlace inválido o revocado", 404
    cfg = build_wstunnel_config(peer["private_key"], peer["ip_address"],
                                server_public_key(_peer_server(peer)))
    return send_file(io.BytesIO(cfg.encode()), mimetype="text/plain",
                     as_attachment=True,
                     download_name=f"rok-vpn-{peer['name']}-wstunnel.conf")


@app.route("/download/<token>/qr.png")
def download_qr(token: str):
    peer = _get_active_peer(token)
    if not peer:
        return "Enlace inválido o revocado", 404
    server = _peer_server(peer)
    cfg = build_config(peer["private_key"], peer["ip_address"],
                       server_public_key(server), server_endpoint(server))
    return send_file(io.BytesIO(generate_qr_png(cfg)), mimetype="image/png")


# ── API v1: auth ───────────────────────────────────────────────────────────────
@app.route("/api/v1/register", methods=["POST"])
@limiter.limit("5 per minute")
def api_register():
    data = request.get_json(silent=True) or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email or not password:
        return jsonify(error="email and password are required"), 400
    if len(password) < 8:
        return jsonify(error="Password must be at least 8 characters"), 400
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        try:
            conn.execute("INSERT INTO users (email,password_hash,created_at) VALUES (?,?,?)",
                         (email, pw_hash, now))
            user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        except sqlite3.IntegrityError:
            return jsonify(error="That email is already registered"), 409
    return jsonify(token=_make_token(user["id"]), tier=user["tier"]), 201


@app.route("/api/v1/login", methods=["POST"])
@limiter.limit("5 per minute")
def api_login():
    data = request.get_json(silent=True) or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not user or not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return jsonify(error="Incorrect credentials"), 401
    return jsonify(token=_make_token(user["id"]), tier=user["tier"])


@app.route("/api/v1/me")
@require_user
def api_me(user):
    with get_db() as conn:
        device_count = conn.execute(
            "SELECT COUNT(*) FROM peers WHERE user_id = ? AND revoked = 0", (user["id"],)
        ).fetchone()[0]
        sub = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? AND status = 'active' "
            "ORDER BY started_at DESC LIMIT 1", (user["id"],)
        ).fetchone()
    limit = PAID_DEVICE_LIMIT if user["tier"] == "paid" else FREE_DEVICE_LIMIT
    return jsonify(
        id=user["id"], email=user["email"], tier=user["tier"],
        devices=device_count, device_limit=limit,
        subscription=dict(sub) if sub else None,
    )


# ── API v1: devices ────────────────────────────────────────────────────────────
@app.route("/api/v1/devices")
@require_user
def api_devices_list(user):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT p.id, p.name, p.ip_address, p.download_token, p.created_at,"
            "       p.server_id, s.code AS server_code, s.country AS server_country,"
            "       s.city AS server_city"
            "  FROM peers p LEFT JOIN servers s ON s.id = p.server_id"
            " WHERE p.user_id = ? AND p.revoked = 0"
            " ORDER BY p.created_at DESC", (user["id"],)
        ).fetchall()
    devices = []
    for row in rows:
        item = dict(row)
        item["server_flag"] = srv.flag_emoji(row["server_code"] or "")
        devices.append(item)
    return jsonify(devices=devices)


@app.route("/api/v1/devices", methods=["POST"])
@require_user
def api_devices_add(user):
    limit = PAID_DEVICE_LIMIT if user["tier"] == "paid" else FREE_DEVICE_LIMIT
    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM peers WHERE user_id = ? AND revoked = 0", (user["id"],)
        ).fetchone()[0]
    if count >= limit:
        return jsonify(error=f"Device limit reached ({limit}). "
                             f"Upgrade to a paid plan to add more."), 403
    data        = request.get_json(silent=True) or {}
    device_name = (data.get("name") or "dispositivo").strip()[:20]
    peer_name   = f"u{user['id']}-{device_name}"[:40]

    with get_db() as conn:
        server, err = resolve_server(conn, data.get("server_id"), user["tier"])
    if err:
        return jsonify(error=err), 403

    try:
        peer = _provision_peer(peer_name, server, user_id=user["id"])
    except sqlite3.IntegrityError:
        return jsonify(error="You already have a device with that name"), 409
    except srv.AgentError as exc:
        return jsonify(error=str(exc)), 502
    except subprocess.CalledProcessError as exc:
        return jsonify(error=f"Failed to create WireGuard peer: {exc}"), 500

    try:
        pubkey = server_public_key(server)
    except srv.AgentError as exc:
        return jsonify(error=str(exc)), 502
    config = build_wstunnel_config(peer["private_key"], peer["ip_address"], pubkey)
    return jsonify(
        id=peer["id"], name=device_name, ip=peer["ip_address"],
        download_token=peer["download_token"], config=config,
        direct_config=build_config(peer["private_key"], peer["ip_address"],
                                   pubkey, server_endpoint(server)),
        server={
            "id":      server["id"],
            "code":    server["code"],
            "flag":    srv.flag_emoji(server["code"]),
            "country": server["country"],
            "city":    server["city"],
            "wstunnel_target": f"wss://{server['endpoint_host']}:{server['ws_port']}",
        },
    ), 201


@app.route("/api/v1/devices/<int:device_id>", methods=["DELETE"])
@require_user
def api_devices_delete(user, device_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM peers WHERE id = ? AND user_id = ? AND revoked = 0",
            (device_id, user["id"])
        ).fetchone()
    if not row:
        return jsonify(error="Device not found"), 404
    with get_db() as conn:
        server = (srv.get_server(conn, row["server_id"]) if row["server_id"]
                  else srv.default_server(conn))
    if not server:
        return jsonify(error="Device server not found"), 500
    try:
        server_remove_peer(server, row["public_key"])
    except srv.AgentError as exc:
        return jsonify(error=str(exc)), 502
    except subprocess.CalledProcessError as exc:
        return jsonify(error=f"Failed to revoke: {exc}"), 500
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE peers SET revoked = 1, revoked_at = ? WHERE id = ?",
                     (now, row["id"]))
    _delete_peer_configs(row["name"])
    return jsonify(ok=True)


# ── API v1: servidores por país ────────────────────────────────────────────────
def _tier_from_optional_auth() -> str:
    """Lee el tier del Bearer token si viene; si no, asume 'free'.

    Permite que la landing pública liste países sin login, mostrando los de
    pago como bloqueados en vez de ocultarlos.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return "free"
    try:
        payload = _decode_token(auth[7:])
    except jwt.PyJWTError:
        return "free"
    with get_db() as conn:
        user = conn.execute("SELECT tier FROM users WHERE id = ?",
                            (payload["sub"],)).fetchone()
    return user["tier"] if user else "free"


@app.route("/api/v1/servers")
def api_servers():
    """Catálogo de países agrupado por región.

    Misma respuesta alimenta app móvil, web y desktop. Cada servidor trae su
    código ISO y el emoji de bandera; los clientes que prefieran SVG usan el
    código con flag-icons.
    """
    tier = _tier_from_optional_auth()
    with get_db() as conn:
        regions = srv.list_servers(conn, tier)
        default = srv.default_server(conn)
    return jsonify(
        tier=tier,
        default_server_id=default["id"] if default else None,
        regions=regions,
    )


# ── API v1: plans & payments ───────────────────────────────────────────────────
@app.route("/api/v1/plans")
def api_plans():
    plans = [
        {"id": "free",    "label": "Free",       "amount": 0,   "devices": FREE_DEVICE_LIMIT, "speed": "5 Mbps"},
        {"id": "monthly", "label": "$4.99/mo",    "amount": 499, "devices": PAID_DEVICE_LIMIT, "speed": "Unlimited"},
        {"id": "annual",  "label": "$29.99/yr",   "amount": 2999,"devices": PAID_DEVICE_LIMIT, "speed": "Unlimited"},
    ]
    return jsonify(plans=plans)


@app.route("/api/v1/subscribe", methods=["POST"])
@limiter.limit("5 per minute")
@require_user
def api_subscribe(user):
    data = request.get_json(silent=True) or {}
    plan = data.get("plan")
    if plan not in ("monthly", "annual"):
        return jsonify(error="plan must be 'monthly' or 'annual'"), 400
    nonce = data.get("nonce") or ""   # Square payment source ID / nonce
    if not nonce:
        return jsonify(error="payment nonce is required"), 400

    # Square payment via REST API (no SDK dependency)
    import urllib.request, urllib.error, json as _json
    price = PLAN_PRICES[plan]
    sq_env = "https://connect.squareup.com" if SQUARE_ENV == "production" else "https://connect.squareupsandbox.com"
    payload = _json.dumps({
        "source_id": nonce,
        "idempotency_key": secrets.token_hex(16),
        "amount_money": {"amount": price["amount"], "currency": price["currency"]},
        "location_id": SQUARE_LOCATION_ID,
    }).encode()
    req = urllib.request.Request(
        f"{sq_env}/v2/payments",
        data=payload,
        headers={"Authorization": f"Bearer {SQUARE_TOKEN}",
                 "Content-Type": "application/json", "Square-Version": "2024-01-18"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = _json.loads(resp.read())
        payment_id = result["payment"]["id"]
    except urllib.error.HTTPError as exc:
        err = _json.loads(exc.read()).get("errors", [{}])[0].get("detail", "Payment error")
        return jsonify(error=err), 402
    except Exception as exc:
        return jsonify(error=f"Payment error: {exc}"), 500

    now = datetime.now(timezone.utc)
    expires = now + (timedelta(days=365) if plan == "annual" else timedelta(days=31))
    with get_db() as conn:
        conn.execute(
            "INSERT INTO subscriptions (user_id,plan,status,square_payment_id,started_at,expires_at)"
            " VALUES (?,?,?,?,?,?)",
            (user["id"], plan, "active", payment_id, now.isoformat(), expires.isoformat()),
        )
        conn.execute("UPDATE users SET tier = 'paid' WHERE id = ?", (user["id"],))
    return jsonify(ok=True, plan=plan, expires_at=expires.isoformat()), 201


@app.route("/api/v1/subscription/cancel", methods=["POST"])
@require_user
def api_subscription_cancel(user):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE subscriptions SET status = 'cancelled' WHERE user_id = ? AND status = 'active'",
            (user["id"],),
        )
        conn.execute("UPDATE users SET tier = 'free' WHERE id = ?", (user["id"],))
        # Revoke extra devices beyond free limit
        rows = conn.execute(
            "SELECT id,public_key,name,server_id FROM peers WHERE user_id = ? AND revoked = 0 "
            "ORDER BY created_at DESC", (user["id"],)
        ).fetchall()
        server_by_id = {s["id"]: s for s in conn.execute("SELECT * FROM servers")}

    # Se revocan los que exceden el límite gratis y, además, los que están en
    # países que sólo cubre el plan pago.
    def _needs_revoke(index, row):
        if index >= FREE_DEVICE_LIMIT:
            return True
        server = server_by_id.get(row["server_id"])
        return bool(server and server["tier_required"] == "paid")

    for idx, row in enumerate(rows):
        if not _needs_revoke(idx, row):
            continue
        server = server_by_id.get(row["server_id"])
        try:
            if server:
                server_remove_peer(server, row["public_key"])
            else:
                wg_remove_peer(row["public_key"])
        except Exception as exc:
            app.logger.warning("No se pudo revocar peer %s: %s", row["name"], exc)
        with get_db() as conn:
            conn.execute("UPDATE peers SET revoked = 1, revoked_at = ? WHERE id = ?",
                         (now, row["id"]))
        _delete_peer_configs(row["name"])
    return jsonify(ok=True, tier="free")


# ── Error handlers ─────────────────────────────────────────────────────────────
@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify(error="Too many requests. Please slow down and try again later."), 429


def site_url() -> str:
    """Base absoluta del sitio para Open Graph.

    Prefiere ROK_SITE_URL; si no está definida cae a la petición actual y
    fuerza https cuando nginx lo indica por X-Forwarded-Proto, porque los
    scrapers descartan una og:image servida por http desde una página https.
    """
    if SITE_URL:
        return SITE_URL
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    return f"{proto}://{request.host}"


# ── Health & root ──────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return {"status": "ok"}, 200


@app.route("/")
def index():
    return render_template("landing.html", site_url=site_url())


@app.route("/mobile-screens")
def mobile_screens():
    """Escaparate de las pantallas de la app: el build de Claude Design tal cual.

    NO pasa por Jinja. El bundle lleva ~40 expresiones `{{ ... }}` propias de su
    runtime y Jinja las evaluaría como variables suyas, vaciándolas y rompiendo
    la página. Sólo se sustituye un token literal para la URL absoluta del OG.
    """
    path = os.path.join(app.root_path, "templates", "mobile-screens.html")
    with open(path, encoding="utf-8") as fh:
        html = fh.read().replace("__SITE_URL__", site_url())
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    port = int(os.environ.get("ROK_PORTAL_PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
