"""Ford-specific mode-22 (ReadDataByIdentifier) live values over a plain ELM327
on HS-CAN -- the "enhanced" data generic OBD-II doesn't carry.

Unlike the Mazda list (community Torque PIDs), every entry here comes from
Ford's OWN dealer tables: IDS 133.01 MDX_PCM_D / MDX_PCM for program code P375
(Ranger T6), decrypted 2026-09-01 and built into data/mdx_dids.json by
tools/dealer_index/build_ids_ford.py. Scaling (RESOLUTION / OFFSET / UNITS)
is taken from that file at import time, so a re-run of the builder changes the
decode here without editing code.

Every reading keeps its RAW hex beside the decoded value. The truck this was
built for is DPF-deleted and remapped, so the soot DIDs are delete EVIDENCE
(frozen = delete confirmed), not health data -- see the notes in mdx_dids.json.
Read-only (service 0x22 only). A DID this PCM doesn't support never answers
and is skipped.
"""

import json
import os
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

PCM = 0x7E0
TCM = 0x7E1

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "mdx_dids.json")


def load_table() -> dict:
    """The MDX DID table (hex string -> entry). Empty if the data file is missing."""
    try:
        with open(_DATA, encoding="utf-8") as fh:
            return json.load(fh).get("dids", {})
    except (OSError, ValueError):
        return {}


TABLE = load_table()

# (req_id, did, our-name, family) -- the order the live pass reads them.
# Names come from the MDX table when it has the DID; these are the short
# labels the chips show. The family groups the health-check worksheet.
LIVE_DIDS: List[Tuple[int, int, str, str]] = [
    (PCM, 0x050D, "Rail pressure control deviation", "fuel_pressure"),
    (PCM, 0xF405, "Coolant temperature (PCM)", "baseline"),
    (PCM, 0xF40D, "Vehicle speed (PCM)", "baseline"),
    (PCM, 0x057B, "Soot load", "delete_check"),
    (PCM, 0x0380, "DPF test result", "delete_check"),
    (PCM, 0x0508, "Soot in oil estimate", "delete_check"),
    (PCM, 0x0509, "Sludge in oil estimate", "oil"),
    (PCM, 0x0519, "Distance since last oil change", "oil"),
    (PCM, 0xFD00, "Pilot correction status", "injection"),
    (PCM, 0x051A, "Pump learning status", "injection"),
    (PCM, 0xDE01, "Injector quantity adjustment (IMA)", "adjustment"),
    (PCM, 0xDE03, "EGR valve adjustment", "adjustment"),
    (PCM, 0xDE04, "Throttle valve actuator adjustment", "adjustment"),
    (PCM, 0xDE0A, "Fan control season counter", "adjustment"),
    (PCM, 0xDE0F, "Rail pressure learn cycle counters", "adjustment"),
]

# Identity DIDs (ISO 14229 F1xx). Ford part numbers read "AB39-14C097-AA".
IDENT_DIDS: List[Tuple[int, str]] = [
    (0xF190, "VIN"),
    (0xF188, "Software part number"),
    (0xF111, "Core assembly part number"),
    (0xF113, "Delivery assembly part number"),
    (0xF124, "Calibration part number"),
    (0xF18C, "ECU serial"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int(body: bytes, signed: bool) -> int:
    return int.from_bytes(body, "big", signed=signed)


def decode(did: int, body: bytes) -> Tuple[str, str]:
    """(value, unit) for the payload after ``62 <did>`` using the MDX scaling.

    Falls back to the raw hex (unit "hex") for packeted / enumerated / unknown
    DIDs, which is what the test plan wants anyway: keep raw, decode later.
    """
    key = f"{did:04X}"
    entry = TABLE.get(key)
    raw = body.hex().upper()
    if not entry or not body:
        return raw, "hex"
    subs = entry.get("subfields") or []
    if entry.get("did_type") != "single_value" or len(subs) != 1:
        return raw, "hex"
    sub = subs[0]
    dtype = sub.get("data_type")
    if dtype == "ascii":
        text = "".join(chr(b) for b in body if 32 <= b < 127).strip()
        return (text or raw), ("" if text else "hex")
    if dtype not in ("unsigned", "signed") or sub.get("resolution") is None:
        return raw, "hex"
    value = _int(body, signed=(dtype == "signed"))
    scaled = value * float(sub["resolution"]) + float(sub.get("offset") or 0)
    unit = sub.get("units") or ""
    text = f"{scaled:.3f}".rstrip("0").rstrip(".") if isinstance(scaled, float) else str(scaled)
    return text, unit


def read_did(client: Elm327UdsClient, req_id: int, did: int, timeout: float = 2.0) -> Optional[bytes]:
    """One raw RDBI read -> bytes after ``62 <did>``, or None if unanswered."""
    try:
        payload = client.request(req_id, bytes([0x22, did >> 8, did & 0xFF]), timeout=timeout)
    except (ObdError, TransportError):
        return None
    if len(payload) < 4 or payload[0] != 0x62:
        return None
    return payload[3:]


def read_live(client: Elm327UdsClient) -> List[dict]:
    """One pass over LIVE_DIDS; every answer decoded AND kept raw."""
    readings = []
    for req_id, did, name, family in LIVE_DIDS:
        body = read_did(client, req_id, did)
        if body is None:
            continue
        value, unit = decode(did, body)
        readings.append({
            "name": name, "value": value, "unit": unit,
            "did": f"{did:04X}", "raw": body.hex().upper(), "family": family,
        })
    return readings


def read_ident(client: Elm327UdsClient, req_id: int) -> List[dict]:
    out = []
    for did, label in IDENT_DIDS:
        body = read_did(client, req_id, did)
        if body is None:
            continue
        text = "".join(chr(b) for b in body if 32 <= b < 127).strip()
        value = text if text and len(text) >= (len(body) + 1) // 2 else body.hex().upper()
        if value:
            out.append({"label": label, "value": value, "did": f"{did:04X}"})
    return out


def live(adapter: Optional[dict], mode: str = "real") -> dict:
    """One pass over the Ford IDS-table DIDs on the PCM. Read-only."""
    if mode == "sim":
        return {
            "ok": True, "simulated": True,
            "readings": [
                {"name": "Rail pressure control deviation", "value": "-1.25", "unit": "MPa",
                 "did": "050D", "raw": "FF60", "family": "fuel_pressure"},
                {"name": "Soot load", "value": "0000", "unit": "hex",
                 "did": "057B", "raw": "0000", "family": "delete_check"},
            ],
            "message": "SIMULATED -- no real car connected.",
            "timestamp": _now(),
        }

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "readings": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k HS-CAN the Ford PCM values use."
                ),
                "timestamp": _now(),
            }
        client = Elm327UdsClient(session)
        client.begin()
        try:
            readings = read_live(client)
        finally:
            client.restore()
        return {
            "ok": True, "simulated": False, "readings": readings,
            "message": (
                f"{len(readings)} Ford IDS-table value(s) answered on the PCM; raw hex kept "
                "beside every decode. DIDs this PCM does not carry are skipped."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {
            "ok": False, "simulated": False, "readings": [],
            "message": str(exc), "timestamp": _now(),
        }
