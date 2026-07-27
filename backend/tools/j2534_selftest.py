"""Self-test for the J2534 pass-thru cable path (the DiagBox/Lexia route), no
hardware required.

Run:  python -m backend.tools.j2534_selftest

Drives the REAL client + scan code -- PsaJ2534UdsClient, the peugeot scan module,
backend.core.uds.* -- against a SIMULATED J2534 device (FakeJ2534Link) wired to
a fake PSA car whose body modules (BSI, airbag, cluster) answer, exactly as they
would over a genuine PSA cable on the pins-3/8 diagnostic CAN. It proves the one
thing that makes the cable worth driving:

  * the same module walk that only sees powertrain modules over a dongle reaches
    the BODY modules (BSI + airbag + cluster) over the cable,
  * per-module response ids are routed correctly (BSI 0x752 -> 0x652),
  * UDS + PSA-KWP DTC/ident/clear all work over J2534,
  * the report drops the "pins 3/8 adapter" hint (we're already on that bus).

The real J2534Link ctypes/DLL code can only be exercised against a physical
device; it is import- and constant-checked here, and the FakeJ2534Link overrides
just the link surface (open/uds_request/close) the scan uses.

Exits 0 on PASS, 1 on FAIL.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.obd import j2534_link  # noqa: E402
from backend.obd.elm327 import ObdError  # noqa: E402
from backend.obd.psa_j2534 import PsaJ2534UdsClient  # noqa: E402
from backend.profiles.peugeot import scan as peugeot_scan  # noqa: E402

FAKE_VIN = "VF3XXXXXXXX000001"
BSI, ENGINE, AIRBAG, ABS, CLUSTER = 0x752, 0x6A8, 0x744, 0x6AD, 0x75F
PRESENT = {BSI, ENGINE, AIRBAG, ABS, CLUSTER}


class FakePsaCarOnCable:
    """A PSA car reached over the pins-3/8 diagnostic bus: body modules answer.
    ABS is a KWP-dialect module; the airbag needs a session before ReadDTC."""

    def __init__(self):
        self.sessions = {}
        self.cleared = []
        self.ext_started = []
        self.kwp = {ABS}

    def handle(self, tx, req):
        if tx not in PRESENT or not req:
            return None
        svc = req[0]
        if svc == 0x3E:                                   # TesterPresent
            return bytes([0x7E, 0x00])
        if svc == 0x10 and len(req) >= 2:                 # session control
            if tx in self.kwp:
                if req[1] == 0xC0:
                    self.sessions[tx] = True
                    self.ext_started.append(tx)
                    return bytes([0x50, 0xC0])
                return bytes([0x7F, 0x10, 0x12])
            if req[1] == 0x03:
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
        if svc == 0x22 and len(req) >= 3:                 # ReadDataByIdentifier
            did = (req[1] << 8) | req[2]
            if tx in self.kwp:
                return bytes([0x7F, 0x22, 0x11])
            if did == 0xF190 and tx in (BSI, CLUSTER):    # both body modules know the VIN
                return bytes([0x62, 0xF1, 0x90]) + FAKE_VIN.encode()
            if tx == ENGINE and did == 0xF188:
                return bytes([0x62, 0xF1, 0x88]) + b"1037564321"
            return bytes([0x7F, 0x22, 0x31])
        if svc == 0x19:                                   # ReadDTCInformation
            if tx in self.kwp:
                return bytes([0x7F, 0x19, 0x11])
            if tx == ENGINE:
                return bytes([0x59, 0x02, 0xFF, 0x02, 0x34, 0x00, 0x2F,
                              0x13, 0x38, 0x00, 0x08])
            if tx == AIRBAG:
                if self.sessions.get(AIRBAG):
                    return bytes([0x59, 0x02, 0xFF, 0x90, 0x04, 0x00, 0x08])  # B1004
                return bytes([0x7F, 0x19, 0x7F])          # not in active session
            return bytes([0x59, 0x02, 0xFF])              # cluster: present, no faults
        if svc == 0x14:                                   # ClearDiagnosticInformation
            self.cleared.append(tx)
            return bytes([0x54])
        return bytes([0x7F, svc, 0x11])


class FakeJ2534Link:
    """The J2534Link surface the scan uses, backed by a fake car. Records the
    (tx -> rx) routing so we can prove the PSA response ids are honoured."""

    def __init__(self, car):
        self.car = car
        self.seen_rx = {}
        self.opened = False

    def open(self):
        self.opened = True
        return {"dll": "fake", "protocol": "ISO15765 CAN 11/500"}

    def close(self):
        self.opened = False

    def uds_request(self, tx_id, rx_id, payload, timeout=3.0):
        self.seen_rx[tx_id] = rx_id
        reply = self.car.handle(tx_id, bytes(payload))
        if reply is None:
            raise ObdError(f"No reply from 0x{rx_id:03X}.", kind="no_data")
        return reply


def main() -> int:
    failures = []

    def check(name, cond, extra=""):
        print(("  PASS  " if cond else "  FAIL  ") + name + (f"  ({extra})" if extra else ""))
        if not cond:
            failures.append(name)

    # --- static: the real J2534Link module is sane -------------------------
    check("ISO15765 protocol id == 6", j2534_link.ISO15765 == 6)
    check("FLOW_CONTROL_FILTER == 3", j2534_link.FLOW_CONTROL_FILTER == 3)
    msg = j2534_link.PASSTHRU_MSG()
    check("PASSTHRU_MSG.Data buffer is 4128 bytes", len(msg.Data) == 4128, str(len(msg.Data)))
    check("J2534Link exposes uds_request", hasattr(j2534_link.J2534Link, "uds_request"))
    idb = j2534_link._id_bytes(0x752)
    check("CAN id -> 4 big-endian bytes", idb == bytes([0x00, 0x00, 0x07, 0x52]), idb.hex())

    # --- client routing: BSI answers on 0x652, NOT request+8 ---------------
    car = FakePsaCarOnCable()
    link = FakeJ2534Link(car)
    client = PsaJ2534UdsClient(link, peugeot_scan.PEUGEOT_ID_MAP)
    client.begin()
    present_bsi = __import__("backend.core.uds", fromlist=["tester_present"]).tester_present(client, BSI)
    check("BSI present over J2534", present_bsi)
    check("BSI request routed to rx 0x652 (PSA pair, not +8)",
          link.seen_rx.get(0x752) == 0x652, hex(link.seen_rx.get(0x752, 0)))

    # --- integration: full scan over the (fake) cable ----------------------
    shared_link = FakeJ2534Link(FakePsaCarOnCable())
    peugeot_scan._make_j2534_link = lambda config: shared_link  # monkeypatch the factory

    adapter = {"typeId": "j2534", "name": "Lexia (J2534)", "config": {"dll": "fake.dll"}}
    result = peugeot_scan.scan(adapter, "real")
    by_addr = {m["address"]: m for m in result["modules"]}

    check("scan ok + not simulated", result["ok"] and not result["simulated"])
    check("VIN read over cable", result["vehicle"]["vin"] == FAKE_VIN, str(result["vehicle"]["vin"]))
    check("protocol names the J2534 cable", "J2534" in result["vehicle"]["protocol"],
          result["vehicle"]["protocol"])

    check("BSI (body module) present over cable", by_addr["0x752"]["present"])
    check("AIRBAG (body module) present over cable", by_addr["0x744"]["present"])
    check("CLUSTER (body module) present over cable", by_addr["0x75F"]["present"])
    check("ENGINE present over cable", by_addr["0x6A8"]["present"])
    check("ABS (KWP) present over cable", by_addr["0x6AD"]["present"])

    airbag = by_addr["0x744"]
    check("airbag fault B1004 read after session escalation",
          any(d["code"] == "B1004" for d in airbag["dtcs"]),
          str([d["code"] for d in airbag["dtcs"]]))
    eng = by_addr["0x6A8"]
    check("engine returns 2 faults incl P0234",
          len(eng["dtcs"]) == 2 and any(d["code"] == "P0234" for d in eng["dtcs"]),
          str([d["code"] for d in eng["dtcs"]]))
    abs_mod = by_addr["0x6AD"]
    check("KWP ABS fault C1335 read via 10 C0 + 17 FF 00",
          any(d["code"] == "C1335" for d in abs_mod["dtcs"]),
          str([d["code"] for d in abs_mod["dtcs"]]))

    # The whole point: on the cable we're already on the 3/8 bus, so the report
    # must NOT tell the user to buy a pin-swap adapter.
    check("no pins-3/8 dongle hint on the cable path",
          "pins 3 & 8" not in result["message"] and not result.get("absentHint"),
          result["message"])
    check("report notes the pass-thru cable", "pass-thru cable" in result["message"])

    # --- clear over the cable ----------------------------------------------
    clr = peugeot_scan.clear(adapter, "real", "0x752")
    check("clear BSI over cable ok", clr["ok"] and "0x752" in clr["cleared"], str(clr["cleared"]))

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SELF-TEST PASSED: J2534 cable path reaches the body modules the dongle "
          "can't, PSA id routing + UDS/KWP DTC/ident/clear all good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
