"""Offline self-test for the Vauxhall E39A engine feature -- no car, no dongle.

WHAT IS WORTH TESTING HERE, AND WHY IT IS NOT THE NUMBERS
---------------------------------------------------------
Every number engine.py can print comes out of GM's GDS2 dealer database and has
never been confirmed against an A20NFT. So a test that asserted "Engine Speed
reads 812 rpm" would only be asserting that this repository can copy a constant
from one file to another.

What can be tested, and what decides whether the first real run is worth
anything, is the MACHINERY:

* the pack's own scaling round-trips -- the sim encodes a value by inverting
  the file's factor/offset and the reader decodes it back with the file's
  equation, so a wrong scaling could only pass by being wrong twice in opposite
  directions;
* **NRC 31 is a result, not a failure.** The mined PID set is GM's shared
  "E39 4cyl" list, so a real engine will refuse a fair number of these. The
  refusal has to be counted and attributed to the right parameter names rather
  than dropped or crashed on -- that count IS the deliverable of the first run;
* the dialect ladder still earns its answer. The fake speaks GMW3110 and
  refuses UDS, and 0x3E TesterPresent (which exists in both dialects) must not
  be taken as proof of either;
* the desired-versus-actual findings fire on a divergence and stay quiet
  without one, and never turn into a pass/fail verdict -- the pack carries no
  threshold that could justify one;
* nothing in the read path can transmit a $AE DeviceControl. Asserted by
  inspecting every frame the fake was handed, not by reading the source.

Run:  cd car-harness; .venv\\Scripts\\python.exe -m backend.tools.vauxhall_engine_selftest
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.obd.elm327 import ObdError  # noqa: E402
from backend.profiles.vauxhall import dealer_data  # noqa: E402
from backend.profiles.vauxhall import engine as vx_engine  # noqa: E402
from backend.profiles.vauxhall import sim_ecm as vx_sim_ecm  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not cond:
        line += f" -- {detail}"
    print(line)


def ascii_hex(text):
    return text.encode("ascii").hex().upper()


# --------------------------------------------------------------------------- #
# Fake sessions. Same shape as the ones in vauxhall_selftest.py: a module that
# answers raw frames, so the reader under test cannot tell it from an ELM327.
# --------------------------------------------------------------------------- #
class EcmSession:
    """A fake Global A engine controller at 0x7E0, hand-built frame by frame.

    Deliberately NOT sim_ecm.SimulatedEcm: that one is built out of the dealer
    pack, so a test written against it could never notice the reader and the
    sim agreeing on a misreading of the file. This one answers bytes written
    out here by hand, from the scaling quoted in the comment beside each.
    """

    # request hex -> reply hex. Everything else is NRC 31.
    ANSWERS = {
        "3E00": "7E00",
        "1A90": None,                       # filled in __init__ (VIN is text)
        "220003": None,
        "22000C": "62000C0CB0",             # 3248 * 0.25 = 812 rpm
        "220005": "62000581",               # 129 - 40 = 89 C
        "22328A": "62328A52C8",             # 21192 * 0.01 = 211.92 kPa desired
        "223298": "62329831FC",             # 12796 * 0.01 = 127.96 kPa actual
        "22328B": "62328BFF",               # 255 * 0.3922 = 100.01 % -- railed
        "221200": "62120047",               # 71 counts total misfire
        "221201": "6212010002",             # cylinder 1: 2
        "221202": "6212020001",             # cylinder 2: 1
        "221203": "62120300D6",             # cylinder 3: 214
        "221204": "6212040003",             # cylinder 4: 3
    }

    VIN = "W0LGP6EG0G1045512"

    def __init__(self):
        self.frames = []
        self.answers = dict(self.ANSWERS)
        self.answers["1A90"] = "5A90" + ascii_hex(self.VIN)

    def begin(self):
        pass

    def restore(self):
        pass

    def request(self, dst, data, timeout=2.0):
        self.frames.append((dst, data.hex().upper()))
        if dst != 0x7E0:
            raise ObdError("no data", kind="no_data")
        cmd = data.hex().upper()
        hit = self.answers.get(cmd)
        if hit is not None:
            return bytes.fromhex(hit)
        if cmd.startswith("A981"):
            # P0299-00 underboost, confirmed.
            return bytes.fromhex("E981FF" + "02990008")
        if cmd == "A982FF":
            return bytes.fromhex("E98200")
        if cmd.startswith("19") or cmd.startswith("14"):
            return bytes.fromhex("7F" + cmd[:2] + "11")   # UDS not supported
        if cmd.startswith("1A"):
            return bytes.fromhex("7F1A31")
        if cmd.startswith("22"):
            return bytes.fromhex("7F2231")                # request out of range
        return bytes.fromhex("7F" + cmd[:2] + "11")


class SilentEcmSession(EcmSession):
    """A module that stops answering the live block after identification.

    Silence is a third outcome, and it must not be counted as a refusal: a
    refusal is evidence about the calibration, silence is evidence about the
    link. The applicability report keeps them apart and this proves it.
    """

    def request(self, dst, data, timeout=2.0):
        self.frames.append((dst, data.hex().upper()))
        cmd = data.hex().upper()
        if cmd.startswith("22") and cmd != "22F190":
            raise ObdError("The car returned no data for this request.",
                           kind="no_data")
        return super().request(dst, data, timeout)


class UdsEcmSession(EcmSession):
    """A late Global A node that answers plain UDS instead of GMW3110.

    The other half of the ladder: proves the profile does not hard-code GMLAN
    for the engine any more than the scan does for the rest of the car.
    """

    def request(self, dst, data, timeout=2.0):
        self.frames.append((dst, data.hex().upper()))
        if dst != 0x7E0:
            raise ObdError("no data", kind="no_data")
        cmd = data.hex().upper()
        if cmd == "3E00":
            return bytes.fromhex("7E00")
        if cmd == "22F190":
            return bytes.fromhex("62F190" + ascii_hex(self.VIN))
        if cmd.startswith("1902"):
            return bytes.fromhex("5902FF" + "0299000C")
        if cmd.startswith("1A") or cmd.startswith("A9"):
            return bytes.fromhex("7F" + cmd[:2] + "11")
        hit = self.answers.get(cmd)
        if hit is not None and cmd.startswith("22"):
            return bytes.fromhex(hit)
        if cmd.startswith("22"):
            return bytes.fromhex("7F2231")
        return bytes.fromhex("7F" + cmd[:2] + "11")


def _names(applicability, state):
    out = set()
    for row in applicability:
        if row["state"] == state:
            out.update(n for n in row["names"] if n)
    return out


def main():
    # ------------------------------------------------------- the data pack
    pack = dealer_data.load_ecm()
    check("ECM pack loads and carries all ten sections plus caveats",
          set(pack) >= {"caveats", "conditions", "connection", "deliverable",
                        "deviceControls", "dtc", "moduleNode", "parameters",
                        "pidSets", "schema", "tests"},
          str(sorted(pack)))
    check("ECM pack is the E39A node on 0x7E0/0x7E8",
          pack["connection"]["canRequest"] == "0x7E0"
          and pack["connection"]["canResponse"] == "0x7E8"
          and "E39A" in pack["moduleNode"]["name"],
          json.dumps(pack["connection"])[:200])
    check("The T76 gearbox pack still loads unchanged beside it",
          dealer_data.load()["parameters"] and
          dealer_data.by_request("22 19 40"),
          "the ECM pack must not disturb the TCM API")

    caveat_ids = {c["id"] for c in dealer_data.ecm_caveats()}
    check("Pack carries its four machine-readable caveats",
          caveat_ids == {"shared-pid-set", "test-bindings-dead",
                         "no-security-access", "no-thresholds"},
          str(caveat_ids))

    entry = dealer_data.ecm_parameter("22 00 0C", "Engine Speed")
    check("ecm_parameter keys on request AND name",
          entry is not None and entry["scaling"]["factor"] == 0.25
          and entry["responseLength"] == 2,
          str(entry and entry.get("scaling")))
    check("A request carrying two names does not collapse to one",
          dealer_data.ecm_parameter("22 32 98", "Boost Pressure Sensor")
          is not None
          and len(dealer_data.ecm_by_request("22 32 98")) > 1,
          str(len(dealer_data.ecm_by_request("22 32 98"))))
    check("The same NAME on two requests stays two parameters",
          dealer_data.ecm_parameter("22 30 88", "Boost Pressure Sensor")["unit"]
          == "V"
          and dealer_data.ecm_parameter("22 32 98", "Boost Pressure Sensor")["unit"]
          == "kPa",
          "22 30 88 is volts, 22 32 98 is kPa")
    check("Every parameter in the curated core block exists in the pack",
          all(dealer_data.ecm_parameter(req, name) is not None
              for req, name, _ in vx_engine.CORE_BLOCK),
          str([f"{r} {n}" for r, n, _ in vx_engine.CORE_BLOCK
               if dealer_data.ecm_parameter(r, n) is None]))
    check("Every core parameter carries a linear scaling from the pack",
          all((dealer_data.ecm_parameter(req, name) or {}).get("scaling")
              for req, name, _ in vx_engine.CORE_BLOCK),
          "a None scaling would mean the reader has to invent one")
    check("Core block is a curated subset, not the whole 970",
          20 <= len(vx_engine.CORE_BLOCK) <= 40
          and len(dealer_data.ecm_read_requests()) > 400,
          f"{len(vx_engine.CORE_BLOCK)} core vs "
          f"{len(dealer_data.ecm_read_requests())} sweepable")
    check("Every parameter is marked UNVERIFIED against this engine",
          all(p.get("applicability") == "UNVERIFIED"
              for p in pack["parameters"]),
          "the pack cannot say which PIDs an A20NFT answers")

    hit = dealer_data.ecm_dtc_name("P0299")
    check("GM's own wording comes back for a powertrain code",
          hit is not None and "boost" in (hit["name"] or "").lower()
          and hit["list"] == "master", str(hit))
    check("A code from this module's own override list is marked local",
          (dealer_data.ecm_dtc_name("B269A") or {}).get("list") == "local",
          str(dealer_data.ecm_dtc_name("B269A")))

    controls = dealer_data.ecm_device_controls()
    check("All 70 device controls are present and every one warns it writes",
          len(controls) == 70
          and all(c.get("warning") for c in controls),
          str(len(controls)))

    # --------------------------------------------- reading a hand-built car
    session = EcmSession()
    trace = []
    hit = vx_engine._probe(session, 0x7E0, trace)
    check("Probe proves the GMLAN dialect from 1A 90, not from 3E 00",
          hit is not None and hit[1] == "gmlan" and hit[0] == "1A 90", str(hit))

    readings, applicability = vx_engine._read_block(
        session, 0x7E0, vx_engine.CORE_BLOCK)
    by_name = {r["name"]: r for r in readings}

    check("Engine Speed decodes through the pack's own 0.25 scaling",
          by_name["Engine Speed"]["value"] == "812.0"
          and by_name["Engine Speed"]["raw"] == 3248
          and by_name["Engine Speed"]["unit"] == "RPM",
          str(by_name.get("Engine Speed")))
    check("ECT Sensor decodes through the pack's own -40 offset",
          by_name["ECT Sensor"]["value"] == "89.0", str(by_name.get("ECT Sensor")))
    check("Total Misfire is read as ONE byte, not two",
          by_name["Total Misfire"]["value"] == "71.0"
          and dealer_data.ecm_parameter("22 12 00", "Total Misfire")["responseLength"] == 1,
          str(by_name.get("Total Misfire")))
    check("Per-cylinder misfire counters are read as two bytes",
          by_name["Cylinder 3 History Misfire Counter"]["value"] == "214.0",
          str(by_name.get("Cylinder 3 History Misfire Counter")))
    check("Every live row carries its applicability and says it is dealer data",
          all(r["applicability"] == "UNVERIFIED" and r["source"] == "dealer-data"
              for r in readings),
          "a reading must never be shown as confirmed on this engine")

    # ------------------------------------------------------ the NRC-31 path
    refused = [a for a in applicability if a["state"] == "refused"]
    answered = [a for a in applicability if a["state"] == "answered"]
    check("NRC 31 is counted as refused, not as an error or a silence",
          len(refused) > 0 and all(a["nrc"] == "31" for a in refused),
          str([(a["request"], a["nrc"]) for a in refused][:4]))
    check("A refusal names the NRC in words",
          all("out of range" in (a["nrcName"] or "") for a in refused),
          str({a["nrcName"] for a in refused}))
    check("A refused frame marks every parameter that rides on it",
          "Long Term Fuel Trim" in _names(applicability, "refused")
          and "Engine Speed" in _names(applicability, "answered"),
          str(sorted(_names(applicability, "refused"))[:5]))
    check("Answered plus refused plus silent accounts for every request asked",
          len(applicability) == len({req for req, _, _ in vx_engine.CORE_BLOCK}),
          f"{len(applicability)} rows for "
          f"{len({r for r, _, _ in vx_engine.CORE_BLOCK})} requests")
    check("A refused frame produces no reading at all",
          "Long Term Fuel Trim" not in by_name,
          "a refused parameter must not appear with an invented value")

    silent_session = SilentEcmSession()
    _, silent_app = vx_engine._read_block(
        silent_session, 0x7E0, vx_engine.CORE_BLOCK)
    check("Silence is reported as silence, never as a refusal",
          all(a["state"] == "silent" for a in silent_app)
          and not any(a["nrc"] for a in silent_app),
          str(silent_app[:2]))

    # ------------------------------------------------------- dialect ladder
    ident, dtcs, dialect, error = vx_engine._read_module(
        session, 0x7E0, "gmlan", trace)
    check("GMLAN fault read wins on a module that refuses UDS",
          dialect == "gmlan" and [d["display"] for d in dtcs] == ["P0299-00"],
          f"{dialect} {[d.get('display') for d in dtcs]}")
    check("GMLAN identification returns the VIN as text",
          any(r["label"] == "VIN" and r["value"] == EcmSession.VIN
              for r in ident), str(ident[:2]))

    uds_session = UdsEcmSession()
    uds_trace = []
    uds_hit = vx_engine._probe(uds_session, 0x7E0, uds_trace)
    check("A UDS-only node answers the probe without proving GMLAN",
          uds_hit is not None and uds_hit[1] is None, str(uds_hit))
    u_ident, u_dtcs, u_dialect, _ = vx_engine._read_module(
        uds_session, 0x7E0, uds_hit[1], uds_trace)
    check("The ladder falls through to UDS when GMLAN is refused",
          u_dialect == "uds" and [d["display"] for d in u_dtcs] == ["P0299-00"],
          f"{u_dialect} {[d.get('display') for d in u_dtcs]}")
    check("UDS identification still produces the VIN",
          any(r["value"] == EcmSession.VIN for r in u_ident), str(u_ident[:2]))

    # ------------------------------------- GM wording attached to the faults
    named = vx_engine._enrich_dtcs(dtcs)
    check("A fault carries GM's own wording and which dictionary it came from",
          named[0].get("gmName") and named[0].get("gmNameList") == "master",
          str(named[0].get("gmName")))

    # ------------------------------------------------------ the simulator
    check("Simulator refuses an unknown scenario rather than inventing one",
          _raises(lambda: vx_sim_ecm.SimulatedEcm("turbo-explodes"), ValueError),
          "an unknown scenario must not silently become 'healthy'")

    healthy = vx_engine.analyze(None, "sim", scenario="healthy")
    check("Healthy sim run is ok, labelled simulated, and says so in words",
          healthy["ok"] and healthy["simulated"]
          and "SIMULATED" in healthy["message"], healthy["message"][:120])
    check("Healthy sim finds no divergence worth a wastegate finding",
          healthy["boost"]["difference"] == 0.0
          and healthy["boost"]["wastegateRailed"] is False,
          str(healthy["boost"]["difference"]))
    check("Healthy sim still exercises the NRC-31 path",
          len(healthy["didReport"]["refused"]) == len(vx_sim_ecm.REFUSED_CORE)
          and all(r["nrc"] == "31" for r in healthy["didReport"]["refused"]),
          str(len(healthy["didReport"]["refused"])))
    check("Healthy sim decodes the identification part numbers as decimals",
          any(r.get("label") == "End Model Part Number"
              and r["value"].isdigit() for r in healthy["ident"]),
          str([r.get("value") for r in healthy["ident"]]))
    check("Every result repeats the pack's four caveats verbatim",
          {c["id"] for c in healthy["caveats"]} ==
          {"shared-pid-set", "test-bindings-dead", "no-security-access",
           "no-thresholds"}
          and all(any(c["text"] in f for f in healthy["findings"])
                  for c in healthy["caveats"]),
          "a caveat that gets paraphrased at each hop goes missing")

    misfire = vx_engine.analyze(None, "sim", scenario="misfire-cyl3")
    check("Misfire scenario names the cylinder carrying the counts",
          misfire["misfire"]["worstCylinder"] == 3
          and misfire["misfire"]["worstCounts"] == 214.0
          and misfire["misfire"]["otherCounts"] == 6.0,
          str(misfire["misfire"]["worstCylinder"]))
    # The verdict check looks at the misfire line alone. Checking every finding
    # would catch the pack's own "no pass/fail thresholds" caveat, which is
    # quoted verbatim on purpose and is the opposite of a verdict.
    misfire_line = next((f for f in misfire["findings"]
                         if f.startswith("Misfire history:")), "")
    check("Misfire finding says cylinder 3 and does not pronounce a verdict",
          "cylinder 3 carries 214" in misfire_line
          and not any(word in misfire_line.lower() for word in
                      ("fail", "out of spec", "faulty", "must be replaced")),
          misfire_line)
    check("Misfire scenario reports both GM-named codes",
          {d["code"] for d in misfire["dtcs"]} == {"P0303", "P0300"}
          and all(d.get("gmName") for d in misfire["dtcs"]),
          str([(d["code"], d.get("gmName")) for d in misfire["dtcs"]]))

    boost = vx_engine.analyze(None, "sim", scenario="boost-fault")
    check("Boost scenario names the divergence with both numbers",
          boost["boost"]["difference"] == -84.0
          and any("128 kPa against Desired Boost Pressure 212 kPa" in f
                  for f in boost["findings"]),
          str(boost["boost"]["difference"]))
    check("Boost scenario reports the wastegate command at the end of its byte",
          boost["boost"]["wastegateRailed"] is True
          and boost["boost"]["wastegate"]["raw"] == 0xFF
          and any("one end of what that single command byte can express" in f
                  for f in boost["findings"]),
          str(boost["boost"]["wastegate"]))
    check("Boost scenario reports P0299 with GM's wording",
          [d["code"] for d in boost["dtcs"]] == ["P0299"]
          and boost["dtcs"][0].get("gmName"),
          str(boost["dtcs"]))

    # ------------------------------------------- no threshold judgements
    all_findings = healthy["findings"] + misfire["findings"] + boost["findings"]
    check("No finding anywhere pronounces a pass or a fail",
          not any(word in f.lower() for f in all_findings
                  for word in ("passes", "failed the", "out of specification",
                               "within limits", "exceeds the limit")),
          "the pack has no thresholds, so nothing here may judge against one")
    check("Divergence findings say a gap is a measurement, not a verdict",
          all("not a verdict" in f
              for f in boost["findings"] if " -- a difference of " in f),
          str([f for f in boost["findings"] if " -- a difference of " in f][:1]))

    # ------------------------------------------------ nothing writes to the car
    fake = vx_sim_ecm.SimulatedEcm("boost-fault")
    vx_engine._probe(fake, 0x7E0, [])
    vx_engine._read_module(fake, 0x7E0, "gmlan", [])
    vx_engine._read_dealer_ident(fake, 0x7E0)
    vx_engine._read_block(fake, 0x7E0, vx_engine.CORE_BLOCK)
    services = {frame[:2] for frame in fake.frames}
    check("A full read pass sends nothing but 3E / 1A / 22 / A9",
          services <= {"3E", "1A", "22", "A9"}, str(sorted(services)))
    check("No DeviceControl frame is ever transmitted",
          "AE" not in services and "20" not in services, str(sorted(services)))
    check("No clear and no write service is transmitted",
          not services & {"04", "14", "2E", "27", "31", "10"},
          str(sorted(services)))
    check("The device controls still reach the operator as descriptions",
          len(boost["deviceControls"]) == 70
          and all(c["warning"] for c in boost["deviceControls"])
          and any(c["request"] == "20" for c in boost["deviceControls"]),
          str(len(boost["deviceControls"])))

    # ------------------------------------------------------------- the sweep
    swept = vx_engine.analyze(None, "sim", sweep=True)
    check("Sweep is never the default and covers the whole 0x22 list",
          len(swept["didReport"]["answered"])
          + len(swept["didReport"]["refused"])
          + len(swept["didReport"]["silent"])
          == len(dealer_data.ecm_read_requests())
          and len(healthy["didReport"]["refused"]) < 10,
          f"{len(dealer_data.ecm_read_requests())} swept vs "
          f"{len(healthy['didReport']['answered'])} core")

    # ---------------------------------------------------- capture and report
    captured = vx_engine.capture(None, seconds=6.0, mode="sim",
                                 scenario="boost-fault", label="pull")
    check("A simulated capture writes rows and says where",
          captured["ok"] and captured["rows"] > 0
          and Path(captured["savedTo"]).exists(), str(captured.get("message"))[:150])
    rows = [json.loads(line) for line in
            Path(captured["savedTo"]).read_text(encoding="utf-8").splitlines()
            if line.strip()]
    check("Every captured row carries the raw bytes beside the reading",
          rows and all("Engine Speed" in r and "Engine Speed (raw)" in r
                       for r in rows),
          str(sorted(rows[0])[:6]) if rows else "no rows")
    # Tick 0 sweeps the slow group, which is where the refused parameters live;
    # tick 1 is the fast group only, so its refusal list must be empty. That
    # pair is what proves the log records the refusals per sample rather than
    # carrying one stale list through the run.
    check("A captured row records which frames that tick refused",
          len(rows) > 1
          and rows[0]["_refused"] == list(vx_sim_ecm.REFUSED_CORE)
          and rows[1]["_refused"] == [],
          str([r.get("_refused") for r in rows[:2]]))

    summary = vx_engine.report(captured["savedTo"])
    check("A capture summarises to a worksheet on disk",
          summary["ok"] and Path(summary["savedTo"]).exists(),
          str(summary.get("message")))
    worksheet = vx_engine.report(None, None, "sim", "boost-fault")
    check("An analyse worksheet writes and warns it is unverified",
          worksheet["ok"]
          and "NOTHING IN THIS REPORT HAS BEEN VERIFIED" in
          Path(worksheet["savedTo"]).read_text(encoding="utf-8"),
          str(worksheet.get("message")))

    faults = vx_engine.faults(None, "sim", "misfire-cyl3")
    check("Faults-only read returns the codes without the live block",
          faults["ok"] and len(faults["dtcs"]) == 2 and "live" not in faults,
          str(faults.get("message")))

    # ------------------------------------------------- the panel declaration
    profile = json.loads(
        (PROJECT_ROOT / "backend" / "profiles" / "vauxhall" / "profile.json")
        .read_text(encoding="utf-8"))
    actions = {a["id"]: a for a in profile["panel"]["actions"]}
    check("profile.json declares the engine action, read-only",
          "engine" in actions and actions["engine"].get("readOnly") is True,
          str(sorted(actions)))
    check("The engine hint says plainly the list is unconfirmed on this engine",
          "never" in actions["engine"]["hint"].lower()
          and "dealer" in actions["engine"]["hint"].lower(),
          actions["engine"]["hint"][:160])
    check("The gearbox action is untouched",
          actions["gearbox"]["label"] == "Gearbox analyse",
          str(actions.get("gearbox", {}).get("label")))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed.")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
        return 1
    print("ALL VAUXHALL ENGINE SELF-TESTS PASSED.")
    return 0


def _raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    sys.exit(main())
