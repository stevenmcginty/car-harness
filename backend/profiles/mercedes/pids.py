"""Mercedes-Benz live "extras" over a plain ELM327 (docs/private/mercedes-research.md §4).

Three tiers, all read-only, ordered by how much car they need:

(0) The E-Class mild-hybrid (W/S 213) answers a small published UDS 0x22 DID
    library at the OBD port: chassis telemetry (wheel speeds, steering angle,
    accelerations, brake pressures) on 0x7E2 and hybrid telemetry (HV battery
    SoC, converter voltage, aux battery voltage) on 0x7E5. Every decode in
    ``eclass.py`` was derived and verified against the OBDb Mercedes-Benz-E-Class
    on-car vectors (tests/test_cases 2018 + 2021, 520 checks pass) -- the bundled
    ``obdb_eclass_signalset.json`` is the curated subset. One cheap probe DID
    gates the tier; a non-hybrid pays one short timeout and falls through.

(a) The one VERIFIED Mercedes-specific value a dealer wants that the generic
    dashboard doesn't carry: the 722.9 / 9G automatic-transmission (ATF) oil
    temperature, on the gearbox ECU 0x7E1. Forum captures read it as KWP
    readDataByLocalId `21 30` on the 722.9 era; later 9G cars answer the same
    datum as UDS DID `22 2130`, so we try `21 30` first then `22 21 30`. Scaling
    is first data byte A - 50 = C. The exact offset (-50 vs -40) needs a real-car
    confirm -- a warm gearbox idles ~40-60 C; if the reading is ~10 C high the
    offset is -40. That caveat rides on the reading's ``description``.

(b) A curated generic Mode-01 set (coolant 05, engine oil temp 5C, control-module
    voltage 42, ambient 46, fuel level 2F) read on the engine ECU 0x7E0. These
    are standard ISO 15031 PIDs (VERIFIED) and carry their own scaling -- the
    honest baseline the research says answers on any reachable car. They overlap
    the generic Car-tab dashboard on purpose: this panel gathers the Mercedes
    dealer-check values in one place next to the ATF temp.

DPF soot / regen and AdBlue level are GENUINE GAPS (research §4): no header + hex
PID + scaling is publicly published for Mercedes, so nothing is invented here.
Until a real-car ELM327 capture exists, DPF/SCR health shows through the fault
codes read by scan.py (19 02 / 18 02 on 0x7E0), which are the dealer-relevant
signal anyway.
"""

from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

from . import eclass

# Standard block, response = request + 8, so no id_map is needed.
ENGINE = 0x7E0
TRANS = 0x7E1


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


# Curated generic Mode-01 set (research §4): standard ISO 15031 PIDs on the
# engine ECU. (pid, name, unit, decode) where decode maps the bytes AFTER
# ``41 <pid>`` to a display value (str) or None to skip.
OBD_PIDS: List[Tuple[int, str, str, Callable[[bytes], Optional[str]]]] = [
    (0x05, "Coolant temperature", "°C", _byte(1, -40, lo=-40, hi=215)),
    (0x5C, "Engine oil temperature", "°C", _byte(1, -40, lo=-40, hi=215)),
    (0x42, "Battery voltage (control module)", "V", _u16_scaled(1 / 1000, dp=2, lo=0, hi=32)),
    (0x46, "Ambient air temperature", "°C", _byte(1, -40, lo=-40, hi=100)),
    (0x2F, "Fuel level", "%", _byte(100 / 255, dp=0, lo=0, hi=100)),
]

# The 722.9/9G ATF oil-temp caveat -- rides on the reading so a warm-gearbox
# sanity check on a real car settles the -50 vs -40 offset before it's trusted.
_ATF_CAVEAT = ("Community-verified on the 722.9; scaling is A-50 = C but the "
               "offset (-50 vs -40) needs a real-car confirm -- a warm gearbox "
               "idles ~40-60 C, so a reading ~10 C high means the offset is -40.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_pid(client: Elm327UdsClient, header: int, pid: int) -> Optional[bytes]:
    """One generic Mode-01 read -> payload after ``41 <pid>``, or None."""
    try:
        payload = client.request(header, bytes([0x01, pid]), timeout=2.0)
    except (ObdError, TransportError):
        return None
    if len(payload) < 3 or payload[0] != 0x41 or payload[1] != pid:
        return None
    return payload[2:]


def _read_atf_temp(client: Elm327UdsClient) -> Optional[int]:
    """722.9/9G ATF oil temp on 0x7E1: KWP ``21 30`` first (722.9 era), then UDS
    ``22 2130`` (later 9G). first data byte A - 50 = C (research §4)."""
    for req, echo_len in ((bytes([0x21, 0x30]), 2), (bytes([0x22, 0x21, 0x30]), 3)):
        try:
            reply = client.request(TRANS, req, timeout=2.0)
        except (ObdError, TransportError):
            continue
        positive = req[0] | 0x40  # 0x61 for KWP 21, 0x62 for UDS 22
        if reply and reply[0] == positive and len(reply) > echo_len:
            temp = reply[echo_len] - 50
            if -50 <= temp <= 200:
                return temp
    return None


def live(adapter: Optional[dict], mode: str = "real") -> dict:
    """One read-only pass over the Mercedes live extras. Skips non-answers."""
    if mode == "sim":
        return {
            "ok": True, "simulated": True,
            "readings": [
                *[dict(r) for r in eclass.SIM_READINGS],
                {"name": "Transmission (722.9/9G) ATF oil temp", "value": "62", "unit": "°C",
                 "description": _ATF_CAVEAT},
                {"name": "Coolant temperature", "value": "88", "unit": "°C"},
                {"name": "Engine oil temperature", "value": "95", "unit": "°C"},
                {"name": "Battery voltage (control module)", "value": "14.10", "unit": "V"},
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
                    "11-bit/500k CAN Mercedes live values use."
                ),
                "timestamp": _now(),
            }
        client = Elm327UdsClient(session, {TRANS: TRANS + 8})
        client.begin()
        readings = []
        try:
            # (0) TIER 0: the E-Class mild-hybrid DID set (research §4b). One cheap
            # probe DID gates it; a non-hybrid returns [] and falls straight
            # through to the extras below, paying one short timeout, not six.
            eclass_readings = eclass.read_live(client)
            if eclass_readings:
                return {
                    "ok": True, "simulated": False, "readings": eclass_readings,
                    "message": (
                        f"{len(eclass_readings) - 1} E-Class hybrid value(s) answered over the "
                        "dongle (OBDb Mercedes-Benz-E-Class, CC-BY-SA-4.0; decodes verified "
                        "against the on-car vectors): wheel speeds, steering, accelerations, "
                        "brake pressures, HV battery SoC and voltages. Read-only pass; nothing "
                        "was changed on the car."
                    ),
                    "timestamp": _now(),
                }

            # (a) The one verified Mercedes-specific extra: 722.9/9G ATF temp.
            atf = _read_atf_temp(client)
            if atf is not None:
                readings.append({
                    "name": "Transmission (722.9/9G) ATF oil temp",
                    "value": str(atf), "unit": "°C", "description": _ATF_CAVEAT,
                })

            # (b) Curated generic Mode-01 set on the engine -- decoded honestly.
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
                f"{len(readings)} Mercedes value(s) answered. The E-Class hybrid DID set "
                "didn't answer (non-hybrid, or a 2018+ DoIP car). The ATF temp is the one "
                "Mercedes-specific extra; the rest are generic OBD PIDs. Values this car "
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
