"""Self-test for the Fiat / Alfa Romeo (FCA) UDS-over-ELM327 stack, no car required.

Run:  python -m backend.tools.fiat_selftest

Drives the REAL client code -- Elm327Uds29Client, backend.core.uds.*, the fiat
scan/pids/resets modules and their tables -- against fake FCA cars that answer
over a fake 29-bit ELM327 session. It proves the parts that make the profile
honest and correct:

  * the FCA table is the five VERIFIED modules only, and TPMS 0xC7 is NOT in it
    (immobilizer trap) -- plus clear() refuses 0xC7 explicitly,
  * the 29-bit AT recipe: ATCP 18 + ATSH DAxxF1 + ATCRA 18DAF1xx, and the
    8-digit ATSH 18DAxxF1 fallback when a clone rejects ATCP ("?"),
  * VIN read FIRST over plain OBD Mode 09,
  * the 19 02 mask ladder (0xFF refused -> 0x08) + the 10 03 extended-session
    escalation on a session NRC, all over 29-bit addressing,
  * TIER 2: no 29-bit answer -> 11-bit engine on 0x7E0 + Mode 03 -> the honest
    "CAN-era Fiat, body needs a B-CAN adapter" message; and the K-line "nothing
    answered" message,
  * the danardi live DID decoders (soot / regen / coolant / trans-oil-temp),
  * the reset catalogue incl. the flagship EPB Brake Service Mode entry,
  * the generic FastAPI brand routes actually answer for Fiat.

Exits 0 on PASS, 1 on FAIL.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core import uds  # noqa: E402
from backend.obd import service as obd_service  # noqa: E402
from backend.obd.elm327 import ObdError  # noqa: E402
from backend.obd.uds_elm327 import Elm327Uds29Client, reassemble  # noqa: E402
from backend.profiles.fiat import pids as fiat_pids  # noqa: E402
from backend.profiles.fiat import resets as fiat_resets  # noqa: E402
from backend.profiles.fiat import scan as fiat_scan  # noqa: E402

FAKE_VIN = "ZAREAABNXH0000001"   # Alfa Giulia-style VIN
ENGINE, TCM, ABS, SCCM, BCM = 0x10, 0x18, 0x28, 0x30, 0x40


def _resp29(dst):
    """The 29-bit response id 18DAF1xx for a one-byte module address."""
    return 0x18DAF100 | dst


class FakeFiatUdsCar:
    """A tiny UDS-era FCA brain keyed by one-byte address. Engine 0x10 needs the
    mask ladder (FF refused with 0x31, 08 answers); ABS 0x28 needs a 10 03
    extended session before 19 02 answers; TCM 0x18 and SCCM 0x30 are clean; BCM
    0x40 is silent (not fitted). Response id is 18DAF1<addr>."""

    PRESENT = {ENGINE, TCM, ABS, SCCM}

    def __init__(self):
        self.ext_started = []   # addresses that got a 10 03 (extended) session
        self.cleared = []       # addresses that got a ClearDiagnosticInformation
        self.masks = {}         # addr -> list of 19 02 masks asked
        self.sessions = set()

    def resp_id(self, dst):
        return _resp29(dst) if dst in self.PRESENT else None

    def handle(self, dst, req):
        resp = self.resp_id(dst)
        if dst not in self.PRESENT or not req:
            return (resp, None)
        svc = req[0]
        if svc == 0x3E:                                   # TesterPresent
            return (resp, bytes([0x7E, 0x00]))
        if svc == 0x10 and len(req) >= 2:                 # session control
            if req[1] == 0x03:
                self.sessions.add(dst)
                self.ext_started.append(dst)
                return (resp, bytes([0x50, 0x03, 0x00, 0x32, 0x01, 0xF4]))
            return (resp, bytes([0x7F, 0x10, 0x12]))
        if svc == 0x22 and len(req) >= 3:                 # ReadDataByIdentifier
            did = (req[1] << 8) | req[2]
            if dst == ENGINE and did == 0xF190:
                return (resp, bytes([0x62, 0xF1, 0x90]) + FAKE_VIN.encode())
            if dst == ENGINE and did == 0xF195:
                return (resp, bytes([0x62, 0xF1, 0x95]) + b"50560012")
            return (resp, bytes([0x7F, 0x22, 0x31]))      # out of range
        if svc == 0x19 and len(req) >= 3:                 # ReadDTCInformation
            mask = req[2]
            self.masks.setdefault(dst, []).append(mask)
            if dst == ENGINE:                             # ladder: FF refused, 08 ok
                if mask == 0xFF:
                    return (resp, bytes([0x7F, 0x19, 0x31]))   # out of range
                return (resp, bytes([0x59, 0x02, mask, 0x04, 0x01, 0x00, 0x2F]))  # P0401
            if dst == ABS:                                # needs an active session
                if dst in self.sessions:
                    return (resp, bytes([0x59, 0x02, mask, 0x53, 0x01, 0x00, 0x08]))  # C1301
                return (resp, bytes([0x7F, 0x19, 0x22]))   # conditions not correct
            return (resp, bytes([0x59, 0x02, mask]))      # TCM / SCCM: clean
        if svc == 0x14:                                   # ClearDiagnosticInformation
            self.cleared.append(dst)
            return (resp, bytes([0x54]))
        return (resp, bytes([0x7F, svc, 0x11]))


class FakeCanEraFiat:
    """A CAN-era Fiat stand-in: no 29-bit module answers; only the 11-bit engine
    (0x7E0) answers generic OBD (Mode 01 PID 00) and Mode 03. Proves the tier-2
    fallback + the honest 'CAN-era Fiat, body needs a B-CAN adapter' message."""

    def resp_id(self, dst):
        return 0x7E8 if dst == 0x7E0 else None

    def handle(self, dst, req):
        if dst == 0x7E0 and req and req[0] == 0x01 and len(req) >= 2 and req[1] == 0x00:
            return (0x7E8, bytes([0x41, 0x00, 0xBE, 0x3E, 0xB8, 0x11]))
        if dst == 0x7E0:
            return (0x7E8, None)                          # UDS ident/DTC silent -> Mode 03
        return (None, None)                               # 29-bit modules silent


class FakeSilentFiat:
    """Nothing answers on 29-bit OR 11-bit -- the K-line era / ignition-off case."""

    def resp_id(self, dst):
        return None

    def handle(self, dst, req):
        return (None, None)


class FakePidCar:
    """Answers the danardi live DIDs: engine 0x10 soot/regen/coolant and TCM 0x18
    transmission oil temp; everything else silent."""

    PRESENT = {ENGINE, TCM}

    def resp_id(self, dst):
        return _resp29(dst) if dst in self.PRESENT else None

    def handle(self, dst, req):
        resp = self.resp_id(dst)
        if dst not in self.PRESENT or not req or req[0] != 0x22 or len(req) < 3:
            return (resp, None)
        did = (req[1] << 8) | req[2]
        if dst == ENGINE and did == 0x18AA:               # soot = A*256+B = 18 g
            return (resp, bytes([0x62, 0x18, 0xAA, 0x00, 0x12]))
        if dst == ENGINE and did == 0x380B:               # regen = 0 -> 0.0 %
            return (resp, bytes([0x62, 0x38, 0x0B, 0x00, 0x00]))
        if dst == ENGINE and did == 0x1003:               # coolant = v*0.02-40 = 88 C
            return (resp, bytes([0x62, 0x10, 0x03, 0x19, 0x00]))
        if dst == TCM and did == 0x04FE:                  # trans oil temp = A-40 = 72 C
            return (resp, bytes([0x62, 0x04, 0xFE, 0x70]))
        return (resp, bytes([0x7F, 0x22, 0x31]))


class FakeElmSession:
    """Just enough of an Elm327Session for the 29-bit UDS client AND the generic
    OBD helpers. Models the ELM's receive filter (a frame is only delivered when
    the current ATCRA matches the module's response id) and the 29-bit header
    scheme: ATSH DAxxF1 / 18DAxxF1 both resolve to the one-byte address xx, and a
    3-digit ATSH is 11-bit (tier 2). ``reject_atcp`` makes ATCP answer '?' so the
    8-digit ATSH fallback is exercised."""

    def __init__(self, car, reject_atcp=False, vin_over_obd=True):
        self.car = car
        self.reject_atcp = reject_atcp
        self.vin_over_obd = vin_over_obd
        self.at_log = []
        self.aimed = None    # resolved destination (one byte for 29-bit, full id for 11-bit)
        self.cra = None      # ATCRA receive filter as int
        self.firmware = "ELM327 v1.5"
        self.protocol = "ISO 15765-4 (CAN 11/500)"
        self.protocol_num = 6
        self.is_can = True

    def ensure_protocol(self):
        pass

    def voltage(self):
        return "12.4V"

    def at(self, cmd, timeout=3.0):
        self.at_log.append(cmd)
        if cmd.startswith("ATCP"):
            return "?" if self.reject_atcp else "OK"
        if cmd.startswith("ATSH"):
            val = cmd[4:]
            try:
                if len(val) == 3:                          # 11-bit e.g. 7E0
                    self.aimed = int(val, 16)
                elif len(val) == 6:                        # 29-bit priority DAxxF1
                    self.aimed = int(val[2:4], 16)
                elif len(val) == 8:                        # 29-bit full 18DAxxF1 (or functional)
                    self.aimed = int(val[4:6], 16)
            except ValueError:
                pass
            return "OK"
        if cmd == "ATCRA":
            self.cra = None
            return "OK"
        if cmd.startswith("ATCRA"):
            try:
                self.cra = int(cmd[5:], 16)
            except ValueError:
                self.cra = None
            return "OK"
        return "OK"

    def _raw(self, data, timeout):
        cmd = data.decode("ascii").strip().upper()
        try:
            req = bytes.fromhex(cmd)
        except ValueError:
            return "?\r>"
        resp_id, reply = self.car.handle(self.aimed, req)
        if reply is None:
            return "NO DATA\r>"
        if self.cra is not None and resp_id is not None and self.cra != resp_id:
            return "NO DATA\r>"
        return reply.hex().upper() + "\r>"

    # ---- generic OBD path (VIN over Mode 09, engine Mode 03 fallback) --------
    def query(self, mode, pid, timeout=None):
        if mode == 0x09 and pid == 0x02 and self.vin_over_obd:
            return [bytes([0x49, 0x02, 0x01]) + FAKE_VIN.encode()]
        if mode == 0x03:                                 # stored DTCs (CAN framing)
            return [bytes([0x43, 0x01, 0x04, 0x01])]     # -> P0401
        raise ObdError("no data", kind="no_data")


def main() -> int:
    failures = []

    def check(name, cond, extra=""):
        print(("  PASS  " if cond else "  FAIL  ") + name + (f"  ({extra})" if extra else ""))
        if not cond:
            failures.append(name)

    # --- static: the FCA id table + the TPMS guard + profile manifest --------
    table = fiat_scan.FCA_ECU_TABLE
    addrs = [row[0] for row in table]
    check("FCA table is the five VERIFIED modules", len(table) == 5, str(len(table)))
    check("all module addresses unique", len(set(addrs)) == len(addrs))
    check("table is engine/TCM/ABS/SCCM/BCM",
          set(addrs) == {0x10, 0x18, 0x28, 0x30, 0x40}, str(sorted(hex(a) for a in addrs)))
    check("TPMS 0xC7 is NOT in the table (immobilizer trap)", 0xC7 not in addrs)
    check("TPMS_ADDR constant is 0xC7", fiat_scan.TPMS_ADDR == 0xC7)

    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "profiles" / "fiat" / "profile.json")
        .read_text(encoding="utf-8-sig")
    )
    check("profile.json id == fiat", manifest.get("id") == "fiat")
    check("profile.json name == Fiat / Alfa Romeo", manifest.get("name") == "Fiat / Alfa Romeo")
    check("panel has exactly one action (live)",
          len(manifest["panel"]["actions"]) == 1
          and manifest["panel"]["actions"][0]["id"] == "live")
    check("panel hint states SGW blocks writes not reads",
          "Security-Gateway" in manifest["panel"]["hint"]
          and "not reads" in manifest["panel"]["hint"])
    check("panel hint names the B-CAN adapter for CAN-era cars",
          "B-CAN" in manifest["panel"]["hint"])

    # --- static: clear() refuses TPMS 0xC7 outright --------------------------
    tpms = fiat_scan.clear(None, "real", "0xC7")
    check("clear() refuses 0xC7", tpms["ok"] is False and not tpms["cleared"])
    check("clear() 0xC7 refusal cites the immobilizer",
          "immobilizer" in tpms["message"].lower(), tpms["message"])

    # --- static: simulated scan/clear shape ----------------------------------
    sim = fiat_scan.scan(None, "sim")
    check("sim scan ok+simulated", sim["ok"] and sim["simulated"])
    check("sim scan has 5 modules", len(sim["modules"]) == 5, str(len(sim["modules"])))
    check("sim module carries address+response",
          all("address" in m and "response" in m for m in sim["modules"]))
    check("sim scan labelled SIMULATED", "SIMULATED" in sim["message"])
    check("no TPMS row in the sim scan",
          all("0xC7" not in m["address"].upper() for m in sim["modules"]))
    simclear = fiat_scan.clear(None, "sim")
    check("sim clear simulated", simclear["simulated"] is True)

    # --- reassemble: single frame, kept negative, no-data --------------------
    rec = reassemble("59 02 08 04 01 00 2F\r>", "190208")
    check("reassemble single frame",
          rec and rec[0] == bytes([0x59, 0x02, 0x08, 0x04, 0x01, 0x00, 0x2F]))
    neg = reassemble("7F 19 22\r>", "190208")
    check("reassemble keeps 0x7F negative", neg and neg[0] == bytes([0x7F, 0x19, 0x22]))

    # --- 29-bit AT recipe: ATCP 18 / ATSH DAxxF1 / ATCRA 18DAF1xx ------------
    car = FakeFiatUdsCar()
    session = FakeElmSession(car)
    client = Elm327Uds29Client(session)
    client.begin()
    check("begin() sets the CAN priority byte (ATCP18)", "ATCP18" in session.at_log)
    check("engine present via 3E 00", fiat_scan._probe(client, ENGINE, []))
    check("aims 3-byte header ATSHDA10F1 (priority mode)", "ATSHDA10F1" in session.at_log,
          str([c for c in session.at_log if c.startswith("ATSH")]))
    check("aims 29-bit receive filter ATCRA18DAF110", "ATCRA18DAF110" in session.at_log)
    check("sets 29-bit flow-control header ATFCSH18DA10F1", "ATFCSH18DA10F1" in session.at_log)

    # ident (F190 VIN) + the mask ladder (FF refused -> 08) on the engine.
    eng_ident = fiat_scan._read_ident(client, ENGINE, [])
    eng_vin = next((i["value"] for i in eng_ident if i["label"] == "VIN"), None)
    check("engine VIN read from F190", eng_vin == FAKE_VIN, str(eng_vin))
    eng_dtcs = fiat_scan._read_dtcs(client, ENGINE, [])
    check("engine mask ladder recovered a fault", len(eng_dtcs) == 1, str(len(eng_dtcs)))
    check("engine mask ladder tried FF then 08",
          car.masks.get(ENGINE) == [0xFF, 0x08], str(car.masks.get(ENGINE)))
    if eng_dtcs:
        check("engine DTC decodes P0401", eng_dtcs[0]["code"] == "P0401", eng_dtcs[0]["code"])

    # ABS: session escalation (10 03) before 19 02 answers a C-code.
    abs_trace = []
    abs_dtcs = fiat_scan._read_dtcs(client, ABS, abs_trace)
    check("ABS fault read after 10 03 escalation", len(abs_dtcs) == 1, str(len(abs_dtcs)))
    check("extended session 10 03 opened for ABS", ABS in car.ext_started)
    check("trace records the 10 03 escalation",
          any("10 03" in line for line in abs_trace), " | ".join(abs_trace))
    if abs_dtcs:
        check("ABS DTC decodes C1301", abs_dtcs[0]["code"] == "C1301", abs_dtcs[0]["code"])

    # clear over 29-bit.
    uds.clear_dtcs(client, ENGINE)
    check("clear accepted on engine (14 FF FF FF)", ENGINE in car.cleared)
    client.restore()
    check("restore clears the 29-bit receive filter (bare ATCRA)", "ATCRA" in session.at_log)
    check("restore sets the 29-bit functional header (ATSH18DB33F1)",
          "ATSH18DB33F1" in session.at_log)

    # --- the ATSH 18DAxxF1 fallback when a clone rejects ATCP ("?") ----------
    car2 = FakeFiatUdsCar()
    session2 = FakeElmSession(car2, reject_atcp=True)
    client2 = Elm327Uds29Client(session2)
    client2.begin()
    check("clone rejecting ATCP -> priority mode off", client2._priority is False)
    fiat_scan._probe(client2, ENGINE, [])
    check("fallback aims the full 8-digit header ATSH18DA10F1",
          "ATSH18DA10F1" in session2.at_log,
          str([c for c in session2.at_log if c.startswith("ATSH")]))
    check("fallback never sends the 3-byte ATSHDA10F1", "ATSHDA10F1" not in session2.at_log)
    client2.restore()

    # --- integration: full UDS-era scan() end to end ------------------------
    car3 = FakeFiatUdsCar()
    session3 = FakeElmSession(car3)
    original = obd_service._with_session
    obd_service._with_session = lambda adapter, fn: fn(session3, None)
    try:
        report = fiat_scan.scan({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    check("full scan ok", report["ok"] is True, report.get("message"))
    by_addr = {m["address"]: m for m in report["modules"]}
    check("VIN surfaced (Mode 09 first)", report["vehicle"]["vin"] == FAKE_VIN,
          str(report["vehicle"]))
    check("engine 0x10 present", by_addr["0x10"]["present"] is True)
    check("engine fault P0401 in full scan",
          [d["code"] for d in by_addr["0x10"]["dtcs"]] == ["P0401"], str(by_addr["0x10"]["dtcs"]))
    check("ABS 0x28 present with C1301",
          by_addr["0x28"]["present"] and
          [d["code"] for d in by_addr["0x28"]["dtcs"]] == ["C1301"], str(by_addr["0x28"]["dtcs"]))
    check("BCM 0x40 not present (not fitted)", by_addr["0x40"]["present"] is False)
    check("scan switched to 29-bit (ATSP7)", "ATSP7" in session3.at_log)
    check("scan restored the discovered protocol (ATSP6 + ATSH7DF)",
          "ATSP6" in session3.at_log and "ATSH7DF" in session3.at_log)
    check("message flags UDS-era FCA + SGW reads OK",
          "UDS-era FCA" in report["message"] and "Security-Gateway" in report["message"],
          report["message"])
    check("no false VIN-mismatch warning", "MISMATCH" not in report["message"], report["message"])

    # --- integration: TIER 2 fallback -> CAN-era Fiat message ---------------
    car4 = FakeCanEraFiat()
    session4 = FakeElmSession(car4)
    obd_service._with_session = lambda adapter, fn: fn(session4, None)
    try:
        report2 = fiat_scan.scan({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    by4 = {m["address"]: m for m in report2["modules"]}
    check("CAN-era scan ok", report2["ok"] is True, report2.get("message"))
    check("tier-2 engine present via 01 00 on 0x7E0", by4["0x7E0"]["present"] is True)
    check("tier-2 engine fault via Mode 03 decodes P0401",
          [d["code"] for d in by4["0x7E0"]["dtcs"]] == ["P0401"], str(by4["0x7E0"]["dtcs"]))
    check("tier-2 tried 29-bit then dropped to 11-bit (ATSP7 then ATSP6)",
          "ATSP7" in session4.at_log and "ATSP6" in session4.at_log)
    check("message flags CAN-era Fiat + the B-CAN adapter",
          "CAN-era Fiat" in report2["message"] and "B-CAN" in report2["message"],
          report2["message"])

    # --- integration: nothing answers -> K-line honesty note ----------------
    session5 = FakeElmSession(FakeSilentFiat(), vin_over_obd=False)
    obd_service._with_session = lambda adapter, fn: fn(session5, None)
    try:
        report3 = fiat_scan.scan({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    check("silent-car scan still ok (honest report)", report3["ok"] is True, report3.get("message"))
    check("message names the K-line era as unsupported",
          "K-line" in report3["message"] and "no engine answered" in report3["message"],
          report3["message"])

    # --- integration: clear() a single module over 29-bit -------------------
    car6 = FakeFiatUdsCar()
    session6 = FakeElmSession(car6)
    obd_service._with_session = lambda adapter, fn: fn(session6, None)
    try:
        cres = fiat_scan.clear({"name": "Fake adapter"}, "real", "0x10")
    finally:
        obd_service._with_session = original
    check("clear() clears the addressed engine module",
          "0x10" in cres.get("cleared", []) and ENGINE in car6.cleared, str(cres))

    # --- pids: sim + the danardi DID decoders -------------------------------
    lsim = fiat_pids.live(None, "sim")
    check("sim live ok+simulated+labelled",
          lsim["ok"] and lsim["simulated"] and "SIMULATED" in lsim["message"])

    psession = FakeElmSession(FakePidCar())
    obd_service._with_session = lambda adapter, fn: fn(psession, None)
    try:
        lreal = fiat_pids.live({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    got = {r["name"]: r["value"] for r in lreal["readings"]}
    check("DPF soot mass decodes to 18 g", got.get("DPF soot mass") == "18", str(got))
    check("DPF regen decodes to 0.0 %", got.get("DPF regeneration in progress") == "0.0", str(got))
    check("coolant decodes to 88 C", got.get("Coolant temperature") == "88", str(got))
    check("transmission oil temp decodes to 72 C (TCM 0x18)",
          got.get("Transmission oil temperature") == "72", str(got))
    check("danardi caveat reading appended",
          any(r["name"] == "Note" and "verify the scaling" in r["value"]
              for r in lreal["readings"]), str(lreal["readings"]))
    check("live DID table only touches the engine + TCM (never TPMS 0xC7)",
          all(addr in (0x10, 0x18) for addr, *_ in fiat_pids.GIULIA_DIDS))

    # --- resets: catalogue incl. the flagship EPB entry ----------------------
    cat = fiat_resets.list_resets()
    check("resets brand == fiat", cat["brand"] == "fiat")
    check("five reset entries shipped", len(cat["resets"]) == 5, str(len(cat["resets"])))
    check("every reset has method=procedure",
          all(r["method"] == "procedure" for r in cat["resets"]))
    check("every reset carries a confidence + source + procedure steps",
          all(r.get("confidence") and r.get("source") and r.get("procedure")
              for r in cat["resets"]))
    epb = next((r for r in cat["resets"] if r["id"] == "alfa-epb-brake-service"), None)
    check("flagship EPB Brake Service Mode entry exists", epb is not None)
    check("EPB entry is verified with 7 steps",
          epb is not None and epb["confidence"] == "verified" and len(epb["procedure"]) == 7,
          str(epb and len(epb["procedure"])))
    check("EPB entry warns the scan-tool actuation is SGW-blocked on 2018+",
          epb is not None and any("Security-Gateway" in c or "SGW" in c for c in epb["cautions"]),
          str(epb and epb["cautions"]))
    check("EPB entry says pump the pedal before reconnecting the battery",
          epb is not None and any("PUMP THE BRAKE PEDAL" in s for s in epb["procedure"]))
    conf = {r["id"]: r["confidence"] for r in cat["resets"]}
    check("Fiat 500 pedal + 2016+ menu + Giulia oil are verified",
          conf.get("fiat-500-pedal") == "verified"
          and conf.get("fiat-cluster-menu") == "verified"
          and conf.get("alfa-giulia-stelvio-oil") == "verified", str(conf))
    check("Punto family reset is community", conf.get("fiat-punto-pedal") == "community")
    perf = fiat_resets.perform(None, "real", "alfa-epb-brake-service", confirm=True)
    check("EPB procedure perform() is not runnable + returns steps",
          perf.get("runnable") is False and bool(perf.get("reset", {}).get("procedure")), perf)

    # --- routes: the generic brand routes actually answer for Fiat ----------
    from fastapi.testclient import TestClient

    from backend.main import app
    tc = TestClient(app)
    profs = tc.get("/api/profiles").json().get("profiles", [])
    fi = next((p for p in profs if p.get("id") == "fiat"), {})
    feats = fi.get("features", {})
    check("API /api/profiles lists fiat with panel", bool(fi) and bool(fi.get("panel")))
    check("fiat features scan/clear/live/resets true, snapshot false",
          feats.get("scan") and feats.get("clear") and feats.get("live")
          and feats.get("resets") and feats.get("snapshot") is False, str(feats))
    rscan = tc.get("/api/fiat/scan?mode=sim").json()
    check("API sim scan over the generic brand route", rscan.get("ok") and rscan.get("simulated"))
    rlive = tc.get("/api/fiat/live?mode=sim").json()
    check("API sim live over the generic brand route", rlive.get("ok") and rlive.get("simulated"))
    rresets = tc.get("/api/fiat/resets").json()
    check("API resets route answers",
          rresets.get("brand") == "fiat" and len(rresets.get("resets", [])) == 5)
    check("API rejects unknown-brand snapshot for fiat (no snapshot.py)",
          tc.get("/api/fiat/snapshot?mode=sim").status_code == 404)

    print()
    print("  sim-scan message: " + rscan.get("message", ""))
    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SELF-TEST PASSED: FCA 29-bit table + TPMS guard, ATCP/ATSH/ATCRA recipe + "
          "8-digit fallback, mask ladder + session escalation, tier-2 CAN-era + K-line "
          "honesty, danardi live decoders, reset catalogue incl. EPB, and brand routes "
          "all good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
