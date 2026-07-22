"""VW / Audi (VAG) live "extras" over a plain ELM327, service 0x22
(ReadDataByIdentifier) addressed to the engine ECU (0x7E0, standard +8).

Two mechanisms (docs/private/vag-research.md §4), both read-only:

(a) The standard OBD PIDs remapped as DIDs at 0xF400 + PID (mechanism CONFIRMED
    on a VW: 22 F4 0D and 22 F4 42 captured). These carry the same scaling as
    the ISO 15031 PID, so coolant/oil/battery/ambient/load are decoded honestly.

(b) The diesel DPF pack and the EV State-of-Charge DID. These are real
    (captured/used by forum tuners) but SINGLE-SOURCE and, crucially, their
    SCALING IS NOT PUBLICLY CONFIRMED (research §4b). So we NEVER fabricate an
    engineering figure for them: we show the raw 16-bit value + the exact hex,
    and append a caveat reading that says the DIDs and scaling are
    community-sourced and must be verified against the car. A DID this
    particular car/engine doesn't support simply never answers and is skipped,
    so unverified entries are harmless.

Research marks a confirmed DSG/ATF-temperature DID and an odometer DID as
genuine gaps -- they are deliberately NOT invented here.

Each remapped entry: (did, name, unit, decode) where decode maps the payload
bytes AFTER ``62 <did_hi> <did_lo>`` to a display value (str) or None to skip.
"""

from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

# All live reads are addressed to the engine ECU on the standard block (0x7E0,
# response 0x7E8 = request + 8), so no id_map is needed.
ENGINE = 0x7E0


def _u8(b: bytes, i: int = 0) -> Optional[int]:
    return b[i] if len(b) > i else None


def _u16(b: bytes, i: int = 0) -> Optional[int]:
    return (b[i] << 8) | b[i + 1] if len(b) > i + 1 else None


def _fmt(v: float, dp: int = 1) -> str:
    return f"{v:.{dp}f}" if dp else f"{v:.0f}"


def _byte(scale: float = 1.0, offset: float = 0.0, dp: int = 0,
          lo: float = float("-inf"), hi: float = float("inf")):
    """First payload byte (A) -> scaled string, or None outside sanity bounds."""
    def decode(b: bytes) -> Optional[str]:
        v = _u8(b)
        if v is None:
            return None
        val = v * scale + offset
        return _fmt(val, dp) if lo <= val <= hi else None
    return decode


def _u16_scaled(scale: float, offset: float = 0.0, dp: int = 1,
                lo: float = float("-inf"), hi: float = float("inf")):
    """Big-endian A*256+B -> scaled string, or None outside sanity bounds."""
    def decode(b: bytes) -> Optional[str]:
        v = _u16(b)
        if v is None:
            return None
        val = v * scale + offset
        return _fmt(val, dp) if lo <= val <= hi else None
    return decode


def _raw_reading(b: bytes) -> Optional[str]:
    """Community DID with unconfirmed scaling: show the raw 16-bit value and the
    exact bytes, never a fabricated engineering figure."""
    if not b:
        return None
    v = _u16(b, 0)
    hexed = b.hex().upper()
    if v is None:
        return f"0x{hexed}"
    return f"{v} (raw 0x{hexed})"


# (a) CONFIRMED remapped OBD set: DID = 0xF400 + standard PID (research §4a).
# Scaling is the ISO 15031 PID's own scaling. (did, name, unit, decode)
OBD_REMAP_DIDS: List[Tuple[int, str, str, Callable[[bytes], Optional[str]]]] = [
    (0xF405, "Coolant temperature", "°C", _byte(1, -40, lo=-40, hi=215)),
    (0xF45C, "Engine oil temperature", "°C", _byte(1, -40, lo=-40, hi=215)),
    (0xF442, "Battery voltage (control module)", "V", _u16_scaled(1 / 1000, dp=2, lo=0, hi=32)),
    (0xF42F, "Fuel level", "%", _byte(100 / 255, dp=0, lo=0, hi=100)),
    (0xF446, "Ambient air temperature", "°C", _byte(1, -40, lo=-40, hi=100)),
    (0xF404, "Calculated engine load", "%", _byte(100 / 255, dp=0, lo=0, hi=100)),
]

# (b) COMMUNITY-SOURCED, UNVERIFIED diesel DPF DIDs (research §4b). Real but
# single-source, and their scaling is NOT publicly confirmed -- shown raw only.
# (did, name)
DPF_DIDS: List[Tuple[int, str]] = [
    (0x114F, "DPF soot mass, calculated"),
    (0x114E, "DPF soot mass, measured"),
    (0x1156, "DPF distance since last regen"),
    (0x115E, "DPF time since last regen"),
    (0x178C, "DPF oil-ash residue"),
    (0x11B2, "DPF inlet temperature"),
    (0x10F9, "DPF outlet temperature"),
]

# COMMUNITY-SOURCED, UNVERIFIED single-source EV DID (research §4b). Raw only.
EV_DIDS: List[Tuple[int, str]] = [
    (0x1DD0, "HV battery State-of-Charge"),
]

_COMMUNITY_CAVEAT = {
    "name": "Note",
    "value": ("DPF / HV readings above are community-sourced DIDs with unconfirmed "
              "scaling — raw values shown; verify against the car before trusting."),
    "unit": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_did(client: Elm327UdsClient, did: int) -> Optional[bytes]:
    """One RDBI read on the engine ECU -> payload after ``62 <did>``, or None."""
    try:
        payload = client.request(ENGINE, bytes([0x22, did >> 8, did & 0xFF]), timeout=2.0)
    except (ObdError, TransportError):
        return None
    if len(payload) < 4 or payload[0] != 0x62:
        return None
    return payload[3:]


def live(adapter: Optional[dict], mode: str = "real") -> dict:
    """One read-only pass over the VAG live extras. Skips non-answers."""
    if mode == "sim":
        return {
            "ok": True, "simulated": True,
            "readings": [
                {"name": "Coolant temperature", "value": "89", "unit": "°C"},
                {"name": "Engine oil temperature", "value": "97", "unit": "°C"},
                {"name": "Battery voltage (control module)", "value": "14.21", "unit": "V"},
                {"name": "DPF soot mass, calculated", "value": "12000 (raw 0x2EE0)", "unit": "raw"},
                _COMMUNITY_CAVEAT,
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
                    "11-bit/500k CAN VAG live values use."
                ),
                "timestamp": _now(),
            }
        client = Elm327UdsClient(session)
        client.begin()
        readings = []
        community = 0
        try:
            # (a) Confirmed remapped OBD set -- decoded honestly.
            for did, name, unit, decode in OBD_REMAP_DIDS:
                body = _read_did(client, did)
                if body is None:
                    continue
                try:
                    value = decode(body)
                except Exception:
                    continue
                if value is not None:
                    readings.append({"name": name, "value": value, "unit": unit})

            # (b) Community diesel DPF + EV DIDs -- raw value + hex only.
            for did, name in DPF_DIDS + EV_DIDS:
                body = _read_did(client, did)
                if body is None:
                    continue
                value = _raw_reading(body)
                if value is not None:
                    readings.append({"name": name, "value": value, "unit": "raw"})
                    community += 1

            if community:
                readings.append(dict(_COMMUNITY_CAVEAT))
        finally:
            client.restore()
        return {
            "ok": True, "simulated": False, "readings": readings,
            "message": (
                f"{len(readings)} VW/Audi value(s) answered. Values this car doesn't "
                "support are skipped -- they vary by model year and engine. Read-only "
                "pass; nothing was changed on the car."
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
