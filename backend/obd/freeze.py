"""Freeze frame (OBD-II mode 02): the live data as it was at the instant a
fault was stored.

This is the most under-used read on a scan tool and the most useful one for
buying stock. A stored P0300 tells you the car misfired. The freeze frame tells
you it misfired at 1,100 rpm, 34 km/h, coolant 91 C, at 18% load -- i.e. pulling
away from cold, not at motorway load. Same code, completely different bill.

Mechanically it is mode 01 with a different service byte: every PID decoder in
pids.py works unchanged, which is why this file is short.

WHAT THE CAR MAY SAY: many ECUs store exactly one frame (frame 00) and some
store none at all once the fault self-heals. An empty result is a real answer,
not a failure, and is reported as one.
"""

from datetime import datetime, timezone

from backend.obd import dtc
from backend.obd.elm327 import ObdError
from backend.obd.pids import GROUP_ORDER, PIDS_FOR_PID, read_supported_pids
from backend.obd.transport import TransportError

# The PID that names the fault the frame was captured for. Two bytes, encoded
# exactly like a mode 03 code.
FREEZE_DTC_PID = 0x02


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _query_frame(session, pid_num: int, frame: int, timeout: float = 4.0):
    """Mode 02 takes a frame number, which Elm327Session.query() has no room
    for, so the command is built here. Returns the per-ECU records."""
    cmd = f"02{pid_num:02X}{frame:02X}"
    reply = session._raw(cmd.encode("ascii") + b"\r", timeout)
    return session._parse(reply, expected=0x42, echo=cmd)


def _payload_of(record: bytes, pid_num: int, frame: int, nbytes: int):
    """Strip '42 <pid> <frame>' -- but a minority of ECUs leave the frame byte
    out, so accept both layouts rather than silently misreading by one byte."""
    if len(record) < 3 or record[1] != pid_num:
        return None
    if record[2] == frame and len(record) - 3 >= nbytes:
        return record[3:]
    if len(record) - 2 >= nbytes:
        return record[2:]
    return None


def read(adapter, mode: str, frame: int = 0) -> dict:
    """The stored freeze frame, decoded with the standard PID table."""
    from backend.obd import service as obd_service  # circular at import time

    if mode == "sim":
        return {
            "ok": True, "simulated": True, "frame": frame,
            "trigger": {"code": "P0301", "description": "Cylinder 1 misfire detected"},
            "readings": [
                {"key": "rpm", "label": "Engine RPM", "value": 1104, "unit": "rpm", "group": "Engine"},
                {"key": "speed", "label": "Vehicle speed", "value": 34, "unit": "km/h", "group": "Engine"},
                {"key": "coolant", "label": "Coolant temp", "value": 91, "unit": "°C", "group": "Temperatures"},
                {"key": "load", "label": "Engine load", "value": 18.4, "unit": "%", "group": "Engine"},
                {"key": "stft1", "label": "Short fuel trim B1", "value": 9.4, "unit": "%", "group": "Fuel & air"},
            ],
            "groups": ["Engine", "Fuel & air", "Temperatures"],
            "message": "SIMULATED — conditions captured when the fault stored.",
            "timestamp": _now(),
        }

    def run(session, entry):
        session.ensure_protocol()
        if entry.get("supported") is None:
            entry["supported"] = read_supported_pids(session)
        supported = entry["supported"] or set()

        trigger = None
        try:
            for record in _query_frame(session, FREEZE_DTC_PID, frame):
                payload = _payload_of(record, FREEZE_DTC_PID, frame, 2)
                if payload and len(payload) >= 2 and (payload[0] or payload[1]):
                    code = dtc.decode_pair(payload[0], payload[1])
                    trigger = {"code": code, "description": dtc.describe(code)}
                    break
        except (ObdError, TransportError):
            pass

        readings = []
        for pid_num in sorted(supported):
            rows = PIDS_FOR_PID.get(pid_num, [])
            if not rows:
                continue
            try:
                records = _query_frame(session, pid_num, frame)
            except (ObdError, TransportError):
                continue
            for record in records:
                matched = False
                for pid in rows:
                    payload = _payload_of(record, pid_num, frame, pid.nbytes)
                    if payload is None:
                        continue
                    value = pid.decode(payload)
                    if value is None:
                        continue
                    matched = True
                    readings.append({
                        "key": pid.key, "label": pid.label,
                        "value": value, "unit": pid.unit, "group": pid.group,
                    })
                if matched:
                    break

        present = {r["group"] for r in readings}
        groups = [g for g in GROUP_ORDER if g in present]

        if readings:
            message = (
                f"{len(readings)} values captured when the fault stored."
                + (f" Triggered by {trigger['code']}." if trigger else "")
            )
        else:
            message = (
                "This car has no freeze frame stored. That is normal when the "
                "fault memory is empty or has been cleared."
            )

        return {
            "ok": True, "simulated": False, "frame": frame,
            "trigger": trigger, "readings": readings, "groups": groups,
            "message": message, "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {
            "ok": False, "simulated": False, "frame": frame, "trigger": None,
            "readings": [], "groups": [], "message": str(exc), "timestamp": _now(),
        }
