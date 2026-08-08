"""Vauxhall / Opel live values over a plain ELM327
(docs/private/vauxhall-astra-k-research.md §4).

Two tiers, both read-only:

(a) The GM-native transmission DIDs on the TCM (0x7E1), from the OBDb
    VauxhallOpel signalset (CC-BY-SA-4.0 -- see the NOTICE file beside this
    one). ReadDataByParameterIdentifier is 0x22 in both GMW3110 and UDS, so
    this tier needs none of the dialect laddering scan.py does:

        22 04FE   transmission fluid temperature
        22 5503   current gear
        22 5504   target (commanded) gear

    Two of those three exist here for one reason. **Current gear against
    target gear is the pair that shows a shift flare.** A flare is the TCM
    having commanded the next gear while the box has not yet delivered it:
    target steps to 4, current stays at 3, and engine rpm climbs into the
    gap. Watching both while the flare happens separates "commanding the
    shift too early" (a calibration problem) from "the clutch is slow to
    fill" (adapts or hydraulics) -- which is the difference between a
    software update and a gearbox job.

    Transmission fluid temperature earns its place twice: it is the health
    reading a gearbox complaint wants anyway, and it is the precondition the
    Service Fast Learn Adapts routine refuses to start outside. Being able to
    watch it over a cheap dongle turns "guess when it's warm enough" into a
    measurement, so the reading carries the window with it.

(b) A curated generic Mode-01 set on the engine (0x7E0): rpm, speed, coolant,
    engine oil temperature, module voltage, ambient, load, throttle. Standard
    ISO 15031 PIDs carrying their own scaling -- the honest baseline that
    answers on any reachable car, gathered here next to the gearbox values so
    a flare can be logged against rpm and road speed in one place.

GENUINE GAP (research §4): nothing public exposes the TCM's clutch adaptive
cells, line pressure, individual clutch slip or TCC slip. GM keeps those in the
GDS2 parameter list, not in any open database. No DID is guessed for them --
what a dealer reads on GDS2 to judge adapt health is simply not available at
this port, and the profile says so rather than showing a plausible number.
"""

from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

ENGINE = 0x7E0
TRANS = 0x7E1

# Fast Learn fluid-temperature window. Sources disagree and the disagreement is
# real, not a typo (research §5): the Astra K brief cites ~64-76 C for Gen 2
# units, GM/aftermarket material for the wider 6T40 family cites 75-85 C, and
# GM's own note is that the window comes from whatever calibration is in the
# TCM. So the reading reports against the union of both and tells the operator
# to take the authoritative window from the tool's own prompt for the VIN.
FAST_LEARN_LOW_C = 64
FAST_LEARN_HIGH_C = 85
FAST_LEARN_NOTE = (
    "Service Fast Learn will not start outside the TCM's fluid-temperature "
    "window. Published windows disagree -- roughly 64-76 C for Gen 2 units, "
    "75-85 C for the wider 6T40 family -- because the real limit lives in the "
    "calibration in this car's TCM. Take the exact window from the scan tool's "
    "own on-screen prompt for the VIN; use this reading to know when you are "
    "near it."
)

# OBDb VauxhallOpel gear map, shared by current and target gear.
GEAR_MAP = {
    0: "Neutral", 1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th",
    6: "6th", 7: "7th", 8: "8th", 9: "9th", 10: "10th",
    11: "Reverse", 13: "Park",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


# Curated generic Mode-01 set on the engine: (pid, name, unit, decode), where
# decode maps the bytes AFTER ``41 <pid>`` to a display value or None to skip.
OBD_PIDS: List[Tuple[int, str, str, Callable[[bytes], Optional[str]]]] = [
    (0x0C, "Engine speed", "rpm", _u16_scaled(1 / 4, dp=0, lo=0, hi=16000)),
    (0x0D, "Vehicle speed", "km/h", _byte(1, dp=0, lo=0, hi=255)),
    (0x04, "Calculated engine load", "%", _byte(100 / 255, dp=0, lo=0, hi=100)),
    (0x11, "Throttle position", "%", _byte(100 / 255, dp=0, lo=0, hi=100)),
    (0x05, "Coolant temperature", "°C", _byte(1, -40, lo=-40, hi=215)),
    (0x5C, "Engine oil temperature", "°C", _byte(1, -40, lo=-40, hi=215)),
    (0x42, "Battery voltage (control module)", "V", _u16_scaled(1 / 1000, dp=2, lo=0, hi=32)),
    (0x46, "Ambient air temperature", "°C", _byte(1, -40, lo=-40, hi=100)),
]


def _read_pid(client: Elm327UdsClient, header: int, pid: int) -> Optional[bytes]:
    """One generic Mode-01 read -> payload after ``41 <pid>``, or None."""
    try:
        payload = client.request(header, bytes([0x01, pid]), timeout=2.0)
    except (ObdError, TransportError):
        return None
    if len(payload) < 3 or payload[0] != 0x41 or payload[1] != pid:
        return None
    return payload[2:]


def _read_did(client: Elm327UdsClient, header: int, did: int) -> Optional[bytes]:
    """One 0x22 read -> payload after the echoed DID, or None.

    Service 0x22 is ReadDataByParameterIdentifier in GMW3110 and
    ReadDataByIdentifier in UDS -- same id, same request shape, same positive
    response id -- so this works whichever dialect the module speaks.
    """
    try:
        payload = client.request(header, bytes([0x22, did >> 8, did & 0xFF]), timeout=2.0)
    except (ObdError, TransportError):
        return None
    if len(payload) < 4 or payload[0] != 0x62:
        return None
    if (payload[1] << 8 | payload[2]) != did:
        return None
    return payload[3:]


def _gear_name(body: bytes) -> Optional[str]:
    """OBDb decode: low nibble of the first payload byte, mapped to a gear."""
    v = _u8(body)
    if v is None:
        return None
    return GEAR_MAP.get(v & 0x0F)


def _fluid_temp_c(body: bytes) -> Optional[int]:
    """OBDb gives one byte as F = A*1.8 - 40, which is exactly C = A - 40 --
    the classic single-byte temperature offset. The algebra agreeing with the
    standard form is a useful independent check that the scaling is right."""
    v = _u8(body)
    if v is None:
        return None
    celsius = v - 40
    return celsius if -40 <= celsius <= 200 else None


def _read_transmission(client: Elm327UdsClient) -> List[dict]:
    """The three GM-native TCM values plus the two derived readings that make
    them useful: the Fast Learn window verdict and the shift-flare indicator."""
    readings: List[dict] = []

    body = _read_did(client, TRANS, 0x04FE)
    temp = _fluid_temp_c(body) if body else None
    if temp is not None:
        readings.append({
            "name": "Transmission fluid temperature", "value": str(temp), "unit": "°C",
            "description": "GM TCM DID 04FE (OBDb VauxhallOpel, CC-BY-SA-4.0). "
                           "One byte, C = A - 40.",
        })
        if temp < FAST_LEARN_LOW_C:
            verdict = f"Too cold -- {FAST_LEARN_LOW_C - temp} °C below the lowest published window."
        elif temp > FAST_LEARN_HIGH_C:
            verdict = f"Too hot -- {temp - FAST_LEARN_HIGH_C} °C above the highest published window."
        else:
            verdict = f"In range of the published windows ({FAST_LEARN_LOW_C}-{FAST_LEARN_HIGH_C} °C)."
        readings.append({
            "name": "Fast Learn temperature window", "value": verdict, "unit": "",
            "description": FAST_LEARN_NOTE,
        })

    current_body = _read_did(client, TRANS, 0x5503)
    target_body = _read_did(client, TRANS, 0x5504)
    current = _gear_name(current_body) if current_body else None
    target = _gear_name(target_body) if target_body else None

    if current is not None:
        readings.append({
            "name": "Current gear", "value": current, "unit": "",
            "description": "GM TCM DID 5503 (OBDb VauxhallOpel, CC-BY-SA-4.0).",
        })
    if target is not None:
        readings.append({
            "name": "Target (commanded) gear", "value": target, "unit": "",
            "description": "GM TCM DID 5504 (OBDb VauxhallOpel, CC-BY-SA-4.0). "
                           "The gear the TCM has asked for.",
        })
    if current is not None and target is not None:
        if current == target:
            state = f"Settled in {current}"
            note = ("Current and commanded gear agree -- no shift in progress at "
                    "this instant.")
        else:
            state = f"Shifting {current} → {target}"
            note = ("The TCM has commanded a gear the box has not delivered yet. "
                    "A single reading here is normal mid-shift. It is only a flare "
                    "if this gap PERSISTS while engine rpm climbs -- so read this "
                    "repeatedly through the shift that flares (3rd to 4th on the "
                    "reported complaint) and watch rpm alongside it.")
        readings.append({
            "name": "Shift state", "value": state, "unit": "", "description": note,
        })

    return readings


# --------------------------------------------------------------------------- #
# The Live Data dashboard's fast interface (contract in backend/obd/livefeed.py).
#
# Worth implementing for this brand specifically. The flare being chased happens
# in the second or so of a single 3-4 upshift: with only the floor interface the
# dashboard re-reads the profile's whole table every tick, and over a Bluetooth
# dongle that is a gauge updating every several seconds -- far too slow to catch
# it. With this, the dashboard reads only the gearbox DIDs on screen, so current
# gear, commanded gear and rpm move together while the shift is happening.
#
# Only the gearbox values are listed. The generic engine PIDs (rpm, speed,
# coolant) already reach the dashboard through the standard OBD-II layer, and
# duplicating them here would put the same value on screen twice.
# --------------------------------------------------------------------------- #
CHANNELS = [
    {"key": "trans_fluid_temp", "label": "Transmission fluid temperature",
     "unit": "°C", "group": "Temperatures"},
    {"key": "gear_current", "label": "Current gear", "unit": "", "group": "Transmission"},
    {"key": "gear_target", "label": "Target (commanded) gear", "unit": "",
     "group": "Transmission"},
    {"key": "shift_state", "label": "Shift state", "unit": "", "group": "Transmission"},
]

_CHANNEL_KEYS = [c["key"] for c in CHANNELS]


def read_channels(adapter: Optional[dict] = None, mode: str = "real",
                  keys: Optional[List[str]] = None) -> List[dict]:
    """Read only the picked gearbox channels. [{"key", "value", "unit"}]."""
    wanted = [k for k in (keys or _CHANNEL_KEYS) if k in _CHANNEL_KEYS]
    if not wanted:
        return []

    if mode == "sim":
        sim = {"trans_fluid_temp": "71", "gear_current": "3rd",
               "gear_target": "4th", "shift_state": "Shifting 3rd → 4th"}
        units = {c["key"]: c["unit"] for c in CHANNELS}
        return [{"key": k, "value": sim[k], "unit": units[k]} for k in wanted]

    # shift_state is derived, so asking for it pulls in both gear reads.
    need_current = "gear_current" in wanted or "shift_state" in wanted
    need_target = "gear_target" in wanted or "shift_state" in wanted
    need_temp = "trans_fluid_temp" in wanted

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return []
        client = Elm327UdsClient(session, {TRANS: TRANS + 8})
        client.begin()
        try:
            temp = current = target = None
            if need_temp:
                body = _read_did(client, TRANS, 0x04FE)
                temp = _fluid_temp_c(body) if body else None
            if need_current:
                body = _read_did(client, TRANS, 0x5503)
                current = _gear_name(body) if body else None
            if need_target:
                body = _read_did(client, TRANS, 0x5504)
                target = _gear_name(body) if body else None
        finally:
            client.restore()

        values = {
            "trans_fluid_temp": str(temp) if temp is not None else None,
            "gear_current": current,
            "gear_target": target,
            "shift_state": (
                None if current is None or target is None
                else (f"Settled in {current}" if current == target
                      else f"Shifting {current} → {target}")
            ),
        }
        units = {c["key"]: c["unit"] for c in CHANNELS}
        return [{"key": k, "value": values[k], "unit": units[k]}
                for k in wanted if values.get(k) is not None]

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError:
        return []


def live(adapter: Optional[dict], mode: str = "real") -> dict:
    """One read-only pass over the Vauxhall live values. Skips non-answers."""
    if mode == "sim":
        return {
            "ok": True, "simulated": True,
            "readings": [
                {"name": "Transmission fluid temperature", "value": "71", "unit": "°C",
                 "description": "SIMULATED."},
                {"name": "Fast Learn temperature window",
                 "value": "In range of the published windows (64-85 °C).", "unit": "",
                 "description": FAST_LEARN_NOTE},
                {"name": "Current gear", "value": "3rd", "unit": "", "description": "SIMULATED."},
                {"name": "Target (commanded) gear", "value": "4th", "unit": "",
                 "description": "SIMULATED."},
                {"name": "Shift state", "value": "Shifting 3rd → 4th", "unit": "",
                 "description": "SIMULATED -- this is what the reported flare looks like "
                                "if it persists while rpm climbs."},
                {"name": "Engine speed", "value": "2740", "unit": "rpm"},
                {"name": "Vehicle speed", "value": "48", "unit": "km/h"},
                {"name": "Coolant temperature", "value": "89", "unit": "°C"},
                {"name": "Battery voltage (control module)", "value": "14.20", "unit": "V"},
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
                    "11-bit/500k High Speed GMLAN these values live on."
                ),
                "timestamp": _now(),
            }

        client = Elm327UdsClient(session, {TRANS: TRANS + 8})
        client.begin()
        readings: List[dict] = []
        try:
            readings.extend(_read_transmission(client))
            gearbox_count = len(readings)
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

        if gearbox_count:
            headline = (f"{gearbox_count} gearbox value(s) answered on the TCM (0x7E1). ")
        else:
            headline = (
                "The TCM (0x7E1) did not answer the GM transmission DIDs -- either this "
                "car is a manual, or the DIDs differ on this model/calibration. The "
                "generic engine values below still apply. "
            )
        return {
            "ok": True, "simulated": False, "readings": readings,
            "message": (
                headline
                + f"{len(readings)} value(s) total. Clutch adapt cells, line pressure and "
                "clutch slip are GDS2-only -- no public DID exposes them, so nothing is "
                "shown for them rather than a guess. Read-only pass; nothing was changed "
                "on the car."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "readings": [],
                "message": str(exc), "timestamp": _now()}
