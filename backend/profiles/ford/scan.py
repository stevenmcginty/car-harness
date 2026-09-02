"""Ford module scan over a generic OBD-II dongle (HS-CAN, UDS).

Generic OBD-II (backend/obd/service.py) only sees the engine's emissions data.
This walks the manufacturer-level diagnostic addresses on the car's HS-CAN bus
and reads each responding module's UDS fault memory + identification. Same shape
as the Mazda scan: an ordinary ELM327 on the OBD port, 11-bit/500k.

IMPORTANT SCOPE: a standard ELM327 only reaches HS-CAN. Ford's body/comfort
modules (doors, seats, parking aid on older cars) sit on MS-CAN and need a
switchable adapter (FORScan-style OBDLink with MS-CAN). Probing an absent
address is harmless -- it times out and shows "not present". Read-only apart
from clear(), which the API layer guards behind an explicit confirm.

The address map is the Ford global module map (FORScan / Ford IDS): the same
module keeps the same 11-bit id across T6 Ranger, F-150, Focus and Fiesta.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.core import uds
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

# 11-bit physical request ids (response = request + 8).
FORD_ECU_TABLE = [
    (0x7E0, "PCM", "Powertrain control module (engine)"),
    (0x7E1, "TCM", "Transmission control module (6R80 auto)"),
    (0x760, "ABS", "Anti-lock brake / stability control"),
    (0x726, "BCM", "Body control module"),
    (0x720, "IPC", "Instrument panel cluster"),
    (0x737, "RCM", "Restraints (airbag) control module"),
    (0x730, "PSCM", "Power steering control module"),
    (0x7D0, "APIM", "SYNC infotainment (if fitted)"),
    (0x727, "ACM", "Audio control module"),
    (0x761, "TCCM", "Transfer case (4x4) control module (if fitted)"),
    (0x765, "OCS", "Occupant classification (if fitted)"),
    (0x706, "IPMA", "Image processing (camera) module (if fitted)"),
    (0x764, "CCM", "Cruise control (radar) module (if fitted)"),
    (0x736, "PAM", "Parking aid module (may be MS-CAN only)"),
    (0x7C4, "HVAC", "Climate control module (may be MS-CAN only)"),
    (0x7E2, "ECU @ 0x7E2", "HS-CAN module"),
    (0x7E3, "ECU @ 0x7E3", "HS-CAN module"),
    (0x7E4, "ECU @ 0x7E4", "HS-CAN module"),
    (0x7E5, "ECU @ 0x7E5", "HS-CAN module"),
    (0x7E6, "ECU @ 0x7E6", "HS-CAN module"),
    (0x7E7, "ECU @ 0x7E7", "HS-CAN module"),
]

# Standard UDS identification DIDs (0x22). Ford PCMs answer F188/F111/F113
# style part numbers ("AB39-14C097-AA"). Best-effort; keep whatever answers.
IDENT_DIDS = [
    (0xF190, "VIN"),
    (0xF188, "Software part number"),
    (0xF111, "Core assembly part number"),
    (0xF113, "Delivery assembly part number"),
    (0xF124, "Calibration part number"),
    (0xF18C, "ECU serial"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_ident(client: Elm327UdsClient, req_id: int) -> List[dict]:
    """Best-effort module identification via ReadDataByIdentifier (0x22)."""
    out: List[dict] = []
    for did, label in IDENT_DIDS:
        try:
            payload = client.request(req_id, bytes([0x22, did >> 8, did & 0xFF]), timeout=2.0)
        except (ObdError, TransportError):
            continue
        if len(payload) < 4 or payload[0] != 0x62:
            continue
        body = payload[3:]
        text = "".join(chr(b) for b in body if 32 <= b < 127).strip()
        value = text if text and len(text) >= (len(body) + 1) // 2 else body.hex().upper()
        if value:
            out.append({"label": label, "value": value})
    return out


def _not_hs_can(session, adapter, what: str, extra: dict) -> dict:
    return {
        "ok": False, "simulated": False,
        "vehicle": {"vin": None, "protocol": session.protocol,
                    "adapter": (adapter or {}).get("name")},
        "message": (
            f"The car answered {session.protocol or 'an unknown protocol'}, not the "
            f"11-bit/500k HS-CAN Ford {what} use. If this is the Ford, turn the ignition "
            "on and retry -- the dongle may have mis-detected."
        ),
        "timestamp": _now(), **extra,
    }


def scan(adapter: Optional[dict], mode: str = "real") -> dict:
    """Walk the Ford's HS-CAN modules and read each one's fault memory. Read-only."""
    if mode == "sim":
        return _simulated_scan()

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return _not_hs_can(session, adapter, "module diagnostics", {"modules": []})

        obd_vin = obd_service._read_vin(session)

        client = Elm327UdsClient(session)
        client.begin()
        modules: List[dict] = []
        module_vin: Optional[str] = None
        try:
            for req_id, code, name in FORD_ECU_TABLE:
                mod = {
                    "address": f"0x{req_id:03X}", "code": code, "name": name,
                    "present": False, "ident": [], "dtcs": [], "error": None,
                }
                if not uds.tester_present(client, req_id):
                    modules.append(mod)
                    continue
                mod["present"] = True
                try:
                    mod["ident"] = _read_ident(client, req_id)
                    mod["dtcs"] = uds.read_dtcs(client, req_id)
                except uds.UdsNegativeResponse as exc:
                    mod["error"] = str(exc)
                except (ObdError, TransportError) as exc:
                    mod["error"] = str(exc)
                if module_vin is None:
                    for item in mod["ident"]:
                        if item["label"] == "VIN" and len(item["value"]) >= 11:
                            module_vin = item["value"][:17]
                modules.append(mod)
        finally:
            client.restore()

        vin = module_vin or obd_vin
        present = [m for m in modules if m["present"]]
        faults = sum(len(m["dtcs"]) for m in present)
        plural = "s" if faults != 1 else ""
        return {
            "ok": True, "simulated": False,
            "vehicle": {"vin": vin, "protocol": session.protocol or "ISO 15765-4 (CAN 11/500)",
                        "adapter": (adapter or {}).get("name")},
            "modules": modules,
            "message": (
                (f"VIN {vin}. " if vin else "")
                + f"{len(present)} module(s) answered on HS-CAN, {faults} fault{plural} stored. "
                "Read-only scan -- nothing was changed on the car. MS-CAN body modules need a "
                "switchable adapter."
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
    """Clear fault memory on one module (address like "0x7E0") or all present."""
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
                "message": f"Bad module address '{address}' (expected hex like 0x7E0).",
                "timestamp": _now(),
            }

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return _not_hs_can(session, adapter, "fault clears", {"cleared": []})

        client = Elm327UdsClient(session)
        client.begin()
        cleared, errors = [], []
        try:
            if wanted is not None:
                targets = [wanted]
            else:
                targets = [rid for rid, _, _ in FORD_ECU_TABLE if uds.tester_present(client, rid)]
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
    def mod(addr, code, name, present, ident=None, dtcs=None):
        return {
            "address": addr, "code": code, "name": name, "present": present,
            "ident": ident or [], "dtcs": dtcs or [], "error": None,
        }

    modules = [
        mod("0x7E0", "PCM", "Powertrain control module (engine)", True,
            ident=[{"label": "VIN", "value": "SIMULATED0000FORD"},
                   {"label": "Software part number", "value": "EB3A-14C204-AAA"}],
            dtcs=[
                {"code": "P0299", "hex": "029900", "failureType": "00",
                 "status": "2F", "flags": ["test failed", "confirmed"]},
                {"code": "P0401", "hex": "040100", "failureType": "00",
                 "status": "08", "flags": ["confirmed"]},
            ]),
        mod("0x7E1", "TCM", "Transmission control module (6R80 auto)", True,
            ident=[{"label": "Software part number", "value": "EB3P-7H417-AB"}]),
        mod("0x760", "ABS", "Anti-lock brake / stability control", True),
        mod("0x726", "BCM", "Body control module", True),
        mod("0x7D0", "APIM", "SYNC infotainment (if fitted)", False),
    ]
    return {
        "ok": True, "simulated": True,
        "vehicle": {"vin": "SIMULATED0000FORD", "protocol": "ISO 15765-4 (CAN 11/500)",
                    "adapter": "Simulated"},
        "modules": modules,
        "message": "SIMULATED -- VIN SIMULATED0000FORD. 4 modules answered, 2 faults stored. "
                   "No real car connected.",
        "timestamp": _now(),
    }
