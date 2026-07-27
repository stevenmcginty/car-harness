"""Self-test for readiness monitors and the "was this car just wiped?" check.

Run:  python -m backend.tools.readiness_selftest

Covers the generic OBD path that every brand shares, so it also guards the
availability-mask fix in core/uds.py. No car, no dongle. Exits 0 on PASS.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core import uds  # noqa: E402
from backend.obd import readiness, sim  # noqa: E402
from backend.obd.pids import PIDS_BY_KEY  # noqa: E402

results: list = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


def main() -> int:
    print("Readiness / recently-cleared self-test\n")

    # ------------------------------------------------------------------ #
    print("New mode-01 PIDs:")
    # ------------------------------------------------------------------ #
    for key, pid, expect_raw, expect in (
        ("warmups_since_clear", 0x30, bytes([12]), 12),
        ("distance_since_clear", 0x31, bytes([0x01, 0x0E]), 270),
        ("mil_minutes", 0x4D, bytes([0x00, 0x2A]), 42),
        ("minutes_since_clear", 0x4E, bytes([0x00, 0x26]), 38),
    ):
        entry = PIDS_BY_KEY.get(key)
        check(entry is not None and entry.pid == pid, f"PID 0x{pid:02X} '{key}' is defined")
        check(entry.decode(expect_raw) == expect, f"PID 0x{pid:02X} decodes to {expect} {entry.unit}")
    check(
        PIDS_BY_KEY["distance_since_clear"].decode(bytes([0xFF, 0xFF])) == 65535,
        "distance-since-clear spans the full 16-bit range",
    )

    # ------------------------------------------------------------------ #
    print("\nReadiness monitor decode (mode 01 PID 01):")
    # ------------------------------------------------------------------ #
    check(readiness.decode_monitors(b"\x00\x07") is None, "a short PID 01 body returns None")

    petrol_ready = readiness.decode_monitors(bytes([0x00, 0x07, 0x61, 0x00]))
    check(petrol_ready["ignitionType"].startswith("spark"), "spark ignition detected")
    check(petrol_ready["supportedCount"] == 6, "3 continuous + 3 non-continuous monitors supported")
    check(petrol_ready["allReady"] is True, "all monitors complete")
    check(petrol_ready["notReadyCount"] == 0, "nothing outstanding")
    check(petrol_ready["milOn"] is False, "MIL bit off")

    petrol_wiped = readiness.decode_monitors(bytes([0x00, 0x77, 0x61, 0x61]))
    check(petrol_wiped["allReady"] is False, "post-clear: monitors incomplete")
    check(petrol_wiped["notReadyCount"] == 6, "post-clear: all 6 outstanding")
    check("Misfire" in petrol_wiped["notReady"], "post-clear: misfire monitor named")

    diesel = readiness.decode_monitors(bytes([0x83, 0x0F, 0x43, 0x02]))
    check(diesel["ignitionType"].startswith("compression"), "compression ignition detected")
    check(diesel["milOn"] is True and diesel["dtcCount"] == 3, "MIL on with 3 codes")
    names = [m["name"] for m in diesel["monitors"]]
    check("NOx / SCR aftertreatment" in names, "diesel monitor set used, not the petrol one")
    check("Catalyst" not in names, "petrol-only monitors absent on a diesel")
    check(diesel["notReady"] == ["NOx / SCR aftertreatment"], "the one incomplete diesel monitor named")

    # ------------------------------------------------------------------ #
    print("\nThe verdict:")
    # ------------------------------------------------------------------ #
    quiet = readiness.assess(None, {}, 0, 0)
    check(quiet["recentlyCleared"] is None, "no data -> no claim in either direction")
    check(quiet["confident"] is False, "...and says it is not confident")

    healthy = readiness.assess(petrol_ready, {"distanceKm": 4210, "minutes": 9860, "warmups": 96}, 0, 2)
    check(healthy["recentlyCleared"] is False, "well-driven car: not recently cleared")
    check(healthy["motRisk"] is False, "well-driven car: no MOT emissions risk")
    check("face value" in healthy["verdict"], "...and says the fault list can be trusted")

    wiped = readiness.assess(petrol_wiped, {"distanceKm": 4, "minutes": 12, "warmups": 0}, 1, 0)
    check(wiped["recentlyCleared"] is True, "just-wiped car: flagged")
    check(wiped["motRisk"] is True, "just-wiped car: MOT emissions risk flagged")
    check("4 km ago" in wiped["verdict"], "verdict quotes the distance")
    check("permanent code" in wiped["verdict"], "verdict cites the permanent code")
    check("re-scan" in wiped["verdict"], "verdict tells the dealer what to do")
    check(len(wiped["signals"]) >= 4, "several independent signals corroborate")

    # a permanent code with a populated stored list is NOT evidence of a wipe
    normal = readiness.assess(petrol_ready, {"distanceKm": 900, "minutes": 4000}, 1, 3)
    check(
        normal["recentlyCleared"] is False,
        "a permanent code alongside stored codes is not treated as a wipe",
    )

    # distance alone, monitors unavailable
    partial = readiness.assess(None, {"distanceKm": 6}, 0, 0)
    check(partial["recentlyCleared"] is True, "distance alone is enough to flag a wipe")
    check(partial["motRisk"] is False, "...without claiming an MOT risk we cannot see")

    # ------------------------------------------------------------------ #
    print("\nDTC status availability mask (core/uds.py):")
    # ------------------------------------------------------------------ #
    check(
        uds.status_flags(0xFF) == [n for _b, n in uds.STATUS_BITS],
        "with a full availability mask every set bit is reported",
    )
    check(
        uds.status_flags(0xFF, 0x09) == ["test failed", "confirmed"],
        "bits the ECU does not implement are not reported",
    )
    check(uds.status_flags(0x00, 0xFF) == [], "a clean DTC reports no flags")

    check(uds.fault_state(0x01)["severity"] == "active", "testFailed -> active now")
    check(uds.fault_state(0x80)["label"] == "lamp on", "warningIndicator -> lamp on")
    check(uds.fault_state(0x08)["severity"] == "historic", "confirmed only -> historic")
    check(uds.fault_state(0x04)["label"] == "pending", "pending -> pending")
    check(uds.fault_state(0x02)["severity"] == "recent", "failed this cycle -> recent")
    check(
        uds.fault_state(0x10)["label"] == "not re-tested since clear",
        "testNotCompletedSinceLastClear -> the wipe tell",
    )
    check(
        uds.fault_state(0x01, 0x08)["severity"] == "historic",
        "a bit outside the availability mask cannot drive the verdict",
    )

    # read_dtcs end to end, through a fake client
    class FakeEcu:
        def __init__(self, availability):
            self.availability = availability

        def request(self, dst, data, timeout=3.0):
            if data[0] == uds.SVC_READ_DTC:
                return bytes([0x59, 0x02, self.availability, 0xC1, 0x0A, 0x00, 0x2F])
            return bytes([0x7F, data[0], 0x11])

    full = uds.read_dtcs(FakeEcu(0xFF), 0x10)
    check(len(full) == 1 and full[0]["hex"] == "C10A00", "read_dtcs returns the fault")
    check(full[0]["statusAvailability"] == "FF", "availability mask surfaced in the result")
    check("state" in full[0] and full[0]["state"]["severity"] == "active", "per-fault state attached")
    limited = uds.read_dtcs(FakeEcu(0x08), 0x10)
    check(
        limited[0]["flags"] == ["confirmed"],
        "an ECU that only implements 'confirmed' has its other bits ignored",
    )
    check(
        limited[0]["state"]["severity"] == "historic",
        "...and the verdict follows the mask, not the raw byte",
    )

    # ------------------------------------------------------------------ #
    print("\nSimulated car end to end:")
    # ------------------------------------------------------------------ #
    sim._state["cleared"] = False
    before = sim.scan()
    check(before["readiness"]["allReady"] is True, "sim before a clear: monitors ready")
    check(before["clearCheck"]["recentlyCleared"] is False, "sim before a clear: not flagged")
    check(before["permanent"] == [], "sim before a clear: no permanent codes")

    sim.clear()
    after = sim.scan()
    check(after["stored"] == [], "sim after a clear: stored list is empty")
    check(len(after["permanent"]) == 1, "sim after a clear: a permanent code survives")
    check(after["readiness"]["allReady"] is False, "sim after a clear: monitors reset")
    check(after["clearCheck"]["recentlyCleared"] is True, "sim after a clear: wipe detected")
    check("Treat the fault list as unproven" in after["message"], "the warning reaches the message")
    check(after["clearCheck"]["motRisk"] is True, "sim after a clear: MOT risk flagged")
    sim._state["cleared"] = False

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    if passed != total:
        print("\nFAILED:")
        for ok, label in results:
            if not ok:
                print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
