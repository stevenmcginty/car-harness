"""Self-test for the Jaguar / Land Rover UDS-over-ELM327 stack, no car required.

Run:  python -m backend.tools.jlr_selftest

Drives the REAL client code -- Elm327UdsClient, backend.core.uds.*, the jlr
scan/pids/resets modules and their tables -- against fake JLR cars that answer
over a fake 11-bit ELM327 session. It proves the parts that make the profile
honest and correct:

  * the JLR table is the three publicly-VERIFIED driveline modules only
    (engine 0x7E0, ZF gearbox 0x7E1, rear e-diff 0x795; response = request+8,
    incl. 795 -> 79D), nothing invented, nothing security-related,
  * VIN read FIRST over plain OBD Mode 09,
  * the 19 02 mask ladder (0xFF refused -> 0x08) + the 10 03 extended-session
    escalation on a session NRC,
  * the KWP2000 fallback rung for the older Ford-era cars: 10 81 session,
    1A ident records, 18 02 FF FF fault read,
  * the generic-OBD-only detection: a DoIP-era / gateway-filtered car (VIN and
    01 00 answer, no module depth) gets the honest DoIP/Evoque note, a fully
    silent car gets the J1850/BMW-era note,
  * the Old Jaguar Group live DID decoders (oil temp/level/volume, ZF ATF,
    e-diff temp) + the single-source caveat row,
  * the reset catalogue: 17 dash-procedure cards incl. the flagship EPB
    service-mode entry with the actuator-destruction warning, the honest
    tool-only battery/BMS card, and NOTHING touching immobilizer/keys,
  * the generic FastAPI brand routes actually answer for JLR.

Exits 0 on PASS, 1 on FAIL.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core import uds  # noqa: E402
from backend.obd import service as obd_service  # noqa: E402
from backend.obd.elm327 import ObdError  # noqa: E402
from backend.obd.uds_elm327 import Elm327UdsClient, reassemble  # noqa: E402
from backend.profiles.jlr import pids as jlr_pids  # noqa: E402
from backend.profiles.jlr import resets as jlr_resets  # noqa: E402
from backend.profiles.jlr import scan as jlr_scan  # noqa: E402

FAKE_VIN = "SALLSAAF4FA000001"   # Land Rover-style VIN
ENGINE, TCM, RDCM = 0x7E0, 0x7E1, 0x795


class FakeJlrUdsCar:
    """A UDS-era JLR brain keyed by 11-bit request id. Engine 0x7E0 needs the
    mask ladder (FF refused with 0x31, 08 answers); the e-diff 0x795 needs a
    10 03 extended session before 19 02 answers; TCM 0x7E1 is clean. Response
    id is request + 8 (795 -> 79D)."""

    PRESENT = {ENGINE, TCM, RDCM}

    def __init__(self):
        self.ext_started = []   # ids that got a 10 03 (extended) session
        self.cleared = []       # ids that got a ClearDiagnosticInformation
        self.masks = {}         # id -> list of 19 02 masks asked
        self.sessions = set()

    def resp_id(self, dst):
        return dst + 8 if dst in self.PRESENT else None

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
            if did == 0xF190 and dst in (ENGINE, TCM):
                return (resp, bytes([0x62, 0xF1, 0x90]) + FAKE_VIN.encode())
            if dst == ENGINE and did == 0xF187:
                return (resp, bytes([0x62, 0xF1, 0x87]) + b"AH22-12A650-BC")
            return (resp, bytes([0x7F, 0x22, 0x31]))      # out of range
        if svc == 0x19 and len(req) >= 3:                 # ReadDTCInformation
            mask = req[2]
            self.masks.setdefault(dst, []).append(mask)
            if dst == ENGINE:                             # ladder: FF refused, 08 ok
                if mask == 0xFF:
                    return (resp, bytes([0x7F, 0x19, 0x31]))   # out of range
                return (resp, bytes([0x59, 0x02, mask, 0x02, 0x34, 0x00, 0x2F]))  # P0234
            if dst == RDCM:                               # needs an active session
                if dst in self.sessions:
                    return (resp, bytes([0x59, 0x02, mask, 0x53, 0x01, 0x00, 0x08]))  # C1301
                return (resp, bytes([0x7F, 0x19, 0x22]))   # conditions not correct
            return (resp, bytes([0x59, 0x02, mask]))      # TCM: clean
        if svc == 0x14:                                   # ClearDiagnosticInformation
            self.cleared.append(dst)
            return (resp, bytes([0x54]))
        return (resp, bytes([0x7F, svc, 0x11]))


class FakeKwpEraJlr:
    """An older Ford-era JLR: the engine answers presence (3E 00) but refuses
    every UDS read, then answers the classic KWP2000 rungs -- 10 81 session,
    1A 87 ident, 18 02 FF FF fault read (-> P0234). Proves the KWP fallback."""

    PRESENT = {ENGINE}

    def __init__(self):
        self.kwp_sessions = []

    def resp_id(self, dst):
        return dst + 8 if dst in self.PRESENT else None

    def handle(self, dst, req):
        resp = self.resp_id(dst)
        if dst not in self.PRESENT or not req:
            return (resp, None)
        svc = req[0]
        if svc == 0x3E:
            return (resp, bytes([0x7E, 0x00]))
        if svc == 0x22:
            return (resp, bytes([0x7F, 0x22, 0x11]))      # UDS not supported here
        if svc == 0x19:
            return (resp, bytes([0x7F, 0x19, 0x11]))
        if svc == 0x10 and len(req) >= 2:
            if req[1] == 0x81:                            # KWP start
                self.kwp_sessions.append(dst)
                return (resp, bytes([0x50, 0x81]))
            return (resp, bytes([0x7F, 0x10, 0x11]))
        if svc == 0x1A and len(req) >= 2:                 # KWP ident record
            if req[1] == 0x87:
                return (resp, bytes([0x5A, 0x87]) + b"C2Z-14C204-AB")
            return (resp, bytes([0x7F, 0x1A, 0x31]))
        if svc == 0x18:                                   # KWP readDTCByStatus
            return (resp, bytes([0x58, 0x01, 0x02, 0x34, 0x68]))   # P0234
        return (resp, bytes([0x7F, svc, 0x11]))


class FakeDoipEraJlr:
    """A MY17+ DoIP (or gateway-filtered Evoque/Disco Sport) stand-in: no module
    answers any manufacturer service, but the engine still answers generic OBD
    (Mode 01 PID 00) and the car reports a VIN over Mode 09. Proves the honest
    generic-OBD-only detection."""

    def resp_id(self, dst):
        return 0x7E8 if dst == ENGINE else None

    def handle(self, dst, req):
        if dst == ENGINE and req and req[0] == 0x01 and len(req) >= 2 and req[1] == 0x00:
            return (0x7E8, bytes([0x41, 0x00, 0xBE, 0x3E, 0xB8, 0x11]))
        if dst == ENGINE:
            return (0x7E8, None)                          # UDS/KWP silent
        return (None, None)


class FakeSilentJlr:
    """Nothing answers at all -- the J1850 S-Type / BMW-era L322 / ignition-off case."""

    def resp_id(self, dst):
        return None

    def handle(self, dst, req):
        return (None, None)


class FakePidCar:
    """Answers the Old Jaguar Group live DIDs: engine oil temp/level, ZF gearbox
    temp and the e-diff temp; everything else silent."""

    PRESENT = {ENGINE, TCM, RDCM}

    def resp_id(self, dst):
        return dst + 8 if dst in self.PRESENT else None

    def handle(self, dst, req):
        resp = self.resp_id(dst)
        if dst not in self.PRESENT or not req or req[0] != 0x22 or len(req) < 3:
            return (resp, None)
        did = (req[1] << 8) | req[2]
        if dst == ENGINE and did == 0x03F3:               # oil temp = A-40 = 96 C
            return (resp, bytes([0x62, 0x03, 0xF3, 0x88]))
        if dst == ENGINE and did == 0x03E6:               # oil level = A = 52 mm
            return (resp, bytes([0x62, 0x03, 0xE6, 0x34]))
        if dst == TCM and did == 0x1E69:                  # ZF ATF = A-40 = 78 C
            return (resp, bytes([0x62, 0x1E, 0x69, 0x76]))
        if dst == RDCM and did == 0x1E8A:                 # e-diff = A-40 = 70 C
            return (resp, bytes([0x62, 0x1E, 0x8A, 0x6E]))
        return (resp, bytes([0x7F, 0x22, 0x31]))


class FakeElmSession:
    """Just enough of an Elm327Session for the 11-bit UDS client AND the generic
    OBD helpers. Models the ELM's receive filter (a frame is only delivered when
    the current ATCRA matches the module's response id) and 3-digit ATSH."""

    def __init__(self, car, vin_over_obd=True):
        self.car = car
        self.vin_over_obd = vin_over_obd
        self.at_log = []
        self.aimed = None    # 11-bit request id the ELM is pointed at
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
        if cmd.startswith("ATSH"):
            val = cmd[4:]
            try:
                if len(val) == 3:
                    self.aimed = int(val, 16)
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
            return [bytes([0x43, 0x01, 0x02, 0x34])]     # -> P0234
        raise ObdError("no data", kind="no_data")


def main() -> int:
    failures = []

    def check(name, cond, extra=""):
        print(("  PASS  " if cond else "  FAIL  ") + name + (f"  ({extra})" if extra else ""))
        if not cond:
            failures.append(name)

    # --- static: the JLR table is the verified driveline, nothing invented ---
    table = jlr_scan.JLR_ECU_TABLE
    addrs = [row[0] for row in table]
    check("JLR table is the three VERIFIED driveline modules", len(table) == 3, str(len(table)))
    check("table is engine/TCM/e-diff",
          set(addrs) == {0x7E0, 0x7E1, 0x795}, str(sorted(hex(a) for a in addrs)))
    check("all module addresses unique", len(set(addrs)) == len(addrs))
    check("nothing security-shaped in the table",
          all("immobil" not in row[2].lower() and "key" not in row[2].lower() for row in table))

    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "profiles" / "jlr" / "profile.json")
        .read_text(encoding="utf-8-sig")
    )
    check("profile.json id == jlr", manifest.get("id") == "jlr")
    check("profile.json name == Jaguar / Land Rover",
          manifest.get("name") == "Jaguar / Land Rover")
    check("panel has exactly one action (live)",
          len(manifest["panel"]["actions"]) == 1
          and manifest["panel"]["actions"][0]["id"] == "live")
    check("panel hint states the DoIP wall",
          "DoIP" in manifest["panel"]["hint"])
    check("panel hint states the MS-CAN body wall",
          "MS-CAN" in manifest["panel"]["hint"])
    check("panel hint says resets work on every era",
          "every era" in manifest["panel"]["hint"])

    # --- static: simulated scan/clear shape ----------------------------------
    sim = jlr_scan.scan(None, "sim")
    check("sim scan ok+simulated", sim["ok"] and sim["simulated"])
    check("sim scan has 3 modules", len(sim["modules"]) == 3, str(len(sim["modules"])))
    check("sim e-diff response id is request+8 (0x79D)",
          any(m["address"] == "0x795" and m["response"] == "0x79D" for m in sim["modules"]))
    check("sim scan labelled SIMULATED", "SIMULATED" in sim["message"])
    simclear = jlr_scan.clear(None, "sim")
    check("sim clear simulated", simclear["simulated"] is True)

    # --- reassemble sanity ---------------------------------------------------
    rec = reassemble("59 02 08 02 34 00 2F\r>", "190208")
    check("reassemble single frame",
          rec and rec[0] == bytes([0x59, 0x02, 0x08, 0x02, 0x34, 0x00, 0x2F]))

    # --- 11-bit AT recipe: ATSH/ATFCSH/ATCRA per module ----------------------
    car = FakeJlrUdsCar()
    session = FakeElmSession(car)
    client = Elm327UdsClient(session)
    client.begin()
    check("engine present via 3E 00", jlr_scan._probe(client, ENGINE, []))
    check("aims ATSH7E0", "ATSH7E0" in session.at_log,
          str([c for c in session.at_log if c.startswith("ATSH")]))
    check("aims receive filter ATCRA7E8", "ATCRA7E8" in session.at_log)
    check("e-diff present and aimed at ATCRA79D",
          jlr_scan._probe(client, RDCM, []) and "ATCRA79D" in session.at_log,
          str([c for c in session.at_log if c.startswith("ATCRA")]))

    # ident (F190 VIN) + the mask ladder (FF refused -> 08) on the engine.
    eng_ident = jlr_scan._read_ident(client, ENGINE, set(), [])
    eng_vin = next((i["value"] for i in eng_ident if i["label"] == "VIN"), None)
    check("engine VIN read from F190", eng_vin == FAKE_VIN, str(eng_vin))
    check("engine part number read from F187",
          any(i["label"] == "Part number" for i in eng_ident), str(eng_ident))
    eng_dtcs = jlr_scan._read_dtcs(client, ENGINE, set(), set(), [])
    check("engine mask ladder recovered a fault", len(eng_dtcs) == 1, str(len(eng_dtcs)))
    check("engine mask ladder tried FF then 08",
          car.masks.get(ENGINE) == [0xFF, 0x08], str(car.masks.get(ENGINE)))
    if eng_dtcs:
        check("engine DTC decodes P0234", eng_dtcs[0]["code"] == "P0234", eng_dtcs[0]["code"])

    # e-diff: session escalation (10 03) before 19 02 answers a C-code.
    rdcm_trace = []
    rdcm_dtcs = jlr_scan._read_dtcs(client, RDCM, set(), set(), rdcm_trace)
    check("e-diff fault read after 10 03 escalation", len(rdcm_dtcs) == 1, str(len(rdcm_dtcs)))
    check("extended session 10 03 opened for the e-diff", RDCM in car.ext_started)
    if rdcm_dtcs:
        check("e-diff DTC decodes C1301", rdcm_dtcs[0]["code"] == "C1301", rdcm_dtcs[0]["code"])

    # clear + restore.
    uds.clear_dtcs(client, ENGINE)
    check("clear accepted on engine (14 FF FF FF)", ENGINE in car.cleared)
    client.restore()
    check("restore clears the receive filter + functional header",
          "ATCRA" in session.at_log and "ATSH7DF" in session.at_log)

    # --- KWP fallback rung: the older Ford-era module ------------------------
    kwp_car = FakeKwpEraJlr()
    ksession = FakeElmSession(kwp_car)
    kclient = Elm327UdsClient(ksession)
    kclient.begin()
    ktrace = []
    kident = jlr_scan._read_ident(kclient, ENGINE, set(), ktrace)
    check("KWP-era ident via 1A 87 after every UDS DID refused",
          any("KWP 1A 87" in i["label"] for i in kident), str(kident))
    check("KWP session 10 81 was opened", ENGINE in kwp_car.kwp_sessions)
    kdtcs = jlr_scan._read_dtcs(kclient, ENGINE, set(), set(), ktrace)
    check("KWP fault read (18 02 FF FF) decodes P0234",
          len(kdtcs) == 1 and kdtcs[0]["code"] == "P0234", str(kdtcs))
    check("KWP DTCs flagged as stored (KWP)",
          kdtcs and kdtcs[0]["flags"] == ["stored (KWP)"], str(kdtcs and kdtcs[0]["flags"]))
    kclient.restore()

    # --- integration: full UDS-era scan() end to end -------------------------
    car3 = FakeJlrUdsCar()
    session3 = FakeElmSession(car3)
    original = obd_service._with_session
    obd_service._with_session = lambda adapter, fn: fn(session3, None)
    try:
        report = jlr_scan.scan({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    check("full scan ok", report["ok"] is True, report.get("message"))
    by_addr = {m["address"]: m for m in report["modules"]}
    check("VIN surfaced (Mode 09 first)", report["vehicle"]["vin"] == FAKE_VIN,
          str(report["vehicle"]))
    check("engine 0x7E0 present with P0234",
          by_addr["0x7E0"]["present"] and
          [d["code"] for d in by_addr["0x7E0"]["dtcs"]] == ["P0234"],
          str(by_addr["0x7E0"]["dtcs"]))
    check("e-diff 0x795 present with C1301",
          by_addr["0x795"]["present"] and
          [d["code"] for d in by_addr["0x795"]["dtcs"]] == ["C1301"],
          str(by_addr["0x795"]["dtcs"]))
    check("TCM clean", by_addr["0x7E1"]["present"] and not by_addr["0x7E1"]["dtcs"])
    check("message flags CAN-era JLR + the MS-CAN wall",
          "CAN-era JLR" in report["message"] and "MS-CAN" in report["message"],
          report["message"])
    check("no false VIN-mismatch warning", "MISMATCH" not in report["message"])

    # --- integration: DoIP-era / gateway-filtered detection ------------------
    session4 = FakeElmSession(FakeDoipEraJlr())
    obd_service._with_session = lambda adapter, fn: fn(session4, None)
    try:
        report2 = jlr_scan.scan({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    by4 = {m["address"]: m for m in report2["modules"]}
    check("DoIP-era scan ok", report2["ok"] is True, report2.get("message"))
    check("engine present via 01 00 on the standard OBD channel",
          by4["0x7E0"]["present"] is True, str(by4["0x7E0"]["trace"]))
    check("engine fault via Mode 03 decodes P0234",
          [d["code"] for d in by4["0x7E0"]["dtcs"]] == ["P0234"], str(by4["0x7E0"]["dtcs"]))
    check("message names DoIP + the gateway-filtered Evoque/Disco Sport",
          "DoIP" in report2["message"] and "Evoque" in report2["message"],
          report2["message"])
    check("message says the dash-procedure resets still work",
          "resets still work" in report2["message"], report2["message"])

    # --- integration: nothing answers -> J1850/BMW-era honesty note ----------
    session5 = FakeElmSession(FakeSilentJlr(), vin_over_obd=False)
    obd_service._with_session = lambda adapter, fn: fn(session5, None)
    try:
        report3 = jlr_scan.scan({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    check("silent-car scan still ok (honest report)", report3["ok"] is True)
    check("message names S-Type/X-Type + BMW-era L322 as unsupported",
          "S-Type" in report3["message"] and "L322" in report3["message"],
          report3["message"])

    # --- integration: clear() a single module --------------------------------
    car6 = FakeJlrUdsCar()
    session6 = FakeElmSession(car6)
    obd_service._with_session = lambda adapter, fn: fn(session6, None)
    try:
        cres = jlr_scan.clear({"name": "Fake adapter"}, "real", "0x7E0")
    finally:
        obd_service._with_session = original
    check("clear() clears the addressed engine module",
          "0x7E0" in cres.get("cleared", []) and ENGINE in car6.cleared, str(cres))

    # --- pids: sim + the Old Jaguar Group DID decoders -----------------------
    lsim = jlr_pids.live(None, "sim")
    check("sim live ok+simulated+labelled",
          lsim["ok"] and lsim["simulated"] and "SIMULATED" in lsim["message"])

    psession = FakeElmSession(FakePidCar())
    obd_service._with_session = lambda adapter, fn: fn(psession, None)
    try:
        lreal = jlr_pids.live({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    got = {r["name"]: r["value"] for r in lreal["readings"]}
    check("oil temp decodes to 96 C (22 03F3, A-40)",
          got.get("Engine oil temperature") == "96", str(got))
    check("oil level decodes to 52 mm (22 03E6)",
          got.get("Engine oil level (measured)") == "52", str(got))
    check("ZF gearbox temp decodes to 78 C (7E1 22 1E69)",
          got.get("Gearbox (ZF) oil temperature") == "78", str(got))
    check("e-diff temp decodes to 70 C (795 22 1E8A)",
          got.get("Rear e-diff oil temperature") == "70", str(got))
    check("Old Jaguar Group caveat reading appended",
          any(r["name"] == "Note" and "verify the scaling" in r["value"]
              for r in lreal["readings"]), str(lreal["readings"]))
    check("live DID table only touches the driveline (7E0/7E1/795)",
          all(addr in (ENGINE, TCM, RDCM) for addr, *_ in jlr_pids.JLR_DIDS))

    # --- resets: catalogue incl. the flagship EPB service-mode entry ---------
    cat = jlr_resets.list_resets()
    check("resets brand == jlr", cat["brand"] == "jlr")
    check("17 reset entries shipped", len(cat["resets"]) == 17, str(len(cat["resets"])))
    check("every reset has method=procedure",
          all(r["method"] == "procedure" for r in cat["resets"]))
    check("every reset carries confidence + source + procedure steps",
          all(r.get("confidence") and r.get("source") and r.get("procedure")
              for r in cat["resets"]))
    epb = next((r for r in cat["resets"] if r["id"] == "jlr-epb-modern"), None)
    check("flagship EPB service-mode entry exists", epb is not None)
    check("EPB entry warns pistons destroy the actuator without service mode",
          epb is not None and any("destroys" in c for c in epb["cautions"]),
          str(epb and epb["cautions"]))
    check("EPB entry records the disputed switch position honestly",
          epb is not None and any("disagree" in c or "try the other" in c
                                  for c in epb["cautions"]))
    cable = next((r for r in cat["resets"] if r["id"] == "jlr-epb-cable"), None)
    check("cable-EPB entry carries the gearset-trap warning",
          cable is not None and any("shoes" in c.lower() and "cable" in c.lower()
                                    for c in cable["cautions"]),
          str(cable and cable["cautions"]))
    bms = next((r for r in cat["resets"] if r["id"] == "jlr-battery-bms"), None)
    check("battery/BMS card is an honest tool-only brief",
          bms is not None and any("REGISTERED" in s or "tool" in s.lower()
                                  for s in bms["procedure"]))
    ids = {r["id"] for r in cat["resets"]}
    check("oil resets cover the UK-volume families",
          {"jlr-oil-xf-x250", "jlr-oil-modern-jaguar", "jlr-oil-evoque-l538",
           "jlr-oil-disco-sport", "jlr-oil-disco3", "jlr-oil-disco4",
           "jlr-oil-rrs-l320", "jlr-oil-rrs-l494", "jlr-oil-l405",
           "jlr-oil-l322", "jlr-oil-freelander2"} <= ids, str(sorted(ids)))
    check("nothing immobilizer/key-shaped in the catalogue",
          all("immobil" not in json.dumps(r).lower() and "key programming"
              not in json.dumps(r).lower() for r in cat["resets"]))
    perf = jlr_resets.perform(None, "real", "jlr-epb-modern", confirm=True)
    check("EPB procedure perform() is not runnable + returns steps",
          perf.get("runnable") is False and bool(perf.get("reset", {}).get("procedure")),
          str(perf.get("runnable")))

    # --- routes: the generic brand routes actually answer for JLR ------------
    from fastapi.testclient import TestClient

    from backend.main import app
    tc = TestClient(app)
    profs = tc.get("/api/profiles").json().get("profiles", [])
    jl = next((p for p in profs if p.get("id") == "jlr"), {})
    feats = jl.get("features", {})
    check("API /api/profiles lists jlr with panel", bool(jl) and bool(jl.get("panel")))
    check("jlr features scan/clear/live/resets true, snapshot false",
          feats.get("scan") and feats.get("clear") and feats.get("live")
          and feats.get("resets") and feats.get("snapshot") is False, str(feats))
    rscan = tc.get("/api/jlr/scan?mode=sim").json()
    check("API sim scan over the generic brand route",
          rscan.get("ok") and rscan.get("simulated"))
    rres = tc.get("/api/jlr/resets").json()
    check("API resets route serves the 17 cards",
          len(rres.get("resets", [])) == 17, str(len(rres.get("resets", []))))

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("ALL JLR SELF-TEST CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
