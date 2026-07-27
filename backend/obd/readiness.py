"""Readiness monitors, and the question a used-car buyer actually needs answered:
"has this fault memory just been wiped?"

A clean fault list is worthless on its own. If somebody cleared the codes in the
car park twenty minutes before the viewing, the list is clean because the car has
not had time to re-test itself, not because the car is healthy. Three independent
signals expose that, and they corroborate each other:

  1. **Readiness monitors** (mode 01 PID 01). Each emissions self-test reports
     complete or not-complete. Clearing the memory resets them all, and they only
     go complete again after a full drive cycle. Also a hard MOT fact: a car with
     unready monitors FAILS the emissions test regardless of whether anything is
     wrong with it.
  2. **Distance and time since the codes were cleared** (PIDs 0x31 / 0x4E / 0x30).
     "Cleared 4 km ago" ends the conversation.
  3. **Permanent codes** (mode 0A). These cannot be cleared by any tool -- only
     the car itself erases them, after it has genuinely re-tested and passed. A
     permanent code alongside an empty stored list is proof of a recent wipe.

Bit layouts are the SAE J1979 standard ones. The monitor sets differ between
spark and compression ignition, which the ignition-type bit tells us.
"""

from typing import List, Optional

# Continuous monitors (byte B): supported in bits 0-2, "not complete" in 4-6.
CONTINUOUS = [
    (0x01, 0x10, "Misfire"),
    (0x02, 0x20, "Fuel system"),
    (0x04, 0x40, "Comprehensive components"),
]

# Non-continuous monitors (bytes C = supported, D = not complete), same bit
# positions in both bytes.
SPARK_MONITORS = [
    (0x01, "Catalyst"),
    (0x02, "Heated catalyst"),
    (0x04, "Evaporative system"),
    (0x08, "Secondary air system"),
    (0x10, "A/C refrigerant"),
    (0x20, "Oxygen sensor"),
    (0x40, "Oxygen sensor heater"),
    (0x80, "EGR system"),
]

COMPRESSION_MONITORS = [
    (0x01, "NMHC catalyst"),
    (0x02, "NOx / SCR aftertreatment"),
    (0x08, "Boost pressure"),
    (0x20, "Exhaust gas sensor"),
    (0x40, "Particulate filter"),
    (0x80, "EGR / VVT system"),
]


def decode_monitors(payload: bytes) -> Optional[dict]:
    """Decode a mode 01 PID 01 response body (the A B C D bytes).

    ``payload`` is the data AFTER the '41 01' prefix, i.e. 4 bytes.
    """
    if len(payload) < 4:
        return None
    a, b, c, d = payload[0], payload[1], payload[2], payload[3]
    compression = bool(b & 0x08)

    monitors: List[dict] = []
    for supported_bit, incomplete_bit, name in CONTINUOUS:
        if not b & supported_bit:
            continue
        monitors.append(
            {"name": name, "ready": not b & incomplete_bit, "type": "continuous"}
        )
    for bit, name in (COMPRESSION_MONITORS if compression else SPARK_MONITORS):
        if not c & bit:
            continue
        monitors.append({"name": name, "ready": not d & bit, "type": "non-continuous"})

    not_ready = [m["name"] for m in monitors if not m["ready"]]
    return {
        "ignitionType": "compression (diesel)" if compression else "spark (petrol)",
        "milOn": bool(a & 0x80),
        "dtcCount": a & 0x7F,
        "monitors": monitors,
        "supportedCount": len(monitors),
        "notReady": not_ready,
        "notReadyCount": len(not_ready),
        "allReady": bool(monitors) and not not_ready,
    }


def assess(
    monitors: Optional[dict],
    since_clear: Optional[dict],
    permanent_count: int,
    stored_count: int,
) -> dict:
    """Turn the three signals into one plain-English verdict.

    ``since_clear`` holds any of distanceKm / minutes / warmups that answered.
    Absent data is never treated as evidence in either direction.
    """
    signals: List[str] = []
    suspicious = False

    distance = (since_clear or {}).get("distanceKm")
    minutes = (since_clear or {}).get("minutes")
    warmups = (since_clear or {}).get("warmups")

    if distance is not None:
        if distance <= 50:
            suspicious = True
            signals.append(f"the codes were cleared only {distance} km ago")
        else:
            signals.append(f"{distance} km driven since the codes were last cleared")
    if minutes is not None:
        if minutes <= 120:
            suspicious = True
            hours = minutes / 60
            when = f"{minutes} minutes" if minutes < 90 else f"{hours:.1f} hours"
            signals.append(f"cleared {when} ago")
        else:
            signals.append(f"cleared {minutes / 60:.0f} hours of running ago")
    if warmups is not None and warmups <= 5:
        suspicious = True
        signals.append(f"only {warmups} warm-up cycle{'s' if warmups != 1 else ''} since the clear")

    if monitors and monitors["notReadyCount"]:
        suspicious = True
        signals.append(
            f"{monitors['notReadyCount']} of {monitors['supportedCount']} "
            "emissions self-tests have not re-run"
        )

    if permanent_count and not stored_count:
        suspicious = True
        signals.append(
            f"{permanent_count} permanent code"
            f"{'s remain' if permanent_count != 1 else ' remains'}, which no tool "
            "can clear, while the stored list is empty"
        )

    if not signals:
        return {
            "recentlyCleared": None,
            "confident": False,
            "signals": [],
            "verdict": (
                "The car did not report enough information to say whether its "
                "fault memory was cleared recently."
            ),
            "motRisk": False,
        }

    mot_risk = bool(monitors and monitors["notReadyCount"])
    if suspicious:
        verdict = (
            "Treat the fault list as unproven: "
            + "; ".join(signals)
            + ". Drive the car properly and re-scan before believing a clean result."
        )
        if mot_risk:
            verdict += (
                " A car in this state will also fail an MOT emissions test until "
                "the monitors have re-run, whether or not anything is wrong with it."
            )
    else:
        verdict = (
            "No sign of a recent wipe: "
            + "; ".join(signals)
            + ". The fault list can be taken at face value."
        )

    return {
        "recentlyCleared": suspicious,
        "confident": True,
        "signals": signals,
        "verdict": verdict,
        "motRisk": mot_risk,
    }
