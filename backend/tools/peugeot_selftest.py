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
        self.kwp81_only = set()  # modules where even 10 C0 is silent: 81 or nothing
        # How a KWP engine exposes its VIN: "b0" = PSA zone 21 B0 (ludwig-v),
        # "1a" = standard KWP 1A 90, "none" = nowhere (zones silent).
        self.vin_via = "b0"

    def handle(self, tx, req):
        if not req:
            return None
        # Generic OBD. Like the real RCZ, this car answers the FUNCTIONAL
        # broadcast 0x7DF as well as the physical engine slot 0x7E0.
        if tx in (0x7E0, 0x7DF):
            if req[0] == 0x01 and len(req) >= 2:
                pid_data = {
                    0x0C: bytes([0x1A, 0xF8]),   # 1726.0 rpm
                    0x05: bytes([0x5A]),          # 50 C coolant
                    0x42: bytes([0x37, 0x9E]),    # 14.238 V
                }
                if req[1] in pid_data:
                    return bytes([0x41, req[1]]) + pid_data[req[1]]
                return None                       # unsupported PID: silence
            if req[0] == 0x09 and len(req) >= 2 and req[1] == 0x04:
                return bytes([0x49, 0x04, 0x01]) + b"5FV0123456"  # calibration id
            return None
        if tx not in PRESENT:
            return None
        svc = req[0]
        if svc == 0x3E:                                   # TesterPresent
            return bytes([0x7E, 0x00])
        if svc == 0x10 and len(req) >= 2:                 # session control
            if tx in self.kwp81_only:                     # 308 CC field shape:
                return None                               # both 10s stay silent
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
        if svc == 0x81:                                   # KWP StartCommunication
            if tx in self.kwp:
                self.sessions[tx] = True
                return bytes([0xC1, 0xEA, 0x8F])
        if svc == 0x17:                                   # KWP ReadStatusOfDTC
            if tx in self.kwp and self.sessions.get(tx):
                return bytes([0x57, 0x01, 0x53, 0x35, 0x60])   # C1335
            return bytes([0x7F, 0x17, 0x11])
        if svc == 0x21 and len(req) == 2:                 # KWP zone read
            if tx == ABS and req[1] == 0x80 and self.sessions.get(tx):
                return bytes([0x61, 0x80]) + b"ABS8.1 9665344180"
            # MED17-style KWP engine (like the 308 CC field car). Zone 80 is
            # the PSA "ZA" block -- part number + serial, NO VIN (exactly what
            # the RCZ field car returned); zone B0 is the PSA VIN zone.
            if tx == ENGINE and tx in self.kwp and self.sessions.get(tx):
                if req[1] == 0x80:
                    return bytes([0x61, 0x80]) + b"MED17.4 9663711580 003966"
                if req[1] == 0xB0 and self.vin_via == "b0":
                    return bytes([0x61, 0xB0]) + FAKE_VIN.encode()
            return bytes([0x7F, 0x21, 0x11])
        if svc == 0x1A and len(req) == 2:                 # KWP ReadECUIdent
            if (tx == ENGINE and tx in self.kwp and self.sessions.get(tx)
                    and req[1] == 0x90 and self.vin_via == "1a"):
                return bytes([0x5A, 0x90]) + FAKE_VIN.encode()
            return bytes([0x7F, 0x1A, 0x11])
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
            if tx == ENGINE and did == 0x1E20:   # stand-in oil-pressure DID
                return bytes([0x62, 0x1E, 0x20, 0x01, 0x90])
            # Imported THP catalog (208 GTi / VLud). 1.70 bar, 80 C, ~1135 mV.
            if tx == ENGINE and did == 0xD429:
                return bytes([0x62, 0xD4, 0x29, 0x29, 0x81])
            if tx == ENGINE and did == 0xD47D:
                return bytes([0x62, 0xD4, 0x7D, 0x01, 0x40])
            if tx == ENGINE and did == 0xD41D:
                return bytes([0x62, 0xD4, 0x1D, 0x16, 0x2B])
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

    # --- integration: the EP6 oil tooling's transport layer ------------------
    from backend.profiles.peugeot import oil as peugeot_oil

    rpm_body = peugeot_oil._read_pid(client, 0x0C)
    check("oil: generic rpm read on 0x7E0", rpm_body is not None)
    check("oil: rpm decodes to 1726.0",
          rpm_body is not None and peugeot_oil._decode_pid(0x0C, rpm_body) == 1726.0,
          str(peugeot_oil._decode_pid(0x0C, rpm_body) if rpm_body else None))
    volt_body = peugeot_oil._read_pid(client, 0x42)
    check("oil: module voltage decodes to 14.24 V",
          volt_body is not None and peugeot_oil._decode_pid(0x42, volt_body) == 14.24,
          str(peugeot_oil._decode_pid(0x42, volt_body) if volt_body else None))
    check("oil: unsupported PID (0x5C) reads as None",
          peugeot_oil._read_pid(client, 0x5C) is None)
    oil_body = peugeot_oil._read_did_raw(client, ENGINE, 0x1E20)
    check("oil: engine DID 1E20 raw payload 0190",
          oil_body is not None and oil_body.hex().upper() == "0190",
          oil_body.hex().upper() if oil_body else "None")
    d429 = peugeot_oil._read_did_raw(client, ENGINE, 0xD429)
    check("oil: D429 raw 2981 (1.70 bar)",
          d429 is not None and d429.hex().upper() == "2981",
          d429.hex().upper() if d429 else "None")
    dec = peugeot_oil.decode_oil_did(0xD429, d429) if d429 else None
    check("oil: D429 decodes to 1.7 bar",
          dec == ("oil_pressure_bar", 1.7, "bar"), str(dec))
    rail = peugeot_oil.decode_oil_did(0xD429, bytes([0xFF, 0xFF]))
    check("oil: D429 0xFFFF is sensor rail ~10.49 bar",
          rail is not None and rail[1] == 10.486, str(rail))
    d47d = peugeot_oil._read_did_raw(client, ENGINE, 0xD47D)
    dec_t = peugeot_oil.decode_oil_did(0xD47D, d47d) if d47d else None
    check("oil: D47D decodes to 80.0 C",
          dec_t == ("oil_temp_c", 80.0, "°C"), str(dec_t))
    check("oil: unimplemented DID (NRC 31) reads as None",
          peugeot_oil._read_did_raw(client, ENGINE, 0x1E21) is None)
    healthy = peugeot_oil.assess([
        {"engine_rpm": 800.0, "oil_pressure_bar": 1.7, "did_D429": "2981"},
        {"engine_rpm": 2000.0, "oil_pressure_bar": 2.0, "did_D429": "30D4"},
    ])
    check("oil: assess healthy idle is in_spec",
          healthy["verdict"] == "in_spec", healthy["verdict"])
    dead = peugeot_oil.assess([
        {"engine_rpm": 800.0, "oil_pressure_bar": 3.5, "did_D429": "55F0"},
    ])
    check("oil: assess 3.5 bar idle is high_mechanical_mode",
          dead["verdict"] == "high_mechanical_mode", dead["verdict"])
    check("oil: catalog json loaded",
          bool(peugeot_oil.REFERENCE.get("dids")))

    # Block C2 decode, against the real bytes captured on RCZ #1: idle sample
    # (1.43 bar) and the 3000 rpm sample (3.46 bar).
    idle_c2 = bytes.fromhex(
        "8F4C1B1C045423009030310606004E01A4012301220601FF01FFFFFFFFFFFF"
        "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF008AFFFFFFFFFFFFFFFFFFFFFF07006B1AFF")
    c2 = peugeot_oil.decode_c2(idle_c2)
    check("oil: C2 decodes idle oil pressure 1.44 bar",
          c2.get("oil_pressure_bar") == 1.44, str(c2.get("oil_pressure_bar")))
    check("oil: C2 reports the mirror copy and their difference",
          c2.get("oil_pressure_mirror_bar") == 1.38
          and c2.get("oil_pressure_delta_bar") == 0.06, str(c2))
    check("oil: C2 decode of a short block returns nothing rather than guessing",
          peugeot_oil.decode_c2(bytes(4)) == {})
    check("oil: trim decode -100%/0%/+99.2% at 0/128/255",
          (peugeot_oil._trim_pct(bytes([0])), peugeot_oil._trim_pct(bytes([128])),
           peugeot_oil._trim_pct(bytes([255]))) == (-100.0, 0.0, 99.2))
    check("oil: generic reads are sent functionally on 0x7DF",
          peugeot_oil.OIL_ID_MAP.get(0x7DF) == 0x7E8)

    # Cold-start verdicts, against the healthy RCZ #2 shape and the failure
    # shapes it has to separate from it.
    healthy = [{"t": 6.0, "oil_pressure_bar": 3.96, "oil_pressure_delta_bar": 0.0},
               {"t": 25.0, "oil_pressure_bar": 4.67, "oil_pressure_delta_bar": 0.25},
               {"t": 44.0, "oil_pressure_bar": 2.29, "oil_pressure_delta_bar": 0.01},
               {"t": 120.0, "oil_pressure_bar": 2.06, "oil_pressure_delta_bar": 0.03},
               {"t": 178.0, "oil_pressure_bar": 1.93, "oil_pressure_delta_bar": 0.02}]
    for label, samples, want in (
        ("healthy cold start is in_spec", healthy, "in_spec"),
        ("never decaying is stuck_high",
         [{"t": i * 10.0, "oil_pressure_bar": 5.2} for i in range(15)], "stuck_high"),
        ("pinned at the sensor rail is sensor_rail",
         [{"t": i * 10.0, "oil_pressure_bar": 10.4} for i in range(15)], "sensor_rail"),
        ("no pressure at all is low_not_delivering",
         [{"t": i * 10.0, "oil_pressure_bar": 0.4} for i in range(15)],
         "low_not_delivering"),
        ("the two copies parting company is split",
         [{"t": i * 10.0, "oil_pressure_bar": 4.0 if i < 4 else 2.0,
           "oil_pressure_delta_bar": 0.9} for i in range(15)], "split"),
    ):
        got = peugeot_oil.assess_coldstart(samples)["verdict"]
        check(f"oil: {label}", got == want, got)
    check("oil: assess_coldstart flags a start that was not actually cold",
          any("NOT a cold start" in n for n in
              peugeot_oil.assess_coldstart(healthy, {"coolant_c": 85.0})["notes"]))
    check("oil: assess_coldstart with no pressure says so",
          peugeot_oil.assess_coldstart([{"t": 1.0}])["verdict"] == "no_pressure_data")
    check("oil: sim spot is labelled",
          peugeot_oil.spot(None, mode="sim")["simulated"] is True)

    # Pump command: the healthy cars step ~240 cold down to ~35 warm. A command
    # that never moves is the failure this has to name.
    warm_cold = [
        {"t": 6.0, "oil_pressure_bar": 3.96, "pump_command": 240},
        {"t": 25.0, "oil_pressure_bar": 4.67, "pump_command": 240},
        {"t": 44.0, "oil_pressure_bar": 2.29, "pump_command": 27},
        {"t": 120.0, "oil_pressure_bar": 2.06, "pump_command": 35},
        {"t": 178.0, "oil_pressure_bar": 1.93, "pump_command": 35},
    ]
    got = peugeot_oil.assess_coldstart(warm_cold)
    check("oil: healthy pump command is reported as stepping down",
          got["pumpCommand"]["steppedDown"] is True and got["verdict"] == "in_spec",
          str(got["pumpCommand"]))
    stuck = [{"t": i * 10.0, "oil_pressure_bar": 2.0, "pump_command": 0}
             for i in range(15)]
    stuck_result = peugeot_oil.assess_coldstart(stuck)
    check("oil: a pump command that never moves gets its own verdict",
          stuck_result["pumpVerdict"] == "stuck", str(stuck_result["pumpVerdict"]))
    check("oil: pressure and pump verdicts are reported independently",
          stuck_result["verdict"] == "low_cold",
          f"{stuck_result['verdict']} / {stuck_result['pumpVerdict']}")
    check("oil: healthy pump command verdict is ok",
          peugeot_oil.assess_coldstart(warm_cold)["pumpVerdict"] == "ok")
    c2_cold = bytearray(60)
    c2_cold[7:9] = (0x01, 0x8C)      # 3.96 bar
    c2_cold[18] = 240
    c2_cold[20] = 240
    decoded = peugeot_oil.decode_c2(bytes(c2_cold))
    check("oil: C2 decodes the pump command and its percentage",
          decoded.get("pump_command") == 240
          and decoded.get("pump_command_pct") == 94.1
          and decoded.get("pump_command_mirror") == 240, str(decoded))

    for name, result in (("probe", peugeot_oil.probe(None, "sim")),
                         ("coldstart", peugeot_oil.coldstart(None, mode="sim")),
                         ("blocks", peugeot_oil.block_capture(None, mode="sim")),
                         ("sweep", peugeot_oil.sweep(None, 0, 1, mode="sim")),
                         ("capture", peugeot_oil.capture(None, mode="sim"))):
        check(f"oil: sim {name} ok+simulated+labelled",
              result["ok"] and result["simulated"] and "SIMULATED" in result["message"])
    bad = peugeot_oil.sweep(None, 0x1000, 0x2FFF)
    check("oil: sweep refuses ranges over 4096 ids",
          bad["ok"] is False and "Range too wide" in bad["message"])
    bad = peugeot_oil.sweep(None, 0x1000, 0x10FF, address="nope")
    check("oil: sweep refuses unknown address", bad["ok"] is False)
    bad = peugeot_oil.capture(None, dids=["xyz"])
    check("oil: capture refuses non-hex dids", bad["ok"] is False)

    client.restore()
    check("restore hands ELM back to generic stack (ATSH7DF)", "ATSH7DF" in session.at_log)

    # --- integration: the VIN ladder -----------------------------------------
    # A KWP-era engine (the 308 CC shape): no Mode 09, UDS refused, zone 80
    # carries part number + serial only -- the VIN sits in the PSA zone B0.
    # The ladder must still land a VIN; that is the whole reason it exists.
    kwp_car = FakePsaCar()
    kwp_car.kwp = {ABS, ENGINE}
    kwp_session = FakeElmSession(kwp_car)
    vin_trace = []
    kwp_vin = peugeot_scan.read_vin(kwp_session, vin_trace)
    check("VIN ladder: KWP engine VIN via PSA session + 21 B0",
          kwp_vin == FAKE_VIN, str(kwp_vin))
    check("VIN ladder: trace names the winning rung",
          any("21 B0" in line and "VIN" in line for line in vin_trace),
          " | ".join(vin_trace))
    check("VIN ladder: 10 C0 was the session that opened (not 10 03)",
          kwp_car.ext_started == [ENGINE] and kwp_session.at_log.count("ATSH6A8") >= 1,
          str(kwp_car.ext_started))

    # The plain fake car answers no VIN rung at all (no 0902, F190 NAKs,
    # engine not KWP) -- the ladder must say None, not invent one.
    silent_session = FakeElmSession(FakePsaCar())
    check("VIN ladder: silent car returns None",
          peugeot_scan.read_vin(silent_session) is None)

    # The exact 308 CC field shape: 10 03 silent, 10 C0 silent, ONLY the bare
    # KWP StartCommunication 81 opens -- then zone B0 carries the VIN.
    cc_car = FakePsaCar()
    cc_car.kwp = {ENGINE}
    cc_car.kwp81_only = {ENGINE}
    cc_trace = []
    cc_vin = peugeot_scan.read_vin(FakeElmSession(cc_car), cc_trace)
    check("VIN ladder: 81-only engine (308 CC shape) still lands the VIN",
          cc_vin == FAKE_VIN, str(cc_vin))
    check("VIN ladder: trace shows 10 03 and 10 C0 going silent first",
          any("10 03" in l and "no answer" in l for l in cc_trace)
          and any("10 C0" in l and "no answer" in l for l in cc_trace)
          and any("KWP start 81" in l and "OK" in l for l in cc_trace),
          " | ".join(cc_trace))

    # An engine whose PSA zones all NAK but answers the standard KWP2000
    # ReadECUIdentification 1A 90 -- the ladder's last VIN rung.
    oneA_car = FakePsaCar()
    oneA_car.kwp = {ENGINE}
    oneA_car.vin_via = "1a"
    oneA_trace = []
    oneA_vin = peugeot_scan.read_vin(FakeElmSession(oneA_car), oneA_trace)
    check("VIN ladder: 1A 90 fallback when the PSA zones all refuse",
          oneA_vin == FAKE_VIN, str(oneA_vin))
    check("VIN ladder: 1A rung named in the trace",
          any("1A 90" in l and "VIN" in l for l in oneA_trace),
          " | ".join(oneA_trace))

    # Zone 80's part-number + serial text must never surface as a VIN even
    # when it is the only zone that answers.
    za_car = FakePsaCar()
    za_car.kwp = {ENGINE}
    za_car.vin_via = "none"
    check("VIN ladder: ZA-only engine (part number, no VIN) returns None",
          peugeot_scan.read_vin(FakeElmSession(za_car)) is None)

    # A binary payload must never false-match the VIN pattern (its hex dump
    # is itself valid VIN characters).
    check("VIN ladder: binary payload never false-matches",
          peugeot_scan._vin_in(bytes(range(0x00, 0x30)), 0) is None)
    check("VIN ladder: real zone payload matches",
          peugeot_scan._vin_in(
              bytes([0x61, 0x80]) + b"MED17.4 " + FAKE_VIN.encode(), 2) == FAKE_VIN)
    # The RCZ field regression: zone 80's part number + serial ran to 17
    # digits and the old pattern called it a VIN (plus a false mismatch
    # warning). A real WMI carries letters; all digits must refuse.
    check("VIN regex: RCZ part-number string is not a VIN",
          peugeot_scan._VIN_RE.search("96637115800003966") is None
          and peugeot_scan._VIN_RE.search("96761847800003967") is None)
    check("VIN regex: real EU VIN still matches",
          peugeot_scan._VIN_RE.search("MED17.4 VF3R9FHZC9S123456 END") is not None)

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SELF-TEST PASSED: PSA id pairs, session escalation, UDS DTC/VIN/clear, "
          "ISO-TP reassembly all good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
