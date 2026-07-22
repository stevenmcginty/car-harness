"""HSFZ (High-Speed Fahrzeugzugang) -- BMW F-series diagnostics over ENET.

F-series cars (like a 2015 F26 X4) do NOT speak DoIP; they speak BMW's older
HSFZ protocol over the same ENET cable:

  * UDP port 6811 -- identification broadcast (car announces its IP + VIN)
  * TCP port 6801 -- diagnostic channel carrying UDS (ISO 14229) payloads

Every HSFZ frame is a 6-byte header followed by a body:

    bytes 0-3  body length (big-endian, excludes this header)
    bytes 4-5  control word

Control words used here:
    0x0001  diagnostic message      body = [src addr][dst addr][UDS bytes]
    0x0002  acknowledge             car echoes our frame; consumed silently
    0x0011  identification          empty request; reply body is an ASCII blob
    0x0012  alive check (car->us)   must be answered with 0x0013 + tester addr
    0x0013  alive check response

The tester (us) is address 0xF4; the central gateway (ZGW) is 0x10 and routes
frames to per-ECU diagnostic addresses (0x63 = head unit, 0x12 = engine, ...).
"""

import re
import socket
import struct
import time
from typing import List, Optional, Tuple

UDP_IDENT_PORT = 6811
TCP_DIAG_PORT = 6801

CW_DIAG = 0x0001
CW_ACK = 0x0002
CW_IDENT = 0x0011
CW_ALIVE = 0x0012
CW_ALIVE_RESP = 0x0013

TESTER_ADDR = 0xF4
GATEWAY_ADDR = 0x10

IDENT_TIMEOUT = 2.0
CONNECT_TIMEOUT = 3.0
REQUEST_TIMEOUT = 2.0

# 17 chars, no I/O/Q -- the standard VIN alphabet.
_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")


def pack_frame(control_word: int, body: bytes = b"") -> bytes:
    return struct.pack(">IH", len(body), control_word) + body


def pack_diag(dst: int, uds: bytes, src: int = TESTER_ADDR) -> bytes:
    return pack_frame(CW_DIAG, bytes([src, dst]) + uds)


def _recv_exact(sock: socket.socket, n: int, deadline: float) -> bytes:
    """Read exactly n bytes from a TCP stream or raise socket.timeout/OSError."""
    chunks = b""
    while len(chunks) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("HSFZ read timed out")
        sock.settimeout(remaining)
        part = sock.recv(n - len(chunks))
        if not part:
            raise OSError("HSFZ connection closed by the car")
        chunks += part
    return chunks


def read_frame(sock: socket.socket, timeout: float) -> Tuple[int, bytes]:
    """Read one full HSFZ frame; returns (control_word, body)."""
    deadline = time.monotonic() + timeout
    header = _recv_exact(sock, 6, deadline)
    length, control_word = struct.unpack(">IH", header)
    if length > 0x100000:  # 1MB sanity cap -- nothing diagnostic is this big
        raise OSError(f"Implausible HSFZ frame length {length}")
    body = _recv_exact(sock, length, deadline) if length else b""
    return control_word, body


def extract_vin(blob: bytes) -> Optional[str]:
    """Pull a VIN out of an identification body without trusting its layout.

    Real gateways tag it "BMWVIN<17 chars>"; use that first so marker letters
    can't bleed into the match, then fall back to isolated 17-char tokens.
    """
    idx = blob.find(b"BMWVIN")
    if idx != -1:
        cand = blob[idx + 6 : idx + 6 + 17].decode("ascii", errors="replace")
        if _VIN_RE.fullmatch(cand):
            return cand
    printable = "".join(chr(b) if 32 <= b < 127 else " " for b in blob)
    for token in printable.split():
        match = _VIN_RE.fullmatch(token)
        if match:
            return match.group(0)
    match = _VIN_RE.search(printable)
    return match.group(0) if match else None


def identify(
    candidate: Optional[dict] = None,
    timeout: float = IDENT_TIMEOUT,
    targets: Optional[List[tuple]] = None,
) -> dict:
    """Broadcast an HSFZ identification request and collect the car's answer.

    Read-only: the request is an empty frame; the reply tells us the car's IP
    (the datagram's source address) and usually contains the VIN. Result keys
    mirror doip.probe(): honest about how far it got. ``targets`` overrides the
    default broadcast destinations (used by the self-test and for retrying a
    known car IP directly).
    """
    result = {
        "reached": False,
        "sent": False,
        "protocol": "HSFZ",
        "ip": None,
        "vin": None,
        "raw": None,
        "bind_ip": None,
        "targets": [],
        "detail": "",
        "error": None,
    }
    request = pack_frame(CW_IDENT)
    bind_ip = candidate["ip"] if candidate else "0.0.0.0"
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind((bind_ip, 0))
            result["bind_ip"] = bind_ip
        except OSError as exc:
            try:
                sock.bind(("0.0.0.0", 0))
                result["bind_ip"] = "0.0.0.0"
                result["detail"] += f"Could not bind to {bind_ip} ({exc}); bound to 0.0.0.0. "
            except OSError as exc2:
                result["error"] = str(exc2)
                return result

        if targets is None:
            targets = [("255.255.255.255", UDP_IDENT_PORT)]
            if candidate and candidate.get("broadcast") and candidate["broadcast"] != "255.255.255.255":
                targets.append((candidate["broadcast"], UDP_IDENT_PORT))
        result["targets"] = [f"{host}:{port}" for host, port in targets]

        for host, port in targets:
            try:
                sock.sendto(request, (host, port))
                result["sent"] = True
            except OSError as exc:
                result["detail"] += f"Send to {host}:{port} failed: {exc}. "

        if not result["sent"]:
            result["error"] = result["error"] or "no identification datagram could be sent"
            return result

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            except OSError as exc:
                result["error"] = str(exc)
                break
            if data == request:
                continue  # our own broadcast echoed back
            if len(data) < 6:
                continue
            length, _control_word = struct.unpack(">IH", data[:6])
            body = data[6 : 6 + length] if length else data[6:]
            result["reached"] = True
            result["ip"] = addr[0]
            result["vin"] = extract_vin(body)
            result["raw"] = body.hex()
            result["detail"] += f"HSFZ identification answered from {addr[0]}. "
            break

        if not result["reached"]:
            result["detail"] += f"No HSFZ identification reply within {timeout:.1f}s."
    except OSError as exc:
        result["error"] = str(exc)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    return result


class HsfzError(Exception):
    pass


class HsfzClient:
    """One TCP diagnostic session with the car's gateway (ZGW).

    Handles the transport chores -- ACK frames are consumed, alive checks are
    answered -- and returns raw UDS payloads. A 0x7F..0x78 (response pending)
    negative response extends the wait instead of being returned.
    """

    def __init__(self, ip: str, port: int = TCP_DIAG_PORT, timeout: float = CONNECT_TIMEOUT):
        self.ip = ip
        self._sock = socket.create_connection((ip, port), timeout=timeout)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def request(self, dst: int, uds: bytes, timeout: float = REQUEST_TIMEOUT) -> bytes:
        """Send one UDS request to ECU address ``dst``; return the UDS reply."""
        try:
            self._sock.sendall(pack_diag(dst, uds))
        except OSError as exc:
            raise HsfzError(f"Send failed: {exc}")

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HsfzError(
                    f"No reply from ECU 0x{dst:02X} within {timeout:.1f}s"
                )
            try:
                control_word, body = read_frame(self._sock, remaining)
            except socket.timeout:
                raise HsfzError(
                    f"No reply from ECU 0x{dst:02X} within {timeout:.1f}s"
                )
            except OSError as exc:
                raise HsfzError(str(exc))

            if control_word == CW_ACK:
                continue
            if control_word == CW_ALIVE:
                try:
                    self._sock.sendall(
                        pack_frame(CW_ALIVE_RESP, bytes([0x00, TESTER_ADDR]))
                    )
                except OSError as exc:
                    raise HsfzError(f"Alive-check response failed: {exc}")
                continue
            if control_word != CW_DIAG or len(body) < 2:
                continue

            src, to = body[0], body[1]
            payload = body[2:]
            if to != TESTER_ADDR:
                continue  # not addressed to us
            if src != dst and dst != GATEWAY_ADDR:
                # Replies must come from the ECU we asked (the gateway routes).
                continue
            # UDS "response pending": the ECU is thinking -- keep waiting.
            if len(payload) >= 3 and payload[0] == 0x7F and payload[2] == 0x78:
                deadline = time.monotonic() + max(timeout, 5.0)
                continue
            return payload
