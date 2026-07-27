"""Self-test for the DoIP (ISO 13400-2) client against a fake entity on loopback.

Run:  python -m backend.tools.doip_selftest

Spins up a pretend DoIP gateway on a random loopback port, then drives the REAL
client code -- doip_client.DoipClient plus the shared uds.* helpers -- through
the same call sequence a module scan uses on a car: routing activation, entity
status, power mode, presence probe, VIN, a DTC read (with a positive ack, an
alive check and a 0x78 response-pending thrown in), and a clear. Also checks the
honest failure paths: an unknown target address must surface as "unreachable",
and a refused routing activation must name the reason rather than time out.

No car and no cable required. Exits 0 on PASS, 1 on FAIL.
"""

import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core import doip_client, uds  # noqa: E402

FAKE_VIN = "WP0ZZZY1ZLSA00001"

# Logical addresses for the fake car -- Taycan-shaped (gateway 0x4010, all
# modules in 0x40xx, functional broadcast 0xE400).
GATEWAY = 0x4010
BMS = 0x407B
ABSENT = 0x4FAA  # gateway answers NACK 0x03 -- no such module
ASLEEP = 0x4044  # gateway answers NACK 0x06 -- fitted, not answering
FUNCTIONAL = 0xE400
PRESENT = {GATEWAY, BMS}

ENTITY_ADDRESS = 0x4010
GOOD_TESTER = doip_client.DEFAULT_TESTER_ADDRESS  # 0x0E80
BAD_TESTER = 0x0BAD

# BMS fault memory: 2 records of [3-byte DTC][status].
BMS_DTCS = bytes([0xC1, 0x0A, 0x00, 0x2F, 0xC1, 0x0B, 0x87, 0x08])

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


def handle_uds(target: int, req: bytes, state: dict):
    """The fake car's UDS brain. Returns (pending_first, reply) or None."""
    service = req[0]
    if service == uds.SVC_TESTER_PRESENT:
        return False, bytes([0x7E, 0x00])
    if service == uds.SVC_READ_DATA_BY_ID and req[1:3] == b"\xF1\x90":
        return False, bytes([0x62, 0xF1, 0x90]) + FAKE_VIN.encode()
    if service == uds.SVC_READ_DTC:
        if target == BMS:
            # Answer the first read only after a 0x78 "still thinking".
            return True, bytes([0x59, 0x02, 0xFF]) + BMS_DTCS
        return False, bytes([0x59, 0x02, 0xFF])
    if service == uds.SVC_CLEAR_DTC:
        state.setdefault("cleared", []).append(target)
        return False, bytes([0x54])
    return False, bytes([0x7F, service, 0x11])


def fake_entity(sock: socket.socket, state: dict, stop: threading.Event) -> None:
    """One connection at a time; mirrors how a real gateway behaves."""
    while not stop.is_set():
        try:
            conn, _addr = sock.accept()
        except OSError:
            return
        activated = False
        with conn:
            try:
                while not stop.is_set():
                    payload_type, body = doip_client.read_message(conn, timeout=5.0)

                    if payload_type == doip_client.PT_ROUTING_ACTIVATION_REQ:
                        src, act_type = struct.unpack(">HB", body[:3])
                        state.setdefault("activation_types", []).append(act_type)
                        if src != GOOD_TESTER:
                            code = 0x00  # unknown source address
                        elif state.get("require_central_security") and act_type == (
                            doip_client.ACTIVATION_DEFAULT
                        ):
                            code = 0x06  # unsupported activation type
                        else:
                            code = 0x10
                        activated = code == 0x10
                        conn.sendall(
                            doip_client.pack_message(
                                doip_client.PT_ROUTING_ACTIVATION_RESP,
                                struct.pack(">HHB", src, ENTITY_ADDRESS, code) + b"\x00\x00\x00\x00",
                            )
                        )
                        continue

                    if payload_type in (
                        doip_client.PT_ENTITY_STATUS_REQ,
                        doip_client.PT_POWER_MODE_REQ,
                    ):
                        # A REAL CAR PUNISHES THIS. Entity status and power mode
                        # are UDP messages in ISO 13400-2. A MY2022 Taycan answers
                        # them on TCP with generic NACK 0x01 and then goes deaf on
                        # that socket for good -- every later UDS read times out
                        # and the car looks dead. The fake entity reproduces that
                        # exactly, so a regression is caught here and not on a car.
                        state["udp_only_on_tcp"] = True
                        conn.sendall(
                            doip_client.pack_message(
                                doip_client.PT_GENERIC_NACK, bytes([0x01])
                            )
                        )
                        state["tcp_poisoned"] = True
                        continue

                    if state.get("tcp_poisoned"):
                        continue  # deaf for the rest of this connection, like the car

                    if payload_type == doip_client.PT_ALIVE_CHECK_RESP:
                        state["alive_answered"] = True
                        continue

                    if payload_type != doip_client.PT_DIAG_MESSAGE:
                        continue

                    if not activated:
                        state["diag_before_activation"] = True
                        continue  # a real entity silently drops these

                    src, target = struct.unpack(">HH", body[:4])
                    req = body[4:]

                    if target == FUNCTIONAL:
                        # Broadcast: every fitted module answers on its own address.
                        for addr in sorted(PRESENT):
                            _pending, reply = handle_uds(addr, req, state)
                            conn.sendall(
                                doip_client.pack_message(
                                    doip_client.PT_DIAG_MESSAGE,
                                    struct.pack(">HH", addr, src) + reply,
                                )
                            )
                        continue

                    if target not in PRESENT:
                        # 0x03 "no such module" vs 0x06 "fitted but not answering"
                        # -- the distinction DoIP gives us that HSFZ cannot.
                        nack = 0x06 if target == ASLEEP else 0x03
                        conn.sendall(
                            doip_client.pack_message(
                                doip_client.PT_DIAG_NACK,
                                struct.pack(">HH", target, src) + bytes([nack]),
                            )
                        )
                        continue

                    # Positive ack first, exactly like a real entity.
                    conn.sendall(
                        doip_client.pack_message(
                            doip_client.PT_DIAG_ACK,
                            struct.pack(">HH", target, src) + bytes([0x00]),
                        )
                    )

                    pending_first, reply = handle_uds(target, req, state)

                    if pending_first and not state.get("pending_sent"):
                        state["pending_sent"] = True
                        conn.sendall(
                            doip_client.pack_message(
                                doip_client.PT_DIAG_MESSAGE,
                                struct.pack(">HH", target, src)
                                + bytes([0x7F, req[0], 0x78]),
                            )
                        )
                        # Slip an alive check in while the client waits.
                        conn.sendall(doip_client.pack_message(doip_client.PT_ALIVE_CHECK_REQ))

                    conn.sendall(
                        doip_client.pack_message(
                            doip_client.PT_DIAG_MESSAGE,
                            struct.pack(">HH", target, src) + reply,
                        )
                    )
            except (OSError, doip_client.DoipError):
                continue


def fake_udp_entity(sock: socket.socket, state: dict, stop: threading.Event) -> None:
    """The UDP half of the fake car: entity status and power mode.

    ISO 13400-2 puts these on UDP, which is where the client now asks for them.
    """
    sock.settimeout(0.25)
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            return
        header = doip_client.parse_header(data)
        if header is None:
            continue
        _v, _i, payload_type, _length = header
        if payload_type == doip_client.PT_ENTITY_STATUS_REQ:
            state["udp_entity_status_asked"] = True
            sock.sendto(
                doip_client.pack_message(
                    doip_client.PT_ENTITY_STATUS_RESP,
                    bytes([0x00, 0x04, 0x01]) + (0x0FFF).to_bytes(4, "big"),
                ),
                addr,
            )
        elif payload_type == doip_client.PT_POWER_MODE_REQ:
            state["udp_power_mode_asked"] = True
            sock.sendto(
                doip_client.pack_message(doip_client.PT_POWER_MODE_RESP, bytes([0x01])),
                addr,
            )


def main() -> int:
    print("DoIP client self-test (fake entity on loopback)\n")

    # -- framing ------------------------------------------------------------ #
    msg = doip_client.pack_message(doip_client.PT_DIAG_MESSAGE, b"\x01\x02")
    header = doip_client.parse_header(msg)
    check(header is not None, "generic header parses")
    version, inverse, ptype, length = header
    check(version == 0x02 and inverse == 0xFD, "protocol version + inverse are 02/FD")
    check(ptype == doip_client.PT_DIAG_MESSAGE, "payload type round-trips")
    check(length == 2, "payload length excludes the 8-byte header")
    check(doip_client.parse_header(b"\x02\xFD") is None, "short header returns None, not a crash")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]

    # The UDP half must listen on the SAME port number, because the client
    # derives the UDP port from the TCP one.
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp.bind(("127.0.0.1", port))

    state: dict = {}
    stop = threading.Event()
    thread = threading.Thread(target=fake_entity, args=(server, state, stop), daemon=True)
    thread.start()
    udp_thread = threading.Thread(
        target=fake_udp_entity, args=(udp, state, stop), daemon=True
    )
    udp_thread.start()

    try:
        # -- activation + entity reads -------------------------------------- #
        with doip_client.DoipClient("127.0.0.1", port=port) as client:
            check(client.activation_code == 0x10, "routing activation succeeds (code 0x10)")
            check(client.entity_address == ENTITY_ADDRESS, "entity logical address captured")
            check(
                client.source_address == 0x0E80,
                "tester source address defaults to 0x0E80 (aftermarket range)",
            )
            check(
                state.get("activation_types") == [doip_client.ACTIVATION_DEFAULT],
                "activation type 0x00 (default) is what we send first",
            )
            check(
                not state.get("diag_before_activation"),
                "no diagnostic message is sent before activation",
            )

            status = client.entity_status()
            check(status is not None and status["nodeType"] == "gateway", "entity status: node type")
            check(status["maxConcurrentSockets"] == 4, "entity status: socket capacity")
            check(status["currentlyOpenSockets"] == 1, "entity status: sockets in use")
            check(status["maxDataSize"] == 0x0FFF, "entity status: max data size")

            check(client.power_mode() == "ready", "diagnostic power mode reads 'ready'")

            # -- THE TAYCAN REGRESSION -------------------------------------- #
            # Both reads must have gone out over UDP, and neither may have
            # touched the TCP diagnostic socket. On a real car, asking for
            # either one on TCP kills the session for good.
            check(
                state.get("udp_entity_status_asked") is True,
                "entity status was asked over UDP, not TCP",
            )
            check(
                state.get("udp_power_mode_asked") is True,
                "power mode was asked over UDP, not TCP",
            )
            check(
                not state.get("udp_only_on_tcp"),
                "no UDP-only payload type was ever sent on the TCP socket",
            )

            # -- the shared UDS helpers, unchanged, over DoIP ---------------- #
            # These run AFTER the two entity reads on purpose: this is the exact
            # order porsche_enet/scan.py uses, and the order that used to fail.
            check(uds.tester_present(client, GATEWAY), "TesterPresent answers at the gateway")
            check(uds.answers(client, BMS), "presence ladder finds the BMS")
            check(not uds.answers(client, ABSENT), "an absent address reads as absent, not an error")

            vin = uds.read_vin(client, GATEWAY)
            check(vin == FAKE_VIN, f"VIN reads back ({vin})")

            dtcs = uds.read_dtcs(client, BMS)
            check(state.get("pending_sent") is True, "the ECU's 0x78 'response pending' was exercised")
            # The client answers the alive check inside its read loop, but the
            # fake entity processes that answer on its own thread -- so wait for
            # it rather than racing it.
            deadline = time.monotonic() + 2.0
            while not state.get("alive_answered") and time.monotonic() < deadline:
                time.sleep(0.02)
            check(state.get("alive_answered") is True, "an alive check mid-request was answered")
            check(len(dtcs) == 2, f"BMS returns 2 faults (got {len(dtcs)})")
            check(dtcs[0]["hex"] == "C10A00", "first DTC raw hex")
            check(dtcs[0]["code"].startswith("U"), "DTC letter decodes from the top two bits")
            check("confirmed" in dtcs[0]["flags"], "status bits decode to flags")

            gateway_dtcs = uds.read_dtcs(client, GATEWAY)
            check(gateway_dtcs == [], "a clean module returns an empty list, not an error")

            uds.clear_dtcs(client, BMS)
            check(state.get("cleared") == [BMS], "clear reaches exactly the module asked for")

            # -- functional broadcast: one request, every module answers ------ #
            responders = client.functional_probe(
                FUNCTIONAL, bytes([uds.SVC_TESTER_PRESENT, 0x00]), window=1.0
            )
            found = {r["address"] for r in responders}
            check(found == PRESENT, f"broadcast finds every fitted module ({len(found)})")
            check(
                all(r["payload"][:1] == b"\x7E" for r in responders),
                "each broadcast answer carries that module's own reply",
            )
            quiet = client.functional_probe(0xE4FF, bytes([0x22, 0x00, 0x00]), window=0.4)
            check(quiet == [], "a broadcast nobody answers returns empty, not an error")

            # -- honest failures --------------------------------------------- #
            try:
                client.request(ABSENT, bytes([0x22, 0xF1, 0x90]), timeout=1.0)
                check(False, "unknown target raises rather than returning junk")
            except doip_client.DoipNegativeAck as exc:
                check(exc.not_fitted, f"NACK 0x03 reads as 'not fitted': {exc}")
                check(not exc.unreachable, "0x03 is not confused with 0x06")

            try:
                client.request(ASLEEP, bytes([0x22, 0xF1, 0x90]), timeout=1.0)
                check(False, "an unreachable module raises")
            except doip_client.DoipNegativeAck as exc:
                check(exc.unreachable, f"NACK 0x06 reads as 'fitted, not answering': {exc}")
                check(not exc.not_fitted, "0x06 is not reported as a missing module")

        # -- a car that demands central-security activation ------------------ #
        state["require_central_security"] = True
        state["activation_types"] = []
        with doip_client.DoipClient("127.0.0.1", port=port) as client:
            check(
                state["activation_types"]
                == [doip_client.ACTIVATION_DEFAULT, doip_client.ACTIVATION_CENTRAL_SECURITY],
                "activation type 0x00 refused with 0x06 falls back to 0xE0",
            )
            check(client.activation_code == 0x10, "the fallback activation succeeds")
            check(uds.tester_present(client, GATEWAY), "diagnostics work after the fallback")
        state["require_central_security"] = False

        # -- refused activation --------------------------------------------- #
        try:
            doip_client.DoipClient("127.0.0.1", port=port, source_address=BAD_TESTER)
            check(False, "a refused activation raises")
        except doip_client.DoipRoutingRejected as exc:
            check(exc.code == 0x00, "refusal code preserved")
            check("does not accept this tester address" in str(exc), f"refusal names the reason: {exc}")
        except doip_client.DoipError as exc:
            check(False, f"refusal surfaced as a generic error: {exc}")

        # -- nothing listening ----------------------------------------------- #
        dead = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()
        try:
            doip_client.DoipClient("127.0.0.1", port=dead_port, timeout=1.0)
            check(False, "connecting to nothing raises")
        except doip_client.DoipError as exc:
            check("Could not open the DoIP channel" in str(exc), "closed port gives a cable-shaped error")
    finally:
        stop.set()
        try:
            server.close()
        except OSError:
            pass
        try:
            udp.close()
        except OSError:
            pass

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
