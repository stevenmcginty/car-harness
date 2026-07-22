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
