"""Fiat / Alfa Romeo live "extras" over a plain ELM327 (docs/private/fiat-alfa-research.md §4).

The UDS-era FCA values are read over 29-bit CAN (ATSP7) with Elm327Uds29Client,
service 0x22 (ReadDataByIdentifier). Every row below is a REAL CarScanner PID
captured on an on-car Alfa Giulia 2.2 MJTD MY2016 (danardi78 custompids.csv), so
it is captured data -- but from a SINGLE source, so the scaling must be
re-checked on the car before the absolute values are trusted (a caveat rides on
the readings, and the panel footer says so). The diesel DPF rows (soot mass +
regeneration) are the highest-value dealer check on a Giulia diesel.

Values live on two modules: the engine 0x10 and the transmission (TCT/ZF) 0x18.
danardi warns DA10F1 reads are slow over BLE, so each DID is polled ONE at a time
with a generous timeout.

TPMS (0xC7 / 18DAC7F1) is NEVER polled here -- it can engage the immobilizer and
stall the car (research §7). There is deliberately no TPMS row.

If the 29-bit reads all fail (a CAN-era Fiat, not a UDS-era car), live() falls
back to a curated generic Mode-01 set on the 11-bit engine so the CAN-era car
still gets something honest, exactly like the Mercedes profile.
"""

from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327Uds29Client, Elm327UdsClient

# FCA one-byte addresses for the two live-data modules (research §2a/§4).
ENGINE = 0x10
TRANS = 0x18
# The engine on the standard OBD block for the CAN-era generic Mode-01 fallback.
ENGINE_OBD = 0x7E0


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


# ---- the danardi Giulia/Stelvio decoders (research §4, VERIFIED single-source) --
# Each formula is exactly as captured; bytes A,B are the payload after 62 <did>.

def _soot(b: bytes) -> Optional[str]:
    v = _u16(b)  # 22 18AA = (A*256)+B grams
    return _fmt(v, 0) if v is not None and 0 <= v <= 300 else None


def _regen(b: bytes) -> Optional[str]:
    v = _u16(b)  # 22 380B = (A*256+B)*100/65535 percent
    if v is None:
        return None
    pct = v * 100 / 65535
    return _fmt(pct, 1) if 0 <= pct <= 100 else None


def _oil_temp(b: bytes) -> Optional[str]:
    v = _u8(b, 1)  # 22 1302 = B degrees C
    return _fmt(v, 0) if v is not None and -40 <= v <= 215 else None


def _oil_level(b: bytes) -> Optional[str]:
    v = _u16(b)  # 22 3A41 = (A*256+B)/1000 litres
    if v is None:
        return None
    litres = v / 1000
    return _fmt(litres, 2) if 0 <= litres <= 12 else None


def _coolant(b: bytes) -> Optional[str]:
    v = _u16(b)  # 22 1003 = ((A*256+B)*0.02)-40 degrees C
    if v is None:
        return None
    c = v * 0.02 - 40
    return _fmt(c, 0) if -40 <= c <= 215 else None


def _boost(b: bytes) -> Optional[str]:
    v = _u16(b)  # 22 195A = ((A*256+B)-32768)/1000-1 bar
    if v is None:
        return None
    bar = (v - 32768) / 1000 - 1
    return _fmt(bar, 2) if -1.5 <= bar <= 4 else None


def _gear(b: bytes) -> Optional[str]:
    v = _u8(b, 0)  # 22 192D = A
    return str(v) if v is not None and 0 <= v <= 12 else None


def _battery(b: bytes) -> Optional[str]:
    v = _u16(b)  # 22 1955 = (A*256+B)*(0.5/1000) volts
    if v is None:
        return None
    volts = v * 0.5 / 1000
    return _fmt(volts, 2) if 0 <= volts <= 32 else None


def _trans_temp(b: bytes) -> Optional[str]:
    v = _u8(b, 0)  # 22 04FE = A-40 degrees C (TCM 0x18)
    if v is None:
        return None
    temp = v - 40
    return _fmt(temp, 0) if -40 <= temp <= 215 else None


# (module_addr, did, name, unit, decode) -- polled ONE at a time (DA10F1 is slow).
GIULIA_DIDS: List[Tuple[int, int, str, str, Callable[[bytes], Optional[str]]]] = [
    (ENGINE, 0x18AA, "DPF soot mass", "g", _soot),
    (ENGINE, 0x380B, "DPF regeneration in progress", "%", _regen),
    (ENGINE, 0x1302, "Engine oil temperature", "°C", _oil_temp),
    (ENGINE, 0x3A41, "Engine oil level", "L", _oil_level),
    (ENGINE, 0x1003, "Coolant temperature", "°C", _coolant),
    (ENGINE, 0x195A, "Boost / turbo pressure", "bar", _boost),
    (ENGINE, 0x192D, "Current gear", "", _gear),
    (ENGINE, 0x1955, "Battery voltage", "V", _battery),
    (TRANS, 0x04FE, "Transmission oil temperature", "°C", _trans_temp),
]

# Curated generic Mode-01 set for the CAN-era fallback (standard ISO 15031 PIDs
# on the 11-bit engine ECU), mirroring the Mercedes profile's honest baseline.
# (pid, name, unit, decode) on the bytes after 41 <pid>.
OBD_PIDS: List[Tuple[int, str, str, Callable[[bytes], Optional[str]]]] = [
    (0x05, "Coolant temperature", "°C", _byte(1, -40, lo=-40, hi=215)),
    (0x5C, "Engine oil temperature", "°C", _byte(1, -40, lo=-40, hi=215)),
    (0x42, "Battery voltage (control module)", "V", _u16_scaled(1 / 1000, dp=2, lo=0, hi=32)),
    (0x2F, "Fuel level", "%", _byte(100 / 255, dp=0, lo=0, hi=100)),
]

# The danardi values are a single on-car capture -- this caveat rides with them
# so the scaling is checked against the car before absolute figures are trusted.
_DANARDI_CAVEAT = {
    "name": "Note",
    "value": ("These Giulia/Stelvio values are from a single on-car MY2016 2.2-diesel capture — "
              "verify the scaling against the car before trusting absolute figures."),
    "unit": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _restore_protocol(session, discovered) -> None:
    """Hand the ELM back on the discovered protocol + 11-bit functional header
    (the session is cached and reused, so a stale ATSP7 must not be left behind)."""
    try:
        session.at(f"ATSP{discovered:X}" if discovered is not None else "ATSP0")
        session.at("ATSH7DF")
    except Exception:
        pass


def _read_did(client, addr: int, did: int) -> Optional[bytes]:
    """One 29-bit RDBI read -> payload after ``62 <did>``, or None."""
    try:
        payload = client.request(addr, bytes([0x22, did >> 8, did & 0xFF]), timeout=3.0)
    except (ObdError, TransportError):
        return None
    if len(payload) < 4 or payload[0] != 0x62:
        return None
    return payload[3:]


def _read_pid(client, header: int, pid: int) -> Optional[bytes]:
    """One generic Mode-01 read -> payload after ``41 <pid>``, or None."""
    try:
        payload = client.request(header, bytes([0x01, pid]), timeout=2.0)
    except (ObdError, TransportError):
        return None
    if len(payload) < 3 or payload[0] != 0x41 or payload[1] != pid:
        return None
    return payload[2:]


def live(adapter: Optional[dict], mode: str = "real") -> dict:
    """One read-only pass over the FCA live extras. Skips non-answers."""
    if mode == "sim":
        return {
            "ok": True, "simulated": True,
            "readings": [
                {"name": "DPF soot mass", "value": "18", "unit": "g"},
                {"name": "DPF regeneration in progress", "value": "0.0", "unit": "%"},
                {"name": "Coolant temperature", "value": "88", "unit": "°C"},
                {"name": "Engine oil temperature", "value": "95", "unit": "°C"},
                {"name": "Transmission oil temperature", "value": "72", "unit": "°C"},
                {"name": "Battery voltage", "value": "14.20", "unit": "V"},
                dict(_DANARDI_CAVEAT),
            ],
            "message": "SIMULATED -- no real car connected.",
            "timestamp": _now(),
        }

    def run(session, entry):
        session.ensure_protocol()
        discovered = session.protocol_num
        if discovered not in (6, None):
            return {
                "ok": False, "simulated": False, "readings": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k CAN Fiat / Alfa live values use."
                ),
                "timestamp": _now(),
            }

        readings = []
        # TIER 1: the UDS-era danardi DIDs on 29-bit CAN.
        session.at("ATSP7")
        session.at("ATCAF1")
        try:
            client = Elm327Uds29Client(session)
            client.begin()
            try:
                for addr, did, name, unit, decode in GIULIA_DIDS:
                    body = _read_did(client, addr, did)
                    if body is None:
                        continue
                    try:
                        value = decode(body)
                    except Exception:
                        continue
                    if value is not None:
                        readings.append({"name": name, "value": value, "unit": unit})
                if readings:
                    readings.append(dict(_DANARDI_CAVEAT))
            finally:
                client.restore()

            # TIER 2: nothing on 29-bit -> curated generic Mode-01 on the 11-bit engine.
            if not readings:
                session.at("ATSP6")
                obd_client = Elm327UdsClient(session)
                obd_client.begin()
                try:
                    for pid, name, unit, decode in OBD_PIDS:
                        body = _read_pid(obd_client, ENGINE_OBD, pid)
                        if body is None:
                            continue
                        try:
                            value = decode(body)
                        except Exception:
                            continue
                        if value is not None:
                            readings.append({"name": name, "value": value, "unit": unit})
                finally:
                    obd_client.restore()
        finally:
            _restore_protocol(session, discovered)

        return {
            "ok": True, "simulated": False, "readings": readings,
            "message": (
                f"{len(readings)} Fiat / Alfa value(s) answered. On a UDS-era Giulia/Stelvio these "
                "are the danardi-captured DPF/engine/gearbox values (verify scaling against the "
                "car); a CAN-era Fiat falls back to a curated generic Mode-01 set. Values this car "
                "doesn't support are skipped. Read-only pass; nothing was changed on the car."
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
