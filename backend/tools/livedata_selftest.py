"""Offline self-test for the live-data engine.

There is no car on the bench, so a fake ELM327 session stands in for one and the
REAL client code is driven through it: support-bitmap discovery, the mode-01
read path, the multi-reading PID case, the sanity bounds, freeze frame, and the
manufacturer overlay. Anything that would need a vehicle to observe is asserted
here instead.

Run:  .venv\\Scripts\\python.exe -m backend.tools.livedata_selftest
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.obd import freeze, livefeed  # noqa: E402
from backend.obd.elm327 import ObdError  # noqa: E402
from backend.obd.pids import (  # noqa: E402
    PIDS,
    PIDS_BY_KEY,
    PIDS_FOR_PID,
    parse_support_bitmap,
    read_supported_pids,
)

checks = []


def check(label, condition):
    checks.append((label, bool(condition)))


# --------------------------------------------------------------------------
# A fake car
# --------------------------------------------------------------------------

class FakeSession:
    """Answers exactly like an ELM327 in front of a modest petrol car.

    Supported PIDs: 0x04 0x05 0x0C 0x0D 0x11 0x14 0x42. Everything else is
    'no data', which is how a real car declines.
    """

    SUPPORTED = {0x04, 0x05, 0x0C, 0x0D, 0x11, 0x14, 0x42}

    DATA = {
        0x04: bytes([0x7F]),              # 49.8 % load
        0x05: bytes([0x7B]),              # 83 C
        0x0C: bytes([0x1A, 0xF8]),        # 1726 rpm
        0x0D: bytes([0x40]),              # 64 km/h
        0x11: bytes([0x33]),              # 20 %
        0x14: bytes([0x60, 0x80]),        # O2: 0.48 V and 0 % trim
        0x42: bytes([0x36, 0xB0]),        # 14.0 V
    }

    def __init__(self):
        self.protocol_num = 6
        self.protocol = "ISO 15765-4 (CAN 11/500)"
        self.asked = []
        self.raw_cmds = []

    def ensure_protocol(self):
        pass

    def _bitmap_for(self, base):
        """Build the real 4-byte support bitmap the car would return.

        The top bit (PID base+0x20) is the 'there is another block' flag, and a
        car only ever sets it when it really does support something further up.
        Getting that wrong here would make the fake advertise a PID that
        discovery could never legitimately reach.
        """
        out = bytearray(4)
        for pid in self.SUPPORTED:
            if base < pid <= base + 0x20:
                index = pid - base - 1
                out[index // 8] |= 0x80 >> (index % 8)
        if any(pid > base + 0x20 for pid in self.SUPPORTED):
            out[3] |= 0x01               # continuation flag
        return bytes(out)

    def query(self, service, pid=None, timeout=8.0):
        self.asked.append((service, pid))
        if service != 0x01:
            raise ObdError("no data", kind="no_data")
        if pid in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0):
            return [bytes([0x41, pid]) + self._bitmap_for(pid)]
        if pid in self.DATA:
            return [bytes([0x41, pid]) + self.DATA[pid]]
        raise ObdError("no data", kind="no_data")

    # Freeze frame goes through _raw/_parse rather than query().
    def _raw(self, data, timeout):
        cmd = data.decode("ascii").strip()
        self.raw_cmds.append(cmd)
        if not cmd.startswith("02"):
            return "NO DATA\r>"
        pid = int(cmd[2:4], 16)
        if pid == 0x02:
            return "42 02 00 01 03\r>"          # P0103 stored the frame
        if pid in self.DATA:
            body = " ".join(f"{b:02X}" for b in self.DATA[pid])
            return f"42 {pid:02X} 00 {body}\r>"
        return "NO DATA\r>"

    def _parse(self, text, expected, echo):
        if "NO DATA" in text:
            raise ObdError("no data", kind="no_data")
        clean = text.replace(">", "").strip().replace(" ", "")
        return [bytes.fromhex(clean)]


last_fake = {}


def fake_with_session(adapter, run):
    """Stands in for obd_service._with_session, cache entry and all. The
    session is kept so a test can inspect what was actually sent to the car."""
    session = FakeSession()
    last_fake["session"] = session
    return run(session, {"supported": None})


# --------------------------------------------------------------------------
# The table itself
# --------------------------------------------------------------------------

check("every PID key is unique", len(PIDS) == len({p.key for p in PIDS}))
check("the table covers far more than the original 23 rows", len(PIDS) > 100)
check("multi-reading PIDs share one query",
      len(PIDS_FOR_PID[0x14]) == 2 and len(PIDS_FOR_PID[0x78]) == 4)
check("every row has a display group", all(p.group for p in PIDS))

# Formulas that a wrong byte offset would quietly break.
check("rpm = (256A+B)/4", PIDS_BY_KEY["rpm"].decode(bytes([0x1A, 0xF8])) == 1726.0)
check("coolant = A-40", PIDS_BY_KEY["coolant"].decode(bytes([0x7B])) == 83)
check("catalyst temp = (256A+B)/10-40",
      PIDS_BY_KEY["cat_temp_b1s1"].decode(bytes([0x13, 0x88])) == 460.0)
check("O2 voltage = A/200", PIDS_BY_KEY["o2_b1s1_v"].decode(bytes([0x60, 0x80])) == 0.48)
check("O2 trim of 0xFF means the sensor does not use it",
      PIDS_BY_KEY["o2_b1s1_trim"].decode(bytes([0x60, 0xFF])) is None)
check("EGT sensor 2 reads at its own offset",
      PIDS_BY_KEY["egt_b1_s2"].decode(bytes([0x03, 0x13, 0x88, 0x1B, 0x58])) == 660.0)
check("commanded lambda 0x8000 = 1.0",
      PIDS_BY_KEY["lambda_cmd"].decode(bytes([0x80, 0x00])) == 1.0)
check("enumerated PIDs decode to words, not numbers",
      PIDS_BY_KEY["fuel_type"].decode(bytes([0x04])) == "Diesel")

# Sanity bounds: rubbish from a clone dongle must be dropped, not displayed.
# 0xFF coolant is 215 C -- the legal top of the scale, so it must NOT be
# rejected. Rejection has to be provoked with a value the formula cannot
# legitimately reach, which for rpm is 0xFFFF (16383.75, past the 16000 bound).
check("the top of a PID's legal scale is kept",
      PIDS_BY_KEY["coolant"].decode(bytes([0xFF])) == 215)
check("a genuinely impossible value is rejected",
      PIDS_BY_KEY["rpm"].decode(bytes([0xFF, 0xFF])) is None)
check("a short payload is rejected", PIDS_BY_KEY["rpm"].decode(bytes([0x1A])) is None)

# Worked by hand from the J1979 bitmap layout: BE=1011 1110 -> 1,3,4,5,6,7;
# 1F -> 12..16; A8=1010 1000 -> 17,19,21; 13 -> 28,31,32.
check("support bitmap decodes the documented example",
      parse_support_bitmap(0x00, bytes([0xBE, 0x1F, 0xA8, 0x13]))
      == {1, 3, 4, 5, 6, 7, 12, 13, 14, 15, 16, 17, 19, 21, 28, 31, 32})

session = FakeSession()
supported = read_supported_pids(session)
check("discovery finds what the car advertises, plus the continuation flags",
      supported == FakeSession.SUPPORTED | {0x20, 0x40})
check("discovery walks on to reach a PID in a later block", 0x42 in supported)
check("discovery stops when the car says there is no next block",
      0x60 not in [p for _, p in session.asked])

# --------------------------------------------------------------------------
# catalog() and read() against the fake car
# --------------------------------------------------------------------------

real_with_session = livefeed.obd_service._with_session
livefeed.obd_service._with_session = fake_with_session

try:
    cat = livefeed.catalog(None, "real")
    keys = {c["key"] for c in cat["channels"]}
    check("catalog is ok", cat["ok"] and not cat["simulated"])
    check("catalog lists only what the car supports",
          {"obd.rpm", "obd.module_voltage"} <= keys and "obd.oil_temp" not in keys)
    check("a two-reading PID contributes both readings",
          {"obd.o2_b1s1_v", "obd.o2_b1s1_trim"} <= keys)
    check("catalog groups are ordered, not arbitrary",
          cat["groups"] == [g for g in livefeed.GROUP_ORDER if g in cat["groups"]])
    check("catalog says how many PIDs the car reported", "supported PIDs" in cat["message"])

    res = livefeed.read(None, "real", ["obd.rpm", "obd.coolant", "obd.o2_b1s1_v",
                                       "obd.o2_b1s1_trim", "obd.oil_temp"])
    by_key = {r["key"]: r for r in res["readings"]}
    check("read returns the requested values", by_key["obd.rpm"]["value"] == 1726.0)
    check("rpm is displayed without silly decimals", by_key["obd.rpm"]["text"] == "1726")
    check("voltage keeps two decimals", livefeed._format(14.0, "V") == "14.00")
    check("temperature keeps one", livefeed._format(83.0, "°C") == "83.0")
    check("both readings of PID 0x14 come back",
          by_key["obd.o2_b1s1_v"]["value"] == 0.48 and "obd.o2_b1s1_trim" in by_key)
    check("an unsupported PID is silently absent, not an error",
          "obd.oil_temp" not in by_key and res["ok"])
    check("a graphable channel is marked graphable", by_key["obd.rpm"]["graphable"])

    # One query per PID even when two readings want it.
    fake = FakeSession()
    livefeed.obd_service._with_session = lambda a, run: run(fake, {"supported": None})
    livefeed.read(None, "real", ["obd.o2_b1s1_v", "obd.o2_b1s1_trim"])
    check("two readings on one PID cost one query",
          sum(1 for s, p in fake.asked if p == 0x14) == 1)

    # ---- freeze frame ----
    import backend.obd.service as obd_service_mod
    real_freeze_ws = obd_service_mod._with_session
    obd_service_mod._with_session = fake_with_session
    try:
        fr = freeze.read(None, "real")
        sent = last_fake["session"].raw_cmds
        check("freeze frame is ok", fr["ok"])
        check("freeze frame names the fault that stored it",
              fr["trigger"] and fr["trigger"]["code"] == "P0103")
        check("freeze frame decodes with the standard table",
              any(r["key"] == "rpm" and r["value"] == 1726.0 for r in fr["readings"]))
        check("freeze frame asks mode 02, never mode 01",
              sent and all(c.startswith("02") for c in sent))
        check("freeze frame asks for the frame number the standard wants",
              all(len(c) == 6 for c in sent))
        check("freeze frame reads the trigger DTC first", sent[0] == "020200")
    finally:
        obd_service_mod._with_session = real_freeze_ws
finally:
    livefeed.obd_service._with_session = real_with_session

# --------------------------------------------------------------------------
# Manufacturer overlay + simulation
# --------------------------------------------------------------------------

norm = livefeed._brand_channels_from_live("mazda", {
    "ok": True,
    "readings": [
        {"name": "Engine oil temperature", "value": "92.4", "unit": "°C"},
        {"name": "Odometer (cluster)", "value": "61,204 mi (98,499 km)", "unit": ""},
        {"name": "Engine oil temperature", "value": "92.4", "unit": "°C"},  # duplicate
    ],
})
check("a manufacturer reading becomes a channel", len(norm) == 2)
check("duplicate display names collapse", len({c["key"] for c in norm}) == 2)
check("a plain number is graphable", norm[0]["graphable"] and norm[0]["value"] == 92.4)
check("a composite string is kept but not graphable",
      norm[1]["text"].startswith("61,204") and not norm[1]["graphable"])
check("manufacturer channels are grouped sensibly", norm[0]["group"] == "Temperatures")
check("manufacturer channels are namespaced by brand",
      all(c["key"].startswith("mazda.") for c in norm))
check("the source names the brand, not just 'enhanced'",
      "Mazda" in norm[0]["source"])

sim = livefeed.catalog(None, "sim", "mazda")
check("sim catalog is labelled simulated", sim["simulated"] and "SIMULATED" in sim["message"])
check("sim covers the whole table", len(sim["channels"]) > 90)
check("sim includes manufacturer channels",
      any(c["key"].startswith("mazda.") for c in sim["channels"]))
sim_by_key = {c["key"]: c for c in sim["channels"]}
check("simulated coolant is a plausible temperature",
      40 <= sim_by_key["obd.coolant"]["value"] <= 110)
check("simulated evap pressure is not pinned to its extreme",
      abs(sim_by_key["obd.evap_pressure"]["value"]) < 2000)
check("simulated voltage looks like a charging system",
      12.0 <= sim_by_key["obd.module_voltage"]["value"] <= 15.0)

sim_read = livefeed.read(None, "sim", ["obd.rpm"], "mazda")
check("sim read returns only what was asked",
      len(sim_read["readings"]) == 1 and sim_read["readings"][0]["key"] == "obd.rpm")

# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app  # noqa: E402

client = TestClient(app)   # no 'with' -- avoids the startup UDP responder

r = client.get("/api/live/catalog", params={"mode": "sim", "brand": "mazda"})
check("GET /api/live/catalog", r.status_code == 200 and r.json()["ok"])
r = client.get("/api/live/read", params={"mode": "sim", "keys": "obd.rpm,obd.coolant"})
check("GET /api/live/read", r.status_code == 200 and len(r.json()["readings"]) == 2)
r = client.get("/api/obd/freeze", params={"mode": "sim"})
check("GET /api/obd/freeze", r.status_code == 200 and r.json()["trigger"]["code"] == "P0301")
check("an unknown manufacturer is a 404",
      client.get("/api/live/catalog", params={"mode": "sim", "brand": "ferrari"}).status_code == 404)
check("no manufacturer is fine -- standard OBD-II alone",
      client.get("/api/live/catalog", params={"mode": "sim"}).json()["ok"])
check("a bad mode is rejected",
      client.get("/api/live/catalog", params={"mode": "banana"}).status_code == 400)
check("real mode without an adapter is refused, not faked",
      client.get("/api/live/read", params={"mode": "real", "keys": "obd.rpm"}).status_code == 400)

# Every brand must be reachable through the live layer -- this is the standing
# rule that a new manufacturer inherits live data automatically.
profiles = client.get("/api/profiles").json()["profiles"]
for profile in profiles:
    got = client.get("/api/live/catalog", params={"mode": "sim", "brand": profile["id"]})
    check(f"{profile['id']}: live catalog answers", got.status_code == 200 and got.json()["ok"])

check("the older /api/obd/live still works",
      client.get("/api/obd/live", params={"mode": "sim"}).json()["ok"])
check("the older /api/{brand}/live still works",
      client.get("/api/mazda/live", params={"mode": "sim"}).json()["ok"])


# --------------------------------------------------------------------------

failed = [label for label, ok in checks if not ok]
for label, ok in checks:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
if failed:
    print("\nFAILED:")
    for label in failed:
        print(f"  - {label}")
sys.exit(1 if failed else 0)
