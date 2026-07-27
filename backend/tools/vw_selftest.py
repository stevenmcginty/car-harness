"""Self-test for the VW / Audi (VAG) UDS-over-ELM327 stack, no car required.

Run:  python -m backend.tools.vw_selftest

Drives the REAL client code -- Elm327UdsClient, backend.core.uds.*, the vw
scan/pids/resets modules and their id table -- against a fake VAG car that
answers over a fake ELM327 session. It proves the parts that make VAG work:

  * the +0x6A response-id map is honoured -- the fake car only answers when the
    receive filter (ATCRA) matches the module's response id, so the gateway
    0x710 must be read at 0x77A, NOT 0x718 (request+8),
  * VIN read FIRST over plain OBD Mode 09,
  * the 19 02 mask ladder (0xFF refused with 0x31 -> 0x08 answers),
  * the UDS extended-session escalation (0x22 refusal -> 10 03 -> retry),
  * the engine 01 00 fallback + generic Mode 03 codes when UDS is silent,
  * UDS DTC decode + clear 14 FFFFFF,
  * the generic FastAPI brand routes actually answer for VW.

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
from backend.profiles.vw import pids as vw_pids  # noqa: E402
from backend.profiles.vw import resets as vw_resets  # noqa: E402
from backend.profiles.vw import scan as vw_scan  # noqa: E402

FAKE_VIN = "WVWZZZ1KZAW000001"
ENGINE, GATEWAY, CLUSTER, ABS, AIRBAG = 0x7E0, 0x710, 0x714, 0x713, 0x715

# request -> response id for the fake car's present modules. Engine answers only
# on the standard OBD channel (it plays a car whose ECU ignores bare UDS reads),
# the rest are the real +0x6A body-block ids from the profile.
VW_RESP = {
    ENGINE: 0x7E8,    # request + 8 (standard block)
    GATEWAY: 0x77A,   # request + 0x6A  <-- NOT 0x718
    CLUSTER: 0x77E,
    ABS: 0x77D,
    AIRBAG: 0x77F,
}
PRESENT = set(VW_RESP)


class FakeVagCar:
    """A tiny VAG brain keyed by request (TX) id. The engine is UDS-silent (only
    01 00 / Mode 03 answer), the cluster needs the 0x08 mask (refuses 0xFF), and
    the airbag only answers 19 02 once a 10 03 extended session is open."""

    def __init__(self):
        self.sessions = {}       # tx -> extended session open
        self.cleared = []        # tx ids that got a ClearDiagnosticInformation
        self.ext_started = []    # tx ids that got a 10 03 escalation
        self.masks = {}          # tx -> list of 19 02 masks it was asked for

    def resp_id(self, tx):
        return VW_RESP.get(tx)

    def handle(self, tx, req):
        """Return (response_id, reply_bytes-or-None) for one request."""
        resp = self.resp_id(tx)
        if tx not in PRESENT or not req:
            return (resp, None)
        svc = req[0]

        # Engine: ignores bare UDS reads; only the standard OBD channel answers.
        if tx == ENGINE:
            if svc == 0x01 and len(req) >= 2 and req[1] == 0x00:
                return (resp, bytes([0x41, 0x00, 0xBE, 0x3E, 0xB8, 0x11]))
            return (resp, None)

        if svc == 0x3E:                                   # TesterPresent
            return (resp, bytes([0x7E, 0x00]))
        if svc == 0x10 and len(req) >= 2:                 # session control
            if req[1] == 0x03:                            # UDS extended session
                self.sessions[tx] = True
                self.ext_started.append(tx)
                return (resp, bytes([0x50, 0x03, 0x00, 0x32, 0x01, 0xF4]))
            return (resp, bytes([0x7F, 0x10, 0x12]))
        if svc == 0x22 and len(req) >= 3:                 # ReadDataByIdentifier
            did = (req[1] << 8) | req[2]
            if tx == CLUSTER and did == 0xF190:
                return (resp, bytes([0x62, 0xF1, 0x90]) + FAKE_VIN.encode())
            if tx == GATEWAY and did == 0xF187:
                return (resp, bytes([0x62, 0xF1, 0x87]) + b"5Q0907530AB")
            return (resp, bytes([0x7F, 0x22, 0x31]))      # out of range
        if svc == 0x19 and len(req) >= 3:                 # ReadDTCInformation
            mask = req[2]
            self.masks.setdefault(tx, []).append(mask)
            if tx == CLUSTER:                             # ladder: FF refused, 08 ok
                if mask == 0xFF:
                    return (resp, bytes([0x7F, 0x19, 0x31]))   # out of range
                return (resp, bytes([0x59, 0x02, mask, 0x94, 0x04, 0x00, 0x08]))
            if tx == AIRBAG:                              # needs an active session
                if self.sessions.get(tx):
                    return (resp, bytes([0x59, 0x02, mask, 0x90, 0x04, 0x00, 0x08]))
                return (resp, bytes([0x7F, 0x19, 0x22]))   # conditions not correct
            if tx == GATEWAY:                             # clean, no faults
                return (resp, bytes([0x59, 0x02, mask]))
            if tx == ABS:                                 # one C-code
                return (resp, bytes([0x59, 0x02, mask, 0x53, 0x35, 0x00, 0x08]))
            return (resp, bytes([0x7F, 0x19, 0x11]))
        if svc == 0x14:                                   # ClearDiagnosticInformation
            self.cleared.append(tx)
            return (resp, bytes([0x54]))
        return (resp, bytes([0x7F, svc, 0x11]))


class FakeElmSession:
    """Just enough of an Elm327Session for the UDS client AND the generic OBD
    helpers (query / _read_vin / read_codes). Crucially it models the ELM's
    receive filter: a car frame is only delivered when the current ATCRA matches
    the module's response id -- so a wrong (+8) filter yields NO DATA."""

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
        # The ELM only surfaces frames matching the receive filter.
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

    # --- static: the VAG id table + profile manifest -------------------------
    table = vw_scan.VW_ECU_TABLE
    id_map = vw_scan.VW_ID_MAP
    txs = [row[0] for row in table]
    rxs = [row[1] for row in table]
    check("ID table has modules", len(table) >= 20, str(len(table)))
    check("all request ids unique", len(set(txs)) == len(txs))
    check("all response ids unique", len(set(rxs)) == len(rxs))
    check("engine 0x7E0 -> 0x7E8 (standard +8)", id_map.get(0x7E0) == 0x7E8)
    check("gateway 0x710 -> 0x77A (+0x6A, NOT +8)",
          id_map.get(0x710) == 0x77A and id_map.get(0x710) != 0x710 + 8,
          f"0x{id_map.get(0x710):03X}")
    check("cluster 0x714 -> 0x77E (+0x6A)", id_map.get(0x714) == 0x77E)
    check("every body id obeys +0x6A",
          all(rx == tx + 0x6A for tx, rx, _, _, _ in table if 0x700 <= tx <= 0x76F))
    check("VCDS address in each row's code",
          all(isinstance(c, str) and c for _, _, c, _, _ in table))

    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "profiles" / "vw" / "profile.json")
        .read_text(encoding="utf-8-sig")
    )
    check("profile.json id == vw", manifest.get("id") == "vw")
    check("profile.json name == VW / Audi", manifest.get("name") == "VW / Audi")
    check("panel has exactly one action (live, no snapshot)",
          len(manifest["panel"]["actions"]) == 1
          and manifest["panel"]["actions"][0]["id"] == "live")

    # --- static: simulated scan/clear shape ---------------------------------
    sim = vw_scan.scan(None, "sim")
    check("sim scan ok+simulated", sim["ok"] and sim["simulated"])
    check("sim scan has modules", len(sim["modules"]) >= 5, str(len(sim["modules"])))
    check("sim module carries address+response",
          all("address" in m and "response" in m for m in sim["modules"]))
    check("sim scan labelled SIMULATED", "SIMULATED" in sim["message"])
    simclear = vw_scan.clear(None, "sim")
    check("sim clear simulated", simclear["simulated"] is True)

    # --- reassemble: single frame, kept negative, no-data -------------------
    rec = reassemble("59 02 FF 53 35 00 08\r>", "1902FF")
    check("reassemble single frame",
          rec and rec[0] == bytes([0x59, 0x02, 0xFF, 0x53, 0x35, 0x00, 0x08]))
    neg = reassemble("7F 19 22\r>", "1902FF")
    check("reassemble keeps 0x7F negative", neg and neg[0] == bytes([0x7F, 0x19, 0x22]))
    try:
        reassemble("NO DATA\r>", "1902FF")
        no_data_raised = False
    except ObdError as exc:
        no_data_raised = exc.kind == "no_data"
    check("reassemble NO DATA -> ObdError(no_data)", no_data_raised)

    # --- integration: real client + uds helpers vs the fake VAG car ---------
    car = FakeVagCar()
    session = FakeElmSession(car)
    client = Elm327UdsClient(session, id_map)
    client.begin()

    # (a) +0x6A id map honoured: the gateway must be read at 0x77A, not 0x718.
    present_gw = uds.tester_present(client, GATEWAY)
    check("gateway present via 3E 00", present_gw)
    check("gateway aims +0x6A rx (ATCRA77A, not ATCRA718)",
          "ATCRA77A" in session.at_log and "ATCRA718" not in session.at_log)

    gw_ident = vw_scan._read_ident(client, GATEWAY, [])
    check("gateway part number read",
          any(i["label"] == "VAG part number" and i["value"] == "5Q0907530AB"
              for i in gw_ident), str(gw_ident))

    # (b, partial) VIN read from the cluster's F190.
    cl_ident = vw_scan._read_ident(client, CLUSTER, [])
    cl_vin = next((i["value"] for i in cl_ident if i["label"] == "VIN"), None)
    check("VIN read from cluster F190", cl_vin == FAKE_VIN, str(cl_vin))

    # (c) mask ladder: cluster refuses 0xFF (NRC 0x31) then answers 0x08.
    cl_trace = []
    cl_dtcs = vw_scan._read_dtcs(client, CLUSTER, cl_trace)
    check("cluster faults recovered via the mask ladder", len(cl_dtcs) == 1, str(len(cl_dtcs)))
    check("mask ladder tried 0xFF then 0x08 (never 0xAF)",
          car.masks.get(CLUSTER) == [0xFF, 0x08], str(car.masks.get(CLUSTER)))
    if cl_dtcs:
        check("cluster DTC decodes B1404", cl_dtcs[0]["genericCode"] == "B1404", cl_dtcs[0])
        check("cluster DTC hex 940400", cl_dtcs[0]["hex"] == "940400", cl_dtcs[0]["hex"])

    # (d) session escalation: airbag NRCs 0x22 until a 10 03 extended session.
    air_trace = []
    air_dtcs = vw_scan._read_dtcs(client, AIRBAG, air_trace)
    check("airbag fault read after 10 03 escalation", len(air_dtcs) == 1, str(len(air_dtcs)))
    check("extended session 10 03 was opened for airbag", AIRBAG in car.ext_started)
    check("trace records the 10 03 escalation",
          any("10 03" in line for line in air_trace), " | ".join(air_trace))
    if air_dtcs:
        check("airbag DTC decodes B1004", air_dtcs[0]["genericCode"] == "B1004", air_dtcs[0])

    # ABS: a clean UDS C-code with no session needed.
    abs_dtcs = vw_scan._read_dtcs(client, ABS, [])
    check("VAG DTCs do not claim a generic SAE meaning",
          all(d.get("codeScheme") == "manufacturer" and d.get("code") != d.get("genericCode")
              for d in cl_dtcs + air_dtcs),
          [d.get("code") for d in cl_dtcs + air_dtcs])
    check("the raw hex fault number is still reported",
          all(d.get("hex") for d in cl_dtcs + air_dtcs),
          [d.get("hex") for d in cl_dtcs + air_dtcs])

    check("ABS DTC decodes C1335",
          len(abs_dtcs) == 1 and abs_dtcs[0]["genericCode"] == "C1335",
          str([d["code"] for d in abs_dtcs]))

    # (f) clear 14 FF FF FF.
    uds.clear_dtcs(client, GATEWAY)
    check("clear accepted on gateway (14 FF FF FF)", GATEWAY in car.cleared)

    client.restore()
    check("restore hands ELM back to generic stack (ATSH7DF)", "ATSH7DF" in session.at_log)

    # --- integration: the full scan() end to end (VIN-first + engine fallback)
    car2 = FakeVagCar()
    session2 = FakeElmSession(car2)
    original = obd_service._with_session
    obd_service._with_session = lambda adapter, fn: fn(session2, None)
    try:
        report = vw_scan.scan({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    check("full scan ok", report["ok"] is True, report.get("message"))
    by_addr = {m["address"]: m for m in report["modules"]}

    # (b) VIN-first over Mode 09 -- surfaced even though the engine is UDS-silent.
    check("VIN surfaced (Mode 09 first)", report["vehicle"]["vin"] == FAKE_VIN,
          str(report["vehicle"]))
    # (a) end to end: gateway present, aimed at its +0x6A id.
    check("scan finds the gateway (id map end to end)", by_addr["0x710"]["present"] is True)
    check("scan aimed gateway at ATCRA77A, never ATCRA718",
          "ATCRA77A" in session2.at_log and "ATCRA718" not in session2.at_log)
    # (e) engine ignores UDS -> 01 00 fallback -> generic Mode 03 codes.
    eng = by_addr["0x7E0"]
    check("engine present via 01 00 fallback", eng["present"] is True)
    check("engine fault via Mode 03 fallback decodes P0171",
          [d["code"] for d in eng["dtcs"]] == ["P0171"], str(eng["dtcs"]))
    check("engine trace records the OBD fallback",
          any("Mode 03" in line for line in eng["trace"]), " | ".join(eng["trace"]))
    # cluster VIN consensus matches the Mode 09 VIN.
    check("cluster present with matching VIN",
          by_addr["0x714"]["present"] and
          any(i["label"] == "VIN" and i["value"] == FAKE_VIN
              for i in by_addr["0x714"]["ident"]))
    check("no false VIN-mismatch warning", "MISMATCH" not in report["message"], report["message"])

    # --- pids: sim + a real F4xx decode + a raw community DID ----------------
    lsim = vw_pids.live(None, "sim")
    check("sim live ok+simulated+labelled",
          lsim["ok"] and lsim["simulated"] and "SIMULATED" in lsim["message"])

    class PidCar(FakeVagCar):
        def handle(self, tx, req):
            if tx == ENGINE and req and req[0] == 0x22 and len(req) >= 3:
                did = (req[1] << 8) | req[2]
                if did == 0xF405:                        # coolant: A-40 => 88C
                    return (0x7E8, bytes([0x62, 0xF4, 0x05, 0x80]))
                if did == 0x114F:                        # community DPF soot (raw)
                    return (0x7E8, bytes([0x62, 0x11, 0x4F, 0x2E, 0xE0]))
                return (0x7E8, bytes([0x7F, 0x22, 0x31]))
            return super().handle(tx, req)

    psession = FakeElmSession(PidCar())
    obd_service._with_session = lambda adapter, fn: fn(psession, None)
    try:
        lreal = vw_pids.live({"name": "Fake adapter"}, "real")
    finally:
        obd_service._with_session = original
    got = {r["name"]: r["value"] for r in lreal["readings"]}
    check("F405 coolant decodes to 88 via the real table",
          got.get("Coolant temperature") == "88", str(got))
    check("community DPF DID shown raw (value + hex, no fabricated figure)",
          got.get("DPF soot mass, calculated") == "12000 (raw 0x2EE0)", str(got))
    check("community caveat reading appended",
          any(r["name"] == "Note" and "community-sourced" in r["value"]
              for r in lreal["readings"]), str(lreal["readings"]))

    # --- resets: catalogue shape + confidence + procedure never touches car --
    cat = vw_resets.list_resets()
    check("resets brand == vw", cat["brand"] == "vw")
    check("two resets shipped", len(cat["resets"]) == 2, str(len(cat["resets"])))
    check("every reset has method=procedure",
          all(r["method"] == "procedure" for r in cat["resets"]))
    check("every reset carries a confidence + source + procedure steps",
          all(r.get("confidence") and r.get("source") and r.get("procedure")
              for r in cat["resets"]))
    check("button reset is 'verified', menu reset is 'community'",
          {r["id"]: r["confidence"] for r in cat["resets"]}
          == {"vw-sri-button": "verified", "vw-sri-menu": "community"})
    perf = vw_resets.perform(None, "real", "vw-sri-button", confirm=True)
    check("procedure perform() is not runnable + returns steps",
          perf.get("runnable") is False and bool(perf.get("reset", {}).get("procedure")), perf)

    # --- routes: the generic brand routes actually answer for VW ------------
    from fastapi.testclient import TestClient

    from backend.main import app
    tc = TestClient(app)
    profs = tc.get("/api/profiles").json().get("profiles", [])
    vw = next((p for p in profs if p.get("id") == "vw"), {})
    feats = vw.get("features", {})
    check("API /api/profiles lists vw with panel", bool(vw) and bool(vw.get("panel")))
    check("vw features scan/clear/live/resets true, snapshot false",
          feats.get("scan") and feats.get("clear") and feats.get("live")
          and feats.get("resets") and feats.get("snapshot") is False, str(feats))
    rscan = tc.get("/api/vw/scan?mode=sim").json()
    check("API sim scan over the generic brand route", rscan.get("ok") and rscan.get("simulated"))
    rlive = tc.get("/api/vw/live?mode=sim").json()
    check("API sim live over the generic brand route", rlive.get("ok") and rlive.get("simulated"))
    rresets = tc.get("/api/vw/resets").json()
    check("API resets route answers", rresets.get("brand") == "vw" and len(rresets.get("resets", [])) == 2)
    check("API rejects unknown-brand snapshot for vw (no snapshot.py)",
          tc.get("/api/vw/snapshot?mode=sim").status_code == 404)

    print()
    print("  sim-scan message: " + rscan.get("message", ""))
    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SELF-TEST PASSED: VAG +0x6A id map, mask ladder, session escalation, "
          "engine fallback, UDS DTC/VIN/clear, live extras and brand routes all good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
