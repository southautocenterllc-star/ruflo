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

app = Flask(__name__)
app.secret_key = os.environ.get("ROK_SECRET_KEY") or secrets.token_hex(32)

# ── Config from environment ────────────────────────────────────────────────────
DB_PATH      = os.environ.get("ROK_DB_PATH",        "/opt/rok-vpn/db/peers.db")
PEERS_DIR    = os.environ.get("ROK_PEERS_DIR",      "/opt/rok-vpn/peers")
VPS_ENDPOINT = os.environ.get("ROK_VPS_ENDPOINT",   "74.208.44.254:1194")
WG_IFACE     = os.environ.get("ROK_WG_IFACE",       "wg0")
WG_SUBNET    = os.environ.get("ROK_WG_SUBNET",      "10.100.0")
DNS          = os.environ.get("ROK_DNS",             "10.100.0.1")
PORTAL_PASS  = os.environ.get("ROK_PORTAL_PASSWORD","changeme")
JWT_SECRET   = os.environ.get("ROK_JWT_SECRET",     secrets.token_hex(32))
JWT_EXP_DAYS = int(os.environ.get("ROK_JWT_EXP_DAYS", "30"))
SQUARE_TOKEN       = os.environ.get("ROK_SQUARE_TOKEN", "")
SQUARE_ENV         = os.environ.get("ROK_SQUARE_ENV",   "sandbox")
SQUARE_LOCATION_ID = os.environ.get("ROK_SQUARE_LOCATION_ID", "")

FREE_DEVICE_LIMIT = 1
PAID_DEVICE_LIMIT = 5
PLAN_PRICES = {
    "monthly": {"amount": 499,  "currency": "USD", "label": "$4.99/mes"},
    "annual":  {"amount": 2999, "currency": "USD", "label": "$29.99/año"},
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


# ── IP allocation ──────────────────────────────────────────────────────────────
def allocate_ip():
    with get_db() as conn:
        rows = conn.execute("SELECT ip_address FROM peers WHERE revoked = 0").fetchall()
    used = {r["ip_address"] for r in rows}
    for last_octet in range(2, 255):
        candidate = f"{WG_SUBNET}.{last_octet}"
        if candidate not in used:
            return candidate
    raise RuntimeError("No available IPs in subnet")


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


def _provision_peer(name: str, user_id: int = None):
    """Create WireGuard peer, save to DB and disk. Returns peer row dict."""
    priv, pub = wg_genkey()
    ip        = allocate_ip()
    token     = secrets.token_urlsafe(32)
    now       = datetime.now(timezone.utc).isoformat()
    wg_add_peer(pub, ip)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO peers (name,email,public_key,private_key,ip_address,"
            "download_token,created_at,user_id) VALUES (?,NULL,?,?,?,?,?,?)",
            (name, pub, priv, ip, token, now, user_id),
        )
        row = conn.execute(
            "SELECT * FROM peers WHERE download_token = ?", (token,)
        ).fetchone()
    _save_peer_configs(name, priv, ip)
    return dict(row)


# ── JWT helpers ────────────────────────────────────────────────────────────────
def _make_token(user_id: int) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXP_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def _decode_token(token: str):
    return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])


def require_user(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify(error="Unauthorized"), 401
        try:
            payload = _decode_token(auth[7:])
        except jwt.PyJWTError:
            return jsonify(error="Token inválido o expirado"), 401
        with get_db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE id = ?", (payload["sub"],)
            ).fetchone()
        if not user:
            return jsonify(error="Usuario no encontrado"), 401
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
def login():
    if request.method == "POST":
        if request.form.get("password") == PORTAL_PASS:
            session["admin"] = True
            return redirect(url_for("admin"))
        flash("Contraseña incorrecta", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
        flash("El nombre es obligatorio", "danger")
        return redirect(url_for("admin"))
    priv, pub = wg_genkey()
    ip        = allocate_ip()
    token     = secrets.token_urlsafe(32)
    now       = datetime.now(timezone.utc).isoformat()
    try:
        wg_add_peer(pub, ip)
    except subprocess.CalledProcessError as exc:
        flash(f"Error al añadir peer en WireGuard: {exc}", "danger")
        return redirect(url_for("admin"))
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO peers (name,email,public_key,private_key,ip_address,"
                "download_token,created_at) VALUES (?,?,?,?,?,?,?)",
                (name, email, pub, priv, ip, token, now),
            )
        except sqlite3.IntegrityError:
            wg_remove_peer(pub)
            flash(f"Ya existe un cliente con el nombre «{name}»", "danger")
            return redirect(url_for("admin"))
    _save_peer_configs(name, priv, ip)
    flash(f"Cliente «{name}» creado — IP {ip}", "success")
    return redirect(url_for("admin"))


@app.route("/admin/revoke/<name>", methods=["POST"])
@require_admin
def admin_revoke(name: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM peers WHERE name = ? AND revoked = 0", (name,)
        ).fetchone()
    if not row:
        flash("Cliente no encontrado o ya revocado", "warning")
        return redirect(url_for("admin"))
    try:
        wg_remove_peer(row["public_key"])
    except subprocess.CalledProcessError as exc:
        flash(f"Error al revocar en WireGuard: {exc}", "danger")
        return redirect(url_for("admin"))
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE peers SET revoked = 1, revoked_at = ? WHERE id = ?",
                     (now, row["id"]))
    _delete_peer_configs(name)
    flash(f"Cliente «{name}» revocado", "success")
    return redirect(url_for("admin"))


# ── Routes: client download (token-gated) ─────────────────────────────────────
def _get_active_peer(token: str):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM peers WHERE download_token = ? AND revoked = 0", (token,)
        ).fetchone()


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
    cfg = build_config(peer["private_key"], peer["ip_address"],
                       wg_get_server_pubkey(), VPS_ENDPOINT)
    return send_file(io.BytesIO(cfg.encode()), mimetype="text/plain",
                     as_attachment=True, download_name=f"rok-vpn-{peer['name']}.conf")


@app.route("/download/<token>/wstunnel-config")
def download_wstunnel_config(token: str):
    peer = _get_active_peer(token)
    if not peer:
        return "Enlace inválido o revocado", 404
    cfg = build_wstunnel_config(peer["private_key"], peer["ip_address"],
                                wg_get_server_pubkey())
    return send_file(io.BytesIO(cfg.encode()), mimetype="text/plain",
                     as_attachment=True,
                     download_name=f"rok-vpn-{peer['name']}-wstunnel.conf")


@app.route("/download/<token>/qr.png")
def download_qr(token: str):
    peer = _get_active_peer(token)
    if not peer:
        return "Enlace inválido o revocado", 404
    cfg = build_config(peer["private_key"], peer["ip_address"],
                       wg_get_server_pubkey(), VPS_ENDPOINT)
    return send_file(io.BytesIO(generate_qr_png(cfg)), mimetype="image/png")


# ── API v1: auth ───────────────────────────────────────────────────────────────
@app.route("/api/v1/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True) or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email or not password:
        return jsonify(error="email y password son obligatorios"), 400
    if len(password) < 8:
        return jsonify(error="La contraseña debe tener al menos 8 caracteres"), 400
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        try:
            conn.execute("INSERT INTO users (email,password_hash,created_at) VALUES (?,?,?)",
                         (email, pw_hash, now))
            user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        except sqlite3.IntegrityError:
            return jsonify(error="El email ya está registrado"), 409
    return jsonify(token=_make_token(user["id"]), tier=user["tier"]), 201


@app.route("/api/v1/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not user or not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return jsonify(error="Credenciales incorrectas"), 401
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
            "SELECT id,name,ip_address,download_token,created_at FROM peers "
            "WHERE user_id = ? AND revoked = 0 ORDER BY created_at DESC", (user["id"],)
        ).fetchall()
    return jsonify(devices=[dict(r) for r in rows])


@app.route("/api/v1/devices", methods=["POST"])
@require_user
def api_devices_add(user):
    limit = PAID_DEVICE_LIMIT if user["tier"] == "paid" else FREE_DEVICE_LIMIT
    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM peers WHERE user_id = ? AND revoked = 0", (user["id"],)
        ).fetchone()[0]
    if count >= limit:
        return jsonify(error=f"Límite de dispositivos alcanzado ({limit}). "
                             f"Actualiza a plan pago para agregar más."), 403
    data        = request.get_json(silent=True) or {}
    device_name = (data.get("name") or "dispositivo").strip()[:20]
    peer_name   = f"u{user['id']}-{device_name}"[:40]
    try:
        peer = _provision_peer(peer_name, user_id=user["id"])
    except sqlite3.IntegrityError:
        return jsonify(error="Ya tienes un dispositivo con ese nombre"), 409
    except subprocess.CalledProcessError as exc:
        return jsonify(error=f"Error al crear peer WireGuard: {exc}"), 500
    server_pubkey = wg_get_server_pubkey()
    config = build_wstunnel_config(peer["private_key"], peer["ip_address"], server_pubkey)
    return jsonify(
        id=peer["id"], name=device_name, ip=peer["ip_address"],
        download_token=peer["download_token"], config=config,
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
        return jsonify(error="Dispositivo no encontrado"), 404
    try:
        wg_remove_peer(row["public_key"])
    except subprocess.CalledProcessError as exc:
        return jsonify(error=f"Error al revocar: {exc}"), 500
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE peers SET revoked = 1, revoked_at = ? WHERE id = ?",
                     (now, row["id"]))
    _delete_peer_configs(row["name"])
    return jsonify(ok=True)


# ── API v1: plans & payments ───────────────────────────────────────────────────
@app.route("/api/v1/plans")
def api_plans():
    plans = [
        {"id": "free",    "label": "Gratis",       "amount": 0,   "devices": FREE_DEVICE_LIMIT, "speed": "5 Mbps"},
        {"id": "monthly", "label": "$4.99/mes",    "amount": 499, "devices": PAID_DEVICE_LIMIT, "speed": "Sin límite"},
        {"id": "annual",  "label": "$29.99/año",   "amount": 2999,"devices": PAID_DEVICE_LIMIT, "speed": "Sin límite"},
    ]
    return jsonify(plans=plans)


@app.route("/api/v1/subscribe", methods=["POST"])
@require_user
def api_subscribe(user):
    data = request.get_json(silent=True) or {}
    plan = data.get("plan")
    if plan not in ("monthly", "annual"):
        return jsonify(error="plan debe ser 'monthly' o 'annual'"), 400
    nonce = data.get("nonce") or ""   # Square payment source ID / nonce
    if not nonce:
        return jsonify(error="nonce de pago requerido"), 400

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
        err = _json.loads(exc.read()).get("errors", [{}])[0].get("detail", "Error de pago")
        return jsonify(error=err), 402
    except Exception as exc:
        return jsonify(error=f"Error de pago: {exc}"), 500

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
        extra = conn.execute(
            "SELECT id,public_key,name FROM peers WHERE user_id = ? AND revoked = 0 "
            "ORDER BY created_at DESC", (user["id"],)
        ).fetchall()
    for row in extra[FREE_DEVICE_LIMIT:]:
        try:
            wg_remove_peer(row["public_key"])
        except Exception:
            pass
        with get_db() as conn:
            conn.execute("UPDATE peers SET revoked = 1, revoked_at = ? WHERE id = ?",
                         (now, row["id"]))
        _delete_peer_configs(row["name"])
    return jsonify(ok=True, tier="free")


# ── Health & root ──────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return {"status": "ok"}, 200


@app.route("/")
def index():
    return redirect(url_for("admin"))


if __name__ == "__main__":
    port = int(os.environ.get("ROK_PORTAL_PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
