"""ROK VPN — Agente de nodo.

Corre en cada VPS de país (Alemania, Japón, Brasil...). No tiene base de datos
ni usuarios: sólo aplica cambios de peers en WireGuard cuando el portal central
se lo pide, autenticado con un token compartido.

El portal central NO corre este agente — ahí las operaciones son locales.

Variables de entorno:
    ROK_AGENT_TOKEN   token compartido con el portal (obligatorio)
    ROK_WG_IFACE      interfaz WireGuard (default: wg0)
    ROK_AGENT_PORT    puerto de escucha (default: 8081)
"""
import ipaddress
import os
import re
import subprocess
from functools import wraps

from flask import Flask, jsonify, request

app = Flask(__name__)

AGENT_TOKEN = os.environ.get("ROK_AGENT_TOKEN", "")
WG_IFACE    = os.environ.get("ROK_WG_IFACE", "wg0")

# Clave pública WireGuard: 44 chars base64 terminados en '='
WG_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")


def require_token(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not AGENT_TOKEN:
            return jsonify(error="Agente sin token configurado"), 503
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not _constant_eq(auth[7:], AGENT_TOKEN):
            return jsonify(error="Unauthorized"), 401
        return f(*args, **kwargs)
    return wrapper


def _constant_eq(a: str, b: str) -> bool:
    """Comparación en tiempo constante para no filtrar el token por timing."""
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())


def _validate_key(key: str) -> str:
    key = (key or "").strip()
    if not WG_KEY_RE.match(key):
        raise ValueError("public_key inválida")
    return key


def _validate_ip(ip: str) -> str:
    ip = (ip or "").strip()
    try:
        parsed = ipaddress.IPv4Address(ip)
    except ValueError as exc:
        raise ValueError("ip inválida") from exc
    if not parsed.is_private:
        raise ValueError("ip debe estar en rango privado")
    return str(parsed)


@app.route("/health")
def health():
    return {"status": "ok", "iface": WG_IFACE}, 200


@app.route("/pubkey", methods=["POST"])
@require_token
def pubkey():
    try:
        key = subprocess.check_output(
            ["wg", "show", WG_IFACE, "public-key"], timeout=10
        ).decode().strip()
    except subprocess.SubprocessError as exc:
        return jsonify(error=f"No se pudo leer la clave: {exc}"), 500
    return jsonify(public_key=key)


@app.route("/peers", methods=["POST"])
@require_token
def add_peer():
    data = request.get_json(silent=True) or {}
    try:
        key = _validate_key(data.get("public_key"))
        ip  = _validate_ip(data.get("ip"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    try:
        subprocess.check_call(
            ["wg", "set", WG_IFACE, "peer", key, "allowed-ips", f"{ip}/32"], timeout=15
        )
        subprocess.check_call(["wg-quick", "save", WG_IFACE], timeout=15)
    except subprocess.SubprocessError as exc:
        return jsonify(error=f"Error al añadir peer: {exc}"), 500
    return jsonify(ok=True, ip=ip), 201


@app.route("/peers/remove", methods=["POST"])
@require_token
def remove_peer():
    data = request.get_json(silent=True) or {}
    try:
        key = _validate_key(data.get("public_key"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    try:
        subprocess.check_call(["wg", "set", WG_IFACE, "peer", key, "remove"], timeout=15)
        subprocess.check_call(["wg-quick", "save", WG_IFACE], timeout=15)
    except subprocess.SubprocessError as exc:
        return jsonify(error=f"Error al revocar peer: {exc}"), 500
    return jsonify(ok=True)


if __name__ == "__main__":
    port = int(os.environ.get("ROK_AGENT_PORT", 8081))
    app.run(host="0.0.0.0", port=port, debug=False)
