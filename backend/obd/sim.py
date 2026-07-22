"""Clearly-labelled simulated OBD-II results for demos with no car present.

Everything here is stamped simulated=True and SIMULATED in the message, and it
is only ever reachable via mode=sim -- the same rule the DoIP probe follows.
Live values wander over time so the demo dashboard visibly moves.
"""

import math
import time
from datetime import datetime, timezone

from backend.obd import dtc
from backend.obd.pids import DASHBOARD_KEYS, PIDS_BY_KEY

SIM_VIN = "SIMULATED0CAR0001"

_SIM_STORED = ["P0301", "P0420"]
_SIM_PENDING = ["P0171"]

# Cleared state persists for the life of the server process so the demo
# clear button visibly works.
_state = {"cleared": False}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _codes(codes, status):
    return [
        {"code": c, "description": f"SIMULATED — {dtc.describe(c)}", "status": status}
        for c in codes
    ]


def scan() -> dict:
    cleared = _state["cleared"]
    stored = [] if cleared else _codes(_SIM_STORED, "stored")
    pending = [] if cleared else _codes(_SIM_PENDING, "pending")
    n = len(stored)
    return {
        "ok": True,
        "simulated": True,
        "vin": SIM_VIN,
        "protocol": "SIMULATED (pretend ISO 15765-4 CAN 11/500)",
        "voltage": "12.4V",
        "firmware": "ELM327 v1.5 (simulated)",
        "milOn": not cleared,
        "dtcCount": n,
        "stored": stored,
        "pending": pending,
        "permanent": [],
        "message": (
            "SIMULATED — no real car connected. "
            + (
                "No fault codes (they were 'cleared')."
                if cleared
                else f"{n} stored fault code(s), MIL (engine light) ON."
            )
        ),
        "timestamp": _now(),
    }


def clear() -> dict:
    _state["cleared"] = True
    return {
        "ok": True,
        "simulated": True,
        "cleared": True,
        "message": "SIMULATED — pretend fault codes cleared. No real car was touched.",
        "timestamp": _now(),
    }


def live(keys=None) -> dict:
    t = time.time()
    base = {
        "rpm": 820 + 260 * math.sin(t / 2.1) + 40 * math.sin(t * 1.7),
        "speed": max(0, 32 + 30 * math.sin(t / 9.0)),
        "coolant": min(92, 55 + (t % 600) / 8.0),
        "load": 22 + 12 * math.sin(t / 3.2),
        "throttle": 14 + 9 * math.sin(t / 2.6),
        "intake_temp": 24 + 2 * math.sin(t / 30.0),
        "maf": 6.5 + 3.5 * math.sin(t / 2.1),
        "fuel_level": 62.0,
        "oil_temp": min(95, 40 + (t % 600) / 7.0),
        "module_voltage": 13.9 + 0.15 * math.sin(t / 5.0),
        "map": 33 + 8 * math.sin(t / 3.0),
        "timing": 12 + 5 * math.sin(t / 2.0),
        "stft1": 1.5 * math.sin(t / 4.0),
        "ltft1": 3.1,
        "runtime": int(t % 3600),
        "mil_distance": 128,
        "baro": 101,
        "ambient": 19,
        "fuel_rate": 1.4 + 0.8 * math.sin(t / 2.4),
    }
    wanted = keys or DASHBOARD_KEYS
    readings = []
    for key in wanted:
        pid = PIDS_BY_KEY.get(key)
        if pid is None or key not in base:
            continue
        readings.append(
            {
                "key": key,
                "label": pid.label,
                "value": round(float(base[key]), 2),
                "unit": pid.unit,
            }
        )
    return {
        "ok": True,
        "simulated": True,
        "readings": readings,
        "message": "SIMULATED — values are generated, no real car connected.",
        "timestamp": _now(),
    }


def supported() -> dict:
    return {
        "ok": True,
        "simulated": True,
        "keys": DASHBOARD_KEYS
        + ["map", "timing", "stft1", "ltft1", "runtime", "baro", "ambient", "fuel_rate"],
        "message": "SIMULATED — pretend PID support list.",
        "timestamp": _now(),
    }
