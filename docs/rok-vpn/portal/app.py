"""ROK VPN Portal — Flask admin + client download portal."""
import io
import os
import secrets
import sqlite3
import subprocess
from datetime import datetime, timezone
from functools import wraps

import qrcode
from flask import (Flask, flash, redirect, render_template, request,
                   send_file, session, url_for)

app = Flask(__name__)
# Debe venir del entorno: con varios workers de gunicorn, una clave generada
# por proceso rompería las sesiones. El fallback solo sirve para desarrollo
# con un único worker.
app.secret_key = os.environ.get("ROK_SECRET_KEY") or secrets.token_hex(32)

# ── Config from environment ────────────────────────────────────────────────────
DB_PATH      = os.environ.get("ROK_DB_PATH",       "/opt/rok-vpn/db/peers.db")
PEERS_DIR    = os.environ.get("ROK_PEERS_DIR",     "/opt/rok-vpn/peers")
VPS_ENDPOINT = os.environ.get("ROK_VPS_ENDPOINT",  "74.208.44.254:1194")
WG_IFACE     = os.environ.get("ROK_WG_IFACE",      "wg0")
WG_SUBNET    = os.environ.get("ROK_WG_SUBNET",     "10.100.0")
DNS          = os.environ.get("ROK_DNS",            "10.100.0.1")
PORTAL_PASS  = os.environ.get("ROK_PORTAL_PASSWORD", "changeme")


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
                revoked        INTEGER DEFAULT 0
            )
        """)
        # El nombre solo debe ser único entre peers activos: revocar a un
        # cliente debe permitir volver a darlo de alta con el mismo nombre
        # (p. ej. tras perder el dispositivo), conservando el histórico.
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_peers_name_active
                ON peers(name) WHERE revoked = 0
        """)


init_db()


# ── WireGuard helpers ──────────────────────────────────────────────────────────
def wg_genkey():
    """Return (private_key, public_key) strings."""
    priv = subprocess.check_output(["wg", "genkey"]).decode().strip()
    pub  = subprocess.check_output(["wg", "pubkey"], input=priv.encode()).decode().strip()
    return priv, pub


def wg_get_server_pubkey():
    return subprocess.check_output(
        ["wg", "show", WG_IFACE, "public-key"]
    ).decode().strip()


def wg_add_peer(public_key: str, ip: str):
    subprocess.check_call([
        "wg", "set", WG_IFACE,
        "peer", public_key,
        "allowed-ips", f"{ip}/32",
    ])
    subprocess.check_call(["wg-quick", "save", WG_IFACE])


def wg_remove_peer(public_key: str):
    subprocess.check_call([
        "wg", "set", WG_IFACE,
        "peer", public_key,
        "remove",
    ])
    subprocess.check_call(["wg-quick", "save", WG_IFACE])


# ── IP allocation ──────────────────────────────────────────────────────────────
def allocate_ip():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ip_address FROM peers WHERE revoked = 0"
        ).fetchall()
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
        f"[Interface]\n"
        f"PrivateKey = {priv}\n"
        f"Address = {ip}/24\n"
        f"DNS = {dns}\n\n"
        f"[Peer]\n"
        f"PublicKey = {server_pubkey}\n"
        f"Endpoint = {endpoint}\n"
        f"AllowedIPs = 0.0.0.0/0, ::/0\n"
        f"PersistentKeepalive = 25\n"
    )


def build_wstunnel_config(priv: str, ip: str, server_pubkey: str,
                          dns: str = DNS) -> str:
    # wstunnel client exposes WireGuard on localhost:51820
    return build_config(priv, ip, server_pubkey,
                        endpoint="127.0.0.1:51820", dns=dns)


def generate_qr_png(config_text: str) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(config_text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── Auth decorator ─────────────────────────────────────────────────────────────
def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


# ── Routes: auth ──────────────────────────────────────────────────────────────
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
        peers = conn.execute(
            "SELECT * FROM peers ORDER BY created_at DESC"
        ).fetchall()
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
                """INSERT INTO peers
                   (name, email, public_key, private_key, ip_address,
                    download_token, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, email, pub, priv, ip, token, now),
            )
        except sqlite3.IntegrityError:
            wg_remove_peer(pub)
            flash(f"Ya existe un cliente con el nombre «{name}»", "danger")
            return redirect(url_for("admin"))

    flash(f"Cliente «{name}» creado — IP {ip}", "success")
    return redirect(url_for("admin"))


@app.route("/admin/revoke/<name>", methods=["POST"])
@require_admin
def admin_revoke(name: str):
    # Filtramos por revoked = 0: un mismo nombre puede aparecer varias veces
    # en el histórico y solo una de esas filas está activa.
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
        conn.execute(
            "UPDATE peers SET revoked = 1, revoked_at = ? WHERE id = ?",
            (now, row["id"]),
        )

    flash(f"Cliente «{name}» revocado", "success")
    return redirect(url_for("admin"))


# ── Routes: client download (token-gated, no auth required) ───────────────────
def _get_active_peer(token: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM peers WHERE download_token = ? AND revoked = 0",
            (token,),
        ).fetchone()
    return row


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

    server_pubkey = wg_get_server_pubkey()
    cfg = build_config(peer["private_key"], peer["ip_address"],
                       server_pubkey, VPS_ENDPOINT)
    filename = f"rok-vpn-{peer['name']}.conf"
    return send_file(
        io.BytesIO(cfg.encode()),
        mimetype="text/plain",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/download/<token>/wstunnel-config")
def download_wstunnel_config(token: str):
    peer = _get_active_peer(token)
    if not peer:
        return "Enlace inválido o revocado", 404

    server_pubkey = wg_get_server_pubkey()
    cfg = build_wstunnel_config(peer["private_key"], peer["ip_address"],
                                server_pubkey)
    filename = f"rok-vpn-{peer['name']}-wstunnel.conf"
    return send_file(
        io.BytesIO(cfg.encode()),
        mimetype="text/plain",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/download/<token>/qr.png")
def download_qr(token: str):
    peer = _get_active_peer(token)
    if not peer:
        return "Enlace inválido o revocado", 404

    server_pubkey = wg_get_server_pubkey()
    cfg = build_config(peer["private_key"], peer["ip_address"],
                       server_pubkey, VPS_ENDPOINT)
    png = generate_qr_png(cfg)
    return send_file(io.BytesIO(png), mimetype="image/png")


# ── Health check ───────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return {"status": "ok"}, 200


# ── Redirect root → admin ──────────────────────────────────────────────────────
@app.route("/")
def index():
    return redirect(url_for("admin"))


if __name__ == "__main__":
    port = int(os.environ.get("ROK_PORTAL_PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
