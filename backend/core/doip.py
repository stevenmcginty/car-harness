"""Real, read-only DoIP Vehicle Identification probe (ISO 13400).

This performs ONLY a Vehicle Identification exchange over UDP:

  * NO routing activation
  * NO UDS diagnostic session
  * NO TCP diagnostic connection

It therefore cannot change anything on the car -- it just asks any DoIP entity
on the wire to announce itself, and reads back the announcement.

DoIP generic header (8 bytes, big-endian):
    byte 0     protocol version           (0x02 = ISO 13400-2:2012)
    byte 1     inverse protocol version   (0xFD = ~0x02)
    bytes 2-3  payload type               (0x0001 = Vehicle Identification Request)
    bytes 4-7  payload length             (0 for the request)

A car answers with payload type 0x0004 (Vehicle Announcement / Vehicle
Identification Response), whose payload is:
    [0:17]   VIN            (17 ASCII bytes)
    [17:19]  logical address (2 bytes)
    [19:25]  EID            (6 bytes)
    [25:31]  GID            (6 bytes)
    [31]     further-action-required byte
    [32]     VIN/GID sync status byte (optional)
"""

import socket
import struct
import time
from typing import List, Optional

DOIP_PORT = 13400
DISCOVERY_TIMEOUT = 2.0  # seconds to wait for announcements

PROTOCOL_VERSION = 0x02
INVERSE_PROTOCOL_VERSION = 0xFD

PT_VEHICLE_ID_REQUEST = 0x0001
PT_VEHICLE_ANNOUNCEMENT = 0x0004  # a.k.a. Vehicle Identification Response

# Payload types used by the TCP diagnostic client below (ISO 13400-2 §5.4-5.8).
PT_ROUTING_ACTIVATION_REQ = 0x0005
PT_ROUTING_ACTIVATION_RESP = 0x0006
PT_ALIVE_CHECK_REQ = 0x0007
PT_ALIVE_CHECK_RESP = 0x0008
PT_DIAG_MESSAGE = 0x8001
PT_DIAG_MESSAGE_ACK = 0x8002       # positive ack (car received our request)
PT_DIAG_MESSAGE_NACK = 0x8003      # negative ack (car could not route/accept it)

# Routing-activation response code 0x10 == "routing successfully activated".
RA_SUCCESS = 0x10

# External test-equipment logical address. ISO reserves 0x0E00-0x0FFF for a
# tester; VAG's own tooling sits in this block. This is OUR address, announced
# to the gateway in the routing-activation request -- it is not car-specific.
DEFAULT_TESTER_ADDRESS = 0x0E00

ACCEPTED_VERSIONS = (0x02, 0x03)  # ISO 13400-2:2012 and :2019
_ANNOUNCEMENT_MIN_PAYLOAD = 32  # VIN(17)+LA(2)+EID(6)+GID(6)+further-action(1)


def build_vehicle_identification_request() -> bytes:
    """The 8-byte generic header with an empty payload: 02 FD 00 01 00 00 00 00."""
    return struct.pack(
        ">BBHI",
        PROTOCOL_VERSION,
        INVERSE_PROTOCOL_VERSION,
        PT_VEHICLE_ID_REQUEST,
        0,
    )


def parse_header(data: bytes):
    """Return (protocol, inverse, payload_type, payload_length) or None if too short."""
    if len(data) < 8:
        return None
    return struct.unpack(">BBHI", data[:8])


def parse_announcement(payload: bytes) -> Optional[dict]:
    """Parse a 0x0004 payload into a vehicle dict, or None if it is too short."""
    if len(payload) < _ANNOUNCEMENT_MIN_PAYLOAD:
        return None
    raw_vin = payload[0:17].decode("ascii", errors="replace")
    vin = "".join(ch for ch in raw_vin if ch.isprintable()).strip()
    logical_address = "0x{:04X}".format(int.from_bytes(payload[17:19], "big"))
    eid = payload[19:25].hex()
    gid = payload[25:31].hex()
    further_action = payload[31]
    vin_gid_sync = payload[32] if len(payload) >= 33 else None
    return {
        "vin": vin,
        "logical_address": logical_address,
        "eid": eid,
        "gid": gid,
        "further_action": further_action,
        "vin_gid_sync": vin_gid_sync,
    }


def probe(candidate: Optional[dict] = None, timeout: float = DISCOVERY_TIMEOUT) -> dict:
    """Broadcast a Vehicle Identification Request and read back any announcement.

    ``candidate`` is the dict from interfaces.best_candidate() (or None). When
    present we bind to its IPv4 and also target its subnet broadcast; otherwise
    we bind to 0.0.0.0. The result is honest about exactly how far it got:

        reached_doip  -- True only if a valid 0x0004 announcement came back
        sent          -- True if at least one datagram left the socket
        vin/logical_address/eid/gid -- populated on success
        raw_from_ip   -- source IP of the announcement
        detail        -- human-readable trace of what happened
        error         -- OS error string (e.g. firewall/permission), if any
    """
    result = {
        "reached_doip": False,
        "sent": False,
        "vin": None,
        "logical_address": None,
        "eid": None,
        "gid": None,
        "further_action": None,
        "vin_gid_sync": None,
        "raw_from_ip": None,
        "bind_ip": None,
        "targets": [],
        "detail": "",
        "error": None,
    }

    request = build_vehicle_identification_request()
    bind_ip = candidate["ip"] if candidate else "0.0.0.0"
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        # Bind to the candidate adapter's IPv4 so the broadcast leaves the right
        # NIC; fall back to 0.0.0.0 if that address is not (yet) bindable.
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
                result["detail"] += f"Could not bind any socket: {exc2}. "
                return result

        targets: List[tuple] = [("255.255.255.255", DOIP_PORT)]
        if candidate and candidate.get("broadcast") and candidate["broadcast"] != "255.255.255.255":
            targets.append((candidate["broadcast"], DOIP_PORT))
        result["targets"] = [f"{host}:{port}" for host, port in targets]

        for host, port in targets:
            try:
                sock.sendto(request, (host, port))
                result["sent"] = True
            except OSError as exc:
                result["detail"] += f"Send to {host}:{port} failed: {exc}. "

        if not result["sent"] and result["error"] is None:
            result["error"] = "no datagram could be sent"

        # Read datagrams until the timeout window closes, keeping the first
        # valid Vehicle Announcement.
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
                result["detail"] += f"Receive error: {exc}. "
                break

            header = parse_header(data)
            if header is None:
                continue
            proto, inv, payload_type, payload_length = header
            if payload_type != PT_VEHICLE_ANNOUNCEMENT:
                continue

            payload = data[8 : 8 + payload_length] if payload_length else data[8:]
            announcement = parse_announcement(payload)
            if announcement is None:
                continue

            if proto not in ACCEPTED_VERSIONS:
                result["detail"] += f"Announcement protocol version 0x{proto:02X} (accepted anyway). "
            if inv != (0xFF ^ proto):
                result["detail"] += f"Inverse-version mismatch (0x{inv:02X}); noted, not rejected. "

            result.update(
                {
                    "reached_doip": True,
                    "vin": announcement["vin"],
                    "logical_address": announcement["logical_address"],
                    "eid": announcement["eid"],
                    "gid": announcement["gid"],
                    "further_action": announcement["further_action"],
                    "vin_gid_sync": announcement["vin_gid_sync"],
                    "raw_from_ip": addr[0],
                }
            )
            result["detail"] += f"Vehicle announcement received from {addr[0]}. "
            break

        if not result["reached_doip"] and result["error"] is None:
            if result["sent"]:
                result["detail"] += f"No DoIP announcement within {timeout:.1f}s."
            else:
                result["detail"] += "No request could be sent."
    except OSError as exc:
        result["error"] = str(exc)
        result["detail"] += f"Socket error: {exc}. "
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    return result


# ---------------------------------------------------------------------------
# Read-only DoIP TCP diagnostic client (ISO 13400-2 §5.5-5.8)
#
# probe()/identify() above only ask the car to announce itself over UDP. To
# actually READ a data identifier (UDS 0x22) we need the TCP diagnostic channel:
# open port 13400, activate routing (hand the gateway our tester address), then
# send DiagnosticMessage frames whose user data is the UDS request. The gateway
# routes each frame to the target ECU by its 16-bit logical address and returns
# the reply the same way. This is the DoIP twin of core/hsfz.py's HsfzClient;
# both return raw UDS payloads so the profile layer stays transport-agnostic.
#
# It is deliberately confined to routing activation + UDS reads. Nothing here
# opens a non-default session, runs a routine, writes, resets, clears, or
# touches security access -- a battery-health read must not be able to change
# the car.
# ---------------------------------------------------------------------------

DIAG_CONNECT_TIMEOUT = 3.0
DIAG_REQUEST_TIMEOUT = 3.0


def build_generic(payload_type: int, payload: bytes = b"") -> bytes:
    """An 8-byte DoIP generic header (version + inverse + type + length) + payload."""
    return struct.pack(
        ">BBHI", PROTOCOL_VERSION, INVERSE_PROTOCOL_VERSION, payload_type, len(payload)
    ) + payload


class DoipError(Exception):
    pass


class DoipDiagClient:
    """One read-only DoIP diagnostic session with a car's gateway.

    Usage::

        with DoipDiagClient(ip) as client:
            client.activate()                 # routing activation (once)
            reply = client.request(0x1234, bytes([0x22, 0x1E, 0x3B]))

    ``request`` returns the raw UDS reply bytes (e.g. ``62 1E 3B ..``) or raises
    DoipError on a transport failure / timeout. UDS "response pending"
    (7F xx 78) extends the wait rather than being returned, exactly like the
    HSFZ client. The caller decides what a 0x7F negative response means.
    """

    def __init__(
        self,
        ip: str,
        port: int = DOIP_PORT,
        tester_address: int = DEFAULT_TESTER_ADDRESS,
        timeout: float = DIAG_CONNECT_TIMEOUT,
    ):
        self.ip = ip
        self.tester_address = tester_address
        self.entity_address: Optional[int] = None
        self._activated = False
        try:
            self._sock = socket.create_connection((ip, port), timeout=timeout)
        except OSError as exc:
            raise DoipError(f"Could not open a DoIP channel to {ip}:{port}: {exc}")

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _read_frame(self, timeout: float):
        """Read one DoIP frame -> (payload_type, payload); raise on timeout/EOF."""
        deadline = time.monotonic() + timeout
        header = self._recv_exact(8, deadline)
        _proto, _inv, payload_type, length = struct.unpack(">BBHI", header)
        if length > 0x100000:  # 1 MB sanity cap -- no diagnostic frame is this big
            raise DoipError(f"Implausible DoIP frame length {length}")
        payload = self._recv_exact(length, deadline) if length else b""
        return payload_type, payload

    def _recv_exact(self, n: int, deadline: float) -> bytes:
        chunks = b""
        while len(chunks) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DoipError("DoIP read timed out")
            self._sock.settimeout(remaining)
            try:
                part = self._sock.recv(n - len(chunks))
            except socket.timeout:
                raise DoipError("DoIP read timed out")
            except OSError as exc:
                raise DoipError(str(exc))
            if not part:
                raise DoipError("DoIP connection closed by the car")
            chunks += part
        return chunks

    def activate(self, activation_type: int = 0x00, timeout: float = DIAG_REQUEST_TIMEOUT) -> int:
        """Send a routing-activation request and wait for a success response.

        Returns the DoIP entity (gateway) logical address on success; raises
        DoipError if the car refuses activation (a non-0x10 response code) or
        does not answer. Idempotent -- a second call is a no-op.
        """
        if self._activated:
            return self.entity_address

        payload = (
            struct.pack(">H", self.tester_address)
            + bytes([activation_type])
            + b"\x00\x00\x00\x00"  # ISO-reserved, must be zero
        )
        try:
            self._sock.sendall(build_generic(PT_ROUTING_ACTIVATION_REQ, payload))
        except OSError as exc:
            raise DoipError(f"Routing-activation send failed: {exc}")

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DoipError("No routing-activation response from the gateway")
            payload_type, body = self._read_frame(remaining)
            if payload_type == PT_ALIVE_CHECK_REQ:
                self._answer_alive_check()
                continue
            if payload_type != PT_ROUTING_ACTIVATION_RESP or len(body) < 5:
                continue
            self.entity_address = int.from_bytes(body[2:4], "big")
            code = body[4]
            if code != RA_SUCCESS:
                raise DoipError(
                    f"Gateway refused routing activation (response code 0x{code:02X})"
                )
            self._activated = True
            return self.entity_address

    def _answer_alive_check(self) -> None:
        """Reply to the gateway's alive check so it keeps the channel open."""
        body = struct.pack(">H", self.tester_address)
        try:
            self._sock.sendall(build_generic(PT_ALIVE_CHECK_RESP, body))
        except OSError as exc:
            raise DoipError(f"Alive-check response failed: {exc}")

    def request(self, target_address: int, uds: bytes, timeout: float = DIAG_REQUEST_TIMEOUT) -> bytes:
        """Send one UDS request to ``target_address``; return the raw UDS reply.

        The frame is a DiagnosticMessage (0x8001) carrying [source][target][uds].
        Positive/negative transport acks (0x8002/0x8003) and alive checks are
        handled here; only the ECU's UDS payload is returned to the caller.
        """
        if not self._activated:
            self.activate()

        frame_payload = (
            struct.pack(">HH", self.tester_address, target_address) + uds
        )
        try:
            self._sock.sendall(build_generic(PT_DIAG_MESSAGE, frame_payload))
        except OSError as exc:
            raise DoipError(f"Diagnostic send to 0x{target_address:04X} failed: {exc}")

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DoipError(
                    f"No DoIP reply from 0x{target_address:04X} within {timeout:.1f}s"
                )
            payload_type, body = self._read_frame(remaining)

            if payload_type == PT_ALIVE_CHECK_REQ:
                self._answer_alive_check()
                continue
            if payload_type == PT_DIAG_MESSAGE_ACK:
                continue  # transport-level "we received it" -- keep waiting for data
            if payload_type == PT_DIAG_MESSAGE_NACK:
                code = body[4] if len(body) >= 5 else None
                raise DoipError(
                    f"Gateway negative-acked the request to 0x{target_address:04X}"
                    + (f" (code 0x{code:02X})" if code is not None else "")
                )
            if payload_type != PT_DIAG_MESSAGE or len(body) < 4:
                continue

            source = int.from_bytes(body[0:2], "big")
            dest = int.from_bytes(body[2:4], "big")
            uds_reply = body[4:]
            if dest != self.tester_address:
                continue  # not addressed to us
            if source != target_address:
                continue  # a different ECU's traffic on the shared channel
            # UDS "response pending": the ECU is still working -- extend the wait.
            if len(uds_reply) >= 3 and uds_reply[0] == 0x7F and uds_reply[2] == 0x78:
                deadline = time.monotonic() + max(timeout, 5.0)
                continue
            return uds_reply
