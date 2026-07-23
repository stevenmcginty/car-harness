"""Offline self-test for the Mazda profile: UDS-over-ELM327 transport, module
scan/clear, and the enhanced (mode-22) live values -- no car, no dongle.

A FakeSession plays a SkyActiv Mazda behind an ELM327: it answers the AT
recipe, tester-present, ident DIDs, DTC reads and clears the way the real
stack expects, including ISO-TP multi-frame formatting and negative
responses. obd_service._with_session is monkeypatched so the real scan()/
clear()/live() code paths run against it end to end.

Run:  cd car-harness; .venv\\Scripts\\python.exe -m backend.tools.mazda_selftest
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.obd import service as obd_service  # noqa: E402
from backend.obd import uds_elm327  # noqa: E402
from backend.obd.elm327 import ObdError  # noqa: E402
from backend.obd.uds_elm327 import Elm327UdsClient, reassemble  # noqa: E402
from backend.profiles.mazda import pids as mazda_pids  # noqa: E402
from backend.profiles.mazda import scan as mazda_scan  # noqa: E402
from backend.profiles.mazda import snapshot as mazda_snap  # noqa: E402

PASS = []
FAIL = []


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

    # Universal OBD Mode 09 PID 02 VIN the scan now reads first (any ~2001+ car
    # answers it). Matches the PCM's F190 here, as a real car's would.
    OBD_VIN = "SELFTEST000MAZDA1"

    # broadcast frames the fake car emits while ATMA monitors a filtered id
    MONITOR = {
        0x202: "202" + "0D48" + "1194" + "1E00" + "0000",  # 850rpm 45km/h 12%
        0x420: "420" + "80" + "0000000000" + "324C",       # 88°C / 19°C
        0x43E: "43E" + "0000000010" + "800000",            # driver door + boot open
        0x3D1: "3D1" + "00000000" + "025F08" + "00",       # 38850 km
    }

    def __init__(self):
        self.protocol_num = 6
        self.protocol = "ISO 15765-4 (CAN 11/500)"
        self.at_log = []
        self.header = "7DF"
        self.cra = None
        self.cleared = set()
        self.present = {0x7E0, 0x7E1, 0x7E4}

    def ensure_protocol(self):
        pass

    def query(self, mode, pid, timeout=None):
        # Generic OBD path used by obd_service._read_vin (Mode 09 PID 02 = VIN).
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
        if not cmd:  # the monitor-stop interrupt char
            return "STOPPED\r\r>"
        if cmd == "ATMA":
            line = self.MONITOR.get(self.cra)
            return f"{line}\r{line}\r" if line else ""
        try:
            req_id = int(self.header, 16)
        except ValueError:
            return "NO DATA\r>"
        return self._reply(req_id, cmd) + "\r\r>"

    # ------------------------------------------------------------- fake car
    def _reply(self, req_id, cmd):
        if req_id not in self.present:
            return "NO DATA"
        if cmd == "3E00":
            return "7E00"
        if cmd == "22F190" and req_id == 0x7E0:
            return iso_tp("62F190" + ascii_hex("SELFTEST000MAZDA1"))
        if cmd == "22F187":
            return iso_tp("62F187" + ascii_hex(f"PY01-{req_id:03X}-K2"))
        if cmd == "22F188" and req_id == 0x7E0:
            return iso_tp("62F188" + ascii_hex("PY0118881"))
        if cmd.startswith("22"):
            return "7F2231"  # request out of range: DID not supported
        if cmd.startswith("1902"):
            if req_id in self.cleared:
                return "5902AF"
            if req_id == 0x7E0:
                return iso_tp("5902AF" + "0302002F" + "04560008")
            if req_id == 0x7E4:
                return iso_tp("5902AF" + "40510008")
            return "5902AF"
        if cmd == "14FFFFFF":
            self.cleared.add(req_id)
            return "54"
        return "NO DATA"


def with_fake_session(fake):
    """Monkeypatch obd_service._with_session to run against ``fake``."""
    def _with_session(adapter, fn):
        return fn(fake, None)
    return _with_session


def main():
    # ------------------------------------------------------------ reassemble
    r = reassemble("7E00\r>", "3E00")
    check("reassemble single frame", r == [bytes.fromhex("7E00")], repr(r))

    vin_payload = "490201" + ascii_hex("SELFTEST000MAZDA1")  # 20 bytes
    multi = iso_tp(vin_payload) + "AA" + "\r>"  # trailing pad byte to trim
    r = reassemble(multi, "0902")
    check("reassemble multi-frame length trim",
          r == [bytes.fromhex(vin_payload)], repr(r))

    r = reassemble("7F1902 31\r>", "1902AF")
    check("reassemble keeps negative responses",
          r == [bytes.fromhex("7F190231")], repr(r))

    try:
        reassemble("CAN ERROR\r>", "3E00")
        check("reassemble raises on link error", False)
    except ObdError as e:
        check("reassemble raises on link error", e.kind == "link", e.kind)

    try:
        reassemble("NO DATA\r>", "3E00")
        check("reassemble raises on NO DATA", False)
    except ObdError as e:
        check("reassemble raises on NO DATA", e.kind == "no_data", e.kind)

    # ------------------------------------------------------- client AT recipe
    fake = FakeSession()
    client = Elm327UdsClient(fake)
    client.begin()
    client.request(0x7E0, bytes([0x3E, 0x00]))
    check("aim sends the 11-bit recipe",
          fake.at_log[:2] == ["ATAT0", "ATST64"] and
          "ATSH7E0" in fake.at_log and "ATFCSH7E0" in fake.at_log and
          "ATCRA7E8" in fake.at_log and "ATFCSD300000" in fake.at_log and
          "ATFCSM1" in fake.at_log,
          str(fake.at_log))
    n_ats = len(fake.at_log)
    client.request(0x7E0, bytes([0x3E, 0x00]))
    check("aim caches the current target", len(fake.at_log) == n_ats)
    client.restore()
    check("restore hands the ELM back",
          fake.at_log[-5:] == ["ATCRA", "ATSH7DF", "ATFCSM0", "ATAT1", "ATST32"],
          str(fake.at_log[-5:]))

    # ----------------------------------------------------------- full scan()
    original = obd_service._with_session
    fake = FakeSession()
    obd_service._with_session = with_fake_session(fake)
    try:
        report = mazda_scan.scan({"name": "Fake adapter"}, "real")
        check("scan ok", report["ok"] is True, report.get("message"))
        by_addr = {m["address"]: m for m in report["modules"]}
        check("scan sees 3 present modules",
              sum(1 for m in report["modules"] if m["present"]) == 3)
        check("scan absent module marked",
              by_addr["0x7E2"]["present"] is False)
        pcm = by_addr["0x7E0"]
        check("PCM ident read",
              any(i["label"] == "Software" and i["value"] == "PY0118881"
                  for i in pcm["ident"]), str(pcm["ident"]))
        check("VIN surfaced from PCM",
              report["vehicle"]["vin"] == "SELFTEST000MAZDA1",
              str(report["vehicle"]))
        check("scan message leads with the VIN",
              report["message"].startswith("VIN SELFTEST000MAZDA1. "),
              report["message"])
        codes = sorted(d["code"] for d in pcm["dtcs"])
        check("PCM DTCs decoded", codes == ["P0302", "P0456"], str(codes))
        check("ABS C-code decoded",
              [d["code"] for d in by_addr["0x7E4"]["dtcs"]] == ["C0051"],
              str(by_addr["0x7E4"]["dtcs"]))
        check("scan restored the ELM", fake.header == "7DF", fake.header)

        # ------------------------------------------------------------ clear()
        result = mazda_scan.clear({"name": "Fake adapter"}, "real", "0x7E0")
        check("targeted clear ok",
              result["ok"] is True and result["cleared"] == ["0x7E0"],
              str(result))
        report2 = mazda_scan.scan({"name": "Fake adapter"}, "real")
        pcm2 = {m["address"]: m for m in report2["modules"]}["0x7E0"]
        check("cleared module reads no faults", pcm2["dtcs"] == [])

        fake2 = FakeSession()
        obd_service._with_session = with_fake_session(fake2)
        result = mazda_scan.clear({"name": "Fake adapter"}, "real", None)
        check("clear-all hits every present module",
              sorted(result["cleared"]) == ["0x7E0", "0x7E1", "0x7E4"],
              str(result))

        # ----------------------------------------------- enhanced live values
        fake3 = FakeSession()
        obd_service._with_session = with_fake_session(fake3)
        n_defined = len(mazda_pids.PCM_DIDS)
        r = mazda_pids.live({"name": "Fake adapter"}, "real")
        check("live ok against non-answering car (all DIDs skipped)",
              r["ok"] is True and r["readings"] == [], str(r))
        check("live restored the ELM", fake3.header == "7DF", fake3.header)

        # Plumbing + decode check with a synthetic DID the fake car answers.
        class AnsweringSession(FakeSession):
            def _reply(self, req_id, cmd):
                if cmd == "22DEAD" and req_id == 0x7E0:
                    return "62DEAD0348"   # 0x0348 = 840
                return super()._reply(req_id, cmd)

        fake4 = AnsweringSession()
        obd_service._with_session = with_fake_session(fake4)
        saved = mazda_pids.PCM_DIDS
        mazda_pids.PCM_DIDS = list(saved) + [
            (0x7E0, 0xDEAD, "Selftest value", "rpm",
             lambda b: f"{((b[0] << 8) | b[1]) / 4:.0f}" if len(b) >= 2 else None),
        ]
        try:
            r = mazda_pids.live({"name": "Fake adapter"}, "real")
            got = {x["name"]: x for x in r["readings"]}
            check("live decodes an answering DID",
                  got.get("Selftest value", {}).get("value") == "210" and
                  got.get("Selftest value", {}).get("unit") == "rpm",
                  str(r["readings"]))
            check("live skips unsupported DIDs quietly",
                  len(r["readings"]) == 1, str(r["readings"]))
        finally:
            mazda_pids.PCM_DIDS = saved
        print(f"[info] PCM_DIDS currently defines {n_defined} enhanced value(s)")

        # Real table decodes: fake car answers two community DIDs.
        class DidSession(FakeSession):
            def _reply(self, req_id, cmd):
                if req_id == 0x7E0 and cmd == "221310":
                    return "62131033AE"  # oil temp (13230/100)-40 = 92.3
                if req_id == 0x726 and cmd == "224028":
                    return "62402854"    # battery charge 84%
                return super()._reply(req_id, cmd)

        fake5 = DidSession()
        fake5.present.add(0x726)
        obd_service._with_session = with_fake_session(fake5)
        r = mazda_pids.live({"name": "Fake adapter"}, "real")
        got = {x["name"]: x["value"] for x in r["readings"]}
        check("oil-temp DID decodes via the real table",
              got.get("Engine oil temperature") == "92.3", str(got))
        check("battery-charge DID decodes via the real table",
              got.get("12V battery charge") == "84", str(got))

        # ------------------------------------------------- broadcast snapshot
        frames = mazda_snap.parse_monitor_text(
            "SEARCHING...\rBUFFER FULL\r2020D4811941E000000\r202\r20FF\rSTOPPED\r>", 0x202
        )
        check("monitor parse keeps only whole matching frames",
              frames == [bytes.fromhex("0D4811941E000000")], str(frames))

        r = mazda_snap.decode_frame(0x202, bytes.fromhex("0D4811941E000000"))
        by_name = {x["name"]: x for x in r}
        check("snapshot decodes rpm/speed/pedal",
              by_name.get("Engine RPM", {}).get("value") == "850" and
              by_name.get("Speed", {}).get("value") == "28.0" and
              by_name.get("Accelerator pedal", {}).get("value") == "12.0",
              str(r))
        r = mazda_snap.decode_frame(0x43E, bytes.fromhex("0000000010800000"))
        check("snapshot decodes doors",
              r and r[0]["value"] == "driver door, boot open", str(r))
        r = mazda_snap.decode_frame(0x3D1, bytes.fromhex("00000000025F0800"))
        check("snapshot decodes odometer",
              r and "38,850 km" in r[0]["value"], str(r))

        fake6 = FakeSession()
        obd_service._with_session = with_fake_session(fake6)
        r = mazda_snap.snapshot({"name": "Fake adapter"}, "real")
        names = {x["name"] for x in r["readings"]}
        check("snapshot end-to-end decodes the fake car's frames",
              r["ok"] and {"Engine RPM", "Coolant temperature", "Doors", "Odometer"} <= names,
              str(r["readings"]))
        check("snapshot restores headers/filter",
              fake6.at_log[-2:] == ["ATCRA", "ATH0"] and fake6.cra is None,
              str(fake6.at_log[-4:]))

        # --------------------- VIN-first via Mode 09 when no module gives F190
        # The real field-test case: Mazda modules answered fine but none
        # volunteered a VIN, so the report must fall back to the universal
        # OBD Mode 09 read. A distinct VIN here proves Mode 09 is the source.
        class NoModuleVinSession(FakeSession):
            OBD_VIN = "MODE09VINMAZDA001"

            def _reply(self, req_id, cmd):
                if cmd == "22F190":
                    return "7F2231"  # DID not supported -> no module VIN
                return super()._reply(req_id, cmd)

        fake7 = NoModuleVinSession()
        obd_service._with_session = with_fake_session(fake7)
        report3 = mazda_scan.scan({"name": "Fake adapter"}, "real")
        check("modules still answer when no module gives F190",
              sum(1 for m in report3["modules"] if m["present"]) == 3,
              str(report3.get("message")))
        check("VIN falls back to OBD Mode 09 when no module volunteers one",
              report3["vehicle"]["vin"] == "MODE09VINMAZDA001",
              str(report3["vehicle"]))
        check("Mode-09 fallback VIN also leads the message",
              report3["message"].startswith("VIN MODE09VINMAZDA001. "),
              report3["message"])
    finally:
        obd_service._with_session = original

    # ------------------------------------------------------------- sim paths
    s = mazda_scan.scan(None, "sim")
    check("sim scan labelled", s["ok"] and s["simulated"] and "SIMULATED" in s["message"])
    c = mazda_scan.clear(None, "sim")
    check("sim clear labelled", c["ok"] and c["simulated"])
    l = mazda_pids.live(None, "sim")
    check("sim live labelled", l["ok"] and l["simulated"])
    sn = mazda_snap.snapshot(None, "sim")
    check("sim snapshot labelled", sn["ok"] and sn["simulated"])

    # ------------------------------------------------------- routes + MCP
    # The brand routes are generic (/api/{brand}/...) -- assert they actually
    # answer for Mazda through the app, not merely that a path is registered.
    from fastapi.testclient import TestClient

    from backend.main import app
    # No `with`: keeps the app's startup hook (UDP discovery responder) from
    # running inside a selftest. Plain requests still exercise the routes.
    client = TestClient(app)
    r = client.get("/api/mazda/scan?mode=sim").json()
    check("API sim scan over the generic brand route", r.get("ok") and r.get("simulated"))
    r = client.get("/api/mazda/live?mode=sim").json()
    check("API sim live over the generic brand route", r.get("ok") and r.get("simulated"))
    r = client.get("/api/mazda/snapshot?mode=sim").json()
    check("API sim snapshot over the generic brand route", r.get("ok") and r.get("simulated"))
    r = client.post("/api/mazda/clear", json={"mode": "sim", "confirm": True}).json()
    check("API sim clear over the generic brand route", r.get("ok") and r.get("simulated"))
    check("API rejects unknown brands",
          client.get("/api/notacar/scan?mode=sim").status_code == 404)
    profs = client.get("/api/profiles").json().get("profiles", [])
    mz = next((p for p in profs if p.get("id") == "mazda"), {})
    check("API profile carries features + panel",
          bool(mz.get("features", {}).get("scan")) and bool(mz.get("panel")))
    from backend import mcp_server
    check("MCP tools registered",
          all(hasattr(mcp_server, n) for n in
              ("mazda_scan_modules", "mazda_clear_module", "mazda_live_values",
               "mazda_snapshot")))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
