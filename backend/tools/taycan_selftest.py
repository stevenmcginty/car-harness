"""Self-test for the Porsche Taycan HV battery read, offline.

Run:  python -m backend.tools.taycan_selftest

Two halves:

1. **Decoder fidelity.** Every scaling is re-checked against the actual response
   frames captured from real Taycans (MY2020-MY2025) and recorded in
   docs/private/taycan-battery-soh-research.md. If someone "tidies" a formula,
   these fail loudly -- which is the point, because a wrong state-of-health
   number on a dealer's screen costs him real money.

2. **Behaviour.** A fake battery controller answers the real read_health() code
   over the same request(dst, uds, timeout) contract a car uses, so the honest
   paths get exercised: missing DIDs reported with a reason rather than
   guessed, the MY2025 J1.2 refusal, the pack-voltage disagreement warning, and
   the read-only guard refusing every write/actuate service.

No car, no cable, no dongle. Exits 0 on PASS, 1 on FAIL.
"""

import socket
import struct
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core import doip_client, uds  # noqa: E402
from backend.profiles.porsche import battery  # noqa: E402
from backend.profiles.porsche_enet import ecus, pids as pids_mod, scan as scan_mod  # noqa: E402

results: list = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


def close(a, b, tol=0.001) -> bool:
    return a is not None and abs(a - b) < tol


# --------------------------------------------------------------------------- #
# A fake Taycan battery controller.
# --------------------------------------------------------------------------- #
class FakeTaycan:
    """Answers ReadDataByIdentifier with the captured frames. Anything not in
    the table gets a genuine negative response, like a real ECU."""

    def __init__(self, dids: dict, addresses=None):
        self.dids = dids
        self.addresses = addresses or battery.CAN_ADDRESSES
        self.sent: list = []
        self.sessions: list = []

    def request(self, dst: int, data: bytes, timeout: float = 3.0) -> bytes:
        self.sent.append((dst, data.hex()))
        service = data[0]
        if service == uds.SVC_SESSION_CONTROL:
            self.sessions.append(data[1])
            return bytes([0x50, data[1]])
        if service == uds.SVC_TESTER_PRESENT:
            return bytes([0x7E, 0x00])
        if service == uds.SVC_READ_DATA_BY_ID:
            did = (data[1] << 8) | data[2]
            payload = self.dids.get((dst, did))
            if payload is None:
                return bytes([0x7F, service, 0x31])  # request out of range
            return bytes([0x62, data[1], data[2]]) + payload
        return bytes([0x7F, service, 0x11])


def h(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


# --------------------------------------------------------------------------- #
# A fake Taycan gateway speaking real DoIP, for the end-to-end scan test.
# --------------------------------------------------------------------------- #
FAKE_FITTED = {
    ecus.GATEWAY_ADDRESS: "EV_Gatew31xPO513",
    0x407B: "EV_BECM1982091",
    0x4013: "EV_ESP9BOSCHPO513",
    0x4014: "EV_DashBoardLGEPO513",
    0x4321: "EV_SomethingWeHaveNoNameFor",  # answers, but not in our table
}
FAKE_VIN_DOIP = "WP0ZZZY1ZNSA00001"

# 0x4013 fault memory: one real confirmed fault, then two entries that are
# defined but have never failed -- exactly the noise the 0x0D filter removes.
ESP_DTC_RECORDS = h("C10A00 2F  C10B87 08  C10C00 50  C10D00 40")


def _fake_uds(target: int, req: bytes) -> bytes:
    service = req[0]
    if service == uds.SVC_TESTER_PRESENT:
        return bytes([0x7E, 0x00])
    if service == uds.SVC_READ_DATA_BY_ID:
        did = (req[1] << 8) | req[2]
        if did == 0xF19E:
            return bytes([0x62, req[1], req[2]]) + FAKE_FITTED[target].encode()
        if did == 0xF190:
            return bytes([0x62, req[1], req[2]]) + FAKE_VIN_DOIP.encode()
        if did == 0x2A30:
            return bytes([0x62, req[1], req[2]]) + bytes(
                [0x8C if target == 0x407B else 0x03, target >> 8, target & 0xFF]
            )
        return bytes([0x7F, service, 0x31])
    if service == uds.SVC_READ_DTC:
        if req[2] != 0xFF:
            return bytes([0x7F, service, 0x31])  # only the wide mask, like the car
        if target == 0x4013:
            return bytes([0x59, 0x02, 0xFF]) + ESP_DTC_RECORDS
        return bytes([0x59, 0x02, 0xFF])
    if service == uds.SVC_CLEAR_DTC:
        return bytes([0x54])
    return bytes([0x7F, service, 0x11])


def fake_taycan(sock, state, stop):
    while not stop.is_set():
        try:
            conn, _addr = sock.accept()
        except OSError:
            return
        with conn:
            try:
                while not stop.is_set():
                    ptype, body = doip_client.read_message(conn, timeout=5.0)
                    if ptype == doip_client.PT_ROUTING_ACTIVATION_REQ:
                        src, _at = struct.unpack(">HB", body[:3])
                        state["tester"] = src
                        conn.sendall(
                            doip_client.pack_message(
                                doip_client.PT_ROUTING_ACTIVATION_RESP,
                                struct.pack(">HHB", src, ecus.GATEWAY_ADDRESS, 0x10)
                                + b"\x00\x00\x00\x00",
                            )
                        )
                        continue
                    if ptype == doip_client.PT_POWER_MODE_REQ:
                        conn.sendall(
                            doip_client.pack_message(
                                doip_client.PT_POWER_MODE_RESP, bytes([0x01])
                            )
                        )
                        continue
                    if ptype != doip_client.PT_DIAG_MESSAGE:
                        continue
                    src, target = struct.unpack(">HH", body[:4])
                    req = body[4:]
                    if target == ecus.FUNCTIONAL_ADDRESS:
                        state["broadcast_used"] = True
                        for addr in sorted(FAKE_FITTED):
                            conn.sendall(
                                doip_client.pack_message(
                                    doip_client.PT_DIAG_MESSAGE,
                                    struct.pack(">HH", addr, src) + bytes([0x7E, 0x00]),
                                )
                            )
                        continue
                    if target not in FAKE_FITTED:
                        conn.sendall(
                            doip_client.pack_message(
                                doip_client.PT_DIAG_NACK,
                                struct.pack(">HH", target, src) + bytes([0x03]),
                            )
                        )
                        continue
                    state.setdefault("sent", []).append((target, req.hex()))
                    conn.sendall(
                        doip_client.pack_message(
                            doip_client.PT_DIAG_ACK,
                            struct.pack(">HH", target, src) + bytes([0x00]),
                        )
                    )
                    conn.sendall(
                        doip_client.pack_message(
                            doip_client.PT_DIAG_MESSAGE,
                            struct.pack(">HH", target, src) + _fake_uds(target, req),
                        )
                    )
            except (OSError, doip_client.DoipError):
                continue


def main() -> int:
    print("Porsche Taycan HV battery self-test (offline)\n")
    bms = battery.CAN_ADDRESSES["bms"]
    vcu = battery.CAN_ADDRESSES["vcu"]
    charger = battery.CAN_ADDRESSES["charger"]

    # ---------------------------------------------------------------- #
    print("Decoders vs captured frames:")
    # ---------------------------------------------------------------- #
    # Tolerance 0.01: the decoder rounds to 2 dp for display, and 1 LSB of this
    # field is 0.127 %, so more precision than that would be invented anyway.
    check(close(battery.decode_soh(h("3958")), 65.786, 0.01), "SoH 0x3958 -> 65.786 %")
    check(close(battery.decode_soh(h("3A00")), 87.122, 0.01), "SoH 0x3A00 -> 87.122 %")
    check(close(battery.decode_soh(h("3A27")), 92.075, 0.01), "SoH 0x3A27 -> 92.075 % (MY2024)")
    check(battery.decode_soh(h("0000")) is None, "SoH raw 0x0000 is 'no value', not -1798 %")

    cmax = battery.decode_cell_extreme(h("8BCE 5E"))
    check(close(cmax["volts"], 3.579) and cmax["cell"] == 94, "cell max 3.579 V at cell 94")
    cmin = battery.decode_cell_extreme(h("8A66 3E"))
    check(close(cmin["volts"], 3.543) and cmin["cell"] == 62, "cell min 3.543 V at cell 62")
    check(
        close(round((cmax["volts"] - cmin["volts"]) * 1000, 1), 36.0, 0.11),
        "cell delta works out at 36 mV",
    )

    check(close(battery.decode_pack_voltage(h("1BA2")), 707.4), "cell-voltage sum 707.4 V")
    check(close(battery.decode_pack_voltage(h("1C39")), 722.5), "pack voltage 722.5 V")
    check(close(battery.decode_pack_current(h("01A6 EA")), -419.68), "pack current -419.68 A (discharging)")
    check(battery.decode_displayed_soc(h("080C 080C")) == 20.60, "displayed SoC 20.60 %")

    tmax = battery.decode_temp_extreme(h("79 09"))
    check(tmax["celsius"] == 21 and tmax["cell"] == 9, "pack temp max 21 C at cell 9 (-100 offset)")
    check(battery.decode_temp_average(h("78")) == 20, "pack temp average 20 C")
    check(
        battery.decode_module_temps(h("46 46 4B")) == [20, 20, 25],
        "per-module temps 20/20/25 C (-50 offset, NOT -100)",
    )

    check(battery.decode_present_energy(h("013C 7E")) == 15.80, "present energy 15.80 kWh")
    check(battery.decode_u32(h("00000002")) == 2, "launch-control count 2")

    block = battery.decode_cell_block(h("83 8480 41 00000000 844E 40 00000000 84C6 41 00000000"))
    check(block["moduleId"] == 131, "module block: hardware id 131")
    check(len(block["cells"]) == 3, "module block: 3 series positions decoded")
    check(close(block["cells"][0]["volts"], 3.392) and block["cells"][0]["soc"] == 65, "cell 1 = 3.392 V, 65 %")
    check(close(block["cells"][1]["volts"], 3.387) and block["cells"][1]["soc"] == 64, "cell 2 = 3.387 V, 64 %")
    check(close(block["cells"][2]["volts"], 3.399) and block["cells"][2]["soc"] == 65, "cell 3 = 3.399 V, 65 %")

    # ---------------------------------------------------------------- #
    print("\nRead-only safety guard:")
    # ---------------------------------------------------------------- #
    for service, name in (
        (0x31, "RoutineControl"),
        (0x2F, "InputOutputControl"),
        (0x2E, "WriteDataByIdentifier"),
        (0x27, "SecurityAccess"),
        (0x11, "ECUReset"),
        (0x14, "ClearDiagnosticInformation"),
        (0x28, "CommunicationControl"),
        (0x85, "ControlDTCSetting"),
        (0x34, "RequestDownload"),
        (0x3D, "WriteMemoryByAddress"),
    ):
        try:
            battery.assert_read_only(bytes([service, 0x01, 0x02]))
            check(False, f"{name} (0x{service:02X}) is refused")
        except battery.UnsafeRequest:
            check(True, f"{name} (0x{service:02X}) is refused at the pack")
    try:
        battery.assert_read_only(bytes([0x10, 0x02]))
        check(False, "programming session refused")
    except battery.UnsafeRequest:
        check(True, "programming session (0x10 02) is refused")
    for allowed in (bytes([0x22, 0x51, 0xE0]), bytes([0x10, 0x03]), bytes([0x3E, 0x00]), bytes([0x19, 0x02, 0xFF])):
        try:
            battery.assert_read_only(allowed)
            check(True, f"read service 0x{allowed[0]:02X} is allowed through")
        except battery.UnsafeRequest as exc:
            check(False, f"read service 0x{allowed[0]:02X} wrongly refused: {exc}")

    # ---------------------------------------------------------------- #
    print("\nModel-year gate (the J1.2 refusal):")
    # ---------------------------------------------------------------- #
    check(battery.model_year_from_vin("WP0ZZZY1ZLSA00001") == 2020, "VIN position 10 'L' -> 2020")
    check(battery.model_year_from_vin("WP0ZZZY1ZNSA00001") == 2022, "VIN position 10 'N' -> 2022")
    check(battery.model_year_from_vin("WP0ZZZY1ZSSA00001") == 2025, "VIN position 10 'S' -> 2025")
    check(battery.model_year_from_vin("JMZND6DMA00000001") is None, "a non-Porsche VIN yields no year")
    check(battery.model_year_from_vin(None) is None, "no VIN yields no year")
    ok, _ = battery.soh_supported("WP0ZZZY1ZNSA00001")
    check(ok, "MY2022 Taycan: SoH supported")
    ok, reason = battery.soh_supported("WP0ZZZY1ZSSA00001")
    check(not ok, "MY2025 J1.2: SoH refused rather than guessed")
    check(reason and "not decodable" in reason, "the refusal explains itself in plain English")

    # ---------------------------------------------------------------- #
    print("\nread_health() against a fake battery controller:")
    # ---------------------------------------------------------------- #
    car = {
        (bms, 0x51E0): h("3A00"),
        (bms, 0x1E33): h("8BCE 5E"),
        (bms, 0x1E34): h("8A66 3E"),
        (bms, 0x1E2C): h("41 5E"),
        (bms, 0x1E2D): h("40 3E"),
        (bms, 0x1E0E): h("79 09"),
        (bms, 0x1E0F): h("77 15"),
        (bms, 0x1E10): h("78"),
        (bms, 0x1801): h("1C39"),
        (bms, 0x1E3B): h("1C35"),
        (bms, 0x1802): h("01A6 EA"),
        (bms, 0x028C): h("20"),
        (bms, 0x5200): h("00000002"),
        (vcu, 0x08D2): h("080C 080C"),
        (charger, 0x4965): h("013C 7E"),
        (charger, 0x15EE): h("0001527B"),
    }
    fake = FakeTaycan(car)
    health = battery.read_health(fake, vin="WP0ZZZY1ZNSA00001")

    check(close(health["soh"]["percent"]["value"], 87.122, 0.01), "SoH surfaces as 87.12 %")
    check(health["soh"]["band"]["label"] == "normal", "87 % bands as 'normal for age'")
    check(health["cells"]["deltaMillivolts"]["value"] == 36.0, "cell delta computed = 36 mV")
    check(
        health["cells"]["deltaMillivolts"]["verdict"]["label"] == "acceptable",
        "36 mV reads as 'acceptable', not alarmist",
    )
    check(health["cells"]["max"]["value"]["cell"] == 94, "worst cell index carried through")
    check(health["thermal"]["max"]["value"]["celsius"] == 21, "pack max temperature 21 C")
    check(close(health["electrical"]["packAmps"]["value"], -419.68), "pack current surfaces")
    check(health["charge"]["trueSoc"]["value"] == 32, "true SoC 32 %")
    check(close(health["charge"]["displayedSoc"]["value"], 20.60), "displayed SoC read from the VCU")
    check(health["history"]["launchControlCount"]["value"] == 2, "launch-control count 2")

    implied = health["charge"]["impliedUsableKwh"]["value"]
    check(close(implied, 49.4, 0.2), f"implied usable capacity computed ({implied} kWh)")
    check(
        "impliedSohPercent" in health["charge"],
        "independent SoH estimate produced from a second code path",
    )
    check(
        any("disagree" in w for w in health["warnings"]),
        "a disagreement between the two health figures is surfaced, not hidden",
    )

    # honest absences
    check(
        health["thermal"]["coolantIn"]["available"] is False,
        "a DID the car does not answer is marked unavailable",
    )
    check(
        health["thermal"]["coolantIn"]["reason"] is not None,
        "...and carries the reason it is missing",
    )
    check(
        health["history"]["energyTurnoverRaw"]["available"] is False,
        "an unanswered history counter is not invented",
    )
    check(
        any("read-only" in n.lower() for n in health["notes"]),
        "the report states plainly that nothing was written",
    )
    check(
        all(entry[1][:2] in ("22", "10", "3e", "19") for entry in fake.sent),
        "only read services ever reached the fake pack",
    )

    # pack-voltage disagreement warning
    check(
        not any("disagree by" in w for w in health["warnings"]),
        "matching pack/cell-sum voltages raise no warning",
    )
    car_bad = dict(car)
    car_bad[(bms, 0x1E3B)] = h("1AF0")  # 691.2 V vs pack 722.5 V
    health_bad = battery.read_health(FakeTaycan(car_bad), vin="WP0ZZZY1ZNSA00001")
    check(
        any("measurement fault" in w for w in health_bad["warnings"]),
        "a pack/cell-sum voltage mismatch is flagged as a measurement fault",
    )

    # MY2025 refusal end to end
    health_25 = battery.read_health(FakeTaycan(car), vin="WP0ZZZY1ZSSA00001")
    check(health_25["soh"]["supported"] is False, "MY2025 car: SoH marked unsupported")
    check(health_25["soh"]["percent"]["value"] is None, "MY2025 car: no SoH number is shown")
    check(
        health_25["cells"]["deltaMillivolts"]["value"] == 36.0,
        "MY2025 car: cell measurements still read (only SoH is withheld)",
    )

    # unknown VIN -> read, but say so
    health_novin = battery.read_health(FakeTaycan(car))
    check(
        any("Model year could not be read" in n for n in health_novin["notes"]),
        "unknown model year is caveated rather than silently assumed",
    )

    # a car that answers nothing
    empty = battery.read_health(FakeTaycan({}), vin="WP0ZZZY1ZNSA00001")
    check(empty["soh"]["percent"]["available"] is False, "silent car: no SoH invented")
    check(
        empty["cells"]["deltaMillivolts"]["available"] is False,
        "silent car: no cell delta invented",
    )
    check("warnings" in empty and "notes" in empty, "silent car still returns a well-formed report")

    # deep pass
    deep_car = dict(car)
    for n in range(28):  # a 28-module Performance Battery: last five stay silent
        deep_car[(bms, battery.CELL_BLOCK_FIRST + n)] = h(
            "83 8480 41 00000000 844E 40 00000000 84C6 41 00000000"
        )
        deep_car[(bms, battery.MODULE_TEMP_FIRST + n)] = h("46 46 4B")
    fake_deep = FakeTaycan(deep_car)
    deep = battery.read_health(fake_deep, vin="WP0ZZZY1ZNSA00001", deep=True)
    present = [m for m in deep["modules"] if m["present"]]
    check(len(present) == 28, f"deep pass: 28 modules answered, 5 silent (got {len(present)})")
    check(
        deep["modules"][30]["present"] is False and "not fitted" in deep["modules"][30]["reason"],
        "a silent module reads as 'not fitted', never as a fault",
    )
    check(deep["cellMap"]["count"] == 84, "deep pass: cell map built from every answering cell")
    check(close(deep["cellMap"]["maxVolts"], 3.399), "cell map max voltage")
    check(close(deep["cellMap"]["spreadMillivolts"], 12.0, 0.11), "cell map spread 12 mV")
    check(len(deep["cellMap"]["weakest"]) == 10, "cell map lists the ten weakest cells")
    check(deep["modules"][0]["temps"] == [20, 20, 25], "deep pass: per-module temperatures")
    check(0x03 in fake_deep.sessions, "deep pass opens the extended session (0x10 03)")
    check(
        all(entry[1][:2] in ("22", "10", "3e", "19") for entry in fake_deep.sent),
        "deep pass also sent read services only",
    )

    # ---------------------------------------------------------------- #
    print("\nECU table integrity:")
    # ---------------------------------------------------------------- #
    addresses = [addr for _n, addr, _v, _a in ecus.TAYCAN_ECUS]
    check(len(addresses) == len(set(addresses)), "no duplicate logical addresses in the table")
    check(all(0x4000 <= a <= 0x4FFF for a in addresses), "every address sits in the 0x4xxx range")
    check(all(n and n.strip() for n, _a, _v, _s in ecus.TAYCAN_ECUS), "every module has a name")
    check(ecus.GATEWAY_ADDRESS in ecus.NAMES, "the gateway is in the table")
    check(0x407B in ecus.NAMES, "the HV battery controller is in the table")
    check(len(ecus.TAYCAN_ECUS) >= 40, f"table covers {len(ecus.TAYCAN_ECUS)} modules")
    check(
        "Unknown module" in ecus.name_for(0x4999),
        "an address we have no name for is labelled honestly, not guessed",
    )
    self_id = ecus.decode_self_id(bytes([0x8C, 0x40, 0x7B]))
    check(
        self_id == {"vagCode": "0x8C", "reportedAddress": "0x407B"},
        "DID 0x2A30 self-identification decodes",
    )
    check(ecus.decode_self_id(b"\x8C") is None, "a short self-id returns None, not junk")
    check(
        all(a in ecus.NAMES for a in scan_mod.HV_MODULES),
        "every high-voltage module named in the clear-guard exists in the table",
    )

    # ---------------------------------------------------------------- #
    print("\nEnd-to-end scan against a fake Taycan gateway:")
    # ---------------------------------------------------------------- #
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    fake_port = server.getsockname()[1]
    state: dict = {}
    stop = threading.Event()
    threading.Thread(target=fake_taycan, args=(server, state, stop), daemon=True).start()

    real_client = doip_client.DoipClient
    real_discover = scan_mod.discover
    real_open = pids_mod.open_client

    class PinnedPort(doip_client.DoipClient):
        """The scan builds its own client; point it at the fake car's port."""

        def __init__(self, ip, **kwargs):
            kwargs["port"] = fake_port
            super().__init__(ip, **kwargs)

    doip_client.DoipClient = PinnedPort
    scan_mod.discover = lambda: {
        "found": True,
        "protocol": "DoIP",
        "ip": "127.0.0.1",
        "vin": None,
        "gatewayAddress": "0x4010",
        "detail": "fake car",
    }
    try:
        report = scan_mod.scan("real")
        check(report["ok"] is True, "scan completes against the fake car")
        check(state.get("broadcast_used") is True, "the functional broadcast was used first")
        check(report["discovery"] == "functional broadcast", "report says how modules were found")
        check(len(report["modules"]) == len(FAKE_FITTED), f"all {len(FAKE_FITTED)} fitted modules reported")
        check(report["vehicle"]["vin"] == FAKE_VIN_DOIP, "VIN read from the gateway")
        check(report["message"].startswith(f"VIN {FAKE_VIN_DOIP}"), "the VIN leads the message")

        by_addr = {m["address"]: m for m in report["modules"]}
        esp = by_addr["0x4013"]
        check(len(esp["dtcs"]) == 2, f"ESP reports 2 real faults (got {len(esp['dtcs'])})")
        check(esp["suppressed"] == 2, "2 never-failed self-test entries filtered out")
        check(esp["dtcs"][0]["hex"] == "C10A00", "first fault carries its raw hex")
        check(
            all(int(d["status"], 16) & 0x0D for d in esp["dtcs"]),
            "every reported fault has a real status bit set",
        )
        check("filtered out" in report["message"], "the filtering is disclosed, not silent")

        becm = by_addr["0x407B"]
        check(becm["name"] == "HV battery management (BECM)", "table supplies the display name")
        check(becm["ident"].get("asamId") == "EV_BECM1982091", "module identity read")
        check(becm["ident"].get("vagCode") == "0x8C", "VAG code from self-identification")
        check(becm["known"] is True, "a table module is marked known")

        stranger = by_addr["0x4321"]
        check(stranger["known"] is False, "a module not in our table is flagged as unknown")
        check("Unknown module" in stranger["name"], "...and named honestly by its address")
        check(
            stranger["ident"].get("asamId") == "EV_SomethingWeHaveNoNameFor",
            "...but still reports what it says about itself",
        )
        check("not in our table" in report["message"], "the report mentions the unnamed module")

        check(
            state.get("tester") == ecus.TESTER_ADDRESS == 0x0E80,
            "the scan identifies itself as tester 0x0E80",
        )
        check(
            all(req[:2] in ("3e", "22", "19") for _t, req in state.get("sent", [])),
            "the scan sent read services only -- no writes reached the fake car",
        )

        # clear() must refuse the high-voltage modules
        cleared = scan_mod.clear("real")
        check(cleared["refused"] == ["0x407B"], "clear-all skips the HV battery controller")
        check("0x4013" in cleared["cleared"], "...while still clearing the ordinary modules")
        check("evidence" in cleared["message"], "the refusal explains why")
        single = scan_mod.clear("real", "0x407B")
        check(single["ok"] is False, "clearing the HV battery directly is refused")
        check(
            "high-voltage module" in single["message"],
            "...with a reason a dealer can act on",
        )

        # battery read over the same transport
        pids_mod.open_client = lambda: (
            PinnedPort("127.0.0.1", source_address=ecus.TESTER_ADDRESS),
            {"vin": FAKE_VIN_DOIP, "ip": "127.0.0.1", "protocol": "DoIP"},
        )
        live = pids_mod.live("real")
        check(live["ok"] is True, "battery read runs over the DoIP transport")
        check(
            live["battery"]["addresses"]["bms"] == "0x407B",
            "battery read targets the DoIP address map, not the CAN one",
        )
        check(
            live["battery"]["soh"]["percent"]["available"] is False,
            "fake car has no SoH DID -- reported missing, not invented",
        )
    finally:
        doip_client.DoipClient = real_client
        scan_mod.discover = real_discover
        pids_mod.open_client = real_open
        stop.set()
        try:
            server.close()
        except OSError:
            pass

    # ---------------------------------------------------------------- #
    print("\nHTTP routes and panel contract:")
    # ---------------------------------------------------------------- #
    from fastapi.testclient import TestClient  # noqa: E402

    from backend.main import app  # noqa: E402

    client = TestClient(app)
    r = client.get("/api/porsche_enet/scan?mode=sim")
    check(r.status_code == 200 and r.json()["simulated"], "GET /api/porsche_enet/scan?mode=sim")
    check(len(r.json()["modules"]) >= 10, "simulated scan returns a realistic module list")
    r = client.get("/api/porsche_enet/live?mode=sim")
    check(r.status_code == 200 and r.json()["battery"]["soh"]["percent"]["available"], "GET live (battery health)")
    live_body = r.json()
    names = [x["name"] for x in live_body["readings"]]
    check("State of health" in names and "Cell spread" in names, "battery read projects display chips")
    check(
        all(x.get("value") is not None for x in live_body["readings"]),
        "no chip is rendered with an empty value",
    )
    r = client.get("/api/porsche_enet/snapshot?mode=sim")
    check(r.status_code == 200 and r.json()["battery"]["cellMap"]["count"] > 100, "GET snapshot (full cell map)")
    snap_names = [x["name"] for x in r.json()["readings"]]
    check("Spread across pack" in snap_names, "cell map projects its own chips")
    check("Battery modules answering" in snap_names, "cell map reports how many modules answered")

    # A car that answers nothing must produce no chips at all rather than blanks.
    empty_health = {"soh": {"supported": True, "percent": {"available": False}}, "cells": {}, "thermal": {}, "electrical": {}, "charge": {}, "history": {}}
    check(pids_mod._readings(empty_health) == [], "a silent battery produces no chips, not empty ones")
    # Discover talks to whatever is actually on the wire, so this check must not
    # assume an empty bench -- it used to fail the moment a real Taycan was
    # plugged in, which is precisely when the suite most needs to be trustworthy.
    # What matters is that the answer is well-formed and self-consistent either
    # way: no car -> found False with a reason; a car -> found True with an IP.
    r = client.get("/api/porsche_enet/discover")
    body = r.json() if r.status_code == 200 else {}
    if body.get("found") is True:
        honest = bool(body.get("ip")) and body.get("protocol") == "DoIP"
        label = "GET discover with a real car attached reports it properly"
    else:
        honest = body.get("found") is False and bool(body.get("detail"))
        label = "GET discover with no cable is honest"
    check(r.status_code == 200 and honest, label)
    r = client.post("/api/porsche_enet/clear", json={"mode": "sim", "confirm": True})
    check(r.status_code == 200, "POST clear with confirm")
    r = client.post("/api/porsche_enet/clear", json={"mode": "sim"})
    check(r.status_code == 400, "POST clear without confirm is refused")
    r = client.get("/api/porsche_enet/scan?mode=nonsense")
    check(r.status_code == 400, "a bad mode is rejected")
    r = client.get("/api/not_a_brand/scan?mode=sim")
    check(r.status_code == 404, "an unknown brand is still 404")

    profiles = client.get("/api/profiles").json()
    entries = profiles["profiles"] if isinstance(profiles, dict) else profiles
    entry = next((p for p in entries if p["id"] == "porsche_enet"), None)
    check(entry is not None, "the profile is listed by /api/profiles")
    check(entry["features"]["needsAdapter"] is False, "listed as a native-cable brand")
    check(entry["features"]["discover"] is True, "discover advertised")
    check(
        {a["id"] for a in entry["panel"]["actions"]} == {"live", "snapshot"},
        "panel advertises both battery actions",
    )
    check(
        all(a["label"] and a["hint"] for a in entry["panel"]["actions"]),
        "every panel action carries a label and an explanation",
    )
    check(
        "porsche" in [p["id"] for p in entries],
        "the original dongle Porsche profile is untouched and still listed",
    )

    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    if passed != total:
        print("\nFAILED:")
        for ok, label in results:
            if not ok:
                print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
