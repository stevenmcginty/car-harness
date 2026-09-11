"""Offline self-test for the Nissan AUDIO profile: UDS-over-ELM327 transport,
module discover/scan/clear, and the Bose-amp audio_faults helper -- no car, no
dongle.

A FakeSession plays a Z34 370Z behind an ELM327: it answers the AT recipe,
tester-present, ident DIDs, DTC reads and clears the way the real stack expects,
including ISO-TP multi-frame formatting. It responds on BOTH addressing
conventions -- powertrain at 0x7E0 (response 0x7E8) and a Nissan body/AV node at
0x74D (response 0x76D) that carries the amp codes B1F00-49 'BOSE AMP' and U1263.
obd_service._with_session is monkeypatched so the real scan()/discover()/
clear()/audio_faults() code paths run against it end to end.

Run:  cd car-harness; .venv\\Scripts\\python.exe -m backend.tools.nissan_selftest
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.obd import service as obd_service  # noqa: E402
from backend.obd.elm327 import ObdError  # noqa: E402
from backend.obd.uds_elm327 import Elm327UdsClient  # noqa: E402
from backend.profiles.nissan import pids as nissan_pids  # noqa: E402
from backend.profiles.nissan import scan as nissan_scan  # noqa: E402

PASS = []
FAIL = []

# The AV node that carries the amp codes in this fake car. It is one of the
# is_av body-block candidates, so audio_faults() sweeps and finds it.
AV_NODE = 0x74D


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not cond:
        line += f" -- {detail}"
    print(line)


def ascii_hex(text):
    return text.encode("ascii").hex().upper()


def iso_tp(payload_hex):
    """Render a payload the way an ELM327 prints it (multi-frame if > 7 bytes)."""
    nbytes = len(payload_hex) // 2
    if nbytes <= 7:
        return payload_hex
    lines = [f"{nbytes:03X}"]
    frame = 0
    for i in range(0, len(payload_hex), 14):
        lines.append(f"{frame & 0xF:X}:" + payload_hex[i:i + 14])
        frame += 1
    return "\r".join(lines)


class FakeSession:
    """Just enough of Elm327Session for Elm327UdsClient + the scan code."""

    OBD_VIN = "SELFTEST00Z34NISM"  # 17 chars

    def __init__(self):
        self.protocol_num = 6
        self.protocol = "ISO 15765-4 (CAN 11/500)"
        self.at_log = []
        self.header = "7DF"
        self.cra = None
        self.cleared = set()
        self.present = {0x7E0, 0x745, AV_NODE}

    def ensure_protocol(self):
        pass

    def query(self, mode, pid, timeout=None):
        if mode == 0x09 and pid == 0x02:
            return [bytes([0x49, 0x02, 0x01]) + self.OBD_VIN.encode("ascii")]
        raise ObdError("no data", kind="no_data")

    def at(self, cmd):
        self.at_log.append(cmd)
        if cmd.startswith("ATSH"):
            self.header = cmd[4:] or "7DF"
        if cmd == "ATCRA":
            self.cra = None
        elif cmd.startswith("ATCRA"):
            try:
                self.cra = int(cmd[5:], 16)
            except ValueError:
                self.cra = None
        return "OK"

    def _raw(self, data, timeout):
        cmd = data.decode("ascii").strip().upper()
        if not cmd:
            return "STOPPED\r\r>"
        try:
            req_id = int(self.header, 16)
        except ValueError:
            return "NO DATA\r>"
        return self._reply(req_id, cmd) + "\r\r>"

    def _reply(self, req_id, cmd):
        if req_id not in self.present:
            return "NO DATA"
        if cmd == "3E00":
            return "7E00"
        if cmd == "22F190" and req_id == 0x7E0:
            return iso_tp("62F190" + ascii_hex(self.OBD_VIN))
        if cmd == "22F188":
            return iso_tp("62F188" + ascii_hex(f"2839-{req_id:03X}"))
        if cmd.startswith("22"):
            return "7F2231"  # request out of range: DID not supported
        if cmd.startswith("1902"):
            if req_id in self.cleared:
                return "5902FF"
            if req_id == AV_NODE:
                # B1F00-49 'BOSE AMP' (9F0049) status 2F, U1263 (D26300) status 08
                return iso_tp("5902FF" + "9F00492F" + "D2630008")
            return "5902FF"  # present, no faults
        if cmd == "14FFFFFF":
            self.cleared.add(req_id)
            return "54"
        return "NO DATA"


def with_fake_session(fake):
    def _with_session(adapter, fn):
        return fn(fake, None)
    return _with_session


def main():
    # ----------------------------------------------------- id_map / table sanity
    idm = nissan_pids.id_map()
    check("id_map powertrain uses +8", idm.get(0x7E0) == 0x7E8, str(idm.get(0x7E0)))
    check("id_map body block uses +0x20 (BCM)", idm.get(0x745) == 0x765, str(idm.get(0x745)))
    check("id_map AV best-guess maps 0x74D->0x76D", idm.get(0x74D) == 0x76D, str(idm.get(0x74D)))
    check("AV candidates are the 0x74x block only",
          all(0x740 <= req <= 0x74F for req, *_ in nissan_pids.av_candidates()),
          str([hex(r) for r, *_ in nissan_pids.av_candidates()]))
    check("AUDIO_DTCS carries the sourced amp codes",
          nissan_pids.dtc_text("B1F00", "49") == "BOSE AMP"
          and "Bose amp" in nissan_pids.dtc_text("U1263"),
          str(nissan_pids.AUDIO_DTCS))
    check("is_amp_fault flags the amp codes, not a random one",
          nissan_pids.is_amp_fault("B1F00") and nissan_pids.is_amp_fault("U1263")
          and not nissan_pids.is_amp_fault("P0300"))

    # ------------------------------------------ client reads a 0x74x node on +0x20
    fake = FakeSession()
    client = Elm327UdsClient(fake, id_map=nissan_pids.id_map())
    client.begin()
    client.request(AV_NODE, bytes([0x3E, 0x00]))
    check("client aims the AV node's +0x20 response filter",
          "ATSH74D" in fake.at_log and "ATCRA76D" in fake.at_log, str(fake.at_log))
    client.restore()
    check("restore hands the ELM back", fake.header == "7DF", fake.header)

    original = obd_service._with_session
    try:
        # ------------------------------------------------------------ discover()
        fake = FakeSession()
        obd_service._with_session = with_fake_session(fake)
        d = nissan_scan.discover({"name": "Fake adapter"}, "real")
        by_addr = {m["address"]: m for m in d["modules"]}
        check("discover ok", d["ok"] is True, d.get("message"))
        check("discover finds the powertrain + body + AV nodes present",
              by_addr["0x7E0"]["present"] and by_addr["0x745"]["present"]
              and by_addr["0x74D"]["present"], str(sorted(a for a, m in by_addr.items() if m["present"])))
        check("discover marks absent body node not present",
              by_addr["0x74E"]["present"] is False)
        check("discover flags the AV candidate row", by_addr["0x74D"]["isAv"] is True)
        check("discover restored the ELM", fake.header == "7DF", fake.header)

        # ---------------------------------------------------------------- scan()
        fake = FakeSession()
        obd_service._with_session = with_fake_session(fake)
        report = nissan_scan.scan({"name": "Fake adapter"}, "real")
        av = {m["address"]: m for m in report["modules"]}["0x74D"]
        codes = sorted(d["code"] for d in av["dtcs"])
        check("scan ok", report["ok"] is True, report.get("message"))
        check("scan decodes the amp DTCs on the AV node",
              codes == ["B1F00", "U1263"], str(codes))
        b1f00 = next(d for d in av["dtcs"] if d["code"] == "B1F00")
        check("scan annotates B1F00-49 with 'BOSE AMP' text",
              b1f00["failureType"] == "49" and b1f00["audioText"] == "BOSE AMP"
              and b1f00["ampFault"] is True, str(b1f00))
        check("scan surfaced the VIN from the PCM",
              report["vehicle"]["vin"] == "SELFTEST00Z34NISM", str(report["vehicle"]))

        # -------------------------------------------------------- audio_faults()
        fake = FakeSession()
        obd_service._with_session = with_fake_session(fake)
        a = nissan_scan.audio_faults({"name": "Fake adapter"}, "real")
        check("audio_faults ok and ampFault True", a["ok"] and a["ampFault"] is True, str(a.get("message")))
        check("audio_faults collects both amp codes",
              sorted(d["code"] for d in a["ampDtcs"]) == ["B1F00", "U1263"],
              str(a["ampDtcs"]))
        check("audio_faults message names the amp fault + symptom",
              "BOSE AMP" in a["message"] and "rear speakers" in a["message"].lower(),
              a["message"])
        check("audio_faults only swept the body/AV block",
              all(0x740 <= int(n["address"], 16) <= 0x74F for n in a["nodes"]),
              str([n["address"] for n in a["nodes"]]))

        # audio_faults on a car with NO amp fault -> flag stays False
        class CleanAv(FakeSession):
            def _reply(self, req_id, cmd):
                if cmd.startswith("1902") and req_id == AV_NODE and req_id not in self.cleared:
                    return "5902FF"  # AV node answers but stores no faults
                return super()._reply(req_id, cmd)

        fake = CleanAv()
        obd_service._with_session = with_fake_session(fake)
        a2 = nissan_scan.audio_faults({"name": "Fake adapter"}, "real")
        check("audio_faults ampFault False when no amp code present",
              a2["ok"] and a2["ampFault"] is False and a2["ampDtcs"] == [], str(a2.get("message")))

        # ---------------------------------------------------------------- clear()
        fake = FakeSession()
        obd_service._with_session = with_fake_session(fake)
        r = nissan_scan.clear({"name": "Fake adapter"}, "real", "0x74D")
        check("targeted clear ok", r["ok"] and r["cleared"] == ["0x74D"], str(r))
        rep2 = nissan_scan.scan({"name": "Fake adapter"}, "real")
        av2 = {m["address"]: m for m in rep2["modules"]}["0x74D"]
        check("cleared AV node reads no faults", av2["dtcs"] == [], str(av2["dtcs"]))

        fake = FakeSession()
        obd_service._with_session = with_fake_session(fake)
        r = nissan_scan.clear({"name": "Fake adapter"}, "real", None)
        check("clear-all hits every present node (never the 0x7DF broadcast)",
              sorted(r["cleared"]) == ["0x745", "0x74D", "0x7E0"], str(r))
    finally:
        obd_service._with_session = original

    # ------------------------------------------------------------- sim paths
    check("sim discover labelled", (lambda s: s["ok"] and s["simulated"])(nissan_scan.discover(None, "sim")))
    s = nissan_scan.scan(None, "sim")
    check("sim scan labelled", s["ok"] and s["simulated"] and "SIMULATED" in s["message"])
    a = nissan_scan.audio_faults(None, "sim")
    check("sim audio_faults labelled + ampFault set", a["ok"] and a["simulated"] and a["ampFault"])
    c = nissan_scan.clear(None, "sim")
    check("sim clear labelled", c["ok"] and c["simulated"])

    # ------------------------------------------------------- routes + MCP
    from fastapi.testclient import TestClient

    from backend.main import app
    client = TestClient(app)
    r = client.get("/api/nissan/scan?mode=sim").json()
    check("API sim scan over the generic brand route", r.get("ok") and r.get("simulated"))
    r = client.get("/api/nissan/discover?mode=sim").json()
    check("API sim discover over the generic brand route", r.get("ok") and r.get("simulated"))
    r = client.post("/api/nissan/clear", json={"mode": "sim", "confirm": True}).json()
    check("API sim clear over the generic brand route", r.get("ok") and r.get("simulated"))
    profs = client.get("/api/profiles").json().get("profiles", [])
    nz = next((p for p in profs if p.get("id") == "nissan"), {})
    check("API lists the nissan profile with a panel",
          bool(nz) and bool(nz.get("panel")), str(nz.get("id")))

    from backend import mcp_server
    check("MCP tools registered",
          all(hasattr(mcp_server, n) for n in
              ("nissan_discover_modules", "nissan_scan_modules",
               "nissan_audio_faults", "nissan_clear_module")))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
