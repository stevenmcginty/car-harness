"""Diagnostic trouble code (DTC) decoding and description lookup.

A DTC travels as 2 bytes. The top 2 bits pick the system letter (P/C/B/U),
the rest are the 4 digits. Descriptions come from dtc_db.json (generic SAE
codes); unknown codes fall back to an honest category description so the user
always gets *something* truthful.
"""

import json
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parent / "dtc_db.json"
_LETTERS = "PCBU"

try:
    _DB = json.loads(_DB_PATH.read_text(encoding="utf-8"))
except Exception:
    _DB = {}

# Honest fallback categories by code prefix.
_CATEGORIES = [
    ("P00", "Fuel/air metering and auxiliary emission controls"),
    ("P01", "Fuel and air metering"),
    ("P02", "Fuel and air metering (injector circuit)"),
    ("P03", "Ignition system or misfire"),
    ("P04", "Auxiliary emissions controls"),
    ("P05", "Vehicle speed control and idle control"),
    ("P06", "Computer output circuit"),
    ("P07", "Transmission"),
    ("P08", "Transmission"),
    ("P09", "Transmission"),
    ("P1", "Manufacturer-specific powertrain code"),
    ("P2", "Powertrain (generic)"),
    ("P3", "Powertrain (ignition/misfire, manufacturer-specific)"),
    ("C", "Chassis (ABS, steering, suspension)"),
    ("B", "Body (airbags, locks, lighting, comfort)"),
    ("U", "Network / module communication"),
]


def decode_pair(b1: int, b2: int) -> str:
    """Two raw bytes -> 'P0301' style code."""
    letter = _LETTERS[(b1 >> 6) & 0x03]
    return f"{letter}{(b1 >> 4) & 0x03}{b1 & 0x0F:X}{(b2 >> 4) & 0x0F:X}{b2 & 0x0F:X}"


def describe(code: str) -> str:
    """Best honest description for a code."""
    desc = _DB.get(code)
    if desc:
        return desc
    for prefix, category in _CATEGORIES:
        if code.startswith(prefix):
            return f"{category} (no generic description for this exact code)"
    return "Unknown code"


def parse_dtc_payload(payload: bytes, is_can: bool) -> list:
    """Payload starts with the response service byte (0x43/0x47/0x4A). On CAN
    a count byte follows; on legacy protocols the code pairs start immediately
    and 00 00 pairs are padding."""
    body = payload[1:]
    if is_can and body:
        body = body[1:]  # skip the count byte
    codes = []
    for i in range(0, len(body) - 1, 2):
        b1, b2 = body[i], body[i + 1]
        if b1 == 0 and b2 == 0:
            continue
        codes.append(decode_pair(b1, b2))
    return codes


def read_codes(session, service: int) -> list:
    """Read one DTC service (0x03 stored / 0x07 pending / 0x0A permanent).
    Returns a list of code strings; 'no data' honestly means an empty list.
    Raises for link-level failures so they are never mistaken for 'no faults'."""
    from backend.obd.elm327 import ObdError

    try:
        payloads = session.query(service, None, timeout=8.0)
    except ObdError as exc:
        if exc.kind == "no_data":
            return []
        raise
    seen = []
    for payload in payloads:  # one per ECU
        for code in parse_dtc_payload(payload, session.is_can):
            if code not in seen:
                seen.append(code)
    return seen
