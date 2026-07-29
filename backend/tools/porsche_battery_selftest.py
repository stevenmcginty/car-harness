"""Self-test for the Porsche Taycan DoIP battery-health stack, no car required.

Run:  python -m backend.tools.porsche_battery_selftest

Drives the REAL code -- core/doip.py's DoipDiagClient (routing activation,
DiagnosticMessage framing, ack/alive/response-pending handling) and the
porsche battery.py decoders and read pass -- against a fake Taycan gateway that
speaks ISO-13400 over a fake socket. It proves the parts that make the Taycan
battery check honest and correct:

  * the DoIP generic-header + routing-activation + diagnostic-message wire format,
  * an alive-check mid-activation is answered, not fatal,
  * UDS "response pending" (7F xx 78) extends the wait instead of erroring,
  * the candidate BMS-address search skips a non-answering address and keeps the
    one that answers the SOC read,
  * every decoder (SOC, pack voltage/current/temp, cell voltage) matches its
    community-captured scaling, with out-of-range guards returning None,
  * the cell-spread balance figure is derived correctly,
  * SoH is never emitted as a number, and the provenance caveat always rides,
  * the honest walls: no DoIP gateway, and gateway-up-but-no-candidate-answers,
  * the generic FastAPI /api/porsche/battery route answers in sim.

Exits 0 on PASS, 1 on FAIL.
"""

import json
import socket
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core import doip  # noqa: E402
from backend.core import interfaces  # noqa: E402
from backend.profiles.porsche import battery  # noqa: E402

CAR_IP = "169.254.7.10"
ENTITY_ADDR = 0x1010
BMS_ADDR = 0x1706
TESTER = doip.DEFAULT_TESTER_ADDRESS
FAKE_VIN = "WP0ZZZY1ZLSA00001"


def _diag_frame(source, target, uds):
    return doip.build_generic(doip.PT_DIAG_MESSAGE, struct.pack(">HH", source, target) + uds)


class FakeTaycanGateway:
    """A tiny DoIP brain. Answers routing activation, then UDS 22 reads on the
    BMS at BMS_ADDR; every other target is negative-acked. One DID
    (0x1E3B pack voltage) is answered with a response-pending frame first."""

    # DID payloads chosen so the decoders land on exact, checkable values.
    DID_PAYLOAD = {
        0x028C: bytes([0xB4]),                    # SOC 180/2.5 = 72 %
        0x1E3B: bytes([0x0B, 0x9A]),              # voltage 2970/4 = 742.5 V
        0x1E3D: bytes([0x00, 0x02, 0x48, 0xBA]),  # current (149690-150000)/100 = -3.1 A
        0x2A0B: bytes([0x80]),                    # temp 128/2-40 = 24 C
    }
    # Six cells then silence -> the sampler stops after the miss run.
    CELLS = [3.902, 3.905, 3.898, 3.901, 3.907, 3.900]

    def __init__(self):
        self.activated = False
        for i, v in enumerate(self.CELLS):
            raw = round((v - 1) * 1000)
            self.DID_PAYLOAD[0x1E40 + i] = bytes([(raw >> 8) & 0xFF, raw & 0xFF])

    def handle(self, payload_type, payload):
        """Return a list of raw DoIP frames to send back."""
        if payload_type == doip.PT_ROUTING_ACTIVATION_REQ:
            self.activated = True
            body = struct.pack(">HH", TESTER, ENTITY_ADDR) + bytes([doip.RA_SUCCESS]) + b"\x00\x00\x00\x00"
            # An alive-check request precedes the response, to exercise the answer path.
            alive = doip.build_generic(doip.PT_ALIVE_CHECK_REQ, struct.pack(">H", ENTITY_ADDR))
            return [alive, doip.build_generic(doip.PT_ROUTING_ACTIVATION_RESP, body)]
        if payload_type == doip.PT_ALIVE_CHECK_RESP:
            return []
        if payload_type == doip.PT_DIAG_MESSAGE:
            source = int.from_bytes(payload[0:2], "big")
            target = int.from_bytes(payload[2:4], "big")
            uds = payload[4:]
            ack = doip.build_generic(
                doip.PT_DIAG_MESSAGE_ACK, struct.pack(">HH", target, source) + bytes([0x00])
            )
            if target != BMS_ADDR:
                # Wrong ECU -> transport negative ack (client raises -> read = None).
                nack = doip.build_generic(
                    doip.PT_DIAG_MESSAGE_NACK, struct.pack(">HH", target, source) + bytes([0x03])
                )
                return [nack]
            if len(uds) >= 3 and uds[0] == 0x22:
                did = (uds[1] << 8) | uds[2]
                if did in self.DID_PAYLOAD:
                    reply = bytes([0x62, uds[1], uds[2]]) + self.DID_PAYLOAD[did]
                    frames = [ack]
                    if did == 0x1E3B:  # exercise response-pending before the real answer
                        pending = _diag_frame(BMS_ADDR, source, bytes([0x7F, 0x22, 0x78]))
                        frames.append(pending)
                    frames.append(_diag_frame(BMS_ADDR, source, reply))
                    return frames
                # unknown DID -> UDS negative response (client returns None)
                return [ack, _diag_frame(BMS_ADDR, source, bytes([0x7F, 0x22, 0x31]))]
        return []


class FakeSock:
    """In-memory socket wired to a FakeTaycanGateway. recv serves queued reply
    bytes; an empty queue raises socket.timeout, exactly like a real silent car."""

    def __init__(self, car):
        self.car = car
        self.buf = b""

    def sendall(self, data):
        offset = 0
        while offset + 8 <= len(data):
            _p, _i, ptype, length = struct.unpack(">BBHI", data[offset:offset + 8])
            payload = data[offset + 8: offset + 8 + length]
            offset += 8 + length
            for frame in self.car.handle(ptype, payload):
                self.buf += frame

    def recv(self, n):
        if not self.buf:
            raise socket.timeout("no data")
        chunk, self.buf = self.buf[:n], self.buf[n:]
        return chunk

    def settimeout(self, t):
        pass

    def close(self):
        pass


def main() -> int:
    failures = []

    def check(name, cond, extra=""):
        print(("  PASS  " if cond else "  FAIL  ") + name + (f"  ({extra})" if extra else ""))
        if not cond:
            failures.append(name)

    # --- decoders: exact scalings + range guards -----------------------------
    check("SOC decode 0xB4 -> 72", battery._soc(bytes([0xB4])) == "72")
    check("SOC out-of-range guarded", battery._soc(bytes([0xFF])) is None)  # 255/2.5=102 -> None
    check("pack voltage 0x0B9A -> 742.5", battery._pack_voltage(bytes([0x0B, 0x9A])) == "742.5")
    check("pack current -3.1 A",
          battery._pack_current(bytes([0x00, 0x02, 0x48, 0xBA])) == "-3.1")
    check("pack temp 0x80 -> 24", battery._pack_temp(bytes([0x80])) == "24")
    cv = battery._cell_voltage(bytes([0x0B, 0x56]))  # 2902/1000+1 = 3.902
    check("cell voltage 3.902", cv is not None and abs(cv - 3.902) < 1e-6, str(cv))
    check("cell voltage out-of-range guarded", battery._cell_voltage(bytes([0xFF, 0xFF])) is None)
    check("SoH DID absent from the read table (never a guessed number)",
          all(did not in (0xF1A0, 0x74) for did, *_ in battery._PACK_DIDS))

    # --- DoIP client framing helpers -----------------------------------------
    hdr = doip.build_generic(doip.PT_ROUTING_ACTIVATION_REQ, b"\x01\x02")
    check("generic header protocol+inverse", hdr[0] == 0x02 and hdr[1] == 0xFD)
    check("generic header type+length",
          struct.unpack(">H", hdr[2:4])[0] == doip.PT_ROUTING_ACTIVATION_REQ
          and struct.unpack(">I", hdr[4:8])[0] == 2)

    # --- integration: real DoipDiagClient vs the fake gateway ----------------
    car = FakeTaycanGateway()
    orig_conn = doip.socket.create_connection
    doip.socket.create_connection = lambda addr, timeout=None: FakeSock(car)
    try:
        client = doip.DoipDiagClient(CAR_IP)
        entity = client.activate()
        check("routing activation returns entity address", entity == ENTITY_ADDR, hex(entity or 0))
        check("gateway marked activated", car.activated)
        # response-pending path: pack voltage answers after a 7F..78
        volt = battery._read_did(client, BMS_ADDR, 0x1E3B)
        check("read through response-pending", volt == bytes([0x0B, 0x9A]), str(volt))
        # wrong target -> negative ack -> None (not an exception to the caller)
        check("non-BMS target read returns None", battery._read_did(client, 0x0001, 0x028C) is None)
        # candidate search finds the BMS (entity 0x1010 nacks SOC, 0x1706 answers)
        found = battery._find_bms_target(client, ENTITY_ADDR)
        check("candidate search lands on the answering BMS", found == BMS_ADDR, hex(found or 0))
        cells = battery._read_cells(client, BMS_ADDR)
        check("cell sampler reads 6 then stops", len(cells) == 6, str(len(cells)))
        client.close()
    finally:
        doip.socket.create_connection = orig_conn

    # --- integration: full battery_health() real path ------------------------
    fake_probe = {
        "reached_doip": True, "raw_from_ip": CAR_IP, "vin": FAKE_VIN,
        "logical_address": hex(ENTITY_ADDR), "detail": "Vehicle announcement received.",
    }
    orig_probe = battery.doip.probe
    orig_cand = battery.interfaces.best_candidate
    battery.doip.probe = lambda candidate=None, timeout=2.0: dict(fake_probe)
    battery.interfaces.best_candidate = lambda: {"ip": "169.254.7.1", "name": "eth0"}
    doip.socket.create_connection = lambda addr, timeout=None: FakeSock(FakeTaycanGateway())
    try:
        res = battery.battery_health("real")
    finally:
        battery.doip.probe = orig_probe
        battery.interfaces.best_candidate = orig_cand
        doip.socket.create_connection = orig_conn

    check("battery_health ok, not simulated", res["ok"] and res["simulated"] is False)
    check("VIN surfaced from the announcement", res.get("vin") == FAKE_VIN, str(res.get("vin")))
    vals = {r["name"]: r["value"] for r in res["readings"]}
    check("SOC 72 %", vals.get("State of charge (BMS)") == "72", str(vals.get("State of charge (BMS)")))
    check("pack voltage 742.5 V", vals.get("HV pack voltage") == "742.5")
    check("pack current -3.1 A", vals.get("HV pack current") == "-3.1")
    check("pack temperature 24 C", vals.get("HV pack temperature") == "24")
    check("highest cell 3.907", vals.get("Highest cell") == "3.907", str(vals.get("Highest cell")))
    check("lowest cell 3.898", vals.get("Lowest cell") == "3.898")
    check("cell spread 9 mV", vals.get("Cell spread (balance)") == "9", str(vals.get("Cell spread (balance)")))
    check("no State-of-Health value emitted",
          not any("health" in name.lower() for name in vals), str(list(vals)))
    caveat = next((r for r in res["readings"] if r["name"] == "Note"), None)
    check("provenance caveat present", caveat is not None and "community" in caveat["value"].lower())
    check("caveat says SoH not shown", caveat is not None and "State-of-Health" in caveat["value"])
    check("message flags candidate scalings", "candidate" in res["message"].lower(), res["message"])

    # --- honest wall #1: no DoIP gateway -------------------------------------
    battery.doip.probe = lambda candidate=None, timeout=2.0: {"reached_doip": False, "detail": "No announcement."}
    battery.interfaces.best_candidate = lambda: None
    try:
        nogw = battery.battery_health("real")
    finally:
        battery.doip.probe = orig_probe
        battery.interfaces.best_candidate = orig_cand
    check("no-gateway -> ok False + ENET guidance",
          nogw["ok"] is False and "ENET" in nogw["message"] and not nogw["readings"])

    # --- honest wall #2: gateway up, no candidate BMS answers ----------------
    class SilentBmsCar(FakeTaycanGateway):
        def handle(self, payload_type, payload):
            if payload_type == doip.PT_DIAG_MESSAGE:
                source = int.from_bytes(payload[0:2], "big")
                target = int.from_bytes(payload[2:4], "big")
                nack = doip.build_generic(
                    doip.PT_DIAG_MESSAGE_NACK, struct.pack(">HH", target, source) + bytes([0x03])
                )
                return [nack]
            return super().handle(payload_type, payload)

    battery.doip.probe = lambda candidate=None, timeout=2.0: dict(fake_probe)
    battery.interfaces.best_candidate = lambda: {"ip": "169.254.7.1", "name": "eth0"}
    doip.socket.create_connection = lambda addr, timeout=None: FakeSock(SilentBmsCar())
    try:
        nobms = battery.battery_health("real")
    finally:
        battery.doip.probe = orig_probe
        battery.interfaces.best_candidate = orig_cand
        doip.socket.create_connection = orig_conn
    check("no-candidate -> ok True, honest 'address not published' note, no readings",
          nobms["ok"] is True and not nobms["readings"]
          and "not publicly published" in nobms["message"], nobms["message"])

    # --- sim mode + profile manifest + API route -----------------------------
    sim = battery.battery_health("sim")
    check("sim ok+simulated+labelled",
          sim["ok"] and sim["simulated"] and "SIMULATED" in sim["message"])
    check("sim carries the caveat and no SoH number",
          any(r["name"] == "Note" for r in sim["readings"])
          and not any("health" in r["name"].lower() for r in sim["readings"]))

    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "profiles" / "porsche" / "profile.json")
        .read_text(encoding="utf-8-sig")
    )
    actions = {a["id"]: a for a in manifest["panel"]["actions"]}
    check("profile.json has a native battery action",
          "battery" in actions and actions["battery"].get("native") is True)

    from fastapi.testclient import TestClient

    from backend.main import app
    tc = TestClient(app)
    profs = tc.get("/api/profiles").json().get("profiles", [])
    por = next((p for p in profs if p.get("id") == "porsche"), {})
    check("API lists porsche with battery feature true", por.get("features", {}).get("battery") is True)
    rbatt = tc.get("/api/porsche/battery?mode=sim").json()
    check("API /api/porsche/battery sim answers", rbatt.get("ok") and rbatt.get("simulated"))
    check("API rejects a bad mode", tc.get("/api/porsche/battery?mode=bogus").status_code == 400)

    print()
    print("  sim message: " + sim.get("message", ""))
    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SELF-TEST PASSED: DoIP routing-activation + diagnostic framing, alive-check "
          "and response-pending handling, candidate-address search, battery decoders, "
          "cell-spread, SoH-omission, honest walls, and the porsche battery route all good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
