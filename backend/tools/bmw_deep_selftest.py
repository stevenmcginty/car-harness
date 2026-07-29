"""Offline self-test for the BMW/MINI deep functions: CBS, battery, gateway
ECU list, live data, and the G-series DoIP module scan.

Runs with no car, no ENET cable and no EDIABAS install:

* The EDIABAS-driven parts (CBS, battery, installed-ECU list, live channels)
  are tested against a stub of the job runner, so the real profile code --
  argument vectors, CBS version handling, result decoding, confirm gating --
  is what runs, with canned EdiabasTest output standing in for the car.
* The DoIP scan is tested against a FAKE G-SERIES GATEWAY on loopback that
  routes one address mapping and answers NACK 0x03 ("unknown target address")
  for the others. That is the real behaviour the mapping search relies on, so
  this proves the search finds the right mapping rather than reporting an
  empty car.

    .venv\\Scripts\\python.exe -m backend.tools.bmw_deep_selftest
"""

from __future__ import annotations

import os
import socket
import struct
import sys
import threading

from backend.core import doip_client, ediabas as core_ediabas
from backend.profiles.bmw import battery, cbs, ediabas as bmw_ediabas, pids, scan

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


# --------------------------------------------------------------------------
# EdiabasTest output parsing
# --------------------------------------------------------------------------
ECU_LIST_OUTPUT = """API Version: 7.7.2
JOB: STATUS_VCM_GET_ECU_LIST_ALL
STAT_SG_ANZAHL = 4
DATASET: 1
STAT_SG_NAME_TEXT = ZGW_01
STAT_SG_DIAG_ADRESSE = 0x10
DATASET: 2
STAT_SG_NAME_TEXT = DME_BX8
STAT_SG_DIAG_ADRESSE = 0x12
DATASET: 3
STAT_SG_NAME_TEXT = KOMB25
STAT_SG_DIAG_ADRESSE = 0x60
DATASET: 4
STAT_SG_NAME_TEXT = SOMETHING_NEW
STAT_SG_DIAG_ADRESSE = 0xAB
JOB_STATUS = OKAY
"""

CBS_DATA_OUTPUT = """JOB: CBS_DATEN_LESEN
DATASET: 1
ID_FN_CBS_MESS_HEX = 0x01
ID_FN_CBS_MESS_WERT = 1
ID_FN_CBS_MESS_TEXT = Motoroel
AVAI_CBS_WERT = 62
COU_RSTG_CBS_MESS_WERT = 3
RMMI_CBS_WERT = 12400
ST_UN_CBS_TEXT = km
DATASET: 2
ID_FN_CBS_MESS_HEX = 0x14
ID_FN_CBS_MESS_WERT = 20
ID_FN_CBS_MESS_TEXT = Uebergabedurchsicht
AVAI_CBS_WERT = 100
COU_RSTG_CBS_MESS_WERT = 0
JOB_STATUS = OKAY
"""


def test_parsing() -> None:
    print("EdiabasTest output parsing")
    parsed = core_ediabas.parse_output(ECU_LIST_OUTPUT)
    check(parsed["job"] == "STATUS_VCM_GET_ECU_LIST_ALL", "job name parsed")
    check(len(parsed["sets"]) == 4, f"4 result sets kept, not flattened (got {len(parsed['sets'])})")
    check(parsed["sets"][0]["results"]["STAT_SG_NAME_TEXT"] == "ZGW_01", "first set intact")
    check(parsed["sets"][3]["results"]["STAT_SG_DIAG_ADRESSE"] == "0xAB", "last set intact")
    check(parsed["results"]["STAT_SG_ANZAHL"] == "4", "job-wide results still flat")
    check(parsed["error"] is None, "no error on a clean run")

    failed = core_ediabas.parse_output(
        "JOB: FS_LESEN\nError occured: 0x0000001C IFH-0018: INITIALIZATION ERROR\n"
    )
    check(failed["error"]["code"] == "0x0000001C", "EDIABAS error code parsed")
    check(failed["sets"] == [], "an errored job yields no sets")

    check(core_ediabas.module_for_address("0x63") == "hu", "address 0x63 maps to the head unit")
    check(core_ediabas.module_for_address("0x12") == "dme", "address 0x12 maps to the engine")
    check(core_ediabas.module_for_address("0xFE") is None, "an unmapped address stays unmapped")
    check(core_ediabas.module_for_address(None) is None, "no address is not an error")


# --------------------------------------------------------------------------
# A stub job runner: the profile code runs for real, the car is canned.
# --------------------------------------------------------------------------
class FakeCar:
    """Answers EDIABAS jobs the way the F56 does."""

    def __init__(self, **overrides):
        self.calls: list[tuple[str, str, list | None]] = []
        self.overrides = overrides
        self.cbs_versions = {"kombi": 5, "dme": 5, "dsc": 6}
        self.refuse = set(overrides.get("refuse", ()))

    def run_job(self, module, job, params=None, mode="real", confirm=False, remote_host=None):
        self.calls.append((module, job, params))
        if (module, job) in self.refuse or job in self.refuse:
            return {"ok": False, "module": module, "job": job,
                    "message": "no answer from this module", "results": {}, "sets": []}
        results, sets = {}, []
        if job == "CBS_INFO":
            version = self.cbs_versions.get(module, 5)
            results = {"CBS_VERSION_TEXT": f"CBS Version {version}"}
        elif job == "CBS_DATEN_LESEN":
            parsed = core_ediabas.parse_output(CBS_DATA_OUTPUT)
            results, sets = parsed["results"], parsed["sets"]
        elif job == "CBS_RESET":
            results = {"JOB_STATUS": "OKAY", "_REQUEST": "2E 5F 8A 01 64 1F"}
        elif job == "STATUS_VCM_GET_ECU_LIST_ALL":
            parsed = core_ediabas.parse_output(ECU_LIST_OUTPUT)
            results, sets = parsed["results"], parsed["sets"]
        elif job == "STEUERN_BATTERIETAUSCH_REGISTRIEREN":
            results = {"JOB_STATUS": "OKAY", "_REQUEST": "31 01 F0 30"}
            self.overrides["registered"] = True
        elif job == "STATUS_SYSTEMCHECK_AEP_INFO_1":
            km = "48210" if self.overrides.get("registered") else "12500"
            results = {"STAT_BATTERIETAUSCH_LETZTER_WERT": km,
                       "STAT_BATTERIETAUSCH_LETZTER_EINH": "km",
                       "STAT_BATTERIELADUNG_SALDO_WERT": "-1.4"}
        elif job == "STATUS_SYSTEMCHECK_AEP_INFO_2":
            results = {"STAT_SOH_WERT": "87", "STAT_SOC_FIT_WERT": "74",
                       "STAT_BATTERIE_KAPAZITAET_WERT": "70"}
        elif job == "STATUS_MESSWERTE_IBS":
            results = {"STAT_U_BATT_WERT": "12.41", "STAT_U_BATT_EINH": "V",
                       "STAT_I_BATT_WERT": "-0.55", "STAT_T_BATT_WERT": "19.5"}
        elif job == "STATUS_LESEN":
            for arg in (params or [])[1:]:
                results[f"STAT_{arg}_WERT"] = "42"
            # The real car answers with the SGBD's result names.
            if params and "ITKUM" in params:
                results["STAT_KUEHLMITTELTEMPERATUR_WERT"] = "88"
                results["STAT_KUEHLMITTELTEMPERATUR_EINH"] = "°C"
        elif job == "STATUS_VERSORGUNGSSPANNUNG":
            results = {"STAT_MILLI_VOLT_WERT": "12380"}
        elif job == "STATUS_GWSZ_ANZEIGE":
            results = {"STAT_GWSZ_ANZEIGE_WERT": "48210"}
        return {"ok": True, "module": module, "job": job, "sgbd": f"{module}.prg",
                "results": results, "sets": sets, "raw": "", "message": "ok"}


def with_fake_car(**overrides):
    """Swap the job runner and the availability check for the duration."""
    car = FakeCar(**overrides)
    saved = (bmw_ediabas.run_job, bmw_ediabas.resolve_host, core_ediabas.availability)
    bmw_ediabas.run_job = car.run_job
    bmw_ediabas.resolve_host = lambda host=None: "169.254.1.1"
    core_ediabas.availability = lambda: {"available": True, "detail": "stub", "exe": "", "ecuDir": ""}
    return car, saved


def restore(saved) -> None:
    bmw_ediabas.run_job, bmw_ediabas.resolve_host, core_ediabas.availability = saved


def test_installed_modules() -> None:
    print("\nGateway installed-ECU list")
    car, saved = with_fake_car()
    try:
        out = bmw_ediabas.installed_modules("real")
    finally:
        restore(saved)
    check(out["ok"], "the list came back")
    check(out["count"] == 4, f"4 modules listed (got {out['count']})")
    keys = [m["module"] for m in out["modules"]]
    check(keys[:3] == ["zgw", "dme", "kombi"], f"addresses mapped to module keys ({keys[:3]})")
    check(out["modules"][3]["module"] is None and out["unmapped"] == 1,
          "an unknown address is reported, not silently dropped")
    check(out["modules"][3]["name"] == "SOMETHING_NEW", "the car's own name for it is kept")
    check("no job menu" in out["message"], "the message says some are unmapped")
    check(car.calls[0][1] == "STATUS_VCM_GET_ECU_LIST_ALL", "the gateway job was the one run")

    car, saved = with_fake_car(refuse={"STATUS_VCM_GET_ECU_LIST_ALL"})
    try:
        out = bmw_ediabas.installed_modules("real")
    finally:
        restore(saved)
    check(not out["ok"] and out["count"] == 0, "a silent gateway is an honest failure")


def test_cbs() -> None:
    print("\nCBS read and reset")
    cat = cbs.catalogue()
    check(len(cat["items"]) == 12, "12 service items from the cluster's own table")
    check(len(cat["versions"]["5"]) == 10 and len(cat["versions"]["6"]) == 11,
          "version 5 takes 10 arguments, version 6 takes 11")
    check(cat["versions"]["6"][9] == "CHOV_CBS_CBR_VIEW",
          "version 6 inserts CHOV_CBS_CBR_VIEW before the reserved byte")
    check(cbs.find_item("oel")["code"] == 0x01, "items resolve by name")
    check(cbs.find_item("0x06")["kennung"] == "Br_h", "items resolve by code")

    args = cbs._build_arguments(cbs.find_item("Oel"), 5, None, "15", "63")
    check(args == ["Oel", "100", "31", "15", "63", "32768", "15", "255", "255", "0"],
          "the version-5 argument vector matches the SGBD's defaults")
    check(cbs._build_arguments(cbs.find_item("Br_v"), 6, None, "15", "63")[-2:] == ["0", "0"],
          "the version-6 vector is one longer")

    unconfirmed = cbs.reset("Oel", mode="real", confirm=False)
    check(unconfirmed.get("needsConfirm") and not unconfirmed["ok"], "a reset without confirm is refused")

    car, saved = with_fake_car()
    try:
        out = cbs.read("real")
        reset_oil = cbs.reset("Oel", mode="real", confirm=True)
        reset_pads = cbs.reset("Br_v", mode="real", confirm=True)
    finally:
        restore(saved)

    check(out["ok"] and len(out["items"]) == 6, f"CBS items read from 3 modules (got {len(out['items'])})")
    oil = next(i for i in out["items"] if i["kennung"] == "Oel")
    check(oil["percent"] == "62" and oil["counter"] == "3", "oil life and service counter decoded")
    check(all(m["version"] for m in out["modules"]), "every module reported its CBS version")

    check(reset_oil["ok"] and reset_oil["module"] == "dme",
          "engine oil is reset on the engine, the module that owns it")
    check(reset_oil["version"] == 5 and len(reset_oil["arguments"]) == 10,
          "the engine got a version-5 vector")
    check(reset_pads["ok"] and reset_pads["module"] == "dsc" and reset_pads["version"] == 6,
          "brake pads go to the ABS unit, which speaks version 6")
    check(len(reset_pads["arguments"]) == 11, "and got the 11-argument vector, not the 10")
    check(reset_oil.get("request") == "2E 5F 8A 01 64 1F", "the raw telegram is reported back")

    # The owner refuses; the cluster is the documented fallback.
    car, saved = with_fake_car(refuse={("dme", "CBS_RESET"), ("dme", "CBS_INFO")})
    try:
        fallback = cbs.reset("Oel", mode="real", confirm=True)
    finally:
        restore(saved)
    check(fallback["ok"] and fallback["module"] == "kombi",
          "a refusing owner falls back to the cluster")

    car, saved = with_fake_car(refuse={"CBS_INFO"})
    try:
        no_version = cbs.reset("Oel", mode="real", confirm=True)
    finally:
        restore(saved)
    check(not no_version["ok"], "no CBS version means no reset is attempted")
    check(all(c[1] != "CBS_RESET" for c in car.calls),
          "and CBS_RESET was never sent -- a guessed vector writes the wrong values")

    for bad, label in (
        (dict(item="Brfl", percent=200), "over-range brake fluid percentage"),
        (dict(item="Oel", percent=150), "150% on an item that isn't brake fluid"),
        (dict(item="TUV", target_month=13), "month 13"),
        (dict(item="TUV", target_year=1990), "a year before 2000"),
        (dict(item="nonsense"), "an item that doesn't exist"),
    ):
        try:
            cbs.reset(mode="sim", confirm=True, **bad)
            check(False, f"refused: {label}")
        except cbs.CbsError:
            check(True, f"refused: {label}")
    try:
        cbs.reset("Oel", mode="sim", confirm=True, module="hu")
        check(False, "refused: a module that carries no CBS jobs")
    except cbs.CbsError:
        check(True, "refused: a module that carries no CBS jobs")


def test_battery() -> None:
    print("\nBattery condition and registration")
    check(battery.register(mode="real", confirm=False).get("needsConfirm"),
          "registering without confirm is refused")
    car, saved = with_fake_car()
    try:
        state = battery.status("real")
        done = battery.register(mode="real", confirm=True)
    finally:
        restore(saved)
    names = {r["name"]: r for r in state["readings"]}
    check(state["ok"] and len(names) >= 8, f"battery readings collected ({len(names)})")
    check(names["Battery voltage"]["value"] == "12.41" and names["Battery voltage"]["unit"] == "V",
          "the car's own unit is used, not ours")
    check(names["State of health"]["value"] == "87", "state of health read")
    check("coded battery type" in state["warning"], "the coding caveat rides along")
    check(done["ok"] and done["before"] == "12500" and done["after"] == "48210",
          "registration is proved by the car's own before/after mileage")
    check("48210" in done["message"], "and that evidence is in the message")

    car, saved = with_fake_car(refuse={"STEUERN_BATTERIETAUSCH_REGISTRIEREN"})
    try:
        refused = battery.register(mode="real", confirm=True)
    finally:
        restore(saved)
    check(not refused["ok"] and "nothing was changed" in refused["message"],
          "a refused registration says nothing was changed")


def test_live() -> None:
    print("\nLive data")
    check(len(pids.CHANNELS) >= len(pids.JOB_CHANNELS),
          "the built-in channels are always available")
    check(all(c["key"] and c["label"] and c["module"] for c in pids.CHANNELS),
          "every channel has a key, a label and a module")
    check(hasattr(pids, "CHANNELS") and hasattr(pids, "read_channels"),
          "the fast livefeed interface is implemented")
    keys = [c["key"] for c in pids.CHANNELS]
    check(len(keys) == len(set(keys)), "channel keys are unique")

    car, saved = with_fake_car()
    try:
        rows = pids.read_channels(None, "real", ["batt_volts", "batt_amps", "kombi_volts"])
        pack_rows = pids.read_channels(None, "real", ["dme_stat_kuehlmitteltemperatur_wert"])
    finally:
        restore(saved)
    got = {r["key"]: r for r in rows}
    check(len(rows) == 3, f"only the requested channels are read (got {len(rows)})")
    check(got["batt_volts"]["value"] == "12.41", "battery voltage came through")
    ibs_calls = [c for c in car.calls if c[1] == "STATUS_MESSWERTE_IBS"]
    check(len(ibs_calls) == 1,
          "two channels from one job cost one call, not two -- batching works")
    if pack_rows:
        check(pack_rows[0]["unit"] == "°C", "the car's unit wins over the table's")
    else:
        check(True, "engine pack channels absent on this machine (pack not generated)")

    check(pids.read_channels(None, "real", []) == [], "asking for nothing reads nothing")
    check(pids.read_channels(None, "sim", ["batt_volts"])[0]["value"], "sim mode answers without a car")


# --------------------------------------------------------------------------
# A fake G-series gateway: routes ONE mapping, NACKs everything else.
# --------------------------------------------------------------------------
ROUTED = {0x0E12: "engine", 0x0E60: "cluster", 0x0E63: "head unit"}
FAKE_VIN = "WBA11AA0X0X000001"


def fake_gateway(sock: socket.socket, stop: threading.Event, state: dict) -> None:
    while not stop.is_set():
        try:
            conn, _addr = sock.accept()
        except OSError:
            return
        with conn:
            try:
                while not stop.is_set():
                    payload_type, body = doip_client.read_message(conn, timeout=5.0)
                    if payload_type == doip_client.PT_ROUTING_ACTIVATION_REQ:
                        src, _act = struct.unpack(">HB", body[:3])
                        state.setdefault("testers", []).append(src)
                        code = 0x10 if src == state["good_tester"] else 0x00
                        conn.sendall(doip_client.pack_message(
                            doip_client.PT_ROUTING_ACTIVATION_RESP,
                            struct.pack(">HHB", src, 0x0010, code) + b"\x00" * 4))
                        continue
                    if payload_type != doip_client.PT_DIAG_MESSAGE:
                        continue
                    src, dst = struct.unpack(">HH", body[:4])
                    uds_req = body[4:]
                    if dst not in ROUTED:
                        state.setdefault("nacked", []).append(dst)
                        conn.sendall(doip_client.pack_message(
                            doip_client.PT_DIAG_NACK,
                            struct.pack(">HHB", dst, src, 0x03)))
                        continue
                    conn.sendall(doip_client.pack_message(
                        doip_client.PT_DIAG_ACK,
                        struct.pack(">HHB", dst, src, 0x00)))
                    service = uds_req[0]
                    if service == 0x3E:
                        reply = bytes([0x7E, 0x00])
                    elif service == 0x22 and uds_req[1:3] == b"\xF1\x90":
                        reply = bytes([0x62, 0xF1, 0x90]) + FAKE_VIN.encode()
                    elif service == 0x19:
                        reply = bytes([0x59, 0x02, 0xFF])
                        if dst == 0x0E63:
                            reply += bytes([0xB7, 0xF2, 0x03, 0x2F])
                    elif service == 0x14:
                        state.setdefault("cleared", []).append(dst)
                        reply = bytes([0x54])
                    else:
                        reply = bytes([0x7F, service, 0x11])
                    conn.sendall(doip_client.pack_message(
                        doip_client.PT_DIAG_MESSAGE,
                        struct.pack(">HH", dst, src) + reply))
            except (OSError, doip_client.DoipError, struct.error):
                continue


def test_doip_scan() -> None:
    print("\nG-series DoIP module scan")
    state = {"good_tester": scan.TESTER_ADDRESSES[0]}
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)
    port = sock.getsockname()[1]
    stop = threading.Event()
    thread = threading.Thread(target=fake_gateway, args=(sock, stop, state), daemon=True)
    thread.start()
    os.environ["CARHARNESS_DOIP_PORT"] = str(port)
    saved_discover = scan.discover
    scan.discover = lambda: {"found": True, "protocol": "DoIP", "ip": "127.0.0.1",
                             "vin": None, "detail": "fake"}
    try:
        out = scan.scan("real")
        cleared = scan.clear("real", "0x63")
    finally:
        scan.discover = saved_discover
        os.environ.pop("CARHARNESS_DOIP_PORT", None)
        stop.set()
        sock.close()

    check(out["ok"], f"the DoIP scan completed ({out['message'][:60]})")
    check(out["vehicle"]["protocol"] == "DoIP", "reported as a DoIP car")
    check(out["vehicle"]["addressMapping"] == "0x0Exx",
          f"the right address mapping was found ({out['vehicle'].get('addressMapping')})")
    present = [m for m in out["modules"] if m["present"]]
    check(len(present) == 3, f"3 modules answered (got {len(present)})")
    check(all("logicalAddress" in m for m in out["modules"]),
          "each module row shows the logical address actually used")
    hu = next(m for m in out["modules"] if m["address"] == "0x63")
    check(len(hu["dtcs"]) == 1, "the head unit's fault was read")
    check(out["vehicle"]["vin"] == FAKE_VIN, "the VIN was read from a module")
    check(state["nacked"], "wrong mappings were rejected by the gateway, as expected")
    check(cleared["ok"] and cleared["cleared"] == ["0x63"], "clear works over DoIP too")
    check(0x0E63 in state.get("cleared", []), "and reached the right logical address")

    # A gateway that routes nothing must read as an unconfirmed mapping, not
    # as a car with no modules.
    state2 = {"good_tester": scan.TESTER_ADDRESSES[0]}
    sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock2.bind(("127.0.0.1", 0))
    sock2.listen(4)
    port2 = sock2.getsockname()[1]
    stop2 = threading.Event()
    routed_backup = dict(ROUTED)
    ROUTED.clear()
    threading.Thread(target=fake_gateway, args=(sock2, stop2, state2), daemon=True).start()
    os.environ["CARHARNESS_DOIP_PORT"] = str(port2)
    scan.discover = lambda: {"found": True, "protocol": "DoIP", "ip": "127.0.0.1",
                             "vin": None, "detail": "fake"}
    try:
        empty = scan.scan("real")
    finally:
        ROUTED.update(routed_backup)
        scan.discover = saved_discover
        os.environ.pop("CARHARNESS_DOIP_PORT", None)
        stop2.set()
        sock2.close()
    check(not empty["ok"], "a car that routes nothing is not reported as scanned")
    check("not published" in empty["message"] or "mapping" in empty["message"],
          "and the message blames the unknown mapping, not the car")


def main() -> int:
    print("BMW/MINI deep functions -- offline self-test\n")
    test_parsing()
    test_installed_modules()
    test_cbs()
    test_battery()
    test_live()
    test_doip_scan()
    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed.")
    if passed != len(results):
        print("FAILED: " + "; ".join(label for ok, label in results if not ok))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
