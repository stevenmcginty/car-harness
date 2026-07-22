"""Self-test for the HSFZ + UDS stack against a fake ZGW on loopback.

Run:  python -m backend.tools.hsfz_selftest

Spins up a pretend F-series gateway (UDP identification + TCP diagnostics on
random loopback ports), then drives the REAL client code -- hsfz.identify,
HsfzClient, uds.* -- through the same call sequence scan.py uses on a car:
identify, presence-probe, VIN, DTC read (with an ACK, an alive check and a
0x78 response-pending thrown in), and clear. Exits 0 on PASS, 1 on FAIL.
"""

import socket
import struct
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core import hsfz, uds  # noqa: E402

FAKE_VIN = "WBAXX910X00R00001"
HU, DME, ZGW = 0x63, 0x12, 0x10
PRESENT = {ZGW, DME, HU}

# HU fault memory: 2 records of [3-byte DTC][status].
HU_DTCS = bytes([0xB7, 0xF2, 0x03, 0x2F, 0xB7, 0xF8, 0x21, 0x08])


def udp_ident_server(sock: socket.socket, stop: threading.Event):
    ident_body = (b"DIAGADR10BMWVIN" + FAKE_VIN.encode() + b"MAC0050C2AABBCC").ljust(64, b"\x00")
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(1024)
        except OSError:
            return
        if len(data) >= 6 and struct.unpack(">IH", data[:6])[1] == hsfz.CW_IDENT:
            sock.sendto(hsfz.pack_frame(hsfz.CW_IDENT, ident_body), addr)


def handle_uds(dst: int, req: bytes, state: dict) -> bytes | None:
    """The fake car's UDS brain. Returns a reply payload or None (no ECU)."""
    if dst not in PRESENT:
        return None
    service = req[0]
    if service == uds.SVC_TESTER_PRESENT:
        return bytes([0x7E, 0x00])
    if service == uds.SVC_READ_DATA_BY_ID and req[1:3] == b"\xF1\x90":
        return bytes([0x62, 0xF1, 0x90]) + FAKE_VIN.encode()
    if service == uds.SVC_READ_DTC:
        if dst == HU:
            # First ask: pretend to think (0x78 pending) -- client must wait.
            if not state.get("hu_pending_sent"):
                state["hu_pending_sent"] = True
                state["hu_send_after_pending"] = True
            return bytes([0x59, 0x02, 0xFF]) + HU_DTCS
        return bytes([0x59, 0x02, 0xFF])  # present, no faults
    if service == uds.SVC_CLEAR_DTC:
        state["cleared"] = state.get("cleared", []) + [dst]
        return bytes([0x54])
    return bytes([0x7F, service, 0x11])


def tcp_diag_server(listener: socket.socket, stop: threading.Event):
    conn, _ = listener.accept()
    conn.settimeout(0.5)
    state: dict = {}
    sent_alive_check = False
    buffer = b""
    while not stop.is_set():
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buffer += chunk
        while len(buffer) >= 6:
            length, cw = struct.unpack(">IH", buffer[:6])
            if len(buffer) < 6 + length:
                break
            body, buffer = buffer[6 : 6 + length], buffer[6 + length :]
            if cw == hsfz.CW_ALIVE_RESP:
                state["alive_answered"] = True
                continue
            if cw != hsfz.CW_DIAG or len(body) < 2:
                continue
            src, dst, req = body[0], body[1], body[2:]
            # Real gateways ACK every request.
            conn.sendall(hsfz.pack_frame(hsfz.CW_ACK, body[:2] + req[:1]))
            # Once, mid-session: fire an alive check the client must answer.
            if not sent_alive_check:
                sent_alive_check = True
                conn.sendall(hsfz.pack_frame(hsfz.CW_ALIVE))
            reply = handle_uds(dst, req, state)
            if reply is None:
                continue  # absent ECU: silence, client times out
            if state.pop("hu_send_after_pending", False):
                conn.sendall(
                    hsfz.pack_diag(src, bytes([0x7F, req[0], 0x78]), src=dst)
                )
            conn.sendall(hsfz.pack_diag(src, reply, src=dst))
    conn.close()


def main() -> int:
    stop = threading.Event()
    failures = []

    def check(name, cond, extra=""):
        print(("  PASS  " if cond else "  FAIL  ") + name + (f"  ({extra})" if extra else ""))
        if not cond:
            failures.append(name)

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp_port = udp.getsockname()[1]
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.bind(("127.0.0.1", 0))
    tcp.listen(1)
    tcp_port = tcp.getsockname()[1]

    threading.Thread(target=udp_ident_server, args=(udp, stop), daemon=True).start()
    threading.Thread(target=tcp_diag_server, args=(tcp, stop), daemon=True).start()

    print(f"Fake ZGW up: udp={udp_port} tcp={tcp_port}")

    ident = hsfz.identify(timeout=2.0, targets=[("127.0.0.1", udp_port)])
    check("identify reaches the car", ident["reached"], ident["detail"].strip())
    check("identify finds the VIN", ident["vin"] == FAKE_VIN, str(ident["vin"]))

    with hsfz.HsfzClient("127.0.0.1", port=tcp_port) as client:
        check("ZGW present", uds.tester_present(client, ZGW))
        check("DME present", uds.tester_present(client, DME))
        check("absent ECU (0x78) not present", not uds.tester_present(client, 0x78))
        vin = uds.read_vin(client, ZGW)
        check("VIN over UDS", vin == FAKE_VIN, str(vin))
        dme_dtcs = uds.read_dtcs(client, DME)
        check("DME has no faults", dme_dtcs == [])
        hu_dtcs = uds.read_dtcs(client, HU)  # served after ACK+alive+pending
        check("HU returns 2 faults (thru ack/alive/pending)", len(hu_dtcs) == 2, str(hu_dtcs))
        if len(hu_dtcs) == 2:
            check("DTC hex decode", hu_dtcs[0]["hex"] == "B7F203", hu_dtcs[0]["hex"])
            check("DTC J2012 decode", hu_dtcs[0]["code"] == "B37F2", hu_dtcs[0]["code"])
            check(
                "DTC status flags",
                "confirmed" in hu_dtcs[0]["flags"] and "test failed" in hu_dtcs[0]["flags"],
                ",".join(hu_dtcs[0]["flags"]),
            )
        uds.clear_dtcs(client, HU)
        check("clear accepted", True)

    stop.set()
    udp.close()
    tcp.close()

    print()
    if failures:
        print(f"SELF-TEST FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("SELF-TEST PASSED: HSFZ framing, ACK/alive/pending handling, UDS VIN/DTC/clear all good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
