"""Offline self-test for the raw diagnostic channel (backend/obd/raw.py):
arbitrary mode-22 DID reads (11-bit + 29-bit) and the raw ELM327 command
sequence -- no car, no dongle.

A FakeSession plays a car behind an ELM327: an engine at 0x7E0 (with a VAG-style
cluster at 0x714 that only answers on its +0x6A response id), and an FCA engine
at 29-bit address 0x10 that only answers once the ELM is aimed with the
18DAxxF1 recipe. obd_service._with_session is monkeypatched so the real
read_dids()/elm_commands() code paths run against it end to end, then the HTTP
routes and MCP tools are checked on top.

Run:  cd car-harness; .venv\\Scripts\\python.exe -m backend.tools.raw_selftest
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.obd import raw as obd_raw  # noqa: E402
from backend.obd import service as obd_service  # noqa: E402

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
    """Just enough of Elm327Session for the raw channel's two clients."""

    VIN = "SELFTEST00RAW0CAR"

    def __init__(self, protocol_num=6):
        self.protocol_num = protocol_num
        self.protocol = "ISO 15765-4 (CAN 11/500)"
        self.at_log = []
        self.header = "7DF"
        self.cra = None

    def ensure_protocol(self):
        pass

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
        if cmd.startswith("AT"):
            return self.at(cmd) + "\r\r>"
        return self._reply(cmd) + "\r\r>"

    # ------------------------------------------------------------- fake car
    def _reply(self, cmd):
        # FCA engine at 29-bit address 0x10 -- answers only through the full
        # 18DAxxF1 aim (ATCP-short or full header, receive filter set right).
        if self.header in ("DA10F1", "18DA10F1"):
            if self.cra != 0x18DAF110:
                return "NO DATA"
            if cmd == "2218AA":
                return "6218AA0C80"  # DPF soot, 0x0C80 = 3200
            if cmd.startswith("22"):
                return "7F2231"
            return "NO DATA"
        # VAG-style cluster at 0x714: only answers on its +0x6A response id.
        if self.header == "714":
            if self.cra != 0x77E:
                return "NO DATA"
            if cmd == "22F187":
                return iso_tp("62F187" + ascii_hex("5G0-CLUSTER"))
            return "7F2231"
        # Plain engine at 0x7E0 (standard +8 pairing).
        if self.header == "7E0":
            if cmd == "221310":
                return "62131009C4"  # 0x09C4 = 2500
            if cmd == "22F190":
                return iso_tp("62F190" + ascii_hex(self.VIN))
            if cmd == "1003":
                return "500300320190"
            if cmd.startswith("22"):
                return "7F2231"
            return "NO DATA"
        return "NO DATA"


def with_fake_session(fake):
    def _with_session(adapter, fn):
        return fn(fake, None)
    return _with_session


ADAPTER = {"id": "fake", "type": "elm327-ble", "name": "fake"}


def main() -> int:
    real_with_session = obd_service._with_session

    # ------------------------------------------------------------- read_dids, 11-bit
    fake = FakeSession()
    obd_service._with_session = with_fake_session(fake)
    try:
        report = obd_raw.read_dids(ADAPTER, "real", "7E0",
                                   ["1310", "F190", "4242"], extended_session=True)
        rows = {r["did"]: r for r in report["results"]}
        check("11bit call ok", report["ok"] and not report["simulated"])
        check("known DID answers with payload + int",
              rows["1310"]["ok"] and rows["1310"]["payload"] == "09C4"
              and rows["1310"]["asInt"] == 2500, str(rows.get("1310")))
        check("multi-frame ASCII DID decodes",
              rows["F190"]["ok"] and rows["F190"]["ascii"] == FakeSession.VIN,
              str(rows.get("F190")))
        check("unsupported DID -> named NRC",
              not rows["4242"]["ok"] and "NRC 31" in rows["4242"]["error"],
              str(rows.get("4242")))
        check("extended session opened + traced",
              any("extended session open" in t for t in report.get("trace", [])))
        check("ELM restored after 11bit",
              fake.at_log[-5:] == ["ATCRA", "ATSH7DF", "ATFCSM0", "ATAT1", "ATST32"],
              str(fake.at_log[-6:]))

        # resp_id override (VAG +0x6A pairing)
        fake2 = FakeSession()
        obd_service._with_session = with_fake_session(fake2)
        report = obd_raw.read_dids(ADAPTER, "real", "714", ["F187"], resp_id="77E")
        row = report["results"][0]
        check("resp_id override reaches +0x6A module",
              row["ok"] and row["ascii"] == "5G0-CLUSTER", str(row))
        check("override set the receive filter", "ATCRA77E" in fake2.at_log)

        report = obd_raw.read_dids(ADAPTER, "real", "714", ["F187"])
        check("without resp_id the +0x6A module is silent (honest no-answer)",
              not report["results"][0]["ok"], str(report["results"][0]))

        # silent module
        report = obd_raw.read_dids(ADAPTER, "real", "7E5", ["1310"])
        check("silent module -> error row, call still ok",
              report["ok"] and not report["results"][0]["ok"])

        # protocol guard
        obd_service._with_session = with_fake_session(FakeSession(protocol_num=3))
        report = obd_raw.read_dids(ADAPTER, "real", "7E0", ["1310"])
        check("non-CAN protocol -> honest refusal",
              not report["ok"] and "not 11-bit" in report["message"], report["message"])

        # ------------------------------------------------------------- read_dids, 29-bit
        fake3 = FakeSession()
        obd_service._with_session = with_fake_session(fake3)
        report = obd_raw.read_dids(ADAPTER, "real", "10", ["18AA"], addressing="29bit")
        row = report["results"][0]
        check("29bit soot DID answers",
              row["ok"] and row["payload"] == "0C80" and row["asInt"] == 3200, str(row))
        check("29bit switched protocol", "ATSP7" in fake3.at_log)
        after_sp7 = fake3.at_log[fake3.at_log.index("ATSP7"):]
        check("29bit restored discovered protocol + functional header",
              "ATSP6" in after_sp7 and "ATSH7DF" in after_sp7, str(after_sp7))

        report = obd_raw.read_dids(ADAPTER, "real", "C7", ["2A05"], addressing="29bit")
        check("FCA TPMS 0xC7 refused",
              not report["ok"] and "immobilizer" in report["message"], report["message"])

        # ------------------------------------------------------------- validation
        checks = [
            obd_raw.read_dids(ADAPTER, "real", "ZZZ", ["1310"]),
            obd_raw.read_dids(ADAPTER, "real", "7E0", []),
            obd_raw.read_dids(ADAPTER, "real", "7E0", ["1310"] * 25),
            obd_raw.read_dids(ADAPTER, "real", "10", ["1310"],
                              addressing="29bit", resp_id="77E"),
            obd_raw.read_dids(ADAPTER, "real", "7E0", ["1310"], addressing="16bit"),
        ]
        check("input validation refuses bad hex/empty/oversize/mixed args",
              all(not r["ok"] for r in checks))

        report = obd_raw.read_dids(None, "sim", "7E0", ["1310"])
        check("read_dids sim is labelled",
              report["ok"] and report["simulated"] and "SIMULATED" in report["message"])

        # ------------------------------------------------------------- elm_commands
        fake4 = FakeSession()
        obd_service._with_session = with_fake_session(fake4)
        report = obd_raw.elm_commands(ADAPTER, "real",
                                      ["ATSH7E0", "ATCRA7E8", "ATSP7", "22 13 10"])
        check("elm sequence executes in order",
              report["ok"] and len(report["replies"]) == 4
              and report["replies"][3]["command"] == "22 13 10")
        tail = fake4.at_log[-9:]
        check("elm restore superset ran",
              all(c in tail for c in ("ATCRA", "ATCEA", "ATCAF1", "ATH0",
                                      "ATSH7DF", "ATFCSM0", "ATAT1", "ATST32")),
              str(tail))
        check("elm restored protocol after an ATSP", "ATSP6" in tail, str(tail))

        for bad in ("ATMA", "ATZ", "ATD", "ATLP", "AT MA"):
            report = obd_raw.elm_commands(ADAPTER, "real", [bad])
            check(f"blocked command refused: {bad}",
                  not report["ok"] and "Refused" in report["message"], report["message"])
        report = obd_raw.elm_commands(ADAPTER, "real", ["ATD0"])
        check("ATD0 (DLC display) is allowed -- only bare ATD is blocked", report["ok"])

        report = obd_raw.elm_commands(None, "sim", ["ATRV"])
        check("elm sim is labelled", report["ok"] and report["simulated"])
    finally:
        obd_service._with_session = real_with_session

    # ------------------------------------------------------------- HTTP routes
    from fastapi.testclient import TestClient  # noqa: E402
    from backend.main import app  # noqa: E402

    client = TestClient(app)  # no `with` -- avoids the startup UDP responder
    r = client.post("/api/raw/did",
                    json={"mode": "sim", "target": "7E0", "dids": ["1310"]})
    check("route /api/raw/did sim",
          r.status_code == 200 and r.json().get("simulated") is True, r.text[:120])
    r = client.post("/api/raw/elm", json={"mode": "sim", "commands": ["ATRV"]})
    check("route /api/raw/elm refuses without confirm", r.status_code == 400)
    r = client.post("/api/raw/elm",
                    json={"mode": "sim", "commands": ["ATRV"], "confirm": True})
    check("route /api/raw/elm confirm + sim",
          r.status_code == 200 and r.json().get("simulated") is True, r.text[:120])
    r = client.post("/api/raw/did",
                    json={"mode": "real", "adapter": "nope",
                          "target": "7E0", "dids": ["1310"]})
    check("route /api/raw/did unknown adapter -> 404", r.status_code == 404)

    # ------------------------------------------------------------- MCP tools
    from backend import mcp_server  # noqa: E402

    report = mcp_server.read_dids("7E0", ["1310"], mode="sim")
    check("MCP read_dids sim", report.get("simulated") is True)
    report = mcp_server.elm_commands(["ATRV"], mode="sim")
    check("MCP elm_commands refuses without confirm", "Refused" in report.get("error", ""))
    report = mcp_server.elm_commands(["ATRV"], confirm=True, mode="sim")
    check("MCP elm_commands confirm + sim", report.get("simulated") is True)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed.")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
