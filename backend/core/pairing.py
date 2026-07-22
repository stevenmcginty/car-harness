"""Phone pairing: one-time codes, bearer tokens and LAN discovery.

The desktop UI (localhost) asks for a 6-digit pairing code; the phone app
claims it from the LAN within 5 minutes and receives a bearer token. Only the
token's SHA-256 is stored (paired_devices.json in the project root), so the
file never contains a usable secret. Every non-localhost API request must
carry a valid token -- enforced by the middleware in main.py.

Discovery: a daemon thread answers UDP broadcasts on port 8766 so the phone
can find the desktop without typing an IP.
"""

import hashlib
import json
import secrets
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEVICES_JSON = PROJECT_ROOT / "paired_devices.json"

CODE_TTL_SECONDS = 300
DISCOVERY_PORT = 8766
DISCOVERY_MAGIC = b"CARHARNESS_DISCOVER"

_lock = threading.Lock()
_pending = {"code": None, "expires": 0.0}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# device persistence
# --------------------------------------------------------------------------- #
def _load() -> list:
    if not DEVICES_JSON.is_file():
        return []
    try:
        data = json.loads(DEVICES_JSON.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("id") and d.get("tokenHash")]


def _save(devices: list) -> None:
    if not devices:
        try:
            if DEVICES_JSON.is_file():
                DEVICES_JSON.unlink()
        except OSError:
            pass
        return
    DEVICES_JSON.write_text(json.dumps(devices, indent=2), encoding="utf-8")


def list_devices() -> list:
    """Public view -- never includes token hashes."""
    return [
        {
            "id": d["id"],
            "name": d.get("name") or "Phone",
            "pairedAt": d.get("pairedAt"),
            "lastSeen": d.get("lastSeen"),
        }
        for d in _load()
    ]


def remove_device(device_id: str) -> bool:
    with _lock:
        devices = _load()
        remaining = [d for d in devices if d.get("id") != device_id]
        if len(remaining) == len(devices):
            return False
        _save(remaining)
        return True


# --------------------------------------------------------------------------- #
# pairing flow
# --------------------------------------------------------------------------- #
def start_pairing() -> dict:
    """Generate (or refresh) the one-time pairing code. Localhost only --
    enforced by the caller."""
    with _lock:
        code = f"{secrets.randbelow(1000000):06d}"
        _pending["code"] = code
        _pending["expires"] = time.monotonic() + CODE_TTL_SECONDS
    return {
        "code": code,
        "expiresInSeconds": CODE_TTL_SECONDS,
        "addresses": lan_addresses(),
        "port": 8765,
    }


def claim(code: str, device_name: str) -> dict:
    """Exchange a valid pairing code for a bearer token. Single use."""
    with _lock:
        pending_code = _pending["code"]
        valid = (
            pending_code is not None
            and time.monotonic() < _pending["expires"]
            and secrets.compare_digest(str(code or ""), pending_code)
        )
        if not valid:
            raise PermissionError(
                "Wrong or expired pairing code. Open Car Harness on the desktop, "
                "click 'Pair phone' and enter the fresh code within 5 minutes."
            )
        _pending["code"] = None  # single use

        token = secrets.token_urlsafe(32)
        device = {
            "id": uuid.uuid4().hex,
            "name": (device_name or "Phone").strip()[:60] or "Phone",
            "tokenHash": _hash(token),
            "pairedAt": _now(),
            "lastSeen": _now(),
        }
        devices = _load()
        devices.append(device)
        _save(devices)
    return {
        "token": token,
        "deviceId": device["id"],
        "serverName": socket.gethostname(),
    }


def token_valid(token: str) -> bool:
    if not token:
        return False
    h = _hash(token)
    with _lock:
        devices = _load()
        for d in devices:
            if secrets.compare_digest(d.get("tokenHash", ""), h):
                d["lastSeen"] = _now()
                _save(devices)
                return True
    return False


# --------------------------------------------------------------------------- #
# LAN info + discovery responder
# --------------------------------------------------------------------------- #
def lan_addresses() -> list:
    """IPv4 addresses a phone on the same network could reach us on."""
    try:
        import psutil
    except Exception:
        return []
    out = []
    try:
        for _name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if getattr(a, "family", None) == socket.AF_INET:
                    ip = a.address
                    if ip.startswith("127.") or ip.startswith("169.254."):
                        continue
                    out.append(ip)
    except Exception:
        pass
    return sorted(set(out))


_discovery_started = False


def start_discovery_responder():
    """Answer UDP 'CARHARNESS_DISCOVER' broadcasts with our identity. Runs as
    a daemon thread; failures are silent (discovery is best-effort, the phone
    can always type the IP)."""
    global _discovery_started
    if _discovery_started:
        return
    _discovery_started = True

    def responder():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", DISCOVERY_PORT))
        except OSError:
            return
        reply = json.dumps(
            {
                "app": "car-harness",
                "name": socket.gethostname(),
                "port": 8765,
                "requiresPairing": True,
            }
        ).encode("utf-8")
        while True:
            try:
                data, addr = sock.recvfrom(256)
                if data.startswith(DISCOVERY_MAGIC):
                    sock.sendto(reply, addr)
            except Exception:
                time.sleep(0.5)

    threading.Thread(target=responder, daemon=True, name="carharness-discovery").start()
