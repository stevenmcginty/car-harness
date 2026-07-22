"""Mazda broadcast-CAN snapshot over a generic ELM327 -- read-only sniffing.

An ELM327 can passively monitor the bus (ATMA) with a receive filter (ATCRA),
so the broadcast frames the car's modules are already sending can be captured
and decoded WITHOUT asking any ECU anything. On the MX-5 ND the instrument
cluster bridges MS-CAN traffic onto HS-CAN, so a plain HS-only dongle on the
OBD port sees body data (doors, fuel, ignition) too.

Decodes are the ND-community-verified ones (multiple independent sources):
  - majbthrd/MazdaCANbus skyactiv.kcd (bench, Mazda6/Mazda3 cluster)
  - comma.ai opendbc mazda_2017.dbc (CX-5 2017)
  - berumiya/CAN_DBC_6thGenMazda (MX-5 ND1/ND2, best ND source)
  - timurrrr RaceChrono profile (2019 ND2), S. Schmoll's ND sheet, mx5things
Where sources conflicted the ND-verified formula won. A frame that doesn't
appear in its window (module not fitted / different year) is skipped silently.

Monitor recipe per CAN id: ATH1 once, then ATCRA<id> + ATMA, collect for a
short window (the transports return whatever arrived when the timeout lapses
-- there is no '>' during monitoring), then send a single SPACE with NO
trailing CR: any character stops the monitor and is discarded, but a bare CR
would RE-RUN the last command (ATMA again). Restore ATCRA + ATH0 after.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.obd import service as obd_service

# (can_id, window_seconds) -- window sized to the frame's broadcast rate.
SNAPSHOT_IDS = [
    (0x202, 0.35),  # PCM: rpm / speed / accelerator (~50/s)
    (0x420, 0.45),  # PCM: coolant / ambient (~10/s)
    (0x215, 0.35),  # DSC: wheel speeds (~30/s)
    (0x078, 0.35),  # DSC: brake pressure
    (0x086, 0.35),  # EPS: steering angle
    (0x165, 0.35),  # PCM: manual gear position
    (0x131, 0.45),  # AT shift position (automatics)
    (0x0FD, 0.45),  # PCM: manifold pressure
    (0x4FA, 0.45),  # PCM: intake air temperature
    (0x09E, 0.45),  # IC: fuel level (litres)
    (0x09F, 0.45),  # IC: handbrake
    (0x050, 0.45),  # SSU: ignition state
    (0x43E, 0.45),  # BCM: doors / boot
    (0x3D1, 1.20),  # CMU: odometer (~1/s)
]

_MT_GEARS = {19: "N or 1st", 11: "2nd", 7: "3rd", 6: "4th", 5: "5th", 3: "6th"}
_AT_GEARS = {1: "P", 2: "R", 3: "N", 4: "D"}
_IGNITION = {0: "OFF", 1: "ACC", 2: "ON", 3: "START"}

KM_TO_MI = 0.621371


def _u16(d, i):
    return (d[i] << 8) | d[i + 1]


def decode_frame(can_id: int, d: bytes) -> List[dict]:
    """One captured frame -> readings. Out-of-range values are dropped rather
    than shown wrong (a mis-parsed frame must not invent data)."""
    out = []

    def add(name, value, unit=""):
        out.append({"name": name, "value": value, "unit": unit})

    if can_id == 0x202 and len(d) >= 6:
        rpm = _u16(d, 0) / 4.0
        if 0 <= rpm <= 9000:
            add("Engine RPM", f"{rpm:.0f}", "rpm")
        kmh = _u16(d, 2) / 100.0
        if 0 <= kmh <= 300:
            add("Speed", f"{kmh * KM_TO_MI:.1f}", "mph")
        accel = _u16(d, 4) / 640.0
        if 0 <= accel <= 101:
            add("Accelerator pedal", f"{accel:.1f}", "%")
    elif can_id == 0x420 and len(d) >= 8:
        add("Coolant temperature", f"{d[0] - 40}", "°C")
        ambient = _u16(d, 6) * 0.25 - 3200
        if -45 <= ambient <= 70:
            add("Outside temperature", f"{ambient:.1f}", "°C")
    elif can_id == 0x215 and len(d) >= 8:
        speeds = [(_u16(d, i) - 10000) / 100.0 for i in (0, 2, 4, 6)]
        if all(0 <= s <= 300 for s in speeds):
            add("Wheel speeds", " / ".join(f"{s:.1f}" for s in speeds), "km/h")
    elif can_id == 0x078 and len(d) >= 6:
        bar = _u16(d, 4) * 0.001 - 40
        if 0 <= bar <= 250:
            add("Brake pressure", f"{bar:.1f}", "bar")
    elif can_id == 0x086 and len(d) >= 2:
        angle = (16000 - _u16(d, 0)) * 0.1
        if -720 <= angle <= 720:
            add("Steering angle", f"{angle:+.1f}", "° (+ right)")
    elif can_id == 0x165 and len(d) >= 7:
        gear = _MT_GEARS.get(d[6])
        if gear:
            add("Gear (manual)", gear)
    elif can_id == 0x131 and len(d) >= 2:
        gear = _AT_GEARS.get(d[1])
        if gear:
            add("Gear selector (auto)", gear)
    elif can_id == 0x0FD and len(d) >= 7:
        kpa = d[6] + 2
        if 10 <= kpa <= 260:
            add("Manifold pressure", f"{kpa}", "kPa")
    elif can_id == 0x4FA and len(d) >= 5:
        iat = d[4] - 40
        if -40 <= iat <= 150:
            add("Intake air temperature", f"{iat}", "°C")
    elif can_id == 0x09E and len(d) >= 6:
        litres = d[5] * 0.2
        if 0 <= litres <= 60:
            add("Fuel in tank", f"{litres:.1f}", "L")
    elif can_id == 0x09F and len(d) >= 1:
        add("Handbrake", "applied" if d[0] & 0x10 else "released")
    elif can_id == 0x050 and len(d) >= 1:
        add("Ignition", _IGNITION.get(d[0] & 0x03, f"state {d[0] & 0x03}"))
    elif can_id == 0x43E and len(d) >= 6:
        open_bits = []
        if d[4] & 0x10:
            open_bits.append("driver door")
        if d[4] & 0x20:
            open_bits.append("passenger door")
        if d[5] & 0x80:
            open_bits.append("boot")
        add("Doors", ", ".join(open_bits) + " open" if open_bits else "all closed")
    elif can_id == 0x3D1 and len(d) >= 7:
        km = (d[4] << 14) | (d[5] << 6) | (d[6] >> 2)
        if 0 < km < 2_000_000:
            add("Odometer", f"{km * KM_TO_MI:,.0f} mi ({km:,} km)")
    return out


def parse_monitor_text(text: str, can_id: int) -> List[bytes]:
    """Raw ATMA capture -> data payloads for ``can_id``.

    With ATH1 + ATS0 a monitored 11-bit frame prints as 3 hex id digits then
    the data bytes, no DLC. Noise ('BUFFER FULL', 'STOPPED', prompt, partial
    first lines) is skipped; a line must match the id and hold whole bytes.
    """
    want = f"{can_id:03X}"
    frames = []
    for line in text.upper().replace(">", "").split():
        line = line.strip()
        if len(line) < 5 or not line.startswith(want):
            continue
        payload = line[3:]
        if len(payload) % 2 or len(payload) > 16:
            continue
        try:
            frames.append(bytes.fromhex(payload))
        except ValueError:
            continue
    return frames


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def snapshot(adapter: Optional[dict], mode: str = "real") -> dict:
    """Passively capture + decode one round of broadcast frames. Read-only."""
    if mode == "sim":
        return {
            "ok": True, "simulated": True,
            "readings": [
                {"name": "Engine RPM", "value": "824", "unit": "rpm"},
                {"name": "Speed", "value": "0.0", "unit": "mph"},
                {"name": "Coolant temperature", "value": "88", "unit": "°C"},
                {"name": "Fuel in tank", "value": "31.4", "unit": "L"},
                {"name": "Ignition", "value": "ON", "unit": ""},
                {"name": "Doors", "value": "all closed", "unit": ""},
                {"name": "Odometer", "value": "24,140 mi (38,850 km)", "unit": ""},
            ],
            "message": "SIMULATED -- no real car connected.",
            "timestamp": _now(),
        }

    def run(session, entry):
        # No ensure_protocol() needed -- monitoring listens to whatever is on
        # the bus. But the protocol must be CAN if it was already detected.
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "readings": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k HS-CAN a Mazda broadcasts on."
                ),
                "timestamp": _now(),
            }
        readings = []
        seen = set()
        session.at("ATH1")  # monitored frames must show their CAN ids
        try:
            for can_id, window in SNAPSHOT_IDS:
                session.at(f"ATCRA{can_id:03X}")
                text = session._raw(b"ATMA\r", window)
                # Stop monitoring: one space, NO CR (a bare CR re-runs ATMA).
                text += session._raw(b" ", 4.0)
                frames = parse_monitor_text(text, can_id)
                if frames:
                    for reading in decode_frame(can_id, frames[-1]):
                        if reading["name"] not in seen:
                            seen.add(reading["name"])
                            readings.append(reading)
        finally:
            for cmd in ("ATCRA", "ATH0"):
                try:
                    session.at(cmd)
                except Exception:
                    pass
        return {
            "ok": True, "simulated": False, "readings": readings,
            "message": (
                f"{len(readings)} value(s) decoded from the car's own broadcast frames "
                "(passive listen -- nothing was sent to any module). Frames this "
                "car/year doesn't broadcast are skipped."
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
