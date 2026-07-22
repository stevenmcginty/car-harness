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
BSI, ENGINE, AIRBAG, ABS, BMS = 0x752, 0x6A8, 0x744, 0x6AD, 0x6B4
PRESENT = {BSI, ENGINE, AIRBAG, ABS, BMS}

# Demo DPF 0x21 block (engine), same bytes the Android demo uses: soot 12.0g,
# diff 30mbar, additive 3200, DPF temp 235C, dist-since-regen 220km, avg 450km.
DPF_BLOCK = bytes.fromhex("0000000000000000002EE000000000001E00000C800000000000EB0000DC01C2")


class FakePsaCar:
    """A tiny PSA brain keyed by request (TX) id. The ABS is deliberately a
    KWP-flavour module (like the field-test car): it refuses every UDS read,
    only opens on PSA's 10 C0 session, and answers 17 FF 00 / 21 80."""

    def __init__(self):
        self.sessions = {}       # tx -> diagnostic session open
        self.cleared = []        # tx ids that got a ClearDiagnosticInformation
        self.ext_started = []    # tx ids that got a session escalation
        self.kwp = {ABS}         # modules speaking PSA's KWP dialect

    def handle(self, tx, req):
        if tx not in PRESENT or not req:
            return None
        svc = req[0]
        if svc == 0x3E:                                   # TesterPresent
            return bytes([0x7E, 0x00])
        if svc == 0x10 and len(req) >= 2:                 # session control
            if tx in self.kwp:
                if req[1] == 0xC0:                        # PSA/KWP session only
                    self.sessions[tx] = True
                    self.ext_started.append(tx)
                    return bytes([0x50, 0xC0])
                return bytes([0x7F, 0x10, 0x12])
            if req[1] == 0x03:                            # UDS extended session
                self.sessions[tx] = True
                self.ext_started.append(tx)
                return bytes([0x50, 0x03, 0x00, 0x32, 0x01, 0xF4])
            return bytes([0x7F, 0x10, 0x12])
        if svc == 0x17:                                   # KWP ReadStatusOfDTC
            if tx in self.kwp and self.sessions.get(tx):
                return bytes([0x57, 0x01, 0x53, 0x35, 0x60])   # C1335
            return bytes([0x7F, 0x17, 0x11])
        if svc == 0x21 and len(req) == 2:                 # KWP zone read
            if tx == ABS and req[1] == 0x80 and self.sessions.get(tx):
                return bytes([0x61, 0x80]) + b"ABS8.1 9665344180"
            return bytes([0x7F, 0x21, 0x11])
        if svc == 0x21 and len(req) >= 4:                 # KWP block read (HDi)
            if tx == ENGINE and req[1:4] == bytes([0xCB, 0x80, 0x01]):
                return bytes([0x61, 0xCB, 0x80, 0x01]) + DPF_BLOCK
            return bytes([0x7F, 0x21, 0x11])
        if svc == 0x22 and len(req) >= 3:                 # ReadDataByIdentifier
            did = (req[1] << 8) | req[2]
            if tx in self.kwp:
                return bytes([0x7F, 0x22, 0x11])          # KWP module: no UDS reads
            if tx == BSI and did == 0xF190:
                return bytes([0x62, 0xF1, 0x90]) + FAKE_VIN.encode()
            if tx == ENGINE and did == 0xF188:
                return bytes([0x62, 0xF1, 0x88]) + b"1037564321"
            # EV HV battery (BMS) health-check DIDs.
            if tx == BMS:
                ev = {
                    0xD860: bytes([0x00, 0x05, 0xC0]),        # SOH 92.0%
                    0xD410: bytes([0xC5, 0x01]),              # SOC 98.5%
                    0xD815: bytes([0x16, 0x80]),              # 360.0 V
                    0xD865: bytes([0x0A, 0x80]),              # 42.00 kWh
                    0xD86F: bytes([0x0F, 0x5A]),              # min cell 3930 mV
                    0xD870: bytes([0x0F, 0x6E]),              # max cell 3950 mV
                    0xD49C: bytes([0x01, 0xC9, 0xC3, 0x80]),  # odometer 30000 km
                }
                if did in ev:
                    return bytes([0x62, req[1], req[2]]) + ev[did]
            return bytes([0x7F, 0x22, 0x31])              # request out of range
        if svc == 0x19:                                   # ReadDTCInformation
            if tx in self.kwp:
                return bytes([0x7F, 0x19, 0x11])          # KWP module: no UDS DTC
            if tx == ENGINE:
                return bytes([0x59, 0x02, 0xFF, 0x02, 0x34, 0x00, 0x2F,
                              0x13, 0x38, 0x00, 0x08])
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

    bsi_ident = peugeot_scan._read_ident(client, BSI, [])
    bsi_vin = next((i["value"] for i in bsi_ident if i["label"] == "VIN"), None)
    check("VIN read from BSI", bsi_vin == FAKE_VIN, str(bsi_vin))

    eng_dtcs = peugeot_scan._read_dtcs(client, ENGINE, [])
    check("engine returns 2 faults", len(eng_dtcs) == 2, str(len(eng_dtcs)))
    if eng_dtcs:
        check("engine DTC decodes P0234", eng_dtcs[0]["code"] == "P0234", eng_dtcs[0]["code"])
        check("engine DTC hex 023400", eng_dtcs[0]["hex"] == "023400", eng_dtcs[0]["hex"])
        check("engine DTC flags include confirmed", "confirmed" in eng_dtcs[0]["flags"],
              ",".join(eng_dtcs[0]["flags"]))

    # The KWP-flavour ABS (like the field-test car): UDS reads all refuse, the
    # 10 03 session refuses, 10 C0 opens, and 17 FF 00 delivers the fault.
    abs_trace = []
    abs_dtcs = peugeot_scan._read_dtcs(client, ABS, abs_trace)
    check("KWP ABS faults via 10 C0 + 17 FF 00", len(abs_dtcs) == 1, str(len(abs_dtcs)))
    if abs_dtcs:
        check("KWP DTC decodes C1335", abs_dtcs[0]["code"] == "C1335", abs_dtcs[0]["code"])
        check("KWP DTC flagged as KWP", abs_dtcs[0]["flags"] == ["stored (KWP)"],
              ",".join(abs_dtcs[0]["flags"]))
    check("PSA session 10 C0 was opened for ABS", ABS in car.ext_started)
    check("trace records the KWP ladder",
          any("17 FF 00" in line for line in abs_trace), " | ".join(abs_trace))

    abs_ident = peugeot_scan._read_ident(client, ABS, [])
    check("KWP ABS ident via zone 21 80",
          any("9665344180" in i["value"] for i in abs_ident),
          str(abs_ident))

    air_dtcs = peugeot_scan._read_dtcs(client, AIRBAG, [])  # needs session escalation
    check("airbag fault read after extended-session escalation", len(air_dtcs) == 1, str(len(air_dtcs)))
    if air_dtcs:
        check("airbag DTC decodes B1004", air_dtcs[0]["code"] == "B1004", air_dtcs[0]["code"])
    check("extended session was actually started for airbag", AIRBAG in car.ext_started)

    uds.clear_dtcs(client, ENGINE)
    check("clear accepted on engine", ENGINE in car.cleared)

    # --- integration: the health-check reads (EV DIDs + HDi 0x21 block) ------
    from backend.profiles.peugeot import pids as peugeot_pids

    dpf_body = peugeot_pids._read_block(client, ENGINE, "CB8001")
    check("DPF 0x21 block read strips echo to 32 bytes",
          dpf_body is not None and len(dpf_body) == 32,
          str(len(dpf_body) if dpf_body else 0))
    if dpf_body:
        soot = peugeot_pids.BLOCKS[0][3][0][2](dpf_body)
        regen = peugeot_pids.BLOCKS[0][3][3][2](dpf_body)
        check("DPF soot load decodes to 12.0 g", soot == "12.0", str(soot))
        check("distance since last regen decodes to 220 km", regen == "220", str(regen))

    soh_body = peugeot_pids._read_did(client, BMS, 0xD860, set())
    check("EV SOH DID read", soh_body is not None)
    if soh_body:
        soh = peugeot_pids.DIDS[0][4](soh_body)
        check("HV battery SOH decodes to 92.0 %", soh == "92.0", str(soh))
    odo_body = peugeot_pids._read_did(client, BMS, 0xD49C, set())
    check("EV odometer read", odo_body is not None and
          abs(peugeot_pids.ODO_SOURCES[0][3](odo_body) - 30000.0) < 0.5)

    check("EV BMS module in id map (0x6B4 -> 0x694)", id_map.get(0x6B4) == 0x694)

    hsim = peugeot_pids.live(None, "sim")
    check("sim health check ok+simulated", hsim["ok"] and hsim["simulated"])
    check("sim health check labelled SIMULATED", "SIMULATED" in hsim["message"])

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
