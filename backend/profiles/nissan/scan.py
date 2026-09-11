"""Nissan module scan over a generic OBD-II dongle (HS-CAN, UDS/KWP).

Scope: AUDIO / AV diagnosis on the 2016 370Z (Z34, UK). Generic OBD-II
(backend/obd/service.py) only sees the engine's emissions data. This walks the
manufacturer-level diagnostic addresses on the car's HS-CAN bus and reads each
responding module's UDS fault memory + identification -- same shape as the Ford
and Mazda scans, an ordinary ELM327 on the OBD port at 11-bit/500k.

Two addressing conventions share the bus (see pids.py): powertrain on the
standard 0x7E0-0x7E7 (response = request + 8), and Nissan body/comfort/AV
modules on the 0x74x block (response = request + 0x20). The Elm327UdsClient is
given pids.id_map() so both are read correctly.

The AV control unit that stores the Bose-amp codes is somewhere in the 0x74x
block but its exact id is UNVERIFIED -- so discover() sweeps the whole candidate
list and reports responders, and audio_faults() reads DTCs from every body/AV
candidate and surfaces the amp codes with their dealer text. Read-only apart
from clear(), which the API layer guards behind an explicit confirm.

This file is deliberately audio/scan-scoped: a keys.py / nats.py (immobiliser
work owned by another session) can be added to this package without collision.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.core import uds
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

from backend.profiles.nissan import pids

# Standard UDS identification DIDs (0x22). Modules support different subsets;
# read best-effort and keep whatever answers as printable text.
IDENT_DIDS = [
    (0xF190, "VIN"),
    (0xF188, "Software part number"),
    (0xF187, "Part number"),
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
            f"11-bit/500k HS-CAN Nissan {what} use. If this is the Nissan, turn the ignition "
            "on and retry -- the dongle may have mis-detected."
        ),
        "timestamp": _now(), **extra,
    }


def _client(session) -> Elm327UdsClient:
    """A UDS client that knows the Nissan 0x74x resp = req + 0x20 override."""
    return Elm327UdsClient(session, id_map=pids.id_map())


# --------------------------------------------------------------------------- #
# discover() -- fast module sweep: which candidate addresses answer.
# --------------------------------------------------------------------------- #
def discover(adapter: Optional[dict], mode: str = "real") -> dict:
    """Sweep the candidate addresses and report which nodes answer. Read-only.

    Lighter than scan(): no DTC read, just presence + a best-effort ident, so
    the operator can see where the AV unit actually lives before reading faults.
    """
    if mode == "sim":
        return _simulated_discover()

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return _not_hs_can(session, adapter, "module diagnostics", {"modules": []})

        client = _client(session)
        client.begin()
        modules: List[dict] = []
        try:
            for req_id, resp_id, code, name, is_av, note in pids.NISSAN_ECU_TABLE:
                mod = {
                    "address": f"0x{req_id:03X}", "respId": f"0x{resp_id:03X}",
                    "code": code, "name": name, "isAv": is_av, "note": note,
                    "present": False, "ident": [],
                }
                if uds.tester_present(client, req_id):
                    mod["present"] = True
                    try:
                        mod["ident"] = _read_ident(client, req_id)
                    except (uds.UdsNegativeResponse, ObdError, TransportError):
                        pass
                modules.append(mod)
        finally:
            client.restore()

        present = [m for m in modules if m["present"]]
        av_present = [m for m in present if m["isAv"]]
        return {
            "ok": True, "simulated": False,
            "vehicle": {"vin": None, "protocol": session.protocol or "ISO 15765-4 (CAN 11/500)",
                        "adapter": (adapter or {}).get("name")},
            "modules": modules,
            "message": (
                f"{len(present)} node(s) answered ({len(av_present)} in the Nissan body/AV "
                "block). Read-only sweep -- nothing was changed on the car. Read DTCs from the "
                "body/AV responders (audio_faults) to find the Bose-amp codes."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "vehicle": None, "modules": [],
                "message": str(exc), "timestamp": _now()}


# --------------------------------------------------------------------------- #
# scan() -- full sweep with DTC read, like the Ford/Mazda scan.
# --------------------------------------------------------------------------- #
def scan(adapter: Optional[dict], mode: str = "real") -> dict:
    """Walk the Nissan modules and read each one's fault memory. Read-only."""
    if mode == "sim":
        return _simulated_scan()

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return _not_hs_can(session, adapter, "module diagnostics", {"modules": []})

        obd_vin = obd_service._read_vin(session)

        client = _client(session)
        client.begin()
        modules: List[dict] = []
        module_vin: Optional[str] = None
        try:
            for req_id, resp_id, code, name, is_av, note in pids.NISSAN_ECU_TABLE:
                mod = {
                    "address": f"0x{req_id:03X}", "respId": f"0x{resp_id:03X}",
                    "code": code, "name": name, "isAv": is_av,
                    "present": False, "ident": [], "dtcs": [], "error": None,
                }
                if not uds.tester_present(client, req_id):
                    modules.append(mod)
                    continue
                mod["present"] = True
                try:
                    mod["ident"] = _read_ident(client, req_id)
                    mod["dtcs"] = _annotate(uds.read_dtcs(client, req_id))
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
                + f"{len(present)} module(s) answered, {faults} fault{plural} stored. "
                "Read-only scan -- nothing was changed on the car."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "vehicle": None, "modules": [],
                "message": str(exc), "timestamp": _now()}


# --------------------------------------------------------------------------- #
# audio_faults() -- the point of this profile.
# --------------------------------------------------------------------------- #
def _annotate(dtcs: List[dict]) -> List[dict]:
    """Attach the AV/amp text and an amp flag to each DTC (unknown -> '')."""
    for d in dtcs:
        code = d.get("code", "")
        d["audioText"] = pids.dtc_text(code, d.get("failureType", ""))
        d["ampFault"] = pids.is_amp_fault(code)
    return dtcs


def audio_faults(adapter: Optional[dict], mode: str = "real") -> dict:
    """Read DTCs from the Nissan body/AV candidate nodes and surface amp faults.

    Returns every DTC found on the body/AV block, each annotated with its dealer
    text from pids.AUDIO_DTCS, plus ``ampFault`` True/False on the whole result
    for whether any B1F00 'BOSE AMP' / U1263 amp-comms class code is present.
    Read-only.
    """
    if mode == "sim":
        return _simulated_audio()

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return _not_hs_can(session, adapter, "audio diagnostics",
                               {"nodes": [], "ampFault": False})

        client = _client(session)
        client.begin()
        nodes: List[dict] = []
        try:
            for req_id, resp_id, code, name, note in pids.av_candidates():
                node = {
                    "address": f"0x{req_id:03X}", "respId": f"0x{resp_id:03X}",
                    "code": code, "name": name, "note": note,
                    "present": False, "dtcs": [], "error": None,
                }
                if not uds.tester_present(client, req_id):
                    nodes.append(node)
                    continue
                node["present"] = True
                try:
                    node["dtcs"] = _annotate(uds.read_dtcs(client, req_id))
                except uds.UdsNegativeResponse as exc:
                    node["error"] = str(exc)
                except (ObdError, TransportError) as exc:
                    node["error"] = str(exc)
                nodes.append(node)
        finally:
            client.restore()

        present = [n for n in nodes if n["present"]]
        amp_dtcs = [d for n in present for d in n["dtcs"] if d.get("ampFault")]
        amp_fault = bool(amp_dtcs)
        total = sum(len(n["dtcs"]) for n in present)
        if amp_fault:
            where = ", ".join(sorted({n["address"] for n in present
                                      if any(d.get("ampFault") for d in n["dtcs"])}))
            msg = (f"Bose-amp fault present: {len(amp_dtcs)} amp code(s) on {where} "
                   f"(e.g. {amp_dtcs[0]['code']} '{amp_dtcs[0].get('audioText') or '?'}'). "
                   "Symptom fit: rear speakers only, no front door/dash speakers.")
        elif present:
            msg = (f"{len(present)} body/AV node(s) answered, {total} fault(s) total, "
                   "none amp-related. Note the AV-unit id is UNVERIFIED -- confirm a node "
                   "actually answered before ruling the amp fault out.")
        else:
            msg = ("No body/AV node answered. The AV-unit diagnostic id is UNVERIFIED; "
                   "check ignition ON and the adapter, then retry discover().")
        return {
            "ok": True, "simulated": False, "ampFault": amp_fault,
            "nodes": nodes, "ampDtcs": amp_dtcs,
            "vehicle": {"protocol": session.protocol or "ISO 15765-4 (CAN 11/500)",
                        "adapter": (adapter or {}).get("name")},
            "message": msg, "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "ampFault": False, "nodes": [],
                "message": str(exc), "timestamp": _now()}


# --------------------------------------------------------------------------- #
# clear() -- CHANGES THE CAR; API layer confirm-gates it.
# --------------------------------------------------------------------------- #
def clear(adapter: Optional[dict], mode: str = "real", address: Optional[str] = None) -> dict:
    """Clear fault memory on one module (address like "0x74D") or all present."""
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
                "message": f"Bad module address '{address}' (expected hex like 0x74D).",
                "timestamp": _now(),
            }

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return _not_hs_can(session, adapter, "fault clears", {"cleared": []})

        client = _client(session)
        client.begin()
        cleared, errors = [], []
        try:
            if wanted is not None:
                targets = [wanted]
            else:
                targets = [req for req, *_ in pids.NISSAN_ECU_TABLE
                           if req != 0x7DF and uds.tester_present(client, req)]
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
        return {"ok": False, "simulated": False, "cleared": [],
                "message": str(exc), "timestamp": _now()}


# --------------------------------------------------------------------------- #
# Simulated payloads (clearly fake; never leak into real mode).
# --------------------------------------------------------------------------- #
def _sim_amp_dtcs() -> List[dict]:
    return _annotate([
        {"code": "B1F00", "hex": "9F0049", "failureType": "49", "status": "2F",
         "flags": ["test failed", "confirmed"]},
        {"code": "U1263", "hex": "D26300", "failureType": "00", "status": "08",
         "flags": ["confirmed"]},
    ])


def _simulated_discover() -> dict:
    def mod(addr, resp, code, name, is_av, present):
        return {"address": addr, "respId": resp, "code": code, "name": name,
                "isAv": is_av, "note": "", "present": present, "ident": []}

    modules = [
        mod("0x7E0", "0x7E8", "ECM", "Engine control module (VQ37VHR)", False, True),
        mod("0x745", "0x765", "BCM", "Body control module", True, True),
        mod("0x74D", "0x76D", "AV?", "AV control unit / head unit", True, True),
        mod("0x74E", "0x76E", "ECU @ 0x74E", "Nissan body/comfort module", True, False),
    ]
    return {
        "ok": True, "simulated": True,
        "vehicle": {"vin": "SIMULATED00NISSAN", "protocol": "ISO 15765-4 (CAN 11/500)",
                    "adapter": "Simulated"},
        "modules": modules,
        "message": "SIMULATED -- 3 nodes answered (2 in the body/AV block). No real car connected.",
        "timestamp": _now(),
    }


def _simulated_scan() -> dict:
    def mod(addr, resp, code, name, is_av, present, ident=None, dtcs=None):
        return {"address": addr, "respId": resp, "code": code, "name": name,
                "isAv": is_av, "present": present, "ident": ident or [],
                "dtcs": dtcs or [], "error": None}

    modules = [
        mod("0x7E0", "0x7E8", "ECM", "Engine control module (VQ37VHR)", False, True,
            ident=[{"label": "VIN", "value": "SIMULATED00NISSAN"}]),
        mod("0x745", "0x765", "BCM", "Body control module", True, True),
        mod("0x74D", "0x76D", "AV?", "AV control unit / head unit", True, True,
            dtcs=_sim_amp_dtcs()),
    ]
    return {
        "ok": True, "simulated": True,
        "vehicle": {"vin": "SIMULATED00NISSAN", "protocol": "ISO 15765-4 (CAN 11/500)",
                    "adapter": "Simulated"},
        "modules": modules,
        "message": "SIMULATED -- VIN SIMULATED00NISSAN. 3 modules answered, 2 faults stored "
                   "(amp codes on the AV node). No real car connected.",
        "timestamp": _now(),
    }


def _simulated_audio() -> dict:
    amp = _sim_amp_dtcs()
    nodes = [
        {"address": "0x745", "respId": "0x765", "code": "BCM", "name": "Body control module",
         "note": "", "present": True, "dtcs": [], "error": None},
        {"address": "0x74D", "respId": "0x76D", "code": "AV?",
         "name": "AV control unit / head unit", "note": "UNVERIFIED best guess",
         "present": True, "dtcs": amp, "error": None},
    ]
    return {
        "ok": True, "simulated": True, "ampFault": True, "nodes": nodes, "ampDtcs": amp,
        "vehicle": {"protocol": "ISO 15765-4 (CAN 11/500)", "adapter": "Simulated"},
        "message": "SIMULATED -- Bose-amp fault present: B1F00-49 'BOSE AMP' + U1263 on the AV "
                   "node. Rear speakers only, no front. No real car connected.",
        "timestamp": _now(),
    }
