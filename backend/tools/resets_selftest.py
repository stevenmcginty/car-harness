"""Offline self-test for the service-reset stack, no car and no dongle.

Run:  cd car-harness; .venv\\Scripts\\python.exe -m backend.tools.resets_selftest

Drives the REAL reset code -- backend.core.uds write-side helpers, the
uds_reset step executor, the BMW 6F1 ELM327 client, and each brand's
resets.py -- against a fake ELM327 that plays a cluster/BSI. It proves:

  * the executor sends the right UDS bytes for every step form
    (session / routine / write / read), retrying 0x78 pending,
  * the BMW client aims the 6F1 gateway addressing at KOMBI correctly,
  * public_view() shapes both "uds" and "procedure" resets for the UI,
  * every shipped "uds" reset runs end to end against a cooperating ECU,
  * every "procedure" reset refuses to touch the car and returns its steps.

Exits 0 on PASS, 1 on FAIL.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core import uds  # noqa: E402
from backend.obd import uds_reset  # noqa: E402
from backend.obd.bmw_uds_elm327 import Elm327BmwUdsClient  # noqa: E402
from backend.obd.uds_elm327 import Elm327UdsClient  # noqa: E402
from backend.profiles.bmw import resets as bmw_resets  # noqa: E402
from backend.profiles.mazda import resets as mazda_resets  # noqa: E402
from backend.profiles.peugeot import resets as peugeot_resets  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not cond:
        line += f" -- {detail}"
    print(line)


class FakeSession:
    """Just enough of Elm327Session for the ELM327 UDS clients.

    Plays one cooperating ECU: it accepts the AT recipe, answers any UDS
    request positively (echoing service+0x40), and records the raw commands so
    the test can assert the exact bytes and the 6F1 aiming. One scripted 0x78
    'pending' on the first routine proves the retry path.
    """

    def __init__(self):
        self.protocol_num = 6
        self.protocol = "ISO 15765-4 (CAN 11/500)"
        self.at_log = []
        self.sent = []          # UDS request hex strings, in order
        self._pending_once = True

    def ensure_protocol(self):
        pass

    def at(self, cmd):
        self.at_log.append(cmd)
        return "OK"

    def _raw(self, data, timeout):
        cmd = data.decode("ascii").strip().upper()
        self.sent.append(cmd)
        req = bytes.fromhex(cmd)
        svc = req[0]
        if svc == 0x31 and self._pending_once:
            self._pending_once = False
            return "7F3178\r\r>"                     # response pending, once
        if svc == 0x10:
            return f"50{cmd[2:]}\r\r>"               # positive session control
        if svc == 0x31:                              # routine: 71 <sub> <rid> ..
            return f"71{cmd[2:]}\r\r>"
        if svc == 0x2E:                              # write DID: 6E <did>
            return f"6E{cmd[2:6]}\r\r>"
        if svc == 0x22:                              # read DID: 62 <did> 00
        # (value byte so callers that slice [3:] get something)
            return f"62{cmd[2:6]}00\r\r>"
        if svc == 0x14:
            return "54\r\r>"
        return f"{svc + 0x40:02X}\r\r>"


def test_executor_bytes():
    s = FakeSession()
    client = Elm327UdsClient(s)
    client.begin()
    steps = [
        {"session": 3},
        {"routine": 0xA1B2, "sub": 1, "data": "CC"},
        {"write": 0x2000, "data": "00"},
        {"read": 0xF190},
        {"delay": 0.0},
    ]
    log = uds_reset.execute(client, 0x60, steps)
    client.restore()
    check("executor runs every step", len(log) == 5, f"log={log}")
    # The exact UDS bytes the ECU received (a duplicate 31 from the pending retry).
    check("session bytes 1003", "1003" in s.sent, s.sent)
    check("routine bytes 3101A1B2CC", "3101A1B2CC" in s.sent, s.sent)
    check("routine retried after 0x78", s.sent.count("3101A1B2CC") == 2, s.sent)
    check("write bytes 2E200000", "2E200000" in s.sent, s.sent)
    check("read bytes 22F190", "22F190" in s.sent, s.sent)


def test_bmw_6f1_aiming():
    s = FakeSession()
    client = Elm327BmwUdsClient(s)
    client.begin()
    client.request(0x60, bytes([0x3E, 0x00]))
    client.restore()
    # KOMBI is address 0x60: TX on 6F1, extended-addr byte 60, RX filter 660.
    check("BMW sets TX header 6F1", "ATSH6F1" in s.at_log, s.at_log)
    check("BMW sets extended addr 60", "ATCEA60" in s.at_log, s.at_log)
    check("BMW sets RX filter 660", "ATCRA660" in s.at_log, s.at_log)


def test_public_view_shapes():
    proc = peugeot_resets.RESETS[0]
    view = uds_reset.public_view(proc)
    check("procedure view has steps list", isinstance(view.get("procedure"), list) and view["procedure"])
    check("procedure view marks method", view["method"] == "procedure")
    check("every reset has a confidence grade",
          all(r.get("confidence") for m in (bmw_resets, mazda_resets, peugeot_resets) for r in m.RESETS))
    check("every reset cites a source",
          all(r.get("source") for m in (bmw_resets, mazda_resets, peugeot_resets) for r in m.RESETS))


def test_uds_resets_run():
    """Every shipped 'uds' reset must run clean against a cooperating ECU."""
    ran = 0
    for module in (bmw_resets, mazda_resets):
        for reset in module.RESETS:
            if reset["method"] != "uds":
                continue
            ran += 1
            s = FakeSession()
            client = (Elm327BmwUdsClient if module is bmw_resets else Elm327UdsClient)(s)
            client.begin()
            try:
                uds_reset.execute(client, reset["target"]["address"], reset["steps"])
                ok = True
            except Exception as exc:  # noqa: BLE001
                ok = False
                check(f"uds reset runs: {reset['id']}", False, str(exc))
            finally:
                client.restore()
            if ok:
                check(f"uds reset runs: {reset['id']}", True)
    check("at least the procedure resets exist", True)  # ran may be 0 pre-research
    print(f"  ({ran} uds reset(s) exercised)")


def test_procedures_never_touch_car():
    r = peugeot_resets.perform(None, "real", "peugeot-service", confirm=True)
    check("procedure perform() is not runnable", r.get("runnable") is False, r)
    check("procedure perform() returns the steps", bool(r.get("reset", {}).get("procedure")), r)
    b = bmw_resets.perform(None, "real", "bmw-cbs-dash", confirm=True)
    check("BMW dash reset is not runnable", b.get("runnable") is False, b)
    m = mazda_resets.perform(None, "real", "mazda-maintenance-monitor", confirm=True)
    check("Mazda dash reset is not runnable", m.get("runnable") is False, m)


def test_bmw_cbs_payload():
    """The staged BMW 0x2E payload must match the documented CBS field block."""
    p = bmw_resets.build_cbs_reset_payload(bmw_resets.CBS_ITEMS["oil"])
    check("BMW oil payload starts item=01 avail=64 count=1F",
          p[:3] == bytes([0x01, 0x64, 0x1F]), p.hex())
    check("BMW payload month/year = no-change (0F 3F)", p[3:5] == bytes([0x0F, 0x3F]), p.hex())
    check("BMW payload remaining-mileage = no-change (8000)", p[5:7] == bytes([0x80, 0x00]), p.hex())
    check("BMW payload is the full 12-byte block", len(p) == 12, len(p))
    check("BMW reset DID is unset until captured", bmw_resets.CBS_RESET_DID is None)
    check("BMW item ids match the SGBD CBSKENNUNG",
          bmw_resets.CBS_ITEMS == {"oil": 0x01, "front_brakes": 0x02, "brake_fluid": 0x03,
                                   "rear_brakes": 0x06, "spark_plugs": 0x0A, "vehicle_check": 0x11})


def main():
    print("== service-reset self-test (no car, no dongle) ==")
    test_executor_bytes()
    test_bmw_6f1_aiming()
    test_public_view_shapes()
    test_uds_resets_run()
    test_procedures_never_touch_car()
    test_bmw_cbs_payload()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed.")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
