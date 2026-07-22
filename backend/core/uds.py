"""Minimal UDS (ISO 14229) helpers for the BMW ENET path.

Only the read-mostly services the harness needs:

    0x3E TesterPresent          -- "are you there?" presence probe
    0x22 ReadDataByIdentifier   -- VIN (DID 0xF190)
    0x19 ReadDTCInformation     -- fault codes by status mask
    0x14 ClearDiagnosticInformation

Anything write-heavy (sessions, security access, flashing) is deliberately
absent -- flashing stays E-Sys's job.
"""

from typing import List, Optional

SVC_TESTER_PRESENT = 0x3E
SVC_READ_DATA_BY_ID = 0x22
SVC_READ_DTC = 0x19
SVC_CLEAR_DTC = 0x14

DID_VIN = 0xF190

NRC_NAMES = {
    0x10: "general reject",
    0x11: "service not supported",
    0x12: "sub-function not supported",
    0x13: "incorrect message length",
    0x14: "response too long",
    0x21: "busy, repeat request",
    0x22: "conditions not correct (ignition on?)",
    0x31: "request out of range",
    0x33: "security access denied",
    0x72: "general programming failure",
    0x78: "response pending",
    0x7F: "service not supported in active session",
}

STATUS_BITS = [
    (0x01, "test failed"),
    (0x02, "failed this cycle"),
    (0x04, "pending"),
    (0x08, "confirmed"),
    (0x10, "not completed since clear"),
    (0x20, "failed since clear"),
    (0x40, "not completed this cycle"),
    (0x80, "warning light requested"),
]


class UdsNegativeResponse(Exception):
    def __init__(self, service: int, nrc: int):
        self.service = service
        self.nrc = nrc
        name = NRC_NAMES.get(nrc, "unknown code")
        super().__init__(f"Negative response to 0x{service:02X}: 0x{nrc:02X} ({name})")


def _check(payload: bytes, service: int) -> bytes:
    """Validate a UDS reply: raise on negative, return payload on positive."""
    if not payload:
        raise UdsNegativeResponse(service, 0x10)
    if payload[0] == 0x7F:
        nrc = payload[2] if len(payload) >= 3 else 0x10
        raise UdsNegativeResponse(service, nrc)
    if payload[0] != service + 0x40:
        raise UdsNegativeResponse(service, 0x10)
    return payload


def tester_present(client, dst: int, timeout: float = 0.8) -> bool:
    """True if anything UDS-shaped answers at this address (present ECU)."""
    try:
        payload = client.request(dst, bytes([SVC_TESTER_PRESENT, 0x00]), timeout=timeout)
    except Exception:
        return False
    # A negative response still proves an ECU lives at the address.
    return bool(payload)


def read_vin(client, dst: int = 0x10, timeout: float = 2.0) -> Optional[str]:
    payload = client.request(
        dst, bytes([SVC_READ_DATA_BY_ID, DID_VIN >> 8, DID_VIN & 0xFF]), timeout=timeout
    )
    payload = _check(payload, SVC_READ_DATA_BY_ID)
    raw = payload[3:]
    vin = "".join(chr(b) for b in raw if 32 <= b < 127).strip()
    return vin or None


def decode_dtc(b0: int, b1: int, b2: int) -> dict:
    """3-byte UDS DTC -> J2012-style code plus the raw hex BMW tools show."""
    letter = "PCBU"[(b0 >> 6) & 0x03]
    code = f"{letter}{(b0 >> 4) & 0x03:d}{b0 & 0x0F:X}{(b1 >> 4) & 0x0F:X}{b1 & 0x0F:X}"
    return {"code": code, "hex": f"{b0:02X}{b1:02X}{b2:02X}", "failureType": f"{b2:02X}"}


def status_flags(status: int) -> List[str]:
    return [name for bit, name in STATUS_BITS if status & bit]


def read_dtcs(client, dst: int, timeout: float = 3.0) -> List[dict]:
    """reportDTCByStatusMask; tries the wide mask first, then a narrow one."""
    last_error = None
    for mask in (0xAF, 0x0C):
        try:
            payload = client.request(
                dst, bytes([SVC_READ_DTC, 0x02, mask]), timeout=timeout
            )
            payload = _check(payload, SVC_READ_DTC)
            break
        except UdsNegativeResponse as exc:
            last_error = exc
            if exc.nrc in (0x12, 0x31):
                continue  # mask not supported -- try the narrower one
            raise
    else:
        raise last_error

    # Reply: 0x59 0x02 <availabilityMask> then 4-byte records [dtc x3][status].
    records = payload[3:]
    dtcs = []
    for i in range(0, len(records) - 3, 4):
        b0, b1, b2, status = records[i : i + 4]
        if b0 == b1 == b2 == 0:
            continue
        entry = decode_dtc(b0, b1, b2)
        entry["status"] = f"{status:02X}"
        entry["flags"] = status_flags(status)
        dtcs.append(entry)
    return dtcs


def clear_dtcs(client, dst: int, timeout: float = 5.0) -> None:
    """ClearDiagnosticInformation for ALL groups (0xFFFFFF) on one ECU."""
    payload = client.request(
        dst, bytes([SVC_CLEAR_DTC, 0xFF, 0xFF, 0xFF]), timeout=timeout
    )
    _check(payload, SVC_CLEAR_DTC)
