"""Mazda module scan over a generic OBD-II dongle (HS-CAN, UDS).

Generic OBD-II (backend/obd/service.py) only sees the engine's emissions data.
This walks the manufacturer-level diagnostic addresses on the car's HS-CAN bus
and reads each responding module's UDS fault memory + identification -- the same
idea as the BMW ENET scan, but over an ordinary ELM327 instead of an ENET cable,
because a 2016 Mazda (MX-5 ND and its SkyActiv siblings) has no cheap native
transport.

IMPORTANT SCOPE: a standard ELM327 on the OBD port only reaches the HS-CAN bus.
Mazda's body/comfort modules (locks, lights, i-Stop, cluster config) live on a
separate MS-CAN bus and are invisible here -- reaching them needs an MS-CAN
switchable adapter. This module is read-only apart from clear(), which the API
layer guards behind an explicit confirm.

The UDS transport is backend/obd/uds_elm327.py; the UDS service/parse layer is
the shared backend/core/uds.py (same code the BMW path uses).
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.core import uds
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

# 11-bit physical request ids to sweep (response = request + 8). The named
# addresses are the community-mapped SkyActiv Gen6 module map (opendbc firmware
# queries + the ND CAN-reverse-engineering projects); which are fitted varies by
# model/trim, and a few live on MS-CAN on some cars -- probing an address that
# doesn't answer is harmless, it just times out and shows "not present". The
# generic 0x7E1-0x7E7 block stays at the end to catch anything unmapped.
MAZDA_ECU_TABLE = [
    (0x7E0, "PCM", "Engine / powertrain control"),
    (0x7E1, "TCM", "Automatic gearbox (if fitted)"),
    (0x720, "IC", "Instrument cluster"),
    (0x726, "FBCM", "Front body control module"),
    (0x730, "EPS", "Power steering"),
    (0x760, "DSC", "ABS / stability control"),
    (0x737, "RCM", "Airbag / restraints"),
    (0x731, "SSU", "Start-stop / keyless unit (may be MS-CAN only)"),
    (0x736, "RBCM", "Rear body control module (may be MS-CAN only)"),
    (0x706, "FSC", "Forward camera (if fitted)"),
    (0x764, "MRCC", "Radar cruise (if fitted)"),
    (0x772, "HUD", "Head-up display (if fitted)"),
    (0x793, "SCBS", "City brake support (if fitted)"),
    (0x7E2, "ECU @ 0x7E2", "HS-CAN module"),
    (0x7E3, "ECU @ 0x7E3", "HS-CAN module"),
    (0x7E4, "ECU @ 0x7E4", "HS-CAN module"),
    (0x7E5, "ECU @ 0x7E5", "HS-CAN module"),
    (0x7E6, "ECU @ 0x7E6", "HS-CAN module"),
    (0x7E7, "ECU @ 0x7E7", "HS-CAN module"),
]

# Standard UDS identification DIDs (0x22). Modules support different subsets;
# we read best-effort and keep whatever answers as printable text.
IDENT_DIDS = [
    (0xF190, "VIN"),
    (0xF187, "Part number"),
    (0xF188, "Software"),
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


def scan(adapter: Optional[dict], mode: str = "real") -> dict:
    """Walk the Mazda's HS-CAN modules and read each one's fault memory.

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
                    "11-bit/500k HS-CAN Mazda module diagnostics use. If this is the Mazda, "
                    "turn the ignition on and retry -- the dongle may have mis-detected."
                ),
                "timestamp": _now(),
            }

        client = Elm327UdsClient(session)
        client.begin()
        modules: List[dict] = []
        vin: Optional[str] = None
        try:
            for req_id, code, name in MAZDA_ECU_TABLE:
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
                f"{len(present)} module(s) answered on HS-CAN, {faults} fault"
                f"{'s' if faults != 1 else ''} stored. Read-only scan -- nothing was changed on "
                "the car. Body/comfort modules on MS-CAN aren't reachable through a standard "
                "OBD dongle (they need an MS-CAN switchable adapter)."
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
            return {
                "ok": False, "simulated": False, "cleared": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k HS-CAN needed to clear Mazda module faults."
                ),
                "timestamp": _now(),
            }

        client = Elm327UdsClient(session)
        client.begin()
        cleared, errors = [], []
        try:
            if wanted is not None:
                targets = [wanted]
            else:
                targets = [rid for rid, _, _ in MAZDA_ECU_TABLE if uds.tester_present(client, rid)]
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
        mod("0x7E0", "PCM", "Engine / powertrain control", True,
            ident=[{"label": "VIN", "value": "SIMULATED000MAZDA"},
                   {"label": "Software", "value": "PY0118881"}],
            dtcs=[
                {"code": "P0300", "hex": "030000", "failureType": "00",
                 "status": "2F", "flags": ["test failed", "confirmed"]},
                {"code": "P0456", "hex": "045600", "failureType": "00",
                 "status": "08", "flags": ["confirmed"]},
            ]),
        mod("0x7E1", "ECU @ 0x7E1", "HS-CAN module (often transmission)", True,
            ident=[{"label": "Part number", "value": "P51D18881"}]),
        mod("0x7E4", "ECU @ 0x7E4", "HS-CAN module (often ABS / DSC)", True,
            dtcs=[{"code": "C0051", "hex": "005100", "failureType": "00",
                   "status": "08", "flags": ["confirmed"]}]),
        mod("0x7E2", "ECU @ 0x7E2", "HS-CAN module", False),
        mod("0x7E3", "ECU @ 0x7E3", "HS-CAN module", False),
    ]
    return {
        "ok": True, "simulated": True,
        "vehicle": {"vin": "SIMULATED000MAZDA", "protocol": "ISO 15765-4 (CAN 11/500)",
                    "adapter": "Simulated"},
        "modules": modules,
        "message": "SIMULATED -- 3 modules answered, 3 faults stored. No real car connected.",
        "timestamp": _now(),
    }
