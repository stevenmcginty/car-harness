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
  * the one verified live extra (722.9/9G ATF temp) + a generic Mode-01 read,
  * the reset catalogue (ASSYST procedures + SBC safety warning),
  * the generic FastAPI brand routes actually answer for Mercedes.

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

    print()
    print("  sim-scan message: " + rscan.get("message", ""))
    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SELF-TEST PASSED: Mercedes engine+TCM table, UDS mask ladder + session "
          "escalation, KWP2000-over-CAN fallback, engine OBD fallback + DoIP note, "
          "ATF live extra, reset catalogue and brand routes all good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
