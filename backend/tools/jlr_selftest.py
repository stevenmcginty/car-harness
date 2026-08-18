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
  * the X260 AdBlue tooling (docs/private/jlr-x260-adblue-research.md):
    discover() finds the PCM/TCM on their table addresses PLUS one unexpected
    answerer (the SCR-controller question), sweep() classifies
    answered/refused-31/named-NRC/silent and refuses both a too-wide range
    and a dead target, health() takes the Mode-01 baseline before anything
    else and flags the SCR/DPF DIDs capture-pending (nothing invented),
    drive_log() pairs rpm with raw DID payloads and reports its rate,
    and ONLY read-only services (3E/10/22/01/19) ever leave on every path,
  * the reset catalogue: 17 dash-procedure cards incl. the flagship EPB
    service-mode entry with the actuator-destruction warning, the honest
    tool-only battery/BMS card, and NOTHING touching immobilizer/keys,
  * the generic FastAPI brand routes actually answer for JLR,
  * the MCP layer registers the four new tools and gives the honest
    pick-an-adapter error in real mode with no adapter configured.

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
from backend.profiles.jlr import adblue as jlr_adblue  # noqa: E402
from backend.profiles.jlr import discover as jlr_discover  # noqa: E402
from backend.profiles.jlr import pids as jlr_pids  # noqa: E402
from backend.profiles.jlr import resets as jlr_resets  # noqa: E402
from backend.profiles.jlr import scan as jlr_scan  # noqa: E402
from backend.profiles.jlr import sweep as jlr_sweep  # noqa: E402

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


class FakeX260Diesel:
    """A 2016 XF (X260) 3.0d stand-in for the AdBlue tooling: PCM 0x7E0 with
    P249D + P2463 stored and the OJG engine DIDs answering raw, TCM 0x7E1
    clean, and one UNEXPECTED UDS answerer at 0x730 (the SCR/reagent question
    discover() exists to answer). The sweep range 0330-0340 carries a known
    scatter: answered / NRC-31 refused / one NRC-22 / silent. Every request
    the car ever sees is recorded, so the tests can prove only read-only
    services leave the dongle."""

    PRESENT = {ENGINE, TCM, 0x730}

    # Engine DIDs that answer, raw payloads (no decoding on purpose).
    LIVE_DIDS = {
        0x0332: "00C4",        # 2 bytes
        0x0339: "00A100B4",    # 4 bytes
        0x033E: "00C4",
        0x03E6: "34",
        0x03F2: "44",
        0x03F3: "58",
        0x0520: "2A",
    }
    NRC_OTHER = {0x033A: 0x22}   # conditionsNotCorrect
    REFUSED_LO, REFUSED_HI = 0x0330, 0x0338   # -> NRC 31

    def __init__(self):
        self.sent = []          # every (dst, request) the car saw
        self.first_01 = None    # index of the first Mode-01 request

    def resp_id(self, dst):
        return dst + 8 if dst in self.PRESENT else None

    def handle(self, dst, req):
        self.sent.append((dst, bytes(req)))
        if self.first_01 is None and req and req[0] == 0x01:
            self.first_01 = len(self.sent) - 1
        resp = self.resp_id(dst)
        if dst not in self.PRESENT or not req:
            return (resp, None)
        svc = req[0]
        if svc == 0x3E:
            return (resp, bytes([0x7E, 0x00]))
        if svc == 0x10 and len(req) >= 2 and req[1] == 0x03:
            return (resp, bytes([0x50, 0x03, 0x00, 0x32, 0x01, 0xF4]))
        if svc == 0x01 and len(req) >= 2:             # generic Mode 01
            pid = req[1]
            if pid == 0x0C:                           # 812.0 rpm
                return (resp, bytes([0x41, 0x0C, 0x0C, 0xB0]))
            if pid == 0x05:                           # 74 C
                return (resp, bytes([0x41, 0x05, 0x72]))
            if pid == 0x42:                           # 14.1 V
                return (resp, bytes([0x41, 0x42, 0x37, 0x14]))
            return (resp, None)
        if svc == 0x22 and len(req) >= 3:             # RDBI
            did = (req[1] << 8) | req[2]
            if did == 0xF190 and dst in (ENGINE, TCM):
                return (resp, bytes([0x62, 0xF1, 0x90]) + FAKE_VIN.encode())
            if dst == ENGINE and did == 0xF187:
                return (resp, bytes([0x62, 0xF1, 0x87]) + b"AA52-12A650-EC")
            if dst == 0x730 and did == 0xF187:
                return (resp, bytes([0x62, 0xF1, 0x87]) + b"XR8Q-5K698-BC")
            if dst == ENGINE and did in self.LIVE_DIDS:
                return (resp, bytes([0x62, did >> 8, did & 0xFF])
                        + bytes.fromhex(self.LIVE_DIDS[did]))
            if dst == ENGINE and did in self.NRC_OTHER:
                return (resp, bytes([0x7F, 0x22, self.NRC_OTHER[did]]))
            if dst == ENGINE and self.REFUSED_LO <= did <= self.REFUSED_HI:
                return (resp, bytes([0x7F, 0x22, 0x31]))
            return (resp, None)                       # everything else silent
        if svc == 0x19 and len(req) >= 3:             # fault read
            mask = req[2]
            if dst == ENGINE:
                if mask == 0xFF:
                    return (resp, bytes([0x7F, 0x19, 0x31]))
                return (resp, bytes([0x59, 0x02, mask,
                                     0x24, 0x9D, 0x00, 0x2F,   # P249D
                                     0x24, 0x63, 0x00, 0x28]))  # P2463
            return (resp, bytes([0x59, 0x02, mask]))  # TCM: clean
        return (resp, bytes([0x7F, svc, 0x11]))


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
    action_ids = [a["id"] for a in manifest["panel"]["actions"]]
    check("panel actions are live + the four X260 AdBlue tools",
          set(action_ids) == {"live", "discover", "sweep", "adblue-health",
                              "adblue-drive-log"}, str(action_ids))
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

    # --- X260 AdBlue tooling: discover / sweep / health / drive log ---------
    dcar = FakeX260Diesel()
    dsession = FakeElmSession(dcar)
    obd_service._with_session = lambda adapter, fn: fn(dsession, None)
    try:
        drep = jlr_discover.discover({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    check("discover ok", drep["ok"] is True, drep.get("message"))
    found = {f["address"]: f for f in drep["found"]}
    check("discover found PCM + TCM + the unexpected 0x730",
          set(found) == {"0x730", "0x7E0", "0x7E1"}, str(sorted(found)))
    check("discover swept 0x700-0x7FF minus the 8 impossible ids",
          drep["sweptAddresses"] == 248 and drep["skipped"] == 8,
          f"{drep['sweptAddresses']}/{drep['skipped']}")
    check("discover names the table rows and flags the stranger",
          found["0x7E0"]["known"].startswith("PCM")
          and found["0x7E1"]["known"].startswith("TCM")
          and found["0x730"]["known"] == "", str(found.get("0x730")))
    check("discover reports the UDS dialect from what ident proved",
          all(f["dialect"] == "uds" for f in drep["found"]),
          str([f["dialect"] for f in drep["found"]]))
    check("discover readings render as panel chips", len(drep["readings"]) == 3)
    # 0x730 used to come back as a bare "not in the shipped table" address. The
    # decrypted SDD 169 L494 map names it PSCM, so the sweep now reports the
    # module AND where the name came from -- the shipped table still outranks
    # SDD for the three rows it covers.
    check("discover names the stranger from the SDD map, marked as SDD-sourced",
          found["0x730"]["module"] == "PSCM"
          and found["0x730"]["source"].startswith("SDD 169")
          and found["0x7E0"]["source"] == "shipped table",
          str(found.get("0x730")))
    check("discover message says which addresses SDD named",
          "0x730" in drep["message"] and "SDD 169" in drep["message"],
          drep["message"])
    discover_services = {req[0] for _, req in dcar.sent}
    check("discover only ever sent 3E 00 / 22 (no fault services, no writes)",
          discover_services <= {0x3E, 0x22},
          str(sorted(hex(s) for s in discover_services)))

    sc = FakeX260Diesel()
    ssession = FakeElmSession(sc)
    obd_service._with_session = lambda adapter, fn: fn(ssession, None)
    try:
        sw = jlr_sweep.sweep({"name": "Fake adapter"}, "real", "0330", "0340")
    finally:
        obd_service._with_session = original
    check("sweep ok", sw["ok"] is True, sw.get("message"))
    check("sweep classified the answered DIDs in walk order",
          [a["did"] for a in sw["answered"]] == ["0332", "0339", "033E"],
          str(sw["answered"]))
    check("sweep kept payloads raw incl. 2- and 4-byte values",
          next(a["payload"] for a in sw["answered"] if a["did"] == "0332") == "00C4"
          and next(a["payload"] for a in sw["answered"] if a["did"] == "0339")
          == "00A100B4", str(sw["answered"]))
    check("sweep counted the NRC-31 refusals instead of listing them",
          sw["refused31"] == 8, str(sw["refused31"]))
    check("sweep listed the other NRC with its name",
          sw["nrcOther"] == [{"did": "033A", "nrc": "22",
                              "nrcName": uds.NRC_NAMES[0x22]}], str(sw["nrcOther"]))
    check("sweep counted the silent tail", sw["silent"] == 5, str(sw["silent"]))
    sweep_services = {req[0] for _, req in sc.sent}
    check("sweep only ever sent 3E 00 / 22 (guard + walk)",
          sweep_services <= {0x3E, 0x22},
          str(sorted(hex(s) for s in sweep_services)))

    wide = jlr_sweep.sweep({"name": "Fake adapter"}, "real", "0000", "2000")
    check("sweep refuses a range wider than 4096 ids",
          wide["ok"] is False and "max 4096" in wide["message"], wide["message"])
    badhex = jlr_sweep.sweep({"name": "Fake adapter"}, "real", "zz", "05FF")
    check("sweep refuses non-hex bounds",
          badhex["ok"] is False and "hex" in badhex["message"], badhex["message"])
    deadsession = FakeElmSession(FakeSilentJlr())
    obd_service._with_session = lambda adapter, fn: fn(deadsession, None)
    try:
        dsweep = jlr_sweep.sweep({"name": "Fake adapter"}, "real", "0330", "0340")
    finally:
        obd_service._with_session = original
    check("sweep refuses a dead target instead of blind-spraying",
          dsweep["ok"] is False and "dead address" in dsweep["message"],
          dsweep["message"])

    hcar = FakeX260Diesel()
    hsession = FakeElmSession(hcar)
    obd_service._with_session = lambda adapter, fn: fn(hsession, None)
    try:
        hrep = jlr_adblue.health({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    check("adblue health ok", hrep["ok"] is True, hrep.get("message"))
    hby = {m["address"]: m for m in hrep["modules"]}
    check("health read the PCM's P249D + P2463 through the scan ladder",
          [d["code"] for d in hby["0x7E0"]["dtcs"]] == ["P249D", "P2463"],
          str(hby["0x7E0"]["dtcs"]))
    check("health baseline taken FIRST (Mode 01 before anything else)",
          hcar.first_01 == 0 and hrep["baseline"] == {"engine_rpm": 812.0,
                                                      "coolant_c": 74.0,
                                                      "module_voltage": 14.1},
          str(hrep["baseline"]))
    vals = {v["did"]: v for v in hrep["values"]}
    check("health read the known engine DIDs raw with provenance",
          set(vals) == {"033E", "03E6", "03F2", "03F3", "0520"}
          and vals["03F3"]["payload"] == "58"
          and "old-jaguar-group" in vals["03F3"]["provenance"], str(sorted(vals)))
    # SDD 169 named the urea/SCR cluster, so "no public DID exists for any of
    # it" is no longer true and the status text no longer says so. What must
    # still hold: no invented decode (the harness's own SCR/DPF dids list stays
    # empty), the urea rows are listed as unverified, and what is genuinely
    # still missing is named rather than glossed over.
    check("health reports the SCR/DPF set honestly: urea named, soot/NOx still missing",
          hrep["candidates"]["dids"] == []
          and [r["did"] for r in hrep["candidates"]["ureaCluster"]]
          == ["05FA", "05FB", "05FC", "05FD"]
          and all(r["verified"] is False and r["scaling"] is None
                  for r in hrep["candidates"]["ureaCluster"])
          and "Still genuinely missing" in hrep["candidates"]["status"],
          str(hrep["candidates"]["status"]))
    # External-research candidates ride along, provenance-flagged and never
    # verified -- the honesty contract for pre-contact seed data.
    ext = hrep["candidates"].get("external", [])
    check("external candidates carry provenance + verified=false",
          bool(ext)
          and all(e.get("provenance") and e.get("verified") is False for e in ext)
          and any(e["did"] == "05FA" for e in ext),
          str(ext)[:120])
    check("health assessment matched the aftertreatment codes it actually read",
          set(hrep["assessment"]["matchedCodes"]) == {"P249D", "P2463"},
          str(hrep["assessment"]["matchedCodes"]))
    notes = " ".join(hrep["assessment"]["notes"])
    check("health assessment carries the sourced fault space + regen thresholds",
          len(hrep["assessment"]["faultSpace"]) == 4
          and set(hrep["assessment"]["expectedCodes"])
          == {"P204F", "P249D", "P2463", "P2201"}
          and "~26" in notes and "~46" in notes,
          str(hrep["assessment"]["regenThresholds"]))
    health_services = {req[0] for _, req in hcar.sent}
    check("health only ever sent 01 / 3E / 22 / 19 (baseline + scan ladder)",
          health_services <= {0x01, 0x3E, 0x22, 0x19},
          str(sorted(hex(s) for s in health_services)))

    lcar = FakeX260Diesel()
    lsession = FakeElmSession(lcar)
    obd_service._with_session = lambda adapter, fn: fn(lsession, None)
    try:
        lrep = jlr_adblue.drive_log({"name": "Fake adapter"}, "real", 0.5)
    finally:
        obd_service._with_session = original
    check("drive log ok with a reported sample rate",
          lrep["ok"] is True and lrep["sampleRate"] > 0, str(lrep.get("sampleRate")))
    check("drive log pairs rpm with raw DID payloads",
          len(lrep["samples"]) > 1
          and all("engine_rpm" in s and "did_03F3" in s for s in lrep["samples"][:5])
          and lrep["samples"][0]["did_03F3"] == "58",
          str(lrep["samples"][:1]))
    check("drive log rides the every-fifth-tick generic baseline",
          "coolant_c" in lrep["samples"][0] and "coolant_c" not in lrep["samples"][1],
          str(lrep["samples"][:2]))
    log_services = {req[0] for _, req in lcar.sent}
    check("drive log only ever sent 01 / 22",
          log_services <= {0x01, 0x22}, str(sorted(hex(s) for s in log_services)))

    deadsession2 = FakeElmSession(FakeSilentJlr(), vin_over_obd=False)
    obd_service._with_session = lambda adapter, fn: fn(deadsession2, None)
    try:
        dlog = jlr_adblue.drive_log({"name": "Fake adapter"}, "real", 30)
    finally:
        obd_service._with_session = original
    check("drive log ends early when the bus goes quiet",
          "ended early" in dlog["message"]
          and len(dlog["samples"]) == jlr_adblue._DEAD_TICKS, dlog["message"])

    for label, res in (("discover", jlr_discover.discover(None, "sim")),
                       ("sweep", jlr_sweep.sweep(None, "sim")),
                       ("health", jlr_adblue.health(None, "sim")),
                       ("drive log", jlr_adblue.drive_log(None, "sim"))):
        check(f"sim {label} ok+simulated+labelled",
              res["ok"] and res["simulated"] and "SIMULATED" in res["message"])

    # The desktop extras panel renders readings chips + a worksheet copy-out
    # block; sweep and health now carry both (discover always did).
    for label, res in (("sweep", jlr_sweep.sweep(None, "sim")),
                       ("health", jlr_adblue.health(None, "sim"))):
        check(f"sim {label} carries panel readings + worksheet",
              isinstance(res.get("readings"), list) and bool(res["readings"])
              and isinstance(res.get("worksheet"), str) and bool(res["worksheet"]),
              f"{len(res.get('readings', []))} chips")
    # The drive log's default DID set includes the live external candidates and
    # excludes 042C -- SDD 169 names it (DPF soot g/l) but this L494 calibration
    # does not answer it, so it is not polled (see data/external_pids.json).
    dsim = jlr_adblue.drive_log(None, "sim")
    dsim_dids = dsim.get("dids", [])
    check("drive log default dids include the live SCR/urea DIDs, not dead 042C",
          "05FA" in dsim_dids and "042C" not in dsim_dids, str(dsim_dids))

    # --- the decrypted SDD 169 layer, and that it reaches a screen ----------
    from backend.profiles.jlr import sdd as jlr_sdd

    sref = jlr_sdd.reference()
    check("SDD layer loaded the whole extract",
          sref["modules"] == 57 and sref["dids"] == 37 and sref["routines"] == 24,
          f"{sref['modules']}/{sref['dids']}/{sref['routines']}")
    check("SDD address map settles the modules the sweep could not name",
          (jlr_sdd.module_code(0x7E6), jlr_sdd.module_code(0x744),
           jlr_sdd.module_code(0x751), jlr_sdd.module_code(0x795))
          == ("ABS", "DSM", "TPM", "RDCM"),
          jlr_sdd.module_code(0x7E6))
    check("SDD scaling reproduces the live L494 payloads it was checked against",
          jlr_sdd.decode_did(0x0396, bytes.fromhex("5AB6"))[:2] == ("23222", "km")
          and jlr_sdd.decode_did(0xF405, bytes.fromhex("6E"))[:2] == ("70", "°C"),
          str(jlr_sdd.decode_did(0x0396, bytes.fromhex("5AB6"))))
    check("SDD grades the cross-checked rows verified and the rest candidate",
          jlr_sdd.PCM_DIDS[0x0396].confidence == "verified"
          and all(jlr_sdd.PCM_DIDS[d].confidence == "candidate"
                  for d in jlr_sdd.UREA_DIDS),
          str([jlr_sdd.PCM_DIDS[d].confidence for d in jlr_sdd.UREA_DIDS]))
    check("SDD invents no scaling for the urea cluster",
          all(jlr_sdd.PCM_DIDS[d].decode is None for d in jlr_sdd.UREA_DIDS)
          and all(jlr_sdd.decode_did(d, b"\x00\x00")[0] is None
                  for d in jlr_sdd.UREA_DIDS))
    # The rule the whole layer turns on: a candidate is never a bare number.
    urea_row = jlr_pids.sdd_candidate_reading(0x05FC, bytes.fromhex("00039CC5"))
    check("a candidate reading always shows its raw payload and its grade",
          "[candidate]" in urea_row["name"] and "raw 00039CC5" in urea_row["value"],
          f"{urea_row['name']} = {urea_row['value']}")
    soot_row = jlr_pids.sdd_candidate_reading(0x042C, bytes.fromhex("3CE6"))
    check("an SDD-scaled candidate shows raw AND decode, decode never alone",
          soot_row["value"].startswith("raw 3CE6")
          and "23.789 g/l" in soot_row["value"], soot_row["value"])
    check("SDD routines are listed but no routine service is ever sent",
          len(jlr_sdd.ROUTINES) == 24
          and any(r.routine == 0x4076 for r in jlr_sdd.ROUTINES)
          and "never sent" in jlr_sdd._worksheet(),
          str(len(jlr_sdd.ROUTINES)))

    # --- the urea watch preset: the next real-world step, one press away ----
    check("drive log offers the urea watch preset",
          [p["id"] for p in jlr_adblue.presets()] == ["urea", "engine"],
          str([p["id"] for p in jlr_adblue.presets()]))
    upre = next(p for p in jlr_adblue.presets() if p["id"] == "urea")
    check("urea preset watches exactly 05FA-05FD",
          upre["dids"] == ["05FA", "05FB", "05FC", "05FD"], str(upre["dids"]))
    ulog = jlr_adblue.drive_log(None, "sim", 60.0, ["urea"])
    check("the preset name expands to its DIDs in the drive log",
          ulog["dids"] == ["05FA", "05FB", "05FC", "05FD"], str(ulog["dids"]))
    check("an ordinary hex DID list still passes through untouched",
          jlr_adblue.drive_log(None, "sim", 60.0, ["05FC"])["dids"] == ["05FC"])

    # --- MCP layer: the four new tools + honest no-adapter errors -----------
    from backend import mcp_server

    check("MCP tools registered",
          all(hasattr(mcp_server, n) for n in
              ("jlr_discover_modules", "jlr_engine_sweep",
               "jlr_adblue_health", "jlr_adblue_drive_log")))
    for tool in (mcp_server.jlr_discover_modules, mcp_server.jlr_engine_sweep,
                 mcp_server.jlr_adblue_health, mcp_server.jlr_adblue_drive_log):
        err = tool(None, "real")
        check("MCP real mode with no adapter gives the honest pick-an-adapter error",
              isinstance(err, dict)
              and "Pick an ELM327 adapter" in err.get("error", ""), str(err))
    msim = mcp_server.jlr_adblue_health(None, "sim")
    check("MCP sim mode returns the labelled fake", msim.get("simulated") is True)

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
    rdisc = tc.get("/api/jlr/discover?mode=sim").json()
    check("API sim discover over the dongle discover.py path",
          rdisc.get("ok") and rdisc.get("simulated"), str(rdisc)[:120])
    rswp = tc.get("/api/jlr/sweep?mode=sim&first=0300&last=031F").json()
    check("API sim sweep over the generic sweep route",
          rswp.get("ok") and rswp.get("simulated"), str(rswp)[:120])
    rhlth = tc.get("/api/jlr/adblue-health?mode=sim").json()
    check("API sim adblue health route",
          rhlth.get("ok") and rhlth.get("simulated"), str(rhlth)[:120])
    rlog = tc.get("/api/jlr/adblue-drive-log?mode=sim&seconds=5").json()
    check("API sim adblue drive-log route",
          rlog.get("ok") and rlog.get("simulated"), str(rlog)[:120])
    # Sim runs must never be stamped as saved -- fake data stays out of
    # captures/jlr/ (the dataset is real-car only).
    check("sim drive log is NOT saved to captures",
          "savedTo" not in rlog and "savedCsv" not in rlog, str(rlog)[:80])
    rbadsec = tc.get("/api/jlr/adblue-drive-log?mode=sim&seconds=1")
    check("API drive-log seconds below 5 is an honest 400",
          rbadsec.status_code == 400, str(rbadsec.status_code))
    rbaddid = tc.get("/api/jlr/adblue-drive-log?mode=sim&dids=042C,zz")
    check("API drive-log non-hex dids is an honest 400",
          rbaddid.status_code == 400, str(rbaddid.status_code))
    rlogd = tc.get("/api/jlr/adblue-drive-log?mode=sim&seconds=5&dids=042C").json()
    check("API drive-log dids param reaches the log",
          rlogd.get("ok") and rlogd.get("dids") == ["042C"], str(rlogd.get("dids")))
    rbad = tc.get("/api/jlr/sweep?mode=real")
    check("API sweep without adapter in real mode is an honest 400",
          rbad.status_code == 400, str(rbad.status_code))

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("ALL JLR SELF-TEST CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
