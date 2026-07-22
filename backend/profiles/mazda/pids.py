"""Mazda-specific mode-22 (ReadDataByIdentifier) live values readable over a
plain ELM327 on HS-CAN -- the "enhanced" data generic OBD-II doesn't carry.

Compiled from community sources (Torque custom-PID lists, FORScan forum work)
for SkyActiv-era cars. Entries are best-effort: a DID this particular
car/engine doesn't support simply never answers and is skipped, so unverified
entries are harmless. Read-only (service 0x22 only).

Each entry: (req_id, did, name, unit, decode) where decode maps the payload
bytes AFTER ``62 <did_hi> <did_lo>`` to a display value (str) or None to skip.
"""

from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient


def _u8(b: bytes, i: int = 0) -> Optional[int]:
    return b[i] if len(b) > i else None


def _u16(b: bytes, i: int = 0) -> Optional[int]:
    return (b[i] << 8) | b[i + 1] if len(b) > i + 1 else None


def _s16(b: bytes, i: int = 0) -> Optional[int]:
    v = _u16(b, i)
    if v is None:
        return None
    return v - 0x10000 if v > 0x7FFF else v


_KM_TO_MI = 0.621371


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


def _gear_auto(b: bytes) -> Optional[str]:
    v = _u8(b)
    return {0x50: "N", 0x60: "R", 0x70: "P"}.get(v) if v not in range(1, 9) else str(v)


def _lockup(b: bytes) -> Optional[str]:
    v = _u8(b)
    return None if v is None else ("locked" if v else "open")


def _odo_cluster(b: bytes) -> Optional[str]:
    if len(b) < 3:
        return None
    km = ((b[0] << 16) | (b[1] << 8) | b[2]) / 10.0
    if not 0 < km < 2_000_000:
        return None
    return f"{km * _KM_TO_MI:,.0f} mi ({km:,.0f} km)"


def _miles_from_km(scale_km: float):
    def decode(b: bytes) -> Optional[str]:
        v = _u16(b)
        if v is None:
            return None
        return _fmt(v * scale_km * _KM_TO_MI, 0)
    return decode


# Community-documented SkyActiv mode-22 DIDs, grouped by module so the ELM
# re-aims once per module, not per value. Sources + confidence per entry are
# in the research notes; the strongest (ND-verified) come first per module.
# An entry this car doesn't support simply never answers -- skipped.
PCM_DIDS: List[Tuple[int, int, str, str, Callable[[bytes], Optional[str]]]] = [
    # ---- PCM @ 0x7E0 -------------------------------------------------------
    (0x7E0, 0x1310, "Engine oil temperature", "°C", _scaled(0.01, -40, lo=-40, hi=160)),
    (0x7E0, 0x0415, "Engine oil pressure", "kPa", _scaled(1, 0, dp=1, lo=0, hi=1500, signed=True)),
    (0x7E0, 0x0202, "Stored faults (PCM)", "", _byte(lo=0, hi=200)),
    (0x7E0, 0x0301, "MAP sensor voltage", "V", _scaled(0.001, dp=2, lo=0, hi=5.1)),
    (0x7E0, 0x16E9, "Alternator output", "V", _scaled(0.125, lo=0, hi=20)),
    (0x7E0, 0x16E8, "Alternator field duty", "%", _scaled(200 / 65535, lo=0, hi=101)),
    (0x7E0, 0x03E8, "Knock / octane adjust ratio", "", _scaled(1 / 16384, dp=3, signed=True, lo=-1.1, hi=1.1)),
    (0x7E0, 0x0353, "Barometric pressure", "kPa", _scaled(0.25, lo=60, hi=110)),
    (0x7E0, 0x057D, "Ambient air temp (PCM)", "°C", _byte(0.5, -40, dp=1, lo=-40, hi=70)),
    (0x7E0, 0x038F, "Coolant temp (hi-res)", "°C", _scaled(1 / 64, signed=True, lo=-40, hi=150)),
    (0x7E0, 0x0519, "Distance since oil change", "mi", _miles_from_km(4.0)),
    (0x7E0, 0x0542, "Oil change interval", "mi", _miles_from_km(1.0)),
    (0x7E0, 0x1172, "Battery voltage (PCM)", "V", _byte(1 / 16, dp=1, lo=5, hi=16)),
    (0x7E0, 0x16C1, "Fuel level (PCM)", "%", _scaled(1 / 328, lo=0, hi=102)),
    (0x7E0, 0x096E, "Idle speed target", "rpm", _scaled(0.25, dp=1, lo=0, hi=3000)),
    (0x7E0, 0x0307, "Fuel pump commanded duty", "%", _scaled(1 / 327.68, lo=0, hi=101)),
    # ---- TCM @ 0x7E1 (automatics; a manual just never answers) -------------
    (0x7E1, 0x1E1C, "Gearbox (ATF) temperature", "°C", _scaled(1 / 80, lo=-40, hi=160)),
    (0x7E1, 0x1E12, "Gear engaged (auto)", "", _gear_auto),
    (0x7E1, 0x1E24, "Torque converter", "", _lockup),
    (0x7E1, 0x1EE4, "Engine torque (TCM)", "Nm", _scaled(0.02, signed=True, lo=-200, hi=500)),
    # ---- Instrument cluster @ 0x720 ----------------------------------------
    (0x720, 0x61B1, "Fuel in tank (cluster)", "L", _byte(dp=1, lo=0, hi=80)),
    (0x720, 0x404C, "Odometer (cluster)", "", _odo_cluster),
    (0x720, 0x2A05, "Tyre pressure 1", "psi", _byte(0.199, dp=1, lo=2, hi=60)),
    (0x720, 0x2A06, "Tyre pressure 2", "psi", _byte(0.199, dp=1, lo=2, hi=60)),
    (0x720, 0x2A07, "Tyre pressure 3", "psi", _byte(0.199, dp=1, lo=2, hi=60)),
    (0x720, 0x2A08, "Tyre pressure 4", "psi", _byte(0.199, dp=1, lo=2, hi=60)),
    # ---- Front body control module @ 0x726 ---------------------------------
    (0x726, 0x4028, "12V battery charge", "%", _byte(lo=0, hi=100)),
    (0x726, 0x4029, "12V battery temperature", "°C", _byte(1, -40, lo=-40, hi=100)),
    (0x726, 0x402A, "12V battery voltage", "V", _byte(0.05, 6, dp=1, lo=5, hi=17)),
    (0x726, 0x402B, "12V battery current", "A", _byte(1, -127, lo=-130, hi=130)),
    (0x726, 0x4027, "12V battery age", "days", _scaled(1, dp=0, lo=0, hi=8000)),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def live(adapter: Optional[dict], mode: str = "real") -> dict:
    """One pass over the Mazda enhanced DIDs. Read-only, skips non-answers."""
    if mode == "sim":
        return {
            "ok": True, "simulated": True,
            "readings": [
                {"name": "SIMULATED value", "value": "42.0", "unit": "demo"},
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
                    "11-bit/500k HS-CAN Mazda enhanced values use."
                ),
                "timestamp": _now(),
            }
        client = Elm327UdsClient(session)
        client.begin()
        readings = []
        try:
            for req_id, did, name, unit, decode in PCM_DIDS:
                try:
                    payload = client.request(
                        req_id, bytes([0x22, did >> 8, did & 0xFF]), timeout=2.0
                    )
                except (ObdError, TransportError):
                    continue
                if len(payload) < 4 or payload[0] != 0x62:
                    continue
                try:
                    value = decode(payload[3:])
                except Exception:
                    continue
                if value is not None:
                    readings.append({"name": name, "value": value, "unit": unit})
        finally:
            client.restore()
        return {
            "ok": True, "simulated": False, "readings": readings,
            "message": (
                f"{len(readings)} Mazda-specific value(s) answered. Values this car "
                "doesn't support are skipped -- they vary by model year and engine."
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
