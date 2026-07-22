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


def _u16_at(i: int, scale: float = 1.0, offset: float = 0.0, dp: int = 0,
            lo: float = float("-inf"), hi: float = float("inf")):
    def decode(b: bytes) -> Optional[str]:
        v = _u16(b, i)
        if v is None:
            return None
        val = v * scale + offset
        return _fmt(val, dp) if lo <= val <= hi else None
    return decode


def _u8_at(i: int, scale: float = 1.0, offset: float = 0.0, dp: int = 0,
           lo: float = float("-inf"), hi: float = float("inf")):
    def decode(b: bytes) -> Optional[str]:
        v = _u8(b, i)
        if v is None:
            return None
        val = v * scale + offset
        return _fmt(val, dp) if lo <= val <= hi else None
    return decode


# UDS service-0x22 single values. e-CMP HV battery on the BMS (0x6B4 -> 0x694),
# from OBDb's Corsa-e signalset (same e-CMP battery in the e-208/e-2008/e-C4/DS3
# E-Tense). SOC is verified on real Peugeot captures; the rest are Corsa-e
# defined (medium). A non-EV never answers. (req_id, did, name, unit, decode)
DIDS: List[Tuple[int, int, str, str, Callable[[bytes], Optional[str]]]] = [
    (0x6B4, 0xD860, "HV battery health (SOH)", "%", _u16_at(1, 1 / 16, dp=1, lo=0, hi=100)),
    (0x6B4, 0xD410, "HV battery charge (SOC)", "%", _u16_at(0, 1 / 512, dp=1, lo=0, hi=100)),
    (0x6B4, 0xD815, "HV battery voltage", "V", _u16_at(0, 1 / 16, dp=1, lo=0, hi=4095)),
    (0x6B4, 0xD865, "HV energy stored", "kWh", _u16_at(0, 1 / 64, dp=2, lo=0, hi=200)),
    (0x6B4, 0xD86F, "HV min cell voltage", "mV", _u16_at(0, lo=0, hi=5000)),
    (0x6B4, 0xD870, "HV max cell voltage", "mV", _u16_at(0, lo=0, hi=5000)),
]

# KWP service-0x21 measurement blocks (HDi diesels, Delphi DCM3.5 / DW10CD from a
# Diagbox trace -- MEDIUM confidence, engine-family specific). One request per
# block returns many values at byte offsets (letter A = index 0 of the block
# payload after the `61 <localId>` echo). A different engine simply NAKs.
# (req_id, local_id_hex, label, [(name, unit, decode), ...])
BLOCKS: List[Tuple[int, str, str, List[Tuple[str, str, Callable[[bytes], Optional[str]]]]]] = [
    (0x6A8, "CB8001", "exhaust / DPF", [
        ("DPF soot load", "g", _u16_at(9, 0.001, dp=1, lo=0, hi=120)),
        ("DPF differential pressure", "mbar", _u16_at(15, lo=0, hi=2000)),
        ("DPF temperature", "°C", _u16_at(25, lo=-50, hi=1000)),
        ("Distance since last regen", "km", _u16_at(28, lo=0, hi=65000)),
        ("Avg distance between regens", "km", _u16_at(30, lo=0, hi=65000)),
        ("Additive (Eolys) tank", "", _u16_at(19, lo=0, hi=65535)),
    ]),
    (0x6A8, "C98001", "injection", [
        ("Engine RPM", "rpm", _u16_at(0, lo=0, hi=8000)),
        ("Rail pressure (measured)", "bar", _u16_at(12, 0.1, dp=1, lo=0, hi=3000)),
        ("Fuel temperature", "°C", _u8_at(28, offset=-50, lo=-40, hi=150)),
    ]),
    (0x6A8, "CA8001", "air / turbo", [
        ("Turbo pressure (measured)", "mbar", _u16_at(8, lo=0, hi=4000)),
        ("Turbo pressure (setpoint)", "mbar", _u16_at(6, lo=0, hi=4000)),
    ]),
    (0x6A8, "CD8001", "coolant", [
        ("Coolant temperature", "°C", _u8_at(3, offset=-50, lo=-40, hi=150)),
    ]),
    (0x6A8, "CC8001", "electrical", [
        ("Battery voltage (engine ECU)", "V", _u8_at(15, 0.1, dp=1, lo=5, hi=16)),
    ]),
]

# Odometer sources for the clocking cross-check. Only the e-CMP BMS has a
# documented odometer DID (D49C: u32/1000 km); pre-2021 diesel/petrol BSI &
# cluster mileage has no clean public DID, so the module-to-module compare only
# fires when 2+ sources answer. (req_id, did, label, decode -> km or None)
ODO_SOURCES: List[Tuple[int, int, str, Callable[[bytes], Optional[float]]]] = [
    (0x6B4, 0xD49C, "HV battery module",
     lambda b: (_u32(b, 0) / 1000.0) if _u32(b, 0) is not None else None),
]


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
            if client.open_session(req_id):
                continue
        return None
    return None


def _read_block(client: PsaElm327UdsClient, req_id: int, local_id: str) -> Optional[bytes]:
    """KWP 0x21 block read -> payload after the ``61 <local_id>`` echo, or None."""
    try:
        reply = client.request(
            req_id, bytes.fromhex("21" + local_id), timeout=3.0
        )
    except (ObdError, TransportError):
        return None
    echo_len = 1 + len(local_id) // 2  # 0x61 + the local-id bytes
    if not reply or reply[0] != 0x61 or len(reply) <= echo_len:
        return None
    return reply[echo_len:]


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

            # KWP 0x21 measurement blocks (HDi diesels): one request, many
            # values. A non-diesel/newer engine just NAKs the local id.
            for req_id, local_id, _label, fields in BLOCKS:
                body = _read_block(client, req_id, local_id)
                if body is None:
                    continue
                for name, unit, decode in fields:
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
