"""Self-test for the Mercedes-Benz UDS+KWP-over-ELM327 stack, no car required.

Run:  python -m backend.tools.mercedes_selftest

Drives the REAL client code -- Elm327UdsClient, backend.core.uds.*, the mercedes
scan/pids/resets modules and their id table -- against fake Mercedes cars that
answer over a fake ELM327 session. It proves the parts that make the Mercedes
profile honest and correct:

  * the table is engine (0x7E0/0x7E8) + transmission (0x7E1/0x7E9) ONLY, both on
    the standard +8 block -- no proprietary body-module rows (STAR-only),
  * VIN read FIRST over plain OBD Mode 09,
  * the UDS 19 02 mask ladder (0xFF refused -> 0x08) + the 10 03 extended-session
    escalation on a session NRC,
  * the KWP2000-over-CAN fallback on a KWP-era module: 10 92 MB session, KWP
    ident 1A 87, KWP DTC read 18 02 FF FF (decoded to a code), KWP clear 14 FF 00,
  * the engine 01 00 fallback + generic Mode 03 codes, and the honest
    "nothing answered Mercedes diag -> DoIP-era / pre-2004" note,
  * UDS DTC decode + clear 14 FFFFFF,
  * the E-Class hybrid live tier (0x7E5/0x7E2 DID set decoded against real
    OBDb vectors) + the verified ATF extra + a generic Mode-01 read,
  * the reset catalogue (ASSYST procedures + SBC safety warning),
  * the generic FastAPI brand routes actually answer for Mercedes.

Exits 0 on PASS, 1 on FAIL.
"""

import json
import pathlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core import uds  # noqa: E402
from backend.obd import service as obd_service  # noqa: E402
from backend.obd.elm327 import ObdError  # noqa: E402
from backend.obd.uds_elm327 import Elm327UdsClient, reassemble  # noqa: E402
from backend.profiles.mercedes import pids as mb_pids  # noqa: E402
from backend.profiles.mercedes import resets as mb_resets  # noqa: E402
from backend.profiles.mercedes import scan as mb_scan  # noqa: E402

FAKE_VIN = "WDD2040011A000001"
ENGINE, TRANS = 0x7E0, 0x7E1


class FakeMercedesCar:
    """A tiny Mercedes brain keyed by request (TX) id. The engine is a UDS
    module whose DTC read needs the mask ladder (FF refused) AND a 10 03 extended
    session before 19 02 08 answers. The transmission is a KWP2000-over-CAN
    module: every UDS read refuses, only the MB session 10 92 opens it, and it
    answers KWP ident (1A 87), KWP DTCs (18 02 FF FF) and KWP clear (14 FF 00).
    Response = request + 8 for both (Mercedes standard block)."""

    PRESENT = {ENGINE, TRANS}

    def __init__(self):
        self.ext_started = []   # tx ids that got a 10 03 (UDS extended) session
        self.kwp_started = []   # tx ids that got a 10 92 (MB KWP) session
        self.cleared = []       # tx ids that got a ClearDiagnosticInformation
        self.masks = {}         # tx -> list of 19 02 masks asked
        self.uds_session = set()
        self.kwp_session = set()

    def resp_id(self, tx):
        return tx + 8 if tx in self.PRESENT else None

    def handle(self, tx, req):
        resp = self.resp_id(tx)
        if tx not in self.PRESENT or not req:
            return (resp, None)
        if tx == ENGINE:
            return (resp, self._engine(req))
        return (resp, self._trans(req))

    def _engine(self, req):
        svc = req[0]
        if svc == 0x3E:                                   # TesterPresent
            return bytes([0x7E, 0x00])
        if svc == 0x10 and len(req) >= 2:                 # session control
            if req[1] == 0x03:                            # UDS extended session
                self.uds_session.add(ENGINE)
                self.ext_started.append(ENGINE)
                return bytes([0x50, 0x03, 0x00, 0x32, 0x01, 0xF4])
            return bytes([0x7F, 0x10, 0x12])
        if svc == 0x22 and len(req) >= 3:                 # ReadDataByIdentifier
            did = (req[1] << 8) | req[2]
            if did == 0xF190:
                return bytes([0x62, 0xF1, 0x90]) + FAKE_VIN.encode()
            return bytes([0x7F, 0x22, 0x31])              # out of range
        if svc == 0x19 and len(req) >= 3:                 # ReadDTCInformation
            mask = req[2]
            self.masks.setdefault(ENGINE, []).append(mask)
            if mask == 0x08:                              # confirmed mask
                if ENGINE in self.uds_session:
                    return bytes([0x59, 0x02, 0x08, 0x01, 0x70, 0x00, 0x08])  # P0170
                return bytes([0x7F, 0x19, 0x22])          # conditions -> need session
            return bytes([0x7F, 0x19, 0x31])              # FF/AF out of range -> next mask
        if svc == 0x14:                                   # ClearDiagnosticInformation
            self.cleared.append(ENGINE)
            return bytes([0x54])
        return bytes([0x7F, svc, 0x11])

    def _trans(self, req):
        svc = req[0]
        if svc == 0x3E:                                   # TesterPresent (present)
            return bytes([0x7E, 0x00])
        if svc == 0x10 and len(req) >= 2:                 # session control
            if req[1] == 0x92:                            # MB KWP session -- only this opens
                self.kwp_session.add(TRANS)
                self.kwp_started.append(TRANS)
                return bytes([0x50, 0x92])
            return bytes([0x7F, 0x10, 0x12])              # 10 03 / 10 81 refused
        if svc == 0x22:                                   # KWP module: no UDS reads
            return bytes([0x7F, 0x22, 0x11])
        if svc == 0x19:                                   # KWP module: no UDS DTC
            return bytes([0x7F, 0x19, 0x11])
        if svc == 0x1A and len(req) >= 2:                 # KWP readEcuIdentification
            if req[1] == 0x87 and TRANS in self.kwp_session:
                return bytes([0x5A, 0x87]) + b"A0325458832"
            return bytes([0x7F, 0x1A, 0x11])
        if svc == 0x18:                                   # KWP readDTCByStatus 18 02 FF FF
            if TRANS in self.kwp_session:
                return bytes([0x58, 0x01, 0x07, 0x29, 0x60])   # P0729
            return bytes([0x7F, 0x18, 0x11])
        if svc == 0x14 and len(req) >= 3:                 # clear
            if req[1] == 0xFF and req[2] == 0x00:         # KWP clear 14 FF 00
                self.cleared.append(TRANS)
                return bytes([0x54])
            return bytes([0x7F, 0x14, 0x11])              # UDS clear 14 FF FF FF refused
        return bytes([0x7F, svc, 0x11])


class FakeMercedesObdOnlyCar:
    """A DoIP-era / pre-2004 stand-in: the engine answers ONLY generic OBD
    (Mode 01 PID 00, Mode 03) and no manufacturer UDS/KWP; the transmission is
    silent. Proves the engine 01 00 fallback + Mode 03 codes and the honest
    'nothing answered Mercedes diag -> DoIP/pre-2004' note."""

    PRESENT = {ENGINE}

    def resp_id(self, tx):
        return tx + 8 if tx in self.PRESENT else None

    def handle(self, tx, req):
        resp = self.resp_id(tx)
        if tx != ENGINE or not req:
            return (resp, None)
        if req[0] == 0x01 and len(req) >= 2 and req[1] == 0x00:
            return (resp, bytes([0x41, 0x00, 0xBE, 0x3E, 0xB8, 0x11]))
        return (resp, None)                               # UDS/KWP all silent


class FakePidCar:
    """Answers just the two live-extra reads: KWP 21 30 ATF temp on the gearbox
    (0x70 -> 112-50 = 62 C) and generic Mode-01 coolant (0x80 -> 128-40 = 88 C)."""

    PRESENT = {ENGINE, TRANS}

    def resp_id(self, tx):
        return tx + 8 if tx in self.PRESENT else None

    def handle(self, tx, req):
        resp = self.resp_id(tx)
        if not req:
            return (resp, None)
        if tx == ENGINE and req[0] == 0x01 and len(req) >= 2:
            if req[1] == 0x05:                            # coolant PID
                return (resp, bytes([0x41, 0x05, 0x80]))
            return (resp, None)                           # other PIDs unsupported
        if tx == TRANS and req[:2] == bytes([0x21, 0x30]):
            return (resp, bytes([0x61, 0x30, 0x70]))      # KWP 21 30 ATF temp
        return (resp, None)


class FakeMercedesEclassCar:
    """An E-Class mild-hybrid (W/S 213): answers the OBDb-verified UDS 0x22 DID
    set over the standard +8 pairing -- hybrid DIDs on 0x7E5 (resp 0x7ED),
    chassis group on 0x7E2 (resp 0x7EA). Replays REAL OBDb test_cases payloads:

      * 22 60 50 -> 62 60 50 10 68            HV SoC 42.0 %
      * 22 60 71 -> 62 60 71 03 E8            converter 25.00 V
      * 22 20 05 -> 62 20 05 64               aux battery 10.16 V
      * 22 20 02 -> 62 20 02 ... (5-frame)    steering -176.1, yaw, pressures...
      * 22 20 01 -> 62 20 01 ... (3-frame)    wheel speeds 12.71/13.73/12.43/13.22
    """

    PRESENT = {0x7E5, 0x7E2}

    # OBDb 2018 7E2.7EA.222002 vector #1 (reassembled, 34 bytes).
    DID_2002 = bytes.fromhex(
        "622002000000000001FFE701F91F0100F90101060100C401B961B7B800FC36011753")
    # OBDb 2018 7E2.7EA.222001 vector #4 (reassembled, 20 bytes).
    DID_2001 = bytes.fromhex(
        "62200100E200F400DD00EBA50000010126BBC2F6")

    def resp_id(self, tx):
        return tx + 8 if tx in self.PRESENT else None

    def handle(self, tx, req):
        resp = self.resp_id(tx)
        if not req or tx not in self.PRESENT:
            return (resp, None)
        if req[0] != 0x22 or len(req) < 3:
            return (resp, None)                           # only UDS 22 reads
        did = (req[1] << 8) | req[2]
        if tx == 0x7E5:
            if did == 0x6050:
                return (resp, bytes.fromhex("6260501068"))
            if did == 0x6071:
                return (resp, bytes.fromhex("62607103E8"))
            if did == 0x2005:
                return (resp, bytes.fromhex("62200564"))
            return (resp, None)
        if tx == 0x7E2:
            if did == 0x2002:
                return (resp, self.DID_2002)
            if did == 0x2001:
                return (resp, self.DID_2001)
            return (resp, None)
        return (resp, None)


class FakeElmSession:
    """Just enough of an Elm327Session for the UDS client AND the generic OBD
    helpers (query / _read_vin / read_codes). Models the ELM's receive filter: a
    car frame is only delivered when the current ATCRA matches the module's
    response id -- so a wrong filter yields NO DATA."""

    def __init__(self, car):
        self.car = car
        self.at_log = []
        self.aimed = None    # ATSH request id
        self.cra = None      # ATCRA receive filter
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
            try:
                self.aimed = int(cmd[4:], 16)
            except ValueError:
                pass
        elif cmd == "ATCRA":
            self.cra = None
        elif cmd.startswith("ATCRA"):
            try:
                self.cra = int(cmd[5:], 16)
            except ValueError:
                self.cra = None
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
        if mode == 0x09 and pid == 0x02:                 # VIN
            return [bytes([0x49, 0x02, 0x01]) + FAKE_VIN.encode()]
        if mode == 0x03:                                 # stored DTCs (CAN framing)
            return [bytes([0x43, 0x01, 0x01, 0x71])]     # -> P0171
        raise ObdError("no data", kind="no_data")


from backend.profiles.mercedes import coldstart as mb_cold  # noqa: E402
from backend.tools import mercedes_coldstart_session as mb_session  # noqa: E402
from backend.profiles.mercedes import routines as mb_routines  # noqa: E402


def mb_routines_dtcs():
    return mb_routines.TESTS.get("dtcs", [])


def main() -> int:
    failures = []

    def check(name, cond, extra=""):
        print(("  PASS  " if cond else "  FAIL  ") + name + (f"  ({extra})" if extra else ""))
        if not cond:
            failures.append(name)

    # --- static: the Mercedes id table + profile manifest --------------------
    table = mb_scan.MERCEDES_ECU_TABLE
    id_map = mb_scan.MERCEDES_ID_MAP
    txs = [row[0] for row in table]
    rxs = [row[1] for row in table]
    check("ID table is engine + transmission only", len(table) == 2, str(len(table)))
    check("all request ids unique", len(set(txs)) == len(txs))
    check("all response ids unique", len(set(rxs)) == len(rxs))
    check("engine 0x7E0 -> 0x7E8 (standard +8)", id_map.get(0x7E0) == 0x7E8)
    check("transmission 0x7E1 -> 0x7E9 (standard +8)", id_map.get(0x7E1) == 0x7E9)
    check("every row is standard +8 (no proprietary low ids)",
          all(rx == tx + 8 for tx, rx, _, _, _ in table))
    check("no body-module row (STAR-only, deliberately not scanned)",
          set(txs) == {0x7E0, 0x7E1})

    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "profiles" / "mercedes" / "profile.json")
        .read_text(encoding="utf-8-sig")
    )
    check("profile.json id == mercedes", manifest.get("id") == "mercedes")
    check("profile.json name == Mercedes-Benz", manifest.get("name") == "Mercedes-Benz")
    check("panel has exactly one action (live)",
          len(manifest["panel"]["actions"]) == 1
          and manifest["panel"]["actions"][0]["id"] == "live")
    check("panel hint states body modules are STAR-only", "STAR" in manifest["panel"]["hint"])

    # --- static: simulated scan/clear shape ---------------------------------
    sim = mb_scan.scan(None, "sim")
    check("sim scan ok+simulated", sim["ok"] and sim["simulated"])
    check("sim scan has 2 modules", len(sim["modules"]) == 2, str(len(sim["modules"])))
    check("sim module carries address+response",
          all("address" in m and "response" in m for m in sim["modules"]))
    check("sim scan labelled SIMULATED", "SIMULATED" in sim["message"])
    simclear = mb_scan.clear(None, "sim")
    check("sim clear simulated", simclear["simulated"] is True)

    # --- reassemble: single frame, kept negative, no-data -------------------
    rec = reassemble("59 02 08 01 70 00 08\r>", "190208")
    check("reassemble single frame",
          rec and rec[0] == bytes([0x59, 0x02, 0x08, 0x01, 0x70, 0x00, 0x08]))
    neg = reassemble("7F 19 22\r>", "190208")
    check("reassemble keeps 0x7F negative", neg and neg[0] == bytes([0x7F, 0x19, 0x22]))
    try:
        reassemble("NO DATA\r>", "190208")
        no_data_raised = False
    except ObdError as exc:
        no_data_raised = exc.kind == "no_data"
    check("reassemble NO DATA -> ObdError(no_data)", no_data_raised)

    # --- integration: real client + uds helpers vs the fake Mercedes car ----
    car = FakeMercedesCar()
    session = FakeElmSession(car)
    client = Elm327UdsClient(session, id_map)
    client.begin()

    # (a) engine present + aimed at the standard +8 receive id.
    check("engine present via 3E 00", mb_scan._probe(client, ENGINE, []))
    check("engine aims +8 rx (ATCRA7E8)", "ATCRA7E8" in session.at_log)

    eng_ident = mb_scan._read_ident(client, ENGINE, [])
    eng_vin = next((i["value"] for i in eng_ident if i["label"] == "VIN"), None)
    check("engine VIN read from F190", eng_vin == FAKE_VIN, str(eng_vin))

    # (b) mask ladder (FF refused) + 10 03 session escalation.
    eng_trace = []
    eng_dtcs = mb_scan._read_dtcs(client, ENGINE, eng_trace)
    check("engine fault recovered via mask ladder + session", len(eng_dtcs) == 1, str(len(eng_dtcs)))
    check("engine mask ladder tried FF then 08",
          0xFF in car.masks.get(ENGINE, []) and 0x08 in car.masks.get(ENGINE, []),
          str(car.masks.get(ENGINE)))
    check("engine 10 03 extended session opened", ENGINE in car.ext_started)
    if eng_dtcs:
        check("engine DTC decodes P0170", eng_dtcs[0]["code"] == "P0170", eng_dtcs[0]["code"])

    # (c) KWP2000-over-CAN fallback on the transmission.
    tcm_trace = []
    tcm_dtcs = mb_scan._read_dtcs(client, TRANS, tcm_trace)
    check("TCM faults via KWP 10 92 + 18 02 FF FF", len(tcm_dtcs) == 1, str(len(tcm_dtcs)))
    check("MB KWP session 10 92 opened for TCM", TRANS in car.kwp_started)
    check("trace records the 18 02 FF FF KWP read",
          any("18 02 FF FF" in line for line in tcm_trace), " | ".join(tcm_trace))
    if tcm_dtcs:
        check("TCM DTC decodes P0729", tcm_dtcs[0]["code"] == "P0729", tcm_dtcs[0]["code"])
        check("TCM DTC flagged as KWP", tcm_dtcs[0]["flags"] == ["stored (KWP)"],
              ",".join(tcm_dtcs[0]["flags"]))

    tcm_ident = mb_scan._read_ident(client, TRANS, [])
    check("TCM ident via KWP 1A 87",
          any(i["value"] == "A0325458832" for i in tcm_ident), str(tcm_ident))

    # (d) UDS clear on the engine.
    uds.clear_dtcs(client, ENGINE)
    check("clear accepted on engine (14 FF FF FF)", ENGINE in car.cleared)

    client.restore()
    check("restore hands ELM back to generic stack (ATSH7DF)", "ATSH7DF" in session.at_log)

    # (e) KWP clear fallback (UDS clear refused -> 10 92 + 14 FF 00) via clear().
    car_c = FakeMercedesCar()
    sc = FakeElmSession(car_c)
    original = obd_service._with_session
    obd_service._with_session = lambda adapter, fn: fn(sc, None)
    try:
        cres = mb_scan.clear({"name": "Fake adapter"}, "real", "0x7E1")
    finally:
        obd_service._with_session = original
    check("TCM clear falls back to KWP 14 FF 00",
          "0x7E1" in cres.get("cleared", []) and TRANS in car_c.cleared, str(cres))

    # --- integration: full scan() end to end (VIN-first + both eras) --------
    car2 = FakeMercedesCar()
    session2 = FakeElmSession(car2)
    obd_service._with_session = lambda adapter, fn: fn(session2, None)
    try:
        report = mb_scan.scan({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    check("full scan ok", report["ok"] is True, report.get("message"))
    by_addr = {m["address"]: m for m in report["modules"]}
    check("VIN surfaced (Mode 09 first)", report["vehicle"]["vin"] == FAKE_VIN,
          str(report["vehicle"]))
    check("engine present (UDS)", by_addr["0x7E0"]["present"] is True)
    check("engine fault P0170 in full scan",
          [d["code"] for d in by_addr["0x7E0"]["dtcs"]] == ["P0170"], str(by_addr["0x7E0"]["dtcs"]))
    check("transmission present (KWP)", by_addr["0x7E1"]["present"] is True)
    check("transmission fault P0729 (KWP) in full scan",
          [d["code"] for d in by_addr["0x7E1"]["dtcs"]] == ["P0729"], str(by_addr["0x7E1"]["dtcs"]))
    check("two modules answered", "2 module(s) answered" in report["message"], report["message"])
    check("message states body modules are STAR-only",
          "STAR/XENTRY-only" in report["message"], report["message"])
    check("no false VIN-mismatch warning", "MISMATCH" not in report["message"], report["message"])
    check("no DoIP note when modules answered", "DoIP-era" not in report["message"], report["message"])

    # --- integration: OBD-only car -> engine 01 00 fallback + DoIP note -----
    car3 = FakeMercedesObdOnlyCar()
    session3 = FakeElmSession(car3)
    obd_service._with_session = lambda adapter, fn: fn(session3, None)
    try:
        report2 = mb_scan.scan({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    by3 = {m["address"]: m for m in report2["modules"]}
    check("OBD-only scan ok", report2["ok"] is True, report2.get("message"))
    check("VIN via Mode 09 on OBD-only car", report2["vehicle"]["vin"] == FAKE_VIN,
          str(report2["vehicle"]))
    check("engine present via 01 00 OBD fallback", by3["0x7E0"]["present"] is True)
    check("engine trace records the OBD fallback",
          any("OBD" in line for line in by3["0x7E0"]["trace"]), " | ".join(by3["0x7E0"]["trace"]))
    check("engine fault via Mode 03 fallback decodes P0171",
          [d["code"] for d in by3["0x7E0"]["dtcs"]] == ["P0171"], str(by3["0x7E0"]["dtcs"]))
    check("transmission silent on OBD-only car", by3["0x7E1"]["present"] is False)
    check("DoIP / pre-2004 honesty note shown", "DoIP-era" in report2["message"], report2["message"])

    # --- pids: sim + the verified ATF extra + a generic Mode-01 decode ------
    lsim = mb_pids.live(None, "sim")
    check("sim live ok+simulated+labelled",
          lsim["ok"] and lsim["simulated"] and "SIMULATED" in lsim["message"])

    psession = FakeElmSession(FakePidCar())
    obd_service._with_session = lambda adapter, fn: fn(psession, None)
    try:
        lreal = mb_pids.live({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    got = {r["name"]: r["value"] for r in lreal["readings"]}
    check("ATF temp decodes to 62 C via KWP 21 30",
          got.get("Transmission (722.9/9G) ATF oil temp") == "62", str(got))
    check("coolant Mode-01 decodes to 88",
          got.get("Coolant temperature") == "88", str(got))
    atf = next((r for r in lreal["readings"] if "ATF" in r["name"]), None)
    check("ATF reading carries the -50 vs -40 caveat",
          atf is not None and "-50" in atf.get("description", "")
          and "confirm" in atf.get("description", "").lower(), str(atf))

    # --- pids tier 0: E-Class hybrid DID set decodes the OBDb vectors --------
    from backend.profiles.mercedes import eclass as mb_eclass  # noqa: E402

    pkg = Path(__file__).resolve().parents[1] / "profiles" / "mercedes"
    ecjson = json.loads((pkg / "obdb_eclass_signalset.json").read_text(encoding="utf-8"))
    check("bundled E-Class signalset JSON parses",
          bool(ecjson.get("commands")), str(len(ecjson.get("commands", []))))
    check("E-Class signalset carries the unverified 226053 note (not wired)",
          any(c.get("verified") is False and "NOT WIRED" in c.get("note", "")
              for c in ecjson["commands"]))
    check("bundled E-Class signalset NOTICE exists",
          (pkg / "obdb_eclass_signalset.NOTICE.txt").exists())

    ecar = FakeMercedesEclassCar()
    esession = FakeElmSession(ecar)
    obd_service._with_session = lambda adapter, fn: fn(esession, None)
    try:
        elive = mb_pids.live({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    egot = {r["name"]: r["value"] for r in elive["readings"]}
    check("E-Class live ok (Tier 0 answered)", elive["ok"] is True, elive.get("message"))
    check("E-Class gate probe is on the hybrid HV-SoC DID",
          mb_eclass._GATE == (0x7E5, 0x6050), str(mb_eclass._GATE))
    check("HV battery SoC decodes to 42.0 %", egot.get("HV battery state of charge") == "42.0",
          str(egot.get("HV battery state of charge")))
    check("converter requested voltage decodes to 25.00 V",
          egot.get("HV converter requested voltage") == "25.00",
          str(egot.get("HV converter requested voltage")))
    check("aux battery voltage decodes to 10.16 V",
          egot.get("Auxiliary battery voltage") == "10.16",
          str(egot.get("Auxiliary battery voltage")))
    check("steering angle decodes to -176.1 deg",
          egot.get("Steering angle") == "-176.1", str(egot.get("Steering angle")))
    check("yaw rate decodes to -0.0015 rad/s",
          egot.get("Yaw rate") == "-0.0015", str(egot.get("Yaw rate")))
    check("longitudinal acceleration decodes to 0.000",
          egot.get("Longitudinal acceleration") == "0.000",
          str(egot.get("Longitudinal acceleration")))
    check("brake master-cylinder pressure decodes to 3.81 bar",
          egot.get("Brake master-cylinder pressure") == "3.81",
          str(egot.get("Brake master-cylinder pressure")))
    check("brake vacuum-brake pressure decodes to 0.196 bar",
          egot.get("Brake vacuum-brake pressure") == "0.196",
          str(egot.get("Brake vacuum-brake pressure")))
    check("wheel speeds decode 12.71/13.72/12.43/13.22",
          egot.get("Wheel speed, front-left") == "12.71"
          and egot.get("Wheel speed, front-right") == "13.72"
          and egot.get("Wheel speed, rear-left") == "12.43"
          and egot.get("Wheel speed, rear-right") == "13.22",
          str({k: egot[k] for k in egot if k.startswith("Wheel speed")}))
    check("E-Class caveat rides with the readings",
          any(r["name"] == "Note" and "CC-BY-SA-4.0" in r.get("value", "")
              for r in elive["readings"]))
    check("non-hybrid still falls through to ATF + generic",
          lreal["ok"] is True and not any("Wheel speed" in r["name"] for r in lreal["readings"]))
    check("sim live includes the E-Class hybrid values",
          any(r["name"] == "HV battery state of charge" for r in lsim["readings"]))
    check("E-Class set has no unverified HV-current (226053) reading wired",
          not any("HV current" in r["name"] for r in elive["readings"]))

    # --- resets: ASSYST procedures + SBC safety warning ---------------------
    cat = mb_resets.list_resets()
    check("resets brand == mercedes", cat["brand"] == "mercedes")
    check("eight reset entries shipped", len(cat["resets"]) == 8, str(len(cat["resets"])))
    check("every reset has method=procedure",
          all(r["method"] == "procedure" for r in cat["resets"]))
    check("every reset carries a confidence + source + procedure steps",
          all(r.get("confidence") and r.get("source") and r.get("procedure")
              for r in cat["resets"]))
    conf = {r["id"]: r["confidence"] for r in cat["resets"]}
    check("W204/W212/W211/W203 resets are verified",
          conf.get("mercedes-assyst-w204") == "verified"
          and conf.get("mercedes-assyst-w212") == "verified"
          and conf.get("mercedes-assyst-w211") == "verified"
          and conf.get("mercedes-assyst-w203-menu") == "verified"
          and conf.get("mercedes-assyst-w203-button") == "verified", str(conf))
    check("W210 reset is community", conf.get("mercedes-assyst-w210") == "community")
    check("ASSYST-PLUS family reset is experimental (UNVERIFIED-plausible)",
          conf.get("mercedes-assyst-plus-family") == "experimental")
    sbc = next((r for r in cat["resets"] if r["id"] == "mercedes-sbc-safety"), None)
    check("SBC safety entry exists", sbc is not None)
    check("SBC entry warns it is STAR-only (never actuated over a dongle)",
          sbc is not None and any("STAR" in c for c in sbc["cautions"]), str(sbc))
    perf = mb_resets.perform(None, "real", "mercedes-assyst-w204", confirm=True)
    check("procedure perform() is not runnable + returns steps",
          perf.get("runnable") is False and bool(perf.get("reset", {}).get("procedure")), perf)
    sbcperf = mb_resets.perform(None, "real", "mercedes-sbc-safety", confirm=True)
    check("SBC safety perform() never touches the car", sbcperf.get("runnable") is False, sbcperf)

    # --- routes: the generic brand routes actually answer for Mercedes ------
    from fastapi.testclient import TestClient

    from backend.main import app
    tc = TestClient(app)
    profs = tc.get("/api/profiles").json().get("profiles", [])
    mb = next((p for p in profs if p.get("id") == "mercedes"), {})
    feats = mb.get("features", {})
    check("API /api/profiles lists mercedes with panel", bool(mb) and bool(mb.get("panel")))
    check("mercedes features scan/clear/live/resets true, snapshot false",
          feats.get("scan") and feats.get("clear") and feats.get("live")
          and feats.get("resets") and feats.get("snapshot") is False, str(feats))
    rscan = tc.get("/api/mercedes/scan?mode=sim").json()
    check("API sim scan over the generic brand route", rscan.get("ok") and rscan.get("simulated"))
    rlive = tc.get("/api/mercedes/live?mode=sim").json()
    check("API sim live over the generic brand route", rlive.get("ok") and rlive.get("simulated"))
    rresets = tc.get("/api/mercedes/resets").json()
    check("API resets route answers",
          rresets.get("brand") == "mercedes" and len(rresets.get("resets", [])) == 8)
    check("API rejects unknown-brand snapshot for mercedes (no snapshot.py)",
          tc.get("/api/mercedes/snapshot?mode=sim").status_code == 404)

    # --- misfire capture: the SLK rough-running workflow (slk.md step 4) ----
    from backend.profiles.mercedes import misfire as mb_misfire

    class FakeMisfireCar:
        """A SIM271DE20-flavoured engine ECU: answers the UDS 0x22 reads the
        mined dealer table asks -- ident, the SIM-only marker (2100), rpm,
        lambda, coolant and misfire counters that climb on cylinder 2 only.
        The MED-only markers (6015+) stay silent, so identification must land
        on sim271de20_m271_turbo. Response = request + 8."""

        PRESENT = {ENGINE}

        def __init__(self):
            self.reads = {}

        def resp_id(self, tx):
            return tx + 8 if tx in self.PRESENT else None

        def _counter(self, did, base, climb_per_read):
            n = self.reads.get(did, 0)
            self.reads[did] = n + 1
            return base + n * climb_per_read

        def handle(self, tx, req):
            resp = self.resp_id(tx)
            if not req or tx not in self.PRESENT:
                return (resp, None)
            if req[0] == 0x3E:
                return (resp, bytes([0x7E, 0x00]))
            if req[0] != 0x22 or len(req) < 3:
                return (resp, None)
            did = (req[1] << 8) | req[2]
            def u16(value):
                # 62 <did hi> <did lo> <data lo> <data hi> -- LITTLE-endian
                # from byte offset 3, the first data byte. That is what the
                # real SIM271DE20 answered on the car on 2026-08-23 (rpm
                # FD 04 = 1277, MAP BC 2E = 992 mbar), and misfire.decode_entry
                # was corrected to match it. This fake trails that decision.
                return bytes([0x62, req[1], req[2],
                              value & 0xFF, (value >> 8) & 0xFF])
            if did == 0xF190:
                return (resp, bytes([0x62, 0xF1, 0x90]) + FAKE_VIN.encode())
            if did == 0x2100:                       # SIM-only marker
                return (resp, u16(0x0100))
            if did == 0x2000:                       # engine speed ~750
                return (resp, u16(750))
            if did == 0x2046:                       # lambda ~1.00 (1/1024)
                return (resp, u16(0x0400))
            if did == 0x2013:                       # coolant: raw*0.75-48 = 25 C
                return (resp, bytes([0x62, req[1], req[2], 97]))
            if did in (0x6011, 0x6012, 0x6013, 0x6014):   # counters
                climb = 20 if did == 0x6012 else 0  # 6012 is cyl 4 on the SIM
                return (resp, u16(self._counter(did, 0, climb)))
            return (resp, None)

    car = FakeMisfireCar()
    session = FakeElmSession(car)
    client = Elm327UdsClient(session, id_map)
    client.begin()

    ident = mb_misfire._identify_client(client)
    check("misfire identify picks SIM271DE20 on the marker split",
          ident["ecu"] == "sim271de20_m271_turbo", str(ident["sim_markers"]))
    check("misfire identify reads VIN via F190", ident["vin"] == FAKE_VIN)
    check("misfire identify names the engine",
          "M271" in (ident.get("engine") or ""), str(ident.get("engine")))

    result = mb_misfire._capture_client(client, seconds=0.0,
                                        ecu_key="sim271de20_m271_turbo")
    # Two passes so every counter has a before/after -- the fake car's
    # cylinder-4 counter climbs 4 counts per read.
    result["samples"] += mb_misfire._capture_client(
        client, seconds=0.0, ecu_key="sim271de20_m271_turbo")["samples"]
    samples = result["samples"]
    plan = result["plan"]
    cyl_map = {e["did"]: e["_key"] for _, e in plan["cyls"]}
    check("capture plan maps SIM cylinders by name (6012 -> cyl4)",
          cyl_map.get("6012") == "cyl4", str(cyl_map))
    check("capture ran ticks with decoded rpm",
          bool(samples) and samples[0].get("rpm") == 750.0,
          str(samples[0] if samples else {}))
    check("capture decodes lambda to about 1.0",
          abs((samples[-1].get("did_2046") or 0) - 1.0) < 0.01,
          str(samples[-1].get("did_2046")))
    check("capture keeps raw bytes beside every decoded value",
          all(isinstance(s.get("rpm_raw"), str)
              for s in samples), "")
    live_verdict = mb_misfire.assess(samples, "sim271de20_m271_turbo")
    check("assess reads the fake car's climbing counter as cyl4 alone",
          live_verdict["verdict"] == "single_cylinder"
          and live_verdict["leaders"] == [4], str(live_verdict["deltas"]))

    # Cylinder 2 climbs alone -> single_cylinder verdict naming that hole.
    synth = []
    for i in range(40):
        s = {"t": float(i), "rpm": 750.0}
        for k in ("cyl1", "cyl2", "cyl3", "cyl4"):
            s[k] = 0.0
        if i >= 10:
            s["cyl2"] = float(i - 9) * 4
        synth.append(s)
    verdict = mb_misfire.assess(synth, "sim271de20_m271_turbo")
    check("assess calls a lone climbing hole single_cylinder on cyl2",
          verdict["verdict"] == "single_cylinder"
          and verdict["leaders"] == [2], str(verdict["deltas"]))
    even = []
    for i in range(40):
        s = {"t": float(i), "rpm": 750.0}
        for k in ("cyl1", "cyl2", "cyl3", "cyl4"):
            s[k] = float(i) * 2
        even.append(s)
    verdict_even = mb_misfire.assess(even, "sim271de20_m271_turbo")
    check("assess calls an even climb fuelling/air, not one hole",
          verdict_even["verdict"] == "all_cylinders_evenly",
          verdict_even["verdict"])
    quiet = mb_misfire.assess(
        [{"t": 0.0, "rpm": 750.0, **{k: 0.0 for k in
         ("cyl1", "cyl2", "cyl3", "cyl4")}}],
        "sim271de20_m271_turbo")
    check("assess says quiet when nothing climbed",
          quiet["verdict"] == "quiet_or_insufficient_data", quiet["verdict"])
    check("assess refuses without an identified ECU",
          mb_misfire.assess(synth, None)["verdict"] == "no_ecu_identified")

    known_all = mb_misfire.known_issues()["issues"]
    known_sim = mb_misfire.known_issues("sim271de20_m271_turbo")["issues"]
    check("known-issues reference loads and filters by engine",
          len(known_sim) >= 4 and len(known_sim) < len(known_all),
          f"{len(known_sim)}/{len(known_all)}")
    check("every known issue carries at least one source link",
          all(i.get("sources") for i in known_all))
    matched = verdict.get("known_issues_matched") or \
        mb_misfire.match_known_issues("single_cylinder",
                                      "sim271de20_m271_turbo", [2])
    check("a single-cylinder signature ranks an issue for that pattern",
          any(m["match_score"] > 0 for m in matched), str(matched[:1]))


    # --- OM651 cold-start capture + XENTRY guided tests (CLA W117) ---------- #
    print("\nOM651 cold start (CRD3-MFA1) -- captures/mercedes/cla-w117-coldstart.md")

    entries = mb_cold._entries()
    by_did = mb_cold._by_did()
    check("41 CRD3MFA1 DIDs load from the mined table", len(entries) == 41,
          str(len(entries)))
    check("every crank DID exists in the mined table",
          all(d in by_did for d in mb_cold.CRANK_DIDS),
          str([d for d in mb_cold.CRANK_DIDS if d not in by_did]))
    check("rail pressure feedback leads the crank list",
          mb_cold.CRANK_DIDS[0] == "D058")

    # Endian choice. These are the REAL bytes the CLA returned on 2026-09-04:
    # 6702 = 2E D3 = 11987 counts, which at the car-corrected x0.001 is
    # 11.987 V -- a battery sitting with the ignition on. Read the other way
    # round (D3 2E = 54062) it is 54 V, which nothing on a car reads, so this
    # single channel separates the two orders on its own.
    volts_big = "6267022ED3"
    vote = mb_cold.pick_endian({"6702": volts_big})
    check("endian vote picks big when only big is plausible",
          vote["endian"] == "big" and vote["confident"], str(vote["votes"]))
    volts_little = "626702D32E"
    vote_l = mb_cold.pick_endian({"6702": volts_little})
    check("endian vote picks little when only little is plausible",
          vote_l["endian"] == "little" and vote_l["confident"],
          str(vote_l["votes"]))
    check("the CLA's own bytes decode to a real battery voltage",
          mb_cold.decode_entry(by_did["6702"],
                               bytes.fromhex(volts_big), "big") == 11.987,
          str(mb_cold.decode_entry(by_did["6702"],
                                   bytes.fromhex(volts_big), "big")))
    check("41 CRD3MFA1 DIDs are now graded live, not candidate",
          all(e["grade"] == "live" for e in entries),
          str({e["grade"] for e in entries}))
    blind = mb_cold.pick_endian({})
    check("endian vote with no evidence defaults little and says so",
          blind["endian"] == "little" and not blind["confident"]
          and "NOT" in blind["note"] or "NO channel" in blind["note"],
          blind["note"][:40])

    rail = by_did["D058"]
    check("rail pressure decodes big-endian from byte 3",
          mb_cold.decode_entry(rail, bytes.fromhex("62D05800FA"), "big") == 250.0)
    check("rail pressure decodes little-endian the other way",
          mb_cold.decode_entry(rail, bytes.fromhex("62D058FA00"), "little") == 250.0)
    check("a negative response decodes to None",
          mb_cold.decode_entry(rail, bytes.fromhex("7F2231")) is None)
    err = by_did["601A"]
    check("rail pressure error reads signed (offset -5000)",
          mb_cold.decode_entry(err, bytes.fromhex("62601A1388"), "big") == 0.0)
    glow = by_did["D017"]
    check("glow state 2 names itself Pre Glow",
          mb_cold.enum_name(glow, 2.0) == "Pre Glow",
          str(mb_cold.enum_name(glow, 2.0)))
    check("an unlisted enum value says so rather than guessing",
          "unlisted" in (mb_cold.enum_name(glow, 200.0) or ""))

    # A synthetic cold start shaped like the real complaint: 1 s key-on, then
    # a ~2 s crank while the rail crawls from 40 bar up past release, then it
    # catches. Ticks are 0.2 s apart because that is roughly what six DIDs
    # over a fast dongle manage, and a 2 s crank only holds about ten of them.
    def sample(t, rpm, bar, sync=1.0, phase="crank"):
        return {"t": round(t, 2), "phase": phase, "did_5017": rpm,
                "did_d058": bar, "did_60f9": sync, "did_d017": 2.0,
                "did_d017_s": "Pre Glow"}
    drain = [sample(0.0, 0.0, 0.0, 0.0, "keyon"),
             sample(0.4, 0.0, 0.0, 0.0, "keyon")]
    drain += [sample(0.8 + i * 0.2, 220.0, 40.0 + i * 24.0) for i in range(10)]
    drain.append(sample(2.8, 900.0, 265.0, 1.0, "run"))
    drain += [sample(3.0 + i * 0.2, 820.0, 300.0, 1.0, "run") for i in range(15)]
    a = mb_cold.assess(drain)
    check("a crawling rail that fires on arrival reads as rail_pressure_delay",
          a["verdict"] == "rail_pressure_delay", a["verdict"])
    check("crank length is measured from first turn to first fire",
          abs(a["crank_seconds"] - 2.0) < 0.01, str(a["crank_seconds"]))
    check("the rail-release crossing is timed",
          a["t_rail_release"] is not None and a["release_to_fire_seconds"] <= 1.5,
          f"{a['t_rail_release']} -> fire +{a['release_to_fire_seconds']}")
    check("a 2 s crank is the complaint, not a healthy start",
          a["crank_seconds"] >= mb_cold.EXTENDED_CRANK_S)

    # The other half of the complaint: it catches, nearly dies, then settles.
    struggle = list(drain[:13])
    struggle += [sample(3.0, 520.0, 280.0, 1.0, "run"),
                 sample(3.2, 430.0, 270.0, 1.0, "run"),
                 sample(3.4, 610.0, 290.0, 1.0, "run")]
    struggle += [sample(3.6 + i * 0.2, 810.0, 300.0, 1.0, "run")
                 for i in range(15)]
    st = mb_cold.assess(struggle)
    check("a catch that nearly dies is reported as the struggle",
          st["stumble_rpm_min"] is not None
          and st["stumble_rpm_min"] < mb_cold.STUMBLE_RPM,
          str(st["stumble_rpm_min"]))
    check("the struggle earns a note in plain words",
          any("nearly died again" in n for n in st["notes"]))
    check("how long it took to hold idle is measured",
          st["settle_seconds"] is not None, str(st["settle_seconds"]))
    check("a clean catch reports no stumble below the threshold",
          a["stumble_rpm_min"] >= mb_cold.STUMBLE_RPM, str(a["stumble_rpm_min"]))

    ready = [sample(0.0, 0.0, 300.0, 0.0, "keyon")]
    ready += [sample(0.4 + i * 0.2, 220.0, 300.0) for i in range(10)]
    ready.append(sample(2.6, 900.0, 320.0, 1.0, "run"))
    b = mb_cold.assess(ready)
    check("a rail already up during a long crank reads as rail_ready_early",
          b["verdict"] == "rail_ready_early", b["verdict"])

    quick = [sample(0.0, 0.0, 0.0, 0.0, "keyon"),
             sample(0.4, 220.0, 100.0), sample(0.8, 900.0, 300.0, 1.0, "run")]
    check("a sub-second crank is called out as not the complaint",
          mb_cold.assess(quick)["verdict"] == "no_extended_crank")
    check("a short crank still tells the reader to check the stumble",
          any("hunts" in n for n in mb_cold.assess(quick)["notes"]))
    check("no crank at all is insufficient_data, not a guess",
          mb_cold.assess([])["verdict"] == "insufficient_data")

    silly = [sample(0.0, 0.0, 0.0, 0.0, "keyon")]
    silly += [sample(0.4 + i * 0.2, 220.0, 60000.0) for i in range(10)]
    silly.append(sample(2.6, 900.0, 60000.0, 1.0, "run"))
    check("an impossible rail pressure is flagged as a scaling problem",
          mb_cold.assess(silly)["scaling_suspect"])

    late = [dict(x, did_60f9=0.0) for x in drain[:9]] + drain[9:]
    check("late cam/crank sync earns its own note",
          any("late" in n for n in mb_cold.assess(late)["notes"]),
          str(mb_cold.assess(late)["sync_seconds"]))

    check("coldstart/probe sim modes label themselves SIMULATED",
          mb_cold.probe(None, "sim")["simulated"]
          and mb_cold.coldstart(None, mode="sim")["simulated"])
    check("coldstart refuses a byte order it does not know",
          mb_cold.coldstart(None, endian="middle")["ok"] is False)

    # --- the mined XENTRY guided tests ------------------------------------- #
    cat = mb_routines.catalogue()
    routines = cat["routines"]
    check("13 XENTRY guided tests mined for the OM651", len(routines) == 13,
          str(len(routines)))
    check("the dynamic hydraulic test is present with start/stop/results",
          all(routines["dyn_hydraulic"][k] for k in ("start", "stop", "results")))
    check("dyn_hydraulic start is 31 01 F004",
          routines["dyn_hydraulic"]["start"]["payload"] == "3101F004",
          routines["dyn_hydraulic"]["start"]["payload"])
    check("the high pressure test carries rise AND leak time",
          sum(1 for f in routines["high_pressure"]["results"]["fields"]
              if f["unit"] == "s") >= 2)
    check("every routine says whether it commands the engine",
          all("cranks_or_commands_engine" in r for r in routines.values()))
    check("every routine is graded candidate until it runs on a car",
          all(r["grade"] == "candidate" for r in routines.values()))
    check("an unknown routine key is refused with the known list",
          mb_routines.catalogue("nope")["ok"] is False)

    status_field = next(f for f in routines["dyn_hydraulic"]["results"]["fields"]
                        if f["name"] == "ROUTINE_STATUS")
    # 71 03 F0 04 <status> ... -- status sits at absolute byte 4.
    fin = mb_routines.decode_field(status_field,
                                   bytes.fromhex("7103F00402"), "big")
    check("routine status byte 02 decodes to TEST_FINISHED",
          fin["state"] == "TEST_FINISHED", str(fin["state"]))
    short = mb_routines.decode_field(status_field, bytes.fromhex("7103"), "big")
    check("a reply too short for a field says so instead of throwing",
          short.get("error") and short["value"] is None)
    bar_field = next(f for f in routines["dyn_hydraulic"]["results"]["fields"]
                     if f["unit"] == "bar")
    got = mb_routines.decode_field(bar_field,
                                   bytes.fromhex("7103F004020104") + b"\x00" * 20,
                                   "big")
    check("a routine pressure field decodes and keeps its raw bytes",
          got["raw"] is not None and got["value"] is not None,
          f"{got['raw']} -> {got['value']}")

    refused = mb_routines.run(None, "dyn_hydraulic")
    check("an active test without confirm is refused, and says why",
          refused["ok"] is False and "ACTIVE test" in refused["message"])
    check("the refusal repeats what the routine does before asking again",
          "starter" in refused["message"])
    check("an unknown routine cannot be run",
          mb_routines.run(None, "nope", confirm=True)["ok"] is False)
    check("run sim mode labels itself SIMULATED",
          mb_routines.run(None, "lift_pump", confirm=True,
                          mode="sim")["simulated"])

    dtcs = mb_routines_dtcs()
    check("194 cold-start fault codes mined with Mercedes' own text",
          len(dtcs) == 194, str(len(dtcs)))
    check("the mined fault text survived the encoding repair",
          any("ü" in (d["description"] or "") for d in dtcs))
    check("every fault code carries its ASCII symbolic name",
          sum(1 for d in dtcs if d.get("symbol")) > len(dtcs) * 0.9)
    look = mb_routines.lookup_dtcs(["P0087", "P0671"])
    check("a P-code stem finds its XENTRY rows",
          all(r["matches"] for r in look["results"]),
          str([(r["code"], len(r["matches"])) for r in look["results"]]))
    check("low rail pressure text is what P0087 comes back as",
          any("Kraftstoffdruck" in (m["description"] or "")
              for m in look["results"][0]["matches"]))


    # --- baseline vs cold: the compare that makes a warm run worth taking --- #
    import contextlib
    import io as _io
    import tempfile

    def _write_run(tmp, name, samples, soak):
        path = pathlib.Path(tmp) / f"{name}.json"
        path.write_text(json.dumps({
            "car": "cla-w117", "command": "capture", "soak": soak,
            "note": "", "savedAt": None,
            "result": {"samples": samples,
                       "assessment": mb_cold.assess(samples)}}), encoding="utf-8")
        return path

    # Baseline: taken a few hours after a short run, so the circuit is still
    # primed -- the rail is already up and it catches almost at once.
    prim = [sample(0.0, 0.0, 180.0, 0.0, "keyon")]
    prim += [sample(0.2 + i * 0.2, 220.0, 240.0 + i * 20.0) for i in range(4)]
    prim += [sample(1.0, 900.0, 320.0, 1.0, "run")]
    prim += [sample(1.2 + i * 0.2, 820.0, 300.0, 1.0, "run") for i in range(15)]
    check("a primed start reads short and is not the complaint",
          mb_cold.assess(prim)["verdict"] == "no_extended_crank",
          mb_cold.assess(prim)["verdict"])

    with tempfile.TemporaryDirectory() as tmp:
        base_p = _write_run(tmp, "base", prim, "hours")
        cold_p = _write_run(tmp, "cold", struggle, "overnight")
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mb_session.compare(base_p, cold_p)
        out = buf.getvalue()
        check("compare runs over two saved captures", rc == 0 and out)
        check("compare lines both soak labels up", "soak=hours" in out
              and "soak=overnight" in out)
        check("compare names the crank growth as the fault",
              "crank grew" in out, out.splitlines()[-1][:60])
        check("compare carries the later run's notes through",
              "nearly died again" in out)

        # A run saved as a bare capture result, pasted out of the MCP tool
        # rather than the session runner, must still compare.
        bare = pathlib.Path(tmp) / "bare.json"
        bare.write_text(json.dumps(
            {"samples": prim, "assessment": mb_cold.assess(prim)}),
            encoding="utf-8")
        with contextlib.redirect_stdout(_io.StringIO()) as buf2:
            check("compare accepts a bare capture result too",
                  mb_session.compare(bare, cold_p) == 0)

    sim_ident = mb_misfire.identify(None, "sim")
    sim_cap = mb_misfire.capture(None, 30, mode="sim")
    check("identify/capture sim modes label themselves SIMULATED",
          sim_ident.get("simulated") and sim_cap.get("simulated"))

    print()

    print()
    print("  sim-scan message: " + rscan.get("message", ""))
    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SELF-TEST PASSED: Mercedes engine+TCM table, UDS mask ladder + session "
          "escalation, KWP2000-over-CAN fallback, engine OBD fallback + DoIP note, "
          "E-Class hybrid live tier, ATF live extra, reset catalogue, brand "
          "routes, the SLK rough-running identify/capture/assess workflow and "
          "the OM651 cold-start capture + mined XENTRY guided tests "
          "all good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
