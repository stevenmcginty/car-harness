"""Offline self-test for the Vauxhall / Opel (GM Global A) profile -- no car,
no dongle.

The thing worth testing here is the one thing that makes this profile different
from every other dongle brand: **GM Global A speaks GMW3110, not UDS**. Fault
codes are 0xA9, clear is 0x04, identification is 0x1A, and the body/chassis
modules answer on request + 0x400 rather than + 8. A scan that silently assumed
UDS would come back empty on a perfectly healthy car, and a receive filter set
to request + 8 would make the whole 0x24x block invisible. Both are asserted
below against a fake car that answers GMLAN and REFUSES UDS.

Also covered: the dialect label is earned rather than guessed (0x3E TesterPresent
exists in both dialects, so answering it must not be reported as proof of
either), the +0x400 filter is actually programmed into the ELM, the address
sweep finds a module the shipped table does not list, and the transmission
decodes that this profile exists for.

Run:  cd car-harness; .venv\\Scripts\\python.exe -m backend.tools.vauxhall_selftest
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.obd import service as obd_service  # noqa: E402
from backend.obd.elm327 import ObdError  # noqa: E402
from backend.profiles.vauxhall import gmlan  # noqa: E402
from backend.profiles.vauxhall import pids as vx_pids  # noqa: E402
from backend.profiles.vauxhall import resets as vx_resets  # noqa: E402
from backend.profiles.vauxhall import scan as vx_scan  # noqa: E402
from backend.profiles.vauxhall import gearbox as vx_gearbox
from backend.profiles.vauxhall import software as vx_software  # noqa: E402

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


class GmlanSession:
    """A fake GM Global A car behind an ELM327.

    It speaks GMW3110 and REFUSES UDS: 0x19 and the F1xx DIDs come back
    "service not supported" / "request out of range", exactly as a real Global
    A module does. It also enforces the receive filter -- a request whose ATCRA
    does not match the module's real reply id gets nothing back, which is what
    makes the +0x400 rule testable rather than assumed.
    """

    OBD_VIN = "W0LBD6EA1H1234567"

    # request id -> reply id. 0x252 is deliberately NOT in the shipped table:
    # the sweep must find it and say so.
    PRESENT = {
        0x7E0: 0x7E8,
        0x7E1: 0x7E9,
        0x241: 0x641,
        0x243: 0x643,
        0x252: 0x652,
    }

    def __init__(self):
        self.protocol_num = 6
        self.protocol = "ISO 15765-4 (CAN 11/500)"
        self.at_log = []
        self.header = "7DF"
        self.cra = None
        self.cleared = set()

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
        try:
            req_id = int(self.header, 16)
        except ValueError:
            return "NO DATA\r>"
        reply_id = self.PRESENT.get(req_id)
        if reply_id is None:
            return "NO DATA\r\r>"
        # The ELM only hands up frames matching its receive filter. A profile
        # that got the +0x400 rule wrong would hear nothing from the 0x24x block.
        if self.cra is not None and self.cra != reply_id:
            return "NO DATA\r\r>"
        return self._reply(req_id, cmd) + "\r\r>"

    # ------------------------------------------------------------- fake car
    def _reply(self, req_id, cmd):
        # --- GMW3110 (what this car actually speaks) ---
        if cmd == "3E00":                       # in BOTH dialects: no proof
            return "7E00"
        if cmd == "1A90":                       # GMLAN-only: dialect proof
            return iso_tp("5A90" + ascii_hex(self.OBD_VIN))
        if cmd == "1A92":
            return iso_tp("5A92" + ascii_hex(f"HW-{req_id:03X}"))
        if cmd == "1AC0":
            return iso_tp("5AC0" + ascii_hex("BOOT13579"))
        if cmd.startswith("A981"):
            if req_id in self.cleared:
                return "E981FF"
            if req_id == 0x7E1:
                # P0776-00 (pressure control solenoid B performance) and
                # P0716-71 (input speed sensor circuit range) -- with the
                # failure-type byte a GM tester would show.
                return iso_tp("E981FF" + "07760008" + "0716712F")
            if req_id == 0x243:
                return iso_tp("E981FF" + "40510008")   # C0051
            return "E981FF"
        if cmd == "A982FF":
            return "E98200"
        if cmd == "04":
            self.cleared.add(req_id)
            return "44"

        # --- UDS: this car refuses it, like a real Global A module ---
        if cmd.startswith("19"):
            return "7F1911"                     # service not supported
        if cmd.startswith("22"):
            return "7F2231"                     # request out of range
        if cmd.startswith("14"):
            return "7F1411"
        return "NO DATA"


class UdsSession(GmlanSession):
    """A late Global A node that answers plain UDS instead -- the other half of
    the ladder. Proves the profile does not hard-code GMLAN either."""

    PRESENT = {0x7E0: 0x7E8, 0x7E1: 0x7E9}

    def _reply(self, req_id, cmd):
        if cmd == "3E00":
            return "7E00"
        if cmd == "22F190":
            return iso_tp("62F190" + ascii_hex(self.OBD_VIN))
        if cmd == "22F188":
            return iso_tp("62F188" + ascii_hex("12345678"))
        if cmd.startswith("1902"):
            if req_id in self.cleared:
                return "5902FF"
            if req_id == 0x7E1:
                return iso_tp("5902FF" + "0776000C")
            return "5902FF"
        if cmd == "14FFFFFF":
            self.cleared.add(req_id)
            return "54"
        if cmd.startswith("1A") or cmd.startswith("A9") or cmd == "04":
            return "7F" + cmd[:2] + "11"        # GMLAN services unsupported
        if cmd.startswith("22"):
            return "7F2231"
        return "NO DATA"


class LiveSession(GmlanSession):
    """Adds the three GM transmission DIDs plus a couple of Mode-01 PIDs."""

    def _reply(self, req_id, cmd):
        if req_id == 0x7E1:
            if cmd == "2204FE":
                return "6204FE6F"          # 0x6F = 111 -> 111-40 = 71 C
            if cmd == "225503":
                return "62550303"          # low nibble 3 -> 3rd
            if cmd == "225504":
                return "62550404"          # low nibble 4 -> 4th
        if req_id == 0x7E0:
            if cmd == "010C":
                return "410C2AC8"          # 10952/4 = 2738 rpm
            if cmd == "010D":
                return "410D30"            # 48 km/h
            if cmd == "0105":
                return "410585"            # 133-40 = 93 C
        return super()._reply(req_id, cmd)


class SoftwareSession(GmlanSession):
    """A car whose modules answer the GMW3110 identification block.

    The point of the fake is the encoding: GM stores part numbers as a 4-byte
    binary value, not as text, so $CB coming back 01724E8F must be reported as
    24268431 and not as the hex. It also proves the two things that are easy to
    get wrong -- an alpha code is glued to its own part number and not to the
    next one, and an identifier answered with all zeros is a slot that was
    never written rather than "part number 0".
    """

    PRESENT = {0x7E0: 0x7E8, 0x7E1: 0x7E9}

    # request id -> {identifier hex: payload hex}
    SOFTWARE = {
        0x7E1: {
            "CB": "01724E8F",           # end model part number 24268431
            "DB": ascii_hex("AC"),      # ... alpha code AC
            "CC": "01724E00",           # base model part number 24268320
            "C0": "00C0ED34",           # boot software 12643636
            "C1": "01724E98",           # software module 01, 24268440
            "D1": ascii_hex("AB"),
            "C2": "01724EA7",           # software module 02, 24268455
            "C3": "00000000",           # a slot that exists but was never written
            "99": "161102",             # programmed 2016-11-02
            "98": ascii_hex("GM01234"),
        },
        0x7E0: {
            "CB": "00C11B40",           # 12655424
            "C1": "00C11B46",           # 12655430
            "99": "170314",
        },
    }

    def _reply(self, req_id, cmd):
        if cmd.startswith("1A") and len(cmd) == 4:
            payload = self.SOFTWARE.get(req_id, {}).get(cmd[2:])
            if payload is not None:
                return iso_tp("5A" + cmd[2:] + payload)
            return "7F1A31"                     # request out of range
        return super()._reply(req_id, cmd)


class UdsSoftwareSession(UdsSession):
    """A late Global A node answering the ISO 14229-1 F18x block instead."""

    def _reply(self, req_id, cmd):
        if req_id == 0x7E0:
            if cmd == "22F188":
                return iso_tp("62F188" + ascii_hex("12655424AB"))
            if cmd == "22F189":
                return iso_tp("62F189" + ascii_hex("0004"))
        return super()._reply(req_id, cmd)


def with_fake_session(fake):
    def _with_session(adapter, fn):
        return fn(fake, None)
    return _with_session


ADAPTER = {"name": "Fake adapter"}


def main():
    # ------------------------------------------------- pure decode functions
    d = gmlan.format_dtc(0x07, 0x16, 0x71)
    check("GM fault renders as code + failure type",
          d["code"] == "P0716" and d["display"] == "P0716-71" and d["failureType"] == "71",
          str(d))
    check("GM fault carries the 6T name, not the SAE-generic one",
          "turbine speed" in (d.get("description") or "").lower() and
          d.get("failureTypeName") == "actuator stuck", str(d))

    class Fake:
        def __init__(self, table):
            self.table = table

        def request(self, dst, data, timeout=3.0):
            return self.table[(dst, data.hex().upper())]

    payload = bytes.fromhex("E981FF") + bytes([0x07, 0x76, 0x00, 0x08])
    got = gmlan.read_dtcs(Fake({(0x7E1, "A981FF"): payload}), 0x7E1, [])
    check("A9 81 decodes with the mask echoed back",
          [x["display"] for x in got] == ["P0776-00"], str(got))

    payload = bytes.fromhex("E981") + bytes([0x07, 0x76, 0x00, 0x08])
    got = gmlan.read_dtcs(Fake({(0x7E1, "A981FF"): payload}), 0x7E1, [])
    check("A9 81 decodes with no mask echo (offset fallback)",
          [x["display"] for x in got] == ["P0776-00"], str(got))

    got = gmlan.read_dtcs(Fake({(0x7E1, "A981FF"): bytes.fromhex("E981FF")}), 0x7E1, [])
    check("A9 81 with no faults returns an empty list", got == [], str(got))

    uudt = gmlan.parse_uudt_dtc_frames("0007760008\r0007167102\r0000000000\r")
    check("0x5E8 UUDT dump (gocan layout) decodes two DTCs and stops on 00 00 00",
          uudt == [(0x07, 0x76, 0x00, 0x08), (0x07, 0x16, 0x71, 0x02)], str(uudt))
    check("GMLAN status 0x02 is current, not UDS 'failed this cycle'",
          gmlan.status_flags(0x02) == ["current"] and
          gmlan.fault_state(0x02)["severity"] == "active",
          str(gmlan.status_flags(0x02)))
    check("GMLAN status 0x10 is history",
          "history" in gmlan.status_flags(0x10) and
          gmlan.fault_state(0x10)["severity"] == "historic")
    import json
    from pathlib import Path
    catalog = json.loads(
        (Path(__file__).resolve().parents[1] / "profiles" / "vauxhall" /
         "data" / "gmlan_catalog.json").read_text(encoding="utf-8")
    )
    check("distilled catalog has the GMW3110 1A table and the 5E8 note",
          catalog["ident_1a"]["CB"] == "End model part number" and
          catalog["uudt_dtc"]["can_id"] == "5E8" and
          catalog["dtc_status_bits"]["bits"][1]["name"] == "current",
          str(catalog["ident_1a"].get("CB")))

    check("transmission fluid temp decodes as A-40",
          vx_pids._fluid_temp_c(bytes([111])) == 71 and
          vx_pids._fluid_temp_c(bytes([40])) == 0,
          str(vx_pids._fluid_temp_c(bytes([111]))))
    check("gear map decodes gears, reverse and park",
          vx_pids._gear_name(bytes([0x03])) == "3rd" and
          vx_pids._gear_name(bytes([0x0B])) == "Reverse" and
          vx_pids._gear_name(bytes([0x0D])) == "Park")

    # --------------------------------------------------- scan on a GMLAN car
    original = obd_service._with_session
    try:
        fake = GmlanSession()
        obd_service._with_session = with_fake_session(fake)
        report = vx_scan.scan(ADAPTER, "real")
        by_addr = {m["address"]: m for m in report["modules"]}
        check("scan ok on a GMW3110 car", report["ok"] is True, report.get("message"))
        check("scan finds the powertrain + body modules that answer",
              sorted(m["address"] for m in report["modules"] if m["present"])
              == ["0x241", "0x243", "0x7E0", "0x7E1"],
              str([m["address"] for m in report["modules"] if m["present"]]))
        check("a body module on the 0x24x block is reached (+0x400 filter)",
              by_addr["0x241"]["present"] is True and
              by_addr["0x241"]["response"] == "0x641",
              str(by_addr["0x241"]))
        check("ELM was aimed with the +0x400 receive filter",
              "ATCRA641" in fake.at_log and "ATSH241" in fake.at_log,
              str([c for c in fake.at_log if c.startswith("ATCRA")]))
        check("dialect reported as GMLAN, not guessed from 3E 00",
              by_addr["0x7E1"]["dialect"] == "gmlan", str(by_addr["0x7E1"]["dialect"]))
        tcm = by_addr["0x7E1"]
        check("TCM faults decode with GM failure types",
              sorted(d["display"] for d in tcm["dtcs"]) == ["P0716-71", "P0776-00"],
              str([d["display"] for d in tcm["dtcs"]]))
        check("fault state collapses the status byte",
              any(d["display"] == "P0716-71" and d["state"]["severity"] == "active"
                  for d in tcm["dtcs"]), str(tcm["dtcs"]))
        check("GMLAN records are labelled as an inferred layout",
              all(d.get("recordLayout") == "inferred" for d in tcm["dtcs"]))
        check("ABS C-code decodes on the 0x24x block",
              [d["display"] for d in by_addr["0x243"]["dtcs"]] == ["C0051-00"],
              str(by_addr["0x243"]["dtcs"]))
        check("identification read over 1A",
              any(i["label"] == "VIN" and i["value"] == "W0LBD6EA1H1234567"
                  for i in by_addr["0x7E0"]["ident"]), str(by_addr["0x7E0"]["ident"]))
        check("VIN surfaced and leads the message",
              report["vehicle"]["vin"] == "W0LBD6EA1H1234567" and
              report["message"].startswith("VIN W0LBD6EA1H1234567. "),
              report["message"])
        check("message names the dialect that actually read",
              "GMW3110" in report["message"], report["message"])
        check("absent table rows are reported absent, not errors",
              by_addr["0x251"]["present"] is False and by_addr["0x251"]["error"] is None)
        check("scan restored the ELM", fake.header == "7DF", fake.header)

        # ------------------------------------------------- clear on a GMLAN car
        result = vx_scan.clear(ADAPTER, "real", "0x7E1")
        check("targeted clear falls back from UDS 14 to GMLAN 04",
              result["ok"] is True and result["cleared"] == ["0x7E1"], str(result))
        again = vx_scan.scan(ADAPTER, "real")
        tcm2 = {m["address"]: m for m in again["modules"]}["0x7E1"]
        check("cleared module then reads no faults", tcm2["dtcs"] == [], str(tcm2["dtcs"]))

        fake_c = GmlanSession()
        obd_service._with_session = with_fake_session(fake_c)
        result = vx_scan.clear(ADAPTER, "real", None)
        check("clear-all hits every module that answers",
              sorted(result["cleared"]) == ["0x241", "0x243", "0x7E0", "0x7E1"],
              str(result))

        # ------------------------------------------------------ scan on a UDS car
        fake_u = UdsSession()
        obd_service._with_session = with_fake_session(fake_u)
        report_u = vx_scan.scan(ADAPTER, "real")
        tcm_u = {m["address"]: m for m in report_u["modules"]}["0x7E1"]
        check("the same scan reads a UDS-dialect car",
              tcm_u["present"] and tcm_u["dialect"] == "uds" and
              [d["display"] for d in tcm_u["dtcs"]] == ["P0776-00"],
              str(tcm_u))
        check("message names UDS when that is what answered",
              "plain UDS" in report_u["message"], report_u["message"])

        # ------------------------------------------------------------- discover()
        fake_d = GmlanSession()
        obd_service._with_session = with_fake_session(fake_d)
        found = vx_scan.discover(ADAPTER, "real")
        addrs = sorted(f["address"] for f in found["found"])
        check("sweep finds every answering address in both blocks",
              addrs == ["0x241", "0x243", "0x252", "0x7E0", "0x7E1"], str(addrs))
        extra = next(f for f in found["found"] if f["address"] == "0x252")
        check("sweep flags an address the shipped table does not list",
              extra["known"] == "" and "0x252" in found["message"], found["message"])
        check("sweep reports the reply id it proved",
              extra["response"] == "0x652", str(extra))
        check("sweep output also carries generic readings for the panel",
              len(found["readings"]) == len(found["found"]) and
              all(r["name"] and r["value"] for r in found["readings"]),
              str(found["readings"][:1]))

        # ------------------------------------------------------------- live()
        fake_l = LiveSession()
        obd_service._with_session = with_fake_session(fake_l)
        live = vx_pids.live(ADAPTER, "real")
        got = {r["name"]: r["value"] for r in live["readings"]}
        check("live reads transmission fluid temperature",
              got.get("Transmission fluid temperature") == "71", str(got))
        check("live reads current and commanded gear",
              got.get("Current gear") == "3rd" and
              got.get("Target (commanded) gear") == "4th", str(got))
        check("live derives the shift state that shows a flare",
              got.get("Shift state") == "Shifting 3rd → 4th", str(got))
        check("live reports the Fast Learn temperature window",
              "In range" in (got.get("Fast Learn temperature window") or ""), str(got))
        check("live adds the generic engine set",
              got.get("Engine speed") == "2738" and got.get("Vehicle speed") == "48" and
              got.get("Coolant temperature") == "93", str(got))
        check("live is honest about the adapt cells it cannot read",
              "GDS2-only" in live["message"], live["message"])
        check("live restored the ELM", fake_l.header == "7DF", fake_l.header)

        # --- the Live Data fast path: only the picked channels get read ---
        fake_ch = LiveSession()
        obd_service._with_session = with_fake_session(fake_ch)
        rows = vx_pids.read_channels(ADAPTER, "real", ["gear_current", "gear_target"])
        check("read_channels returns only what was asked for",
              [r["key"] for r in rows] == ["gear_current", "gear_target"] and
              [r["value"] for r in rows] == ["3rd", "4th"], str(rows))
        sent = [c for c in fake_ch.at_log if c.startswith("ATSH")]
        check("read_channels does not read the fluid temp it was not asked for",
              all("2204FE" not in c for c in fake_ch.at_log) and sent, str(rows))
        rows = vx_pids.read_channels(ADAPTER, "real", ["shift_state"])
        check("read_channels derives shift_state from both gear reads",
              [r["key"] for r in rows] == ["shift_state"] and
              rows[0]["value"] == "Shifting 3rd → 4th", str(rows))
        from backend.obd import livefeed
        check("Live Data picks up the brand's fast channel interface",
              livefeed._rich_brand_module("vauxhall") is not None)

        # Cold gearbox -> the window verdict must say so, not stay silent.
        class ColdSession(LiveSession):
            def _reply(self, req_id, cmd):
                if req_id == 0x7E1 and cmd == "2204FE":
                    return "6204FE50"     # 80-40 = 40 C
                return super()._reply(req_id, cmd)

        obd_service._with_session = with_fake_session(ColdSession())
        cold = {r["name"]: r["value"] for r in vx_pids.live(ADAPTER, "real")["readings"]}
        check("a cold gearbox is called too cold for Fast Learn",
              "Too cold" in (cold.get("Fast Learn temperature window") or ""), str(cold))

        # A manual car: no TCM answers, engine values still come back.
        class ManualSession(GmlanSession):
            PRESENT = {0x7E0: 0x7E8}

            def _reply(self, req_id, cmd):
                if req_id == 0x7E0 and cmd == "010C":
                    return "410C2AC8"
                return super()._reply(req_id, cmd)

        obd_service._with_session = with_fake_session(ManualSession())
        manual = vx_pids.live(ADAPTER, "real")
        check("a car with no answering TCM still returns engine values and says why",
              manual["ok"] and "did not answer" in manual["message"] and
              any(r["name"] == "Engine speed" for r in manual["readings"]),
              manual["message"])

        # ---------------------------------------------- wrong bus is called out
        class KLineSession(GmlanSession):
            def __init__(self):
                super().__init__()
                self.protocol_num = 3
                self.protocol = "ISO 9141-2"

        obd_service._with_session = with_fake_session(KLineSession())
        wrong = vx_scan.scan(ADAPTER, "real")
        check("a non-CAN car is refused with a useful message",
              wrong["ok"] is False and "Peugeot profile" in wrong["message"],
              wrong["message"])
    finally:
        obd_service._with_session = original

    # ------------------------------------------------------------ resets
    cat = vx_resets.list_resets()
    ids = [r["id"] for r in cat["resets"]]
    check("reset catalogue lists the gearbox procedures",
          "vauxhall-trans-adaptive-values-learn" in ids and
          "vauxhall-tcm-calibration-check" in ids, str(ids))
    fast = next(r for r in cat["resets"] if r["id"] == "vauxhall-trans-adaptive-values-learn")
    check("Fast Learn ships as a procedure, not a UDS write",
          fast["method"] == "procedure" and len(fast["procedure"]) > 8, str(fast["method"]))
    check("Fast Learn names both published temperature windows",
          any("64-76" in c and "75-85" in c for c in fast["cautions"]), str(fast["cautions"]))
    run = vx_resets.perform(None, "real", "vauxhall-trans-adaptive-values-learn", confirm=True)
    check("Fast Learn refuses to run even when confirmed",
          run["ok"] is False and run["runnable"] is False and
          "DeviceControl" in run["message"], str(run.get("message")))
    check("an unknown reset id is rejected",
          vx_resets.perform(None, "real", "nope", confirm=True)["ok"] is False)

    # -------------------------------------------------- software versions
    sw_session = SoftwareSession()
    obd_service._with_session = with_fake_session(sw_session)
    sw = vx_software.versions(ADAPTER, "real")
    tcm = next((m for m in sw["modules"] if m["code"] == "7E1"), {})
    entries = {e["id"]: e for e in tcm.get("entries", [])}

    check("software read finds the gearbox module", bool(entries), str(sw.get("message")))
    check("a GM part number is decoded from 4 binary bytes, not shown as hex",
          entries.get("$CB", {}).get("value", "").startswith("24268431"),
          str(entries.get("$CB")))
    check("an alpha code is joined to its own part number",
          entries.get("$CB", {}).get("value") == "24268431 AC" and
          entries.get("$C1", {}).get("value") == "24268440 AB",
          f"{entries.get('$CB')} / {entries.get('$C1')}")
    check("the raw bytes are kept alongside the decode",
          entries.get("$CB", {}).get("raw") == "01724E8F", str(entries.get("$CB")))
    check("the programming date is decoded from BCD",
          entries.get("$99", {}).get("value") == "2016-11-02", str(entries.get("$99")))
    check("the tester that last programmed the module is reported",
          entries.get("$98", {}).get("value") == "GM01234", str(entries.get("$98")))
    check("an all-zero identifier is treated as never written, not part number 0",
          "$C3" not in entries, str(entries.get("$C3")))
    # $C3 is blank and $C4/$C5 are refused -- three consecutive empty slots, so
    # $C6-$CA are never asked for. A dongle pays a timeout for every one of them.
    check("software module scanning stops after three empty slots",
          not any(f"$C{n:X}" in entries for n in range(0x4, 0xB)) and
          any("stopped after" in line for line in tcm.get("trace", [])),
          sorted(entries))
    check("the gearbox is called out in the message",
          "TCM" in sw["message"] and "24268431" in sw["message"], sw["message"])
    check("a worksheet is produced for comparing against the maker's system",
          "24268431 AC" in sw["worksheet"] and "$99" in sw["worksheet"])
    check("the software read restored the ELM",
          sw_session.header == "7DF" and sw_session.cra is None,
          f"{sw_session.header} / {sw_session.cra}")

    # The write is not on offer, and the module says so where a reader will see it.
    import inspect
    src = inspect.getsource(vx_software)
    check("the software module records why it does not flash",
          "WHY THERE IS NO FLASH FUNCTION HERE" in src)
    check("no programming services are implemented",
          not any(s in src for s in ("RequestDownload(", "0x34,", "0x36,", "0x37,")))

    obd_service._with_session = with_fake_session(UdsSoftwareSession())
    sw_uds = vx_software.versions(ADAPTER, "real")
    ecm = next((m for m in sw_uds["modules"] if m["code"] == "7E0"), {})
    uds_entries = {e["id"]: e["value"] for e in ecm.get("entries", [])}
    check("a UDS-dialect module is read over the F18x block instead",
          ecm.get("dialect") == "uds" and uds_entries.get("$F188") == "12655424AB",
          f"{ecm.get('dialect')} {uds_entries}")

    sw_sim = vx_software.versions(None, "sim")
    check("sim software labelled",
          sw_sim["ok"] and sw_sim["simulated"] and "SIMULATED" in sw_sim["message"])

    # ------------------------------------------------------------ sim paths
    s = vx_scan.scan(None, "sim")
    check("sim scan labelled", s["ok"] and s["simulated"] and "SIMULATED" in s["message"])
    c = vx_scan.clear(None, "sim")
    check("sim clear labelled", c["ok"] and c["simulated"])
    d = vx_scan.discover(None, "sim")
    check("sim discover labelled", d["ok"] and d["simulated"] and bool(d["readings"]))
    live_sim = vx_pids.live(None, "sim")
    check("sim live labelled", live_sim["ok"] and live_sim["simulated"])

    # ------------------------------------------------------- routes + MCP
    from fastapi.testclient import TestClient

    from backend.main import app
    client = TestClient(app)
    # -------------------------------------------------- gearbox analyse
    p2723 = vx_gearbox.enrich_dtc({
        "code": "P2723", "hex": "272374", "failureType": "74",
        "display": "P2723-74",
    })
    check("P2723 is the 1-2-3-4 clutch, not SAE shift-solenoid E",
          "1-2-3-4" in p2723["description"] and "PCS 5" in p2723["part"],
          str(p2723))
    check("failure type 74 is actuator slipping",
          p2723.get("failureTypeName") == "actuator slipping", str(p2723))
    check("SAE name is kept so a reader can see why we discarded it",
          "Solenoid E" in (p2723.get("saeDescription") or ""), str(p2723))
    p0776 = vx_gearbox.enrich_dtc({
        "code": "P0776", "hex": "077600", "failureType": "00",
        "display": "P0776-00",
    })
    check("P0776 is the 3-5-R clutch / PCS 2",
          "3-5-R" in p0776["description"] and "PCS 2" in p0776["part"],
          str(p0776))
    unknown = vx_gearbox.enrich_dtc({
        "code": "P1234", "hex": "123400", "failureType": "00",
        "display": "P1234-00",
    })
    check("an unknown code is not given a clutch",
          "description" not in unknown and "part" not in unknown, str(unknown))

    gb_sim = vx_gearbox.analyze(None, "sim")
    check("sim gearbox analyse is labelled and has a worksheet",
          gb_sim["ok"] and gb_sim["simulated"] and "P2723" in gb_sim["worksheet"]
          and gb_sim["findings"], str(gb_sim.get("message")))
    check("sim gearbox worksheet names the 1-2-3-4 clutch",
          "1-2-3-4 clutch" in gb_sim["worksheet"], gb_sim["worksheet"][:200])

    # Real path against the existing GMLAN fake: TCM at textbook 0x7E1.
    obd_service._with_session = with_fake_session(GmlanSession())
    gb = vx_gearbox.analyze(ADAPTER, "real")
    gb_codes = [d["code"] for d in gb.get("dtcs", [])]
    check("gearbox analyse finds the TCM on the textbook address when 0x7E2 is silent",
          gb["ok"] and gb.get("tcm", {}).get("address") == "0x7E1",
          str(gb.get("tcm")))
    check("gearbox analyse names the stored 6T codes",
          "P0776" in gb_codes and "P0716" in gb_codes, str(gb_codes))
    check("gearbox analyse writes a copy-out worksheet",
          "P0776" in gb.get("worksheet", "") and "3-5-R" in gb.get("worksheet", ""),
          gb.get("worksheet", "")[:300])
    obd_service._with_session = original

    for path in ("scan", "live", "discover", "software", "gearbox"):
        r = client.get(f"/api/vauxhall/{path}?mode=sim").json()
        check(f"API sim {path} over the generic brand route",
              r.get("ok") and r.get("simulated"), str(r.get("message")))
    r = client.post("/api/vauxhall/clear", json={"mode": "sim", "confirm": True}).json()
    check("API sim clear over the generic brand route", r.get("ok") and r.get("simulated"))
    check("API refuses an unconfirmed clear",
          client.post("/api/vauxhall/clear", json={"mode": "sim"}).status_code == 400)
    r = client.get("/api/vauxhall/resets").json()
    check("API serves the reset catalogue", r.get("brand") == "vauxhall" and r.get("resets"))
    profs = client.get("/api/profiles").json().get("profiles", [])
    vx = next((p for p in profs if p.get("id") == "vauxhall"), {})
    check("API profile carries features + panel",
          bool(vx.get("features", {}).get("scan")) and
          bool(vx.get("features", {}).get("discover")) and
          bool(vx.get("features", {}).get("gearbox")) and bool(vx.get("panel")))

    from backend import mcp_server
    check("MCP tools registered",
          all(hasattr(mcp_server, n) for n in
              ("vauxhall_scan_modules", "vauxhall_discover_modules",
               "vauxhall_live_values", "vauxhall_clear_module",
               "vauxhall_software_versions", "vauxhall_gearbox_analyze")))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
