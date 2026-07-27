"""DoIP (ISO 13400-2) TCP diagnostic client -- the Ethernet path into a modern car.

``backend/core/doip.py`` only asks a car to announce itself over UDP. This is the
other half: the TCP channel that actually carries UDS, so a DoIP car can be
walked module by module the way HSFZ walks a BMW F-series.

Same contract as ``hsfz.HsfzClient`` -- ``request(dst, uds, timeout) -> bytes``
-- so every helper in ``backend/core/uds.py`` works over it unchanged. The one
difference: DoIP addresses ECUs by a 16-bit *logical address* (0x1706), not
HSFZ's 8-bit diagnostic address (0x12).

Wire format. Every message is an 8-byte generic header plus a payload:

    byte 0     protocol version          0x02 = ISO 13400-2:2012, 0x03 = :2019
    byte 1     inverse protocol version  (0xFF ^ version)
    bytes 2-3  payload type
    bytes 4-7  payload length            (excludes this header)

Payload types used here:

    0x0005  routing activation request   [SA(2)][type(1)][reserved(4)]
    0x0006  routing activation response  [SA(2)][entity(2)][code(1)][reserved(4)]
    0x0007  alive check request          (entity -> tester, empty)
    0x0008  alive check response         [SA(2)]
    0x8001  diagnostic message           [SA(2)][TA(2)][UDS...]
    0x8002  diagnostic positive ack      [SA(2)][TA(2)][0x00][echo...]
    0x8003  diagnostic negative ack      [SA(2)][TA(2)][NACK code]
    0x0000  generic header negative ack  [code(1)]
    0x4001/0x4002  DoIP entity status request/response
    0x4003/0x4004  diagnostic power mode request/response

**Routing activation is mandatory.** A DoIP entity accepts the TCP connection
but silently discards diagnostic messages until the tester has activated a
route; that is the single most common reason a DoIP scan reads as "connected
but nothing answers".

Read-only by construction: this module sends routing activation, alive-check
responses and whatever UDS bytes the caller hands it. It never composes a write,
a routine or a security-access request of its own.
"""

import socket
import struct
import time
from typing import Optional, Tuple

TCP_DATA_PORT = 13400
UDP_DISCOVERY_PORT = 13400

PROTOCOL_VERSION_2012 = 0x02
PROTOCOL_VERSION_2019 = 0x03

# Payload types
PT_GENERIC_NACK = 0x0000
PT_ROUTING_ACTIVATION_REQ = 0x0005
PT_ROUTING_ACTIVATION_RESP = 0x0006
PT_ALIVE_CHECK_REQ = 0x0007
PT_ALIVE_CHECK_RESP = 0x0008
PT_ENTITY_STATUS_REQ = 0x4001
PT_ENTITY_STATUS_RESP = 0x4002
PT_POWER_MODE_REQ = 0x4003
PT_POWER_MODE_RESP = 0x4004
PT_DIAG_MESSAGE = 0x8001
PT_DIAG_ACK = 0x8002
PT_DIAG_NACK = 0x8003

# Routing activation types (ISO 13400-2 table 47)
ACTIVATION_DEFAULT = 0x00
ACTIVATION_WWH_OBD = 0x01
ACTIVATION_CENTRAL_SECURITY = 0xE0

# Tester source address. ISO gives external test equipment 0x0E00-0x0FFF, split
# so legislated tools use 0x0E00-0x0E7F and aftermarket tools 0x0E80-0x0EFF.
# 0x0E80 is the aftermarket default (Intrepid Vehicle Spy uses it too) and is
# the address a real Taycan was observed accepting.
DEFAULT_TESTER_ADDRESS = 0x0E80

CONNECT_TIMEOUT = 3.0
ACTIVATION_TIMEOUT = 2.0
REQUEST_TIMEOUT = 2.0

# The entity's socket pool is small; see _open().
CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY = 2.0

_ACTIVATION_CODES = {
    0x00: "unknown source address -- the car does not accept this tester address",
    0x01: "all concurrent diagnostic sockets are already in use",
    0x02: "source address differs from the one already activated on this socket",
    0x03: "this source address is already registered on another socket",
    0x04: "missing authentication",
    0x05: "rejected confirmation",
    0x06: "unsupported routing activation type",
    0x07: "encrypted link required",
    0x10: "success",
    0x11: "success, pending confirmation",
}
_ACTIVATION_SUCCESS = (0x10, 0x11)

_DIAG_NACK_CODES = {
    0x02: "invalid source address",
    0x03: "unknown target address -- no ECU at that logical address",
    0x04: "diagnostic message too large",
    0x05: "out of memory",
    0x06: "target unreachable -- the ECU is not answering on its own bus",
    0x07: "unknown network",
    0x08: "transport protocol error",
}

_GENERIC_NACK_CODES = {
    0x00: "incorrect pattern format",
    0x01: "unknown payload type",
    0x02: "message too large",
    0x03: "out of memory",
    0x04: "invalid payload length",
}

_POWER_MODES = {
    0x00: "not ready",
    0x01: "ready",
    0x02: "not supported",
}


class DoipError(Exception):
    """Transport-level DoIP failure (socket, activation, or an entity NACK)."""


class DoipRoutingRejected(DoipError):
    """The car refused to activate a diagnostic route."""

    def __init__(self, code: int):
        self.code = code
        meaning = _ACTIVATION_CODES.get(code, "unknown response code")
        super().__init__(f"Routing activation refused: 0x{code:02X} ({meaning})")


class DoipNegativeAck(DoipError):
    """The gateway refused to route a diagnostic message to an ECU.

    Worth its own type because DoIP draws a distinction HSFZ cannot: NACK 0x03
    means "there is no such module on this car" (honest "not fitted"), while
    0x06 means "the module is fitted but did not answer" (asleep, or a bus
    fault). A scan should report those differently.
    """

    def __init__(self, code: int, dst: int):
        self.code = code
        self.dst = dst
        meaning = _DIAG_NACK_CODES.get(code, "unknown code")
        super().__init__(f"ECU 0x{dst:04X}: DoIP NACK 0x{code:02X} ({meaning})")

    @property
    def not_fitted(self) -> bool:
        """0x03 -- the gateway says no module lives at this address."""
        return self.code == 0x03

    @property
    def unreachable(self) -> bool:
        """0x06 -- fitted, but not answering right now."""
        return self.code == 0x06


def pack_message(payload_type: int, payload: bytes = b"", version: int = PROTOCOL_VERSION_2012) -> bytes:
    """One DoIP message: 8-byte generic header + payload."""
    return struct.pack(">BBHI", version, 0xFF ^ version, payload_type, len(payload)) + payload


def parse_header(data: bytes) -> Optional[tuple]:
    """(version, inverse, payload_type, payload_length) or None if too short."""
    if len(data) < 8:
        return None
    return struct.unpack(">BBHI", data[:8])


def _recv_exact(sock: socket.socket, n: int, deadline: float) -> bytes:
    """Read exactly n bytes or raise socket.timeout/OSError."""
    chunks = b""
    while len(chunks) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("DoIP read timed out")
        sock.settimeout(remaining)
        part = sock.recv(n - len(chunks))
        if not part:
            raise OSError("DoIP connection closed by the car")
        chunks += part
    return chunks


def read_message(sock: socket.socket, timeout: float) -> Tuple[int, bytes]:
    """Read one full DoIP message; returns (payload_type, payload)."""
    deadline = time.monotonic() + timeout
    header = _recv_exact(sock, 8, deadline)
    _version, _inverse, payload_type, length = struct.unpack(">BBHI", header)
    if length > 0x100000:  # 1MB sanity cap -- nothing diagnostic is this big
        raise DoipError(f"Implausible DoIP payload length {length}")
    payload = _recv_exact(sock, length, deadline) if length else b""
    return payload_type, payload


class DoipClient:
    """One activated TCP diagnostic session with a car's DoIP entity.

    Handles the transport chores -- routing activation on connect, positive
    acks consumed, alive checks answered, entity NACKs decoded -- and returns
    raw UDS payloads. A 0x7F..0x78 "response pending" extends the wait rather
    than being returned, matching HsfzClient.

    Usage mirrors HsfzClient exactly::

        with DoipClient("192.168.0.10") as client:
            vin = uds.read_vin(client, 0x1706)
            dtcs = uds.read_dtcs(client, 0x1706)
    """

    def __init__(
        self,
        ip: str,
        port: int = TCP_DATA_PORT,
        source_address: int = DEFAULT_TESTER_ADDRESS,
        activation_type: int = ACTIVATION_DEFAULT,
        version: int = PROTOCOL_VERSION_2012,
        timeout: float = CONNECT_TIMEOUT,
        activate: bool = True,
    ):
        self.ip = ip
        self.source_address = source_address
        self.activation_type = activation_type
        self.version = version
        self.entity_address: Optional[int] = None
        self.activation_code: Optional[int] = None
        self._port = port
        # Entity status and power mode are UDP messages (see the note further
        # down). On the standard port these are the same number; kept separate
        # so a non-standard TCP port in a test rig does not drag UDP with it.
        self._udp_port = UDP_DISCOVERY_PORT if port == TCP_DATA_PORT else port
        self._connect_timeout = timeout
        self._open()
        if activate:
            try:
                self.activate_routing()
            except DoipRoutingRejected as exc:
                # 0x06 = "unsupported activation type". A car whose announcement
                # asked for central security wants type 0xE0 instead; a denied
                # activation can leave the socket unusable, so reconnect first.
                if exc.code != 0x06 or activation_type != ACTIVATION_DEFAULT:
                    self.close()
                    raise
                self.close()
                self.activation_type = ACTIVATION_CENTRAL_SECURITY
                self._open()
                try:
                    self.activate_routing()
                except Exception:
                    self.close()
                    raise
            except Exception:
                self.close()
                raise

    def _open(self) -> None:
        # A DoIP entity holds only a handful of concurrent diagnostic sockets --
        # a Taycan's gateway was measured refusing connections for a few seconds
        # after several sessions in quick succession, while still answering UDP
        # discovery perfectly. That reads as "cable unplugged" when the car is
        # fine, so a couple of retries buy the entity time to reap old sockets.
        last: Optional[OSError] = None
        attempts = 0
        for attempt in range(CONNECT_ATTEMPTS):
            attempts = attempt + 1
            try:
                self._sock = socket.create_connection(
                    (self.ip, self._port), timeout=self._connect_timeout
                )
                last = None
                break
            except OSError as exc:
                last = exc
                # Only a timeout means "busy, try again". A refused connection is
                # a definite answer (nothing listening) -- retrying just stalls.
                if not isinstance(exc, socket.timeout):
                    break
                if attempt < CONNECT_ATTEMPTS - 1:
                    time.sleep(CONNECT_RETRY_DELAY)

        if last is not None:
            detail = (
                f"Could not open the DoIP channel to {self.ip}:{self._port}: {last}"
            )
            if attempts > 1:
                detail += (
                    f" (tried {attempts} times). Discovery reached the car, so the "
                    "cable and ignition are fine -- the gateway is most likely still "
                    "holding sockets from a previous session. Wait ~10s and retry."
                )
            raise DoipError(detail)
        # Nagle would sit on our small diagnostic frames waiting for company.
        try:
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    # -- lifecycle ---------------------------------------------------------- #
    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _send(self, payload_type: int, payload: bytes = b"") -> None:
        try:
            self._sock.sendall(pack_message(payload_type, payload, self.version))
        except OSError as exc:
            raise DoipError(f"Send failed: {exc}")

    # -- routing activation ------------------------------------------------- #
    def activate_routing(self, timeout: float = ACTIVATION_TIMEOUT) -> int:
        """Activate a diagnostic route. Returns the response code on success.

        Without this the entity accepts TCP but ignores every diagnostic
        message, which looks exactly like a dead car.
        """
        payload = struct.pack(">HB", self.source_address, self.activation_type) + b"\x00\x00\x00\x00"
        self._send(PT_ROUTING_ACTIVATION_REQ, payload)

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DoipError(
                    f"No routing-activation answer within {timeout:.1f}s "
                    f"(tester address 0x{self.source_address:04X}, "
                    f"activation type 0x{self.activation_type:02X})"
                )
            try:
                payload_type, body = read_message(self._sock, remaining)
            except socket.timeout:
                raise DoipError(
                    f"No routing-activation answer within {timeout:.1f}s "
                    f"(tester address 0x{self.source_address:04X})"
                )
            except OSError as exc:
                raise DoipError(str(exc))

            if payload_type == PT_ALIVE_CHECK_REQ:
                self._answer_alive_check()
                continue
            if payload_type == PT_GENERIC_NACK:
                raise DoipError(self._generic_nack_text(body))
            if payload_type != PT_ROUTING_ACTIVATION_RESP:
                continue
            if len(body) < 5:
                raise DoipError("Malformed routing activation response")

            tester_addr, entity_addr, code = struct.unpack(">HHB", body[:5])
            self.entity_address = entity_addr
            self.activation_code = code
            if code not in _ACTIVATION_SUCCESS:
                raise DoipRoutingRejected(code)
            if tester_addr != self.source_address:
                # Informational: some entities echo their own view of us.
                pass
            return code

    def _answer_alive_check(self) -> None:
        self._send(PT_ALIVE_CHECK_RESP, struct.pack(">H", self.source_address))

    @staticmethod
    def _generic_nack_text(body: bytes) -> str:
        code = body[0] if body else 0xFF
        meaning = _GENERIC_NACK_CODES.get(code, "unknown code")
        return f"DoIP entity rejected the message: 0x{code:02X} ({meaning})"

    # -- diagnostics -------------------------------------------------------- #
    def request(self, dst: int, uds: bytes, timeout: float = REQUEST_TIMEOUT) -> bytes:
        """Send one UDS request to logical address ``dst``; return the UDS reply.

        Same signature as HsfzClient.request, so backend/core/uds.py drives it
        unchanged. ``dst`` is a 16-bit DoIP logical address.
        """
        self._send(PT_DIAG_MESSAGE, struct.pack(">HH", self.source_address, dst) + uds)

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DoipError(f"No reply from ECU 0x{dst:04X} within {timeout:.1f}s")
            try:
                payload_type, body = read_message(self._sock, remaining)
            except socket.timeout:
                raise DoipError(f"No reply from ECU 0x{dst:04X} within {timeout:.1f}s")
            except OSError as exc:
                raise DoipError(str(exc))

            if payload_type == PT_ALIVE_CHECK_REQ:
                self._answer_alive_check()
                continue
            if payload_type == PT_DIAG_ACK:
                continue  # the entity took our message; the ECU answer follows
            if payload_type == PT_DIAG_NACK:
                raise DoipNegativeAck(body[4] if len(body) >= 5 else 0xFF, dst)
            if payload_type == PT_GENERIC_NACK:
                raise DoipError(self._generic_nack_text(body))
            if payload_type != PT_DIAG_MESSAGE or len(body) < 4:
                continue

            src, target = struct.unpack(">HH", body[:4])
            payload = body[4:]
            if target != self.source_address:
                continue  # not addressed to us
            if src != dst:
                continue  # an answer from a different ECU -- keep waiting
            # UDS "response pending": the ECU is thinking -- keep waiting.
            if len(payload) >= 3 and payload[0] == 0x7F and payload[2] == 0x78:
                deadline = time.monotonic() + max(timeout, 5.0)
                continue
            return payload

    def functional_probe(
        self, functional_address: int, uds: bytes, window: float = 2.0
    ) -> list:
        """Broadcast one UDS request and collect every ECU that answers.

        A functional (broadcast) address reaches all fitted modules in a single
        request, so ``3E 00`` to it is the fastest honest "which modules are on
        this car" probe there is -- far better than sweeping addresses and
        inferring absence from silence.

        Unlike ``request()`` this never raises on a quiet bus: an empty list
        means nothing answered, which is a result, not an error. Returns
        ``[{"address": int, "payload": bytes}, ...]`` in arrival order,
        de-duplicated by address.
        """
        self._send(
            PT_DIAG_MESSAGE, struct.pack(">HH", self.source_address, functional_address) + uds
        )

        responders: list = []
        seen: set = set()
        deadline = time.monotonic() + window
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return responders
            try:
                payload_type, body = read_message(self._sock, remaining)
            except (socket.timeout, OSError, DoipError):
                return responders

            if payload_type == PT_ALIVE_CHECK_REQ:
                self._answer_alive_check()
                continue
            if payload_type in (PT_DIAG_ACK, PT_DIAG_NACK, PT_GENERIC_NACK):
                continue  # per-ECU routing chatter; absence is the signal here
            if payload_type != PT_DIAG_MESSAGE or len(body) < 4:
                continue

            src, target = struct.unpack(">HH", body[:4])
            if target != self.source_address or src in seen:
                continue
            payload = body[4:]
            # A module still thinking will answer again on its own address.
            if len(payload) >= 3 and payload[0] == 0x7F and payload[2] == 0x78:
                continue
            seen.add(src)
            responders.append({"address": src, "payload": payload})

    # -- entity-level reads (no UDS involved, both read-only) --------------- #
    #
    # THESE TWO GO OVER UDP, NOT THE TCP DIAGNOSTIC SOCKET, AND THAT IS LOAD-
    # BEARING. ISO 13400-2 defines entity status (0x4001) and power mode
    # (0x4003) as UDP messages; the TCP channel carries only routing activation,
    # alive checks and diagnostic messages.
    #
    # Sending them on the TCP socket is not a harmless protocol nicety. Measured
    # on a real MY2022 Taycan (2026-07-27): the gateway answers 0x4001/0x4003 on
    # TCP with a generic header NACK 0x01 "unknown payload type" and then IGNORES
    # EVERY SUBSEQUENT MESSAGE ON THAT SOCKET. The connection stays open, routing
    # stays activated, and every UDS read times out -- which reads as a dead car
    # when the car is in fact fine. A scan that called power_mode() first could
    # never see a single module.
    #
    # The UDP socket is bound to the local address the TCP connection is already
    # using, so on a laptop with Wi-Fi up as well the request cannot leave via
    # the wrong interface.
    def _udp_request(
        self, request_type: int, response_type: int, timeout: float
    ) -> Optional[bytes]:
        """One UDP request/response against this car's DoIP entity.

        Returns the payload, or None if nothing valid arrived in time. Never
        raises: these are optional status reads, and an entity that stays quiet
        is a result, not a transport failure.
        """
        try:
            local_ip = self._sock.getsockname()[0]
        except OSError:
            local_ip = ""

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if local_ip:
                sock.bind((local_ip, 0))
            sock.sendto(
                pack_message(request_type, b"", self.version), (self.ip, self._udp_port)
            )
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                sock.settimeout(remaining)
                try:
                    data, _addr = sock.recvfrom(4096)
                except socket.timeout:
                    return None
                header = parse_header(data)
                if header is None:
                    continue
                _version, _inverse, payload_type, length = header
                if payload_type == response_type:
                    return data[8 : 8 + length]
        except OSError:
            return None
        finally:
            sock.close()

    def entity_status(self, timeout: float = 1.5) -> Optional[dict]:
        """DoIP entity status (0x4001): node type, socket capacity, in-use count.

        Useful honesty signal -- "all sockets busy" explains a refused
        activation without blaming the cable. Sent over UDP; see the note above.
        """
        payload = self._udp_request(PT_ENTITY_STATUS_REQ, PT_ENTITY_STATUS_RESP, timeout)
        if payload is None or len(payload) < 3:
            return None
        node_type, max_sockets, open_sockets = payload[0], payload[1], payload[2]
        result = {
            "nodeType": "gateway" if node_type == 0x00 else "node",
            "maxConcurrentSockets": max_sockets,
            "currentlyOpenSockets": open_sockets,
            "maxDataSize": None,
        }
        if len(payload) >= 7:
            result["maxDataSize"] = int.from_bytes(payload[3:7], "big")
        return result

    def power_mode(self, timeout: float = 1.5) -> Optional[str]:
        """Diagnostic power mode (0x4003): is the car electrically ready to be
        diagnosed? A "not ready" here is the polite version of "ignition off".

        Sent over UDP; see the note above. Not every entity implements it -- a
        real Taycan does not answer at all -- so None means "the car did not
        say", never "the car is not ready". Callers must not treat None as a
        reason to stop.
        """
        payload = self._udp_request(PT_POWER_MODE_REQ, PT_POWER_MODE_RESP, timeout)
        if payload is None or not payload:
            return None
        return _POWER_MODES.get(payload[0], f"unknown (0x{payload[0]:02X})")

    def _await(self, wanted_type: int, timeout: float) -> Optional[bytes]:
        """Read messages until ``wanted_type`` arrives or the window closes."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                payload_type, body = read_message(self._sock, remaining)
            except (socket.timeout, OSError, DoipError):
                return None
            if payload_type == PT_ALIVE_CHECK_REQ:
                self._answer_alive_check()
                continue
            if payload_type == wanted_type:
                return body
