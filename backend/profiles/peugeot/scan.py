"""Peugeot / PSA module scan over a generic OBD-II dongle (diagnostic CAN, UDS).

Generic OBD-II (backend/obd/service.py) only sees the engine's emissions data.
This walks the manufacturer-level modules on a PSA car's diagnostic CAN bus and
reads each responding module's UDS fault memory + identification -- the same idea
as the BMW ENET scan and the Mazda dongle scan, but using PSA's own per-module
CAN id pairs instead of the standard 0x7E0+8 block.

WHY PSA IS DIFFERENT: on a normal OBD-II car a request to 0x7E0 is answered on
0x7E8 (request+8). PSA does not follow that rule -- every module has its own
request/response pair (BSI 0x752 -> 0x652, engine 0x6A8 -> 0x688, airbag
0x744 -> 0x644, ...). The transport that knows those pairs is
backend/obd/psa_uds_elm327.py; the UDS service/parse layer is the shared
backend/core/uds.py (same code the BMW and Mazda paths use).

SCOPE: 11-bit ISO 15765-4 CAN, i.e. AEE2010-era cars (roughly 2010+). Pre-2004
PSA cars use the VAN bus (no ELM327 can reach it) and 2004-2010 cars often speak
KWP2000 rather than UDS -- both are out of scope, the same way the Mazda path is
HS-CAN only. This module is read-only apart from clear(), which the API layer
guards behind an explicit confirm.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.core import uds
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.psa_uds_elm327 import PsaElm327UdsClient

# Curated PSA diagnostic address table for the modules a dealer actually cares
# about on a pre-sale check. Each row is (request_id, response_id, code, name,
# description). These are 11-bit CAN ids taken from the community PSA diagnostic
# databases (ludwig-v/arduino-psa-diag, prototux/PSA-RE). "(if fitted)" modules
# simply time out and are marked not present on cars without them -- harmless.
PEUGEOT_ECU_TABLE = [
    (0x752, 0x652, "BSI", "Body Systems Interface", "Central body computer / gateway"),
    (0x6A8, 0x688, "CMM", "Engine control", "Engine / injection (petrol or diesel)"),
    (0x6A9, 0x689, "BVA", "Automatic gearbox", "Automatic transmission (if fitted)"),
    (0x6AD, 0x68D, "ABS/ESP", "Braking / stability", "ABS anti-lock + ESP stability"),
    (0x744, 0x644, "AIRBAG", "Airbags / SRS", "Restraint / airbag control"),
    (0x75F, 0x65F, "COMBINE", "Instrument cluster", "Dashboard cluster"),
    (0x75D, 0x65D, "AAS", "Parking aid", "Park distance sensors (if fitted)"),
    (0x6B5, 0x695, "DIRECTION", "Power steering", "Electric power steering"),
    (0x6B3, 0x693, "FSE", "Electric parking brake", "Electric parking brake (if fitted)"),
    (0x76D, 0x66D, "CLIM", "Climate control", "Air conditioning / heating"),
    (0x6AF, 0x68F, "DSG", "Tyre pressure (TPMS)", "Tyre pressure monitoring (if fitted)"),
    (0x760, 0x660, "AUTORADIO", "Radio / audio", "Audio head unit"),
    (0x765, 0x665, "AFFICHEUR", "Central display", "Central screen (if fitted)"),
    (0x764, 0x664, "TELEMAT", "Telematics", "Telematics / emergency call (if fitted)"),
    (0x6A1, 0x681, "SCR", "AdBlue / SCR", "Diesel selective catalytic reduction"),
    (0x6C1, 0x601, "ADDITIF", "DPF additive", "Diesel particulate additive pump"),
    (0x730, 0x710, "CPL", "Rain / light sensor", "Rain & light sensor (if fitted)"),
]

# request_id -> response_id map handed to the PSA UDS client so it can set the
# right receive filter per module (PSA response != request+8).
PEUGEOT_ID_MAP = {tx: rx for tx, rx, _, _, _ in PEUGEOT_ECU_TABLE}

# Standard UDS identification DIDs (0x22). Modules support different subsets;
# we read best-effort and keep whatever answers as printable text.
IDENT_DIDS = [
    (0xF190, "VIN"),
    (0xF187, "Part number"),
    (0xF188, "Software"),
    (0xF18C, "ECU serial"),
]

# UDS NRCs that mean "I might answer in a different session" -- when a plain
# ReadDTC returns one of these we escalate to an extended session and retry once.
_SESSION_NRCS = (0x11, 0x22, 0x7E, 0x7F)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_ident(client: PsaElm327UdsClient, req_id: int) -> List[dict]:
    """Best-effort module identification via ReadDataByIdentifier (0x22)."""
    out: List[dict] = []
    for did, label in IDENT_DIDS:
        try:
            payload = client.request(req_id, bytes([0x22, did >> 8, did & 0xFF]), timeout=2.0)
        except (ObdError, TransportError):
            continue
        # Positive RDBI reply is 0x62 <did_hi> <did_lo> <data...>; anything else
        # (incl. a 0x7F negative for an unsupported DID) we simply skip.
        if len(payload) < 4 or payload[0] != 0x62:
            continue
        body = payload[3:]
        text = "".join(chr(b) for b in body if 32 <= b < 127).strip()
        # Prefer readable text; fall back to hex only if it clearly isn't ASCII.
        value = text if text and len(text) >= (len(body) + 1) // 2 else body.hex().upper()
        if value:
            out.append({"label": label, "value": value})
    return out


def _read_dtcs(client: PsaElm327UdsClient, req_id: int) -> List[dict]:
    """ReadDTC, escalating to an extended session if the default one refuses.

    Many PSA modules answer ReadDTCInformation straight away; the airbag and BSI
    often want an extended diagnostic session first. Starting that session is
    read-safe (it writes nothing and lapses on its own), so we only reach for it
    when the module explicitly says the service is unavailable in this session.
    """
    try:
        return uds.read_dtcs(client, req_id)
    except uds.UdsNegativeResponse as exc:
        if exc.nrc in _SESSION_NRCS and client.start_extended_session(req_id):
            return uds.read_dtcs(client, req_id)
        raise


def scan(adapter: Optional[dict], mode: str = "real") -> dict:
    """Walk the Peugeot's diagnostic-CAN modules and read each one's fault memory.

    ``adapter`` is a saved ELM327 adapter-instance dict (None for sim). Read-only.
    """
    if mode == "sim":
        return _simulated_scan()

    def run(session, entry):
        session.ensure_protocol()  # ObdError('link') here -> caught below as "ignition?"
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False,
                "vehicle": {"vin": None, "protocol": session.protocol,
                            "adapter": (adapter or {}).get("name")},
                "modules": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k CAN PSA module diagnostics use. If this is the Peugeot, turn "
                    "the ignition on and retry. Pre-2004 (VAN bus) and many 2004-2010 (KWP) "
                    "cars can't be module-scanned through a plain OBD dongle."
                ),
                "timestamp": _now(),
            }

        client = PsaElm327UdsClient(session, PEUGEOT_ID_MAP)
        client.begin()
        modules: List[dict] = []
        vin: Optional[str] = None
        try:
            for req_id, resp_id, code, name, desc in PEUGEOT_ECU_TABLE:
                mod = {
                    "address": f"0x{req_id:03X}", "response": f"0x{resp_id:03X}",
                    "code": code, "name": name, "description": desc,
                    "present": False, "ident": [], "dtcs": [], "error": None,
                }
                if not uds.tester_present(client, req_id):
                    modules.append(mod)
                    continue
                mod["present"] = True
                try:
                    mod["ident"] = _read_ident(client, req_id)
                    mod["dtcs"] = _read_dtcs(client, req_id)
                except uds.UdsNegativeResponse as exc:
                    mod["error"] = str(exc)
                except (ObdError, TransportError) as exc:
                    mod["error"] = str(exc)
                if vin is None:
                    for item in mod["ident"]:
                        if item["label"] == "VIN" and len(item["value"]) >= 11:
                            vin = item["value"][:17]
                modules.append(mod)
        finally:
            client.restore()

        present = [m for m in modules if m["present"]]
        faults = sum(len(m["dtcs"]) for m in present)
        return {
            "ok": True, "simulated": False,
            "vehicle": {"vin": vin, "protocol": session.protocol or "ISO 15765-4 (CAN 11/500)",
                        "adapter": (adapter or {}).get("name")},
            "modules": modules,
            "message": (
                f"{len(present)} module(s) answered on the diagnostic CAN, {faults} fault"
                f"{'s' if faults != 1 else ''} stored. Read-only scan -- nothing was changed on "
                "the car. Modules on other PSA buses (or older KWP/VAN cars) aren't reachable "
                "through a standard OBD dongle."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {
            "ok": False, "simulated": False, "vehicle": None, "modules": [],
            "message": str(exc), "timestamp": _now(),
        }


def clear(adapter: Optional[dict], mode: str = "real", address: Optional[str] = None) -> dict:
    """Clear fault memory on one module (address like "0x752") or all present."""
    if mode == "sim":
        return {
            "ok": True, "simulated": True, "cleared": [address or "all present"],
            "message": "SIMULATED -- nothing was cleared on any real car.",
            "timestamp": _now(),
        }

    wanted: Optional[int] = None
    if address:
        try:
            wanted = int(address, 16)
        except ValueError:
            return {
                "ok": False, "simulated": False, "cleared": [],
                "message": f"Bad module address '{address}' (expected hex like 0x752).",
                "timestamp": _now(),
            }

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "cleared": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k CAN needed to clear Peugeot module faults."
                ),
                "timestamp": _now(),
            }

        client = PsaElm327UdsClient(session, PEUGEOT_ID_MAP)
        client.begin()
        cleared, errors = [], []
        try:
            if wanted is not None:
                targets = [wanted]
            else:
                targets = [rid for rid, _, _, _, _ in PEUGEOT_ECU_TABLE
                           if uds.tester_present(client, rid)]
            for rid in targets:
                try:
                    uds.clear_dtcs(client, rid)
                    cleared.append(f"0x{rid:03X}")
                except Exception as exc:
                    errors.append(f"0x{rid:03X}: {exc}")
        finally:
            client.restore()

        ok = bool(cleared) and not errors
        message = f"Cleared fault memory on {', '.join(cleared) or 'no modules'}."
        if errors:
            message += " Failed: " + "; ".join(errors)
        return {
            "ok": ok, "simulated": False, "cleared": cleared,
            "message": message, "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {
            "ok": False, "simulated": False, "cleared": [],
            "message": str(exc), "timestamp": _now(),
        }


def _simulated_scan() -> dict:
    def mod(addr, resp, code, name, desc, present, ident=None, dtcs=None):
        return {
            "address": addr, "response": resp, "code": code, "name": name,
            "description": desc, "present": present,
            "ident": ident or [], "dtcs": dtcs or [], "error": None,
        }

    modules = [
        mod("0x752", "0x652", "BSI", "Body Systems Interface", "Central body computer / gateway",
            True, ident=[{"label": "VIN", "value": "SIMULATED00PEUGEOT"},
                         {"label": "Part number", "value": "9822009980"}]),
        mod("0x6A8", "0x688", "CMM", "Engine control", "Engine / injection (petrol or diesel)",
            True, ident=[{"label": "Software", "value": "1037564321"}],
            dtcs=[
                {"code": "P0234", "hex": "023400", "failureType": "00",
                 "status": "2F", "flags": ["test failed", "confirmed"]},
                {"code": "P1338", "hex": "133800", "failureType": "00",
                 "status": "08", "flags": ["confirmed"]},
            ]),
        mod("0x744", "0x644", "AIRBAG", "Airbags / SRS", "Restraint / airbag control",
            True, dtcs=[{"code": "B1004", "hex": "100400", "failureType": "00",
                         "status": "08", "flags": ["confirmed"]}]),
        mod("0x6AD", "0x68D", "ABS/ESP", "Braking / stability", "ABS anti-lock + ESP stability",
            True),
        mod("0x6A1", "0x681", "SCR", "AdBlue / SCR", "Diesel selective catalytic reduction",
            True, dtcs=[{"code": "P20E8", "hex": "20E800", "failureType": "00",
                         "status": "08", "flags": ["confirmed"]}]),
        mod("0x75D", "0x65D", "AAS", "Parking aid", "Park distance sensors (if fitted)", False),
        mod("0x764", "0x664", "TELEMAT", "Telematics", "Telematics / emergency call (if fitted)",
            False),
    ]
    return {
        "ok": True, "simulated": True,
        "vehicle": {"vin": "SIMULATED00PEUGEOT", "protocol": "ISO 15765-4 (CAN 11/500)",
                    "adapter": "Simulated"},
        "modules": modules,
        "message": "SIMULATED -- 5 modules answered, 4 faults stored. No real car connected.",
        "timestamp": _now(),
    }
