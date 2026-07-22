"""Peugeot / PSA (Citroen, DS) mode-22 health-check values over a plain ELM327
on the diagnostic CAN -- the dealer data generic OBD-II doesn't carry: DPF
state, AdBlue, battery, and odometer readings from more than one module,
cross-checked against each other (a mismatch is the classic clocking flag).

Compiled from community sources (OBDb PSA signalsets, ludwig-v's
arduino-psa-diag, PSA-RE). Entries are best-effort: a DID this particular
car/engine doesn't support simply never answers and is skipped, so unverified
entries are harmless. Read-only (service 0x22 only, plus the read-safe
extended-session escalation some PSA modules want).

Each entry: (req_id, did, name, unit, decode) where decode maps the payload
bytes AFTER ``62 <did_hi> <did_lo>`` to a display value (str) or None to skip.
Mirror of the Android app's PeugeotPids.kt -- keep the two in step.
"""

from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.psa_uds_elm327 import PsaElm327UdsClient
from backend.profiles.peugeot.scan import PEUGEOT_ID_MAP

_KM_TO_MI = 0.621371

# UDS NRCs that mean "I might answer in a different session" -- escalate once.
_SESSION_NRCS = (0x11, 0x22, 0x7E, 0x7F)


def _u8(b: bytes, i: int = 0) -> Optional[int]:
    return b[i] if len(b) > i else None


def _u16(b: bytes, i: int = 0) -> Optional[int]:
    return (b[i] << 8) | b[i + 1] if len(b) > i + 1 else None


def _u24(b: bytes, i: int = 0) -> Optional[int]:
    return (b[i] << 16) | (b[i + 1] << 8) | b[i + 2] if len(b) > i + 2 else None


def _u32(b: bytes, i: int = 0) -> Optional[int]:
    v = _u24(b, i)
    return (v << 8) | b[i + 3] if v is not None and len(b) > i + 3 else None


def _s16(b: bytes, i: int = 0) -> Optional[int]:
    v = _u16(b, i)
    if v is None:
        return None
    return v - 0x10000 if v > 0x7FFF else v


def _fmt(v: float, dp: int = 1) -> str:
    return f"{v:.{dp}f}" if dp else f"{v:.0f}"


def _scaled(scale: float, offset: float = 0.0, dp: int = 1,
            lo: float = float("-inf"), hi: float = float("inf"), signed: bool = False):
    """u16/s16 big-endian -> scaled string, or None outside sanity bounds."""
    def decode(b: bytes) -> Optional[str]:
        v = _s16(b) if signed else _u16(b)
        if v is None:
            return None
        val = v * scale + offset
        return _fmt(val, dp) if lo <= val <= hi else None
    return decode


def _byte(scale: float = 1.0, offset: float = 0.0, dp: int = 0,
          lo: float = float("-inf"), hi: float = float("inf")):
    def decode(b: bytes) -> Optional[str]:
        v = _u8(b)
        if v is None:
            return None
        val = v * scale + offset
        return _fmt(val, dp) if lo <= val <= hi else None
    return decode


# Filled from the community DID research -- see module docstring.
DIDS: List[Tuple[int, int, str, str, Callable[[bytes], Optional[str]]]] = []

# Odometer sources for the clocking cross-check:
# (req_id, did, label, decode -> km float or None).
ODO_SOURCES: List[Tuple[int, int, str, Callable[[bytes], Optional[float]]]] = []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_did(client: PsaElm327UdsClient, req_id: int, did: int,
              escalated: set) -> Optional[bytes]:
    """One RDBI read with per-module extended-session escalation (read-safe)."""
    for attempt in (0, 1):
        try:
            payload = client.request(
                req_id, bytes([0x22, did >> 8, did & 0xFF]), timeout=2.0
            )
        except (ObdError, TransportError):
            return None
        if len(payload) >= 4 and payload[0] == 0x62:
            return payload[3:]
        # A 0x7F negative comes back as a payload too on this client only via
        # exception; anything else malformed we just skip.
        if (len(payload) >= 3 and payload[0] == 0x7F
                and payload[2] in _SESSION_NRCS
                and attempt == 0 and req_id not in escalated):
            escalated.add(req_id)
            if client.start_extended_session(req_id):
                continue
        return None
    return None


def live(adapter: Optional[dict], mode: str = "real") -> dict:
    """Peugeot dealer health check: one read-only pass. Skips non-answers."""
    if mode == "sim":
        return {
            "ok": True, "simulated": True,
            "readings": [
                {"name": "DPF soot load", "value": "34", "unit": "%"},
                {"name": "Odometer (BSI)", "value": "52,410 mi (84,345 km)", "unit": ""},
                {"name": "Odometer (cluster)", "value": "52,410 mi (84,345 km)", "unit": ""},
                {"name": "Mileage cross-check", "value": "OK - sources agree", "unit": ""},
            ],
            "message": "SIMULATED -- no real car connected.",
            "timestamp": _now(),
        }

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "readings": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k CAN PSA diagnostics use."
                ),
                "timestamp": _now(),
            }
        client = PsaElm327UdsClient(session, PEUGEOT_ID_MAP)
        client.begin()
        readings = []
        escalated: set = set()
        try:
            for req_id, did, name, unit, decode in DIDS:
                body = _read_did(client, req_id, did, escalated)
                if body is None:
                    continue
                try:
                    value = decode(body)
                except Exception:
                    continue
                if value is not None:
                    readings.append({"name": name, "value": value, "unit": unit})

            # Odometer cross-check: show every source that answers, compare.
            odo_km = []
            for req_id, did, label, decode_km in ODO_SOURCES:
                body = _read_did(client, req_id, did, escalated)
                if body is None:
                    continue
                try:
                    km = decode_km(body)
                except Exception:
                    continue
                if km is None:
                    continue
                odo_km.append((label, km))
                readings.append({
                    "name": f"Odometer ({label})",
                    "value": f"{km * _KM_TO_MI:,.0f} mi ({km:,.0f} km)",
                    "unit": "",
                })
            if len(odo_km) >= 2:
                kms = [km for _, km in odo_km]
                spread_mi = (max(kms) - min(kms)) * _KM_TO_MI
                readings.append({
                    "name": "Mileage cross-check",
                    "value": ("OK - sources agree" if spread_mi <= 6.0 else
                              f"WARNING - sources differ by {spread_mi:,.0f} mi "
                              "(clocking risk)"),
                    "unit": "",
                })
        finally:
            client.restore()
        return {
            "ok": True, "simulated": False, "readings": readings,
            "message": (
                f"{len(readings)} Peugeot-specific value(s) answered. Values this car "
                "doesn't support are skipped -- they vary by model year and engine. "
                "Read-only pass; nothing was changed on the car."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {
            "ok": False, "simulated": False, "readings": [],
            "message": str(exc), "timestamp": _now(),
        }
