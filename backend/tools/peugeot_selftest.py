"""Self-test for the Peugeot / PSA UDS-over-ELM327 stack, no car required.

Run:  python -m backend.tools.peugeot_selftest

Drives the REAL client code -- PsaElm327UdsClient, backend.core.uds.*, the
peugeot scan module and its ID table -- against a fake PSA car that answers over
a fake ELM327 session. It proves the parts that make PSA different from a plain
OBD-II car actually work:

  * per-module response ids (BSI 0x752 -> 0x652, NOT request+8),
  * the extended-session escalation an airbag/BSI needs before ReadDTC,
  * UDS DTC decode for P- and B- codes, VIN read, clear,
  * ISO-TP reassembly incl. multi-frame and kept negative responses.

Exits 0 on PASS, 1 on FAIL.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core import uds  # noqa: E402
from backend.obd.elm327 import ObdError  # noqa: E402
from backend.obd.psa_uds_elm327 import PsaElm327UdsClient  # noqa: E402
from backend.obd.uds_elm327 import reassemble  # noqa: E402
from backend.profiles.peugeot import scan as peugeot_scan  # noqa: E402

FAKE_VIN = "VF3XXXXXXXX000001"
BSI, ENGINE, AIRBAG, ABS = 0x752, 0x6A8, 0x744, 0x6AD
PRESENT = {BSI, ENGINE, AIRBAG, ABS}


class FakePsaCar:
    """A tiny PSA UDS brain keyed by request (TX) id."""

    def __init__(self):
        self.sessions = {}       # tx -> is-extended-session
        self.cleared = []        # tx ids that got a ClearDiagnosticInformation
        self.ext_started = []    # tx ids that got a session escalation

    def handle(self, tx, req):
        if tx not in PRESENT or not req:
            return None
        svc = req[0]
        if svc == 0x3E:                                   # TesterPresent
            return bytes([0x7E, 0x00])
        if svc == 0x10 and len(req) >= 2 and req[1] == 0x03:  # extended session
            self.sessions[tx] = True
            self.ext_started.append(tx)
            return bytes([0x50, 0x03, 0x00, 0x32, 0x01, 0xF4])
        if svc == 0x22 and len(req) >= 3:                 # ReadDataByIdentifier
            did = (req[1] << 8) | req[2]
            if tx == BSI and did == 0xF190:
                return bytes([0x62, 0xF1, 0x90]) + FAKE_VIN.encode()
            if tx == ENGINE and did == 0xF188:
                return bytes([0x62, 0xF1, 0x88]) + b"1037564321"
            return bytes([0x7F, 0x22, 0x31])              # request out of range
        if svc == 0x19:                                   # ReadDTCInformation
            if tx == ENGINE:
                return bytes([0x59, 0x02, 0xFF, 0x02, 0x34, 0x00, 0x2F,
                              0x13, 0x38, 0x00, 0x08])
            if tx == ABS:
                return bytes([0x59, 0x02, 0xFF])          # present, no faults
            if tx == AIRBAG:
                if self.sessions.get(AIRBAG):
                    return bytes([0x59, 0x02, 0xFF, 0x90, 0x04, 0x00, 0x08])
                return bytes([0x7F, 0x19, 0x7F])          # not in active session
            return bytes([0x7F, 0x19, 0x11])
        if svc == 0x14:                                   # ClearDiagnosticInformation
            self.cleared.append(tx)
            return bytes([0x54])
        return bytes([0x7F, svc, 0x11])


class FakeElmSession:
    """Just enough of an Elm327Session for the UDS client: at() and _raw()."""

    def __init__(self, car):
        self.car = car
        self.at_log = []
        self.aimed = None
        self.firmware = "ELM327 v1.5"
        self.protocol = "ISO 15765-4 (CAN 11/500)"
        self.protocol_num = 6

    def at(self, cmd, timeout=3.0):
        self.at_log.append(cmd)
        if cmd.startswith("ATSH"):
            try:
                self.aimed = int(cmd[4:], 16)
            except ValueError:
                pass
        return "OK"

    def _raw(self, data, timeout):
        cmd = data.decode("ascii").strip().upper()
        try:
            req = bytes.fromhex(cmd)
        except ValueError:
            return "?\r>"
        reply = self.car.handle(self.aimed, req)
        if reply is None:
            return "NO DATA\r>"
        return reply.hex().upper() + "\r>"


def main() -> int:
    failures = []

    def check(name, cond, extra=""):
        print(("  PASS  " if cond else "  FAIL  ") + name + (f"  ({extra})" if extra else ""))
        if not cond:
            failures.append(name)

    # --- static: the PSA id table + profile manifest -------------------------
    table = peugeot_scan.PEUGEOT_ECU_TABLE
    id_map = peugeot_scan.PEUGEOT_ID_MAP
    txs = [row[0] for row in table]
    rxs = [row[1] for row in table]
    check("ID table has modules", len(table) >= 15, str(len(table)))
    check("all request ids unique", len(set(txs)) == len(txs))
    check("all response ids unique", len(set(rxs)) == len(rxs))
    check("BSI 0x752 -> 0x652", id_map.get(0x752) == 0x652)
    check("engine 0x6A8 -> 0x688", id_map.get(0x6A8) == 0x688)
    check("airbag 0x744 -> 0x644", id_map.get(0x744) == 0x644)
    check("PSA rx is NOT request+8 (BSI)", id_map.get(0x752) != 0x752 + 8,
          f"0x{id_map.get(0x752):03X}")

    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "profiles" / "peugeot" / "profile.json")
        .read_text(encoding="utf-8-sig")
    )
    check("profile.json id == peugeot", manifest.get("id") == "peugeot")

    # --- static: simulated scan/clear shape ---------------------------------
    sim = peugeot_scan.scan(None, "sim")
    check("sim scan ok+simulated", sim["ok"] and sim["simulated"])
    check("sim scan has modules", len(sim["modules"]) >= 5, str(len(sim["modules"])))
    check("sim module carries address+response",
          all("address" in m and "response" in m for m in sim["modules"]))
    check("sim scan labelled SIMULATED", "SIMULATED" in sim["message"])
    simclear = peugeot_scan.clear(None, "sim")
    check("sim clear simulated", simclear["simulated"] is True)

    # --- reassemble: single frame, kept negative, no-data, multi-frame ------
    rec = reassemble("59 02 FF 02 34 00 2F\r>", "1902AF")
    check("reassemble single frame", rec and rec[0] == bytes([0x59, 0x02, 0xFF, 0x02, 0x34, 0x00, 0x2F]))
    neg = reassemble("7F 19 7F\r>", "1902AF")
    check("reassemble keeps 0x7F negative", neg and neg[0] == bytes([0x7F, 0x19, 0x7F]))
    try:
        reassemble("NO DATA\r>", "1902AF")
        no_data_raised = False
    except ObdError as exc:
        no_data_raised = exc.kind == "no_data"
    check("reassemble NO DATA -> ObdError(no_data)", no_data_raised)
    multi = reassemble("014\n0:62F19056463358\n1:58585858585858\n2:58303030303031\n>", "22F190")
    check("reassemble multi-frame (20 bytes, starts 62F190)",
          multi and len(multi[0]) == 20 and multi[0][:3] == bytes([0x62, 0xF1, 0x90]),
          str(len(multi[0]) if multi else 0))

    # --- integration: real client + uds helpers vs the fake PSA car ---------
    car = FakePsaCar()
    session = FakeElmSession(car)
    client = PsaElm327UdsClient(session, id_map)
    client.begin()

    present_bsi = uds.tester_present(client, BSI)
    check("BSI aims PSA rx (ATCRA652, not ATCRA75A)",
          "ATCRA652" in session.at_log and "ATCRA75A" not in session.at_log)
    check("BSI present", present_bsi)
    check("engine present", uds.tester_present(client, ENGINE))
    check("absent module (telematics 0x764) not present",
          not uds.tester_present(client, 0x764))

    bsi_ident = peugeot_scan._read_ident(client, BSI)
    bsi_vin = next((i["value"] for i in bsi_ident if i["label"] == "VIN"), None)
    check("VIN read from BSI", bsi_vin == FAKE_VIN, str(bsi_vin))

    eng_dtcs = peugeot_scan._read_dtcs(client, ENGINE)
    check("engine returns 2 faults", len(eng_dtcs) == 2, str(len(eng_dtcs)))
    if eng_dtcs:
        check("engine DTC decodes P0234", eng_dtcs[0]["code"] == "P0234", eng_dtcs[0]["code"])
        check("engine DTC hex 023400", eng_dtcs[0]["hex"] == "023400", eng_dtcs[0]["hex"])
        check("engine DTC flags include confirmed", "confirmed" in eng_dtcs[0]["flags"],
              ",".join(eng_dtcs[0]["flags"]))

    abs_dtcs = peugeot_scan._read_dtcs(client, ABS)
    check("ABS present with no faults", abs_dtcs == [])

    air_dtcs = peugeot_scan._read_dtcs(client, AIRBAG)  # needs session escalation
    check("airbag fault read after extended-session escalation", len(air_dtcs) == 1, str(len(air_dtcs)))
    if air_dtcs:
        check("airbag DTC decodes B1004", air_dtcs[0]["code"] == "B1004", air_dtcs[0]["code"])
    check("extended session was actually started for airbag", AIRBAG in car.ext_started)

    uds.clear_dtcs(client, ENGINE)
    check("clear accepted on engine", ENGINE in car.cleared)

    client.restore()
    check("restore hands ELM back to generic stack (ATSH7DF)", "ATSH7DF" in session.at_log)

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SELF-TEST PASSED: PSA id pairs, session escalation, UDS DTC/VIN/clear, "
          "ISO-TP reassembly all good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
