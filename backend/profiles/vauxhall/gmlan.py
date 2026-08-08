"""GMW3110 -- GM's "GMLAN Enhanced Diagnostic Test Mode" dialect.

Everything in the harness so far has been UDS (ISO 14229) or a KWP relative
close enough to fake it. GM Global A is neither, and getting this wrong is the
single biggest way a Vauxhall scan can come back empty on a perfectly healthy
car. The services that differ from UDS are exactly the ones a scan needs
(docs/private/vauxhall-astra-k-research.md §3):

    read fault codes   UDS 0x19  ->  GMLAN 0xA9 ReadDiagnosticInformation
    clear fault codes  UDS 0x14  ->  GMLAN 0x04 ClearDiagnosticInformation
    identification     UDS 0x22 F1xx (2-byte DID) -> GMLAN 0x1A xx (1-byte id)
    routine / actuator UDS 0x31  ->  GMLAN 0xAE DeviceControl (CPID based)

Live data is the one place they agree: ReadDataByParameterIdentifier is 0x22 in
both, which is why pids.py needs none of this.

This module is the GMLAN half only. scan.py owns the ladder -- it tries the UDS
dialect first (some late Global A nodes do answer it, and it costs one request)
and falls back here. Nothing assumes which one a given module speaks; the scan
reports what actually answered.

Service ids and 0xA9 sub-functions verified against Scapy's GMLAN contrib
implementation (scapy.contrib.automotive.gm.gmlan), which is a reading of
GMW3110-2010.

Read-only. DeviceControl (0xAE) is deliberately NOT implemented -- see
resets.py for why writing to a GM powertrain module needs GDS2, not a dongle.
"""

from typing import List

from backend.core import uds
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError

# --------------------------------------------------------------------------- #
# GMW3110 services (only the read-side ones this profile uses).
# --------------------------------------------------------------------------- #
SVC_CLEAR_DIAG_INFO = 0x04       # ClearDiagnosticInformation (no arguments)
SVC_INITIATE_DIAG = 0x10         # InitiateDiagnosticOperation
SVC_READ_DATA_BY_ID = 0x1A       # ReadDataByIdentifier, ONE-byte identifier
SVC_TESTER_PRESENT = 0x3E        # same id as UDS
SVC_READ_DIAG_INFO = 0xA9        # ReadDiagnosticInformation

# 0xA9 sub-functions.
RDI_BY_DTC_NUMBER = 0x80
RDI_BY_STATUS_MASK = 0x81
RDI_ON_CHANGE_COUNT = 0x82

# GMW3110 negative-response codes that UDS either doesn't define or names
# differently. Merged over the UDS table so one lookup covers both dialects.
GMLAN_NRC_NAMES = {
    0x22: "conditions not correct or request sequence error",
    0x81: "scheduler full",
    0x83: "voltage out of range",
    0x85: "general programming failure",
    0x89: "device type error",
    0x99: "ready for download, DTC stored",
    0xE3: "device control limits exceeded",
}

# 0x1A identifiers we are confident enough of to put a name on. GMW3110 defines
# roughly 95; the rest are read as raw hex rather than mislabelled.
IDENT_IDS: List[tuple] = [
    (0x90, "VIN"),
    (0x92, "System supplier ECU hardware"),
    (0xC0, "Boot software part number"),
]

# 0xA9 0x81 status masks, laddered widest-first. There is no single published
# "GM mask"; 0xFF asks for everything, 0x08 for confirmed only. A module that
# dislikes a mask answers NRC 0x31 (out of range) or 0x12 (sub-function not
# supported), which means "try the next one", not "this module is broken".
DTC_MASKS = (0xFF, 0x08)
MASK_RETRY_NRCS = (0x12, 0x31)


def nrc_name(nrc: int) -> str:
    return GMLAN_NRC_NAMES.get(nrc) or uds.NRC_NAMES.get(nrc, "unknown code")


# --------------------------------------------------------------------------- #
# Fault codes
# --------------------------------------------------------------------------- #
def format_dtc(b0: int, b1: int, failure_type: int) -> dict:
    """A GM fault as a GM tester displays it: code + failure type, "P0716-71".

    The three-byte-to-J2012 conversion is identical to UDS (top two bits pick
    P/C/B/U), so core.uds.decode_dtc does the structural work. What differs is
    presentation: GM service information indexes a fault by code AND failure
    type together -- P0716-71 and P0716-00 are different entries -- so the
    display string carries both. Unlike the VAG family, GM powertrain codes do
    use the generic SAE meanings, so the code itself is not suppressed.
    """
    entry = uds.decode_dtc(b0, b1, failure_type)
    entry["display"] = f"{entry['code']}-{failure_type:02X}"
    return entry


def _records_from(payload: bytes, offset: int) -> List[tuple]:
    """Split a DTC payload into 4-byte [hi][lo][failureType][status] groups.

    See research §3: this grouping is INFERRED from the GMLAN DTC structure,
    not confirmed against a real Astra K capture, which is why callers keep the
    raw payload hex in the module trace and label the results accordingly.
    """
    records = []
    body = payload[offset:]
    for i in range(0, len(body) - 3, 4):
        b0, b1, ftype, status = body[i : i + 4]
        if b0 == b1 == ftype == 0:
            continue
        records.append((b0, b1, ftype, status))
    return records


def read_dtcs(client, req_id: int, trace: List[str]) -> List[dict]:
    """0xA9 0x81 <mask> -- fault memory in the GMLAN dialect.

    Raises UdsNegativeResponse (the shared exception type; the NRC numbering is
    compatible) if the module refuses every mask, and ObdError/TransportError
    if it goes silent, so the caller can tell "refused" from "not there".
    """
    last_error = None
    payload = None
    for mask in DTC_MASKS:
        reply = client.request(
            req_id, bytes([SVC_READ_DIAG_INFO, RDI_BY_STATUS_MASK, mask]), timeout=3.0
        )
        if reply and reply[0] == SVC_READ_DIAG_INFO + 0x40:
            trace.append(f"A9 81 {mask:02X} -> OK ({reply.hex().upper()})")
            payload = reply
            break
        if reply and reply[0] == 0x7F and len(reply) >= 3:
            last_error = uds.UdsNegativeResponse(SVC_READ_DIAG_INFO, reply[2])
            if reply[2] in MASK_RETRY_NRCS:
                continue  # this module dislikes the mask, not the service
            trace.append(f"A9 81 {mask:02X} -> NRC {reply[2]:02X} ({nrc_name(reply[2])})")
            raise last_error
    if payload is None:
        if last_error is not None:
            trace.append(f"A9 81 -> NRC {last_error.nrc:02X} ({nrc_name(last_error.nrc)})")
            raise last_error
        raise ObdError("GMLAN fault read returned nothing usable.", kind="no_data")

    # Reply: 0xE9 0x81 then the DTC records. Some modules echo the mask back
    # before the records, some do not, so both offsets are tried and the one
    # producing a plausible set (every record's status byte non-zero) wins.
    # The raw hex stays in the trace either way -- a real capture settles it.
    for offset in (3, 2):
        records = _records_from(payload, offset)
        if records and all(status for _, _, _, status in records):
            break
    else:
        records = _records_from(payload, 3)

    dtcs = []
    for b0, b1, ftype, status in records:
        entry = format_dtc(b0, b1, ftype)
        entry["status"] = f"{status:02X}"
        entry["flags"] = uds.status_flags(status)
        entry["state"] = uds.fault_state(status)
        entry["dialect"] = "gmlan"
        entry["recordLayout"] = "inferred"
        dtcs.append(entry)
    return dtcs


def clear_dtcs(client, req_id: int, timeout: float = 5.0) -> None:
    """0x04 ClearDiagnosticInformation -- CHANGES THE CAR. Callers confirm-gate.

    Unlike UDS 0x14 this service takes no group argument: GMLAN clears the
    module's whole fault memory or nothing.
    """
    reply = client.request(req_id, bytes([SVC_CLEAR_DIAG_INFO]), timeout=timeout)
    if not reply:
        raise ObdError("No reply to the GMLAN clear request.", kind="no_data")
    if reply[0] == 0x7F:
        raise uds.UdsNegativeResponse(SVC_CLEAR_DIAG_INFO, reply[2] if len(reply) >= 3 else 0x10)
    if reply[0] != SVC_CLEAR_DIAG_INFO + 0x40:
        raise uds.UdsNegativeResponse(SVC_CLEAR_DIAG_INFO, 0x10)


# --------------------------------------------------------------------------- #
# Identification
# --------------------------------------------------------------------------- #
def _printable(body: bytes) -> str:
    """Text if it reads as text, otherwise hex. Same rule as the VAG scan."""
    text = "".join(chr(b) for b in body if 32 <= b < 127).strip()
    if text and len(text) >= (len(body) + 1) // 2:
        return text
    return body.hex().upper()


def read_ident(client, req_id: int, trace: List[str]) -> List[dict]:
    """0x1A best-effort identification. Only the three identifiers whose meaning
    is certain are asked for -- guessing at the rest of GMW3110's ~95-entry
    table would put confident wrong labels next to real bytes."""
    out: List[dict] = []
    for ident, label in IDENT_IDS:
        try:
            reply = client.request(req_id, bytes([SVC_READ_DATA_BY_ID, ident]), timeout=2.0)
        except (ObdError, TransportError):
            continue
        if len(reply) < 3 or reply[0] != SVC_READ_DATA_BY_ID + 0x40 or reply[1] != ident:
            if ident == 0x90 and reply and reply[0] == 0x7F and len(reply) >= 3:
                trace.append(f"1A 90 -> NRC {reply[2]:02X} ({nrc_name(reply[2])})")
            continue
        value = _printable(reply[2:])
        if value:
            out.append({"label": label, "value": value, "dialect": "gmlan"})
    return out
