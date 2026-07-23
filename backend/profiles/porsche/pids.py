"""Porsche live "extras" over a plain ELM327
(docs/private/porsche-research-modules.md §5, §10b).

Two tiers, kept separate by reachability, exactly like the JLR profile:

TIER 1 -- the one Porsche-specific value with a published wire format: engine oil
  temperature via KWP2000 service 0x21, local identifier 0x32 (``21 32`` -> reply
  ``61 32 ...``), read on the DME (0x7E0) over 11-bit CAN. This is a REAL on-car
  capture, but from a SINGLE source -- one Rennlist "CAN PIDs for 997.2" thread --
  so the candidate scaling (A - 60) must be re-checked against the car before the
  absolute figure is trusted (a caveat rides on the reading). The thread's other
  0x21 values (21 02 oil pressure, 21 1A oil-pressure deviation) have UNKNOWN
  scaling and are deliberately NOT read -- no garbage numbers.

  The measuring values a Porsche dealer most wants -- camshaft deviation (the
  M96/M97 IMS/chain-wear crown jewel), over-rev history, electronic oil level, PDK
  clutch temps -- are read by Durametric/PIWIS but nobody published their DIDs
  (research §5a). They are capture targets for a real car, NOT invented here.

TIER 2 -- if the KWP value doesn't answer (a UDS-era 981/991/970, a TP2.0 Cayenne
  955/957, or a DoIP MY17+ car), fall back to a curated generic Mode-01 set on the
  11-bit engine so the car still gets something honest, exactly like the JLR/
  Mercedes/Fiat profiles. All generic PIDs are VERIFIED ISO 15031 scalings.

Each request is polled ONE at a time with a generous timeout -- BLE dongles are
slow. Values live on the engine DME (0x7E0). The K-line 996/986 cars can't be
reached by this CAN client at all -- their generic values come from the plain OBD
path, not here.
"""

from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

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


# ---- TIER 1: the single Porsche KWP 0x21 value with a published wire format
# (research §5a-i / §10b, UNVERIFIED-single-source scaling). (module_addr, lid,
# name, unit, decode) -- lid is the KWP local identifier after service 0x21.
# ONLY oil temperature: 21 02 / 21 1A have UNKNOWN scaling and are omitted.
KWP21_LIDS: List[Tuple[int, int, str, str, Callable[[bytes], Optional[str]]]] = [
    (ENGINE, 0x32, "Engine oil temperature (KWP 21 32)", "°C", _byte(1, -60, dp=0, lo=-40, hi=215)),
]

# ---- TIER 2: curated generic Mode-01 set for the UDS-era / TP2.0 / DoIP fallback
# (standard ISO 15031 PIDs on the 11-bit engine), mirroring JLR/Mercedes/Fiat.
OBD_PIDS: List[Tuple[int, str, str, Callable[[bytes], Optional[str]]]] = [
    (0x05, "Coolant temperature", "°C", _byte(1, -40, lo=-40, hi=215)),
    (0x5C, "Engine oil temperature", "°C", _byte(1, -40, lo=-40, hi=215)),
    (0x0C, "Engine RPM", "rpm", _u16_scaled(1 / 4, dp=0, lo=0, hi=9000)),
    (0x04, "Calculated engine load", "%", _byte(100 / 255, dp=0, lo=0, hi=100)),
    (0x0F, "Intake air temperature", "°C", _byte(1, -40, lo=-40, hi=215)),
    (0x42, "Battery voltage (control module)", "V", _u16_scaled(1 / 1000, dp=2, lo=0, hi=32)),
]

# The KWP 0x21 value is a single on-car capture -- this caveat rides with it so the
# scaling is checked against the car before the figure is trusted.
_KWP21_CAVEAT = {
    "name": "Note",
    "value": ("The 21 32 oil temperature is from a single Rennlist 997.2 CAN capture "
              "(candidate scaling A-60) — verify against the car before trusting the absolute "
              "figure. Camshaft deviation, over-rev and oil level have no public DID and are "
              "not shown."),
    "unit": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_kwp_lid(client, addr: int, lid: int) -> Optional[bytes]:
    """One 11-bit KWP 0x21 read -> payload after ``61 <lid>``, or None."""
    try:
        payload = client.request(addr, bytes([0x21, lid]), timeout=3.0)
    except (ObdError, TransportError):
        return None
    if len(payload) < 3 or payload[0] != 0x61 or payload[1] != lid:
        return None
    return payload[2:]


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
    """One read-only pass over the Porsche live extras. Skips non-answers."""
    if mode == "sim":
        return {
            "ok": True, "simulated": True,
            "readings": [
                {"name": "Engine oil temperature (KWP 21 32)", "value": "92", "unit": "°C"},
                {"name": "Coolant temperature", "value": "88", "unit": "°C"},
                {"name": "Engine RPM", "value": "820", "unit": "rpm"},
                dict(_KWP21_CAVEAT),
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
                    "11-bit/500k CAN Porsche live values use. A 996/986 on K-line gives generic "
                    "values over the plain OBD path, not these extras."
                ),
                "timestamp": _now(),
            }

        readings = []
        client = Elm327UdsClient(session)
        client.begin()
        try:
            # TIER 1: the single published Porsche KWP 0x21 value (997/987 DME).
            for addr, lid, name, unit, decode in KWP21_LIDS:
                body = _read_kwp_lid(client, addr, lid)
                if body is None:
                    continue
                try:
                    value = decode(body)
                except Exception:
                    continue
                if value is not None:
                    readings.append({"name": name, "value": value, "unit": unit})
            if readings:
                readings.append(dict(_KWP21_CAVEAT))

            # TIER 2: nothing Porsche-specific -> curated generic Mode-01 set
            # (a UDS-era, TP2.0 or DoIP car still gets something honest).
            if not readings:
                for pid, name, unit, decode in OBD_PIDS:
                    body = _read_pid(client, ENGINE, pid)
                    if body is None:
                        continue
                    try:
                        value = decode(body)
                    except Exception:
                        continue
                    if value is not None:
                        readings.append({"name": name, "value": value, "unit": unit})
        finally:
            client.restore()

        return {
            "ok": True, "simulated": False, "readings": readings,
            "message": (
                f"{len(readings)} Porsche value(s) answered. On a 997/987 the first is the "
                "single published KWP 21 32 oil temperature (single-source capture -- verify "
                "scaling against the car); a UDS-era, TP2.0 or DoIP car falls back to a curated "
                "generic Mode-01 set. Values this car doesn't support are skipped. Read-only "
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
