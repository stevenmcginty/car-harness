"""VW / Audi (VAG) module scan over a generic OBD-II dongle (pure UDS).

Generic OBD-II (backend/obd/service.py) only sees the engine's emissions data.
This walks the manufacturer-level modules a UDS-era VAG car exposes to the
dongle and reads each responding module's fault memory + identification -- the
same idea as the Mazda and Peugeot dongle scans, but VAG is pure UDS (no KWP
dialect) and uses two id blocks on 11-bit/500k (docs/private/vag-research.md §2):

* Powertrain sits in the standard ISO 15765-4 block: engine 0x7E0/0x7E8,
  gearbox 0x7E1/0x7E9, ... (response = request + 8).
* Body / chassis / comfort modules use VAG's own diagnostic block: request ids
  in 0x700-0x76F answered on **request + 0x6A** (gateway 0x710/0x77A, cluster
  0x714/0x77E, ABS 0x713/0x77D, ...). Elm327UdsClient is handed VW_ID_MAP so it
  sets the right receive filter (ATCRA) per module -- a VAG body module does NOT
  answer on request+8.

The module ``code`` carries the VCDS 2-digit address (01 engine, 19 gateway,
17 cluster, ...) so the report reads like a VCDS auto-scan.

SCOPE (research §1): only the UDS era is reachable over a plain dongle --
VW/Škoda/SEAT ~2012+ (MQB, Golf 7 on) and Audi ~2008+ (B8 on). Pre-2012 cars
speak TP2.0 (KWP-over-CAN) or K-line, whose timed channel setup an ELM327 can't
hold; the scan says so honestly rather than listing every module as absent.
SFD on 2020+ cars blocks writes only, not the reads here (research §5).

Read-only apart from clear(), which the API layer guards behind a confirm.
"""

import re
from datetime import datetime, timezone
from typing import List, Optional

from backend.core import dtc_scheme, uds
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

# Curated VAG UDS diagnostic address table for the modules a dealer cares about
# on a pre-sale check. Each row is (request_id, response_id, code, name,
# description); ``code`` is the VCDS 2-digit address. Ids are from the
# ODIS-extracted table (ConnorHowell/vag-uds-ids) cross-checked with the +0x6A
# rule and the carscanner MQB address list -- see docs/private/vag-research.md
# §2. "(if fitted)" modules simply time out and are marked not present on cars
# without them -- harmless. Powertrain rows are request+8; every 0x7xx body row
# obeys response = request + 0x6A.
VW_ECU_TABLE = [
    (0x7E0, 0x7E8, "01", "Engine control (ECM)", "Engine / injection (petrol or diesel)"),
    (0x7E1, 0x7E9, "02", "Transmission (TCM / DSG)", "Automatic / DSG gearbox (if fitted)"),
    (0x7E2, 0x7EA, "11", "Engine control 2", "Second engine ECU (V-engine / bi-turbo, if fitted)"),
    (0x710, 0x77A, "19", "Gateway (J533)", "CAN gateway / central diagnostic node"),
    (0x714, 0x77E, "17", "Instrument cluster", "Dashboard cluster"),
    (0x713, 0x77D, "03", "ABS / brakes (ESP)", "ABS anti-lock + ESP stability"),
    (0x715, 0x77F, "15", "Airbag / SRS", "Restraint / airbag control"),
    (0x712, 0x77C, "44", "Power steering (EPS)", "Electric power steering"),
    (0x70E, 0x778, "09", "Central electrics (BCM)", "Onboard supply / body control"),
    (0x746, 0x7B0, "08", "Climate / A/C", "Air conditioning / heating"),
    (0x711, 0x77B, "25", "Immobilizer", "Immobiliser (if fitted)"),
    (0x70C, 0x776, "16", "Steering column electronics", "Steering column module"),
    (0x70F, 0x779, "22", "AWD / Haldex", "All-wheel-drive coupling (4Motion / quattro, if fitted)"),
    (0x732, 0x79C, "05", "Access & start (Kessy)", "Keyless entry / start (if fitted)"),
    (0x70B, 0x775, "65", "TPMS", "Tyre pressure monitoring (if fitted)"),
    (0x70A, 0x774, "10", "Park / drive assist", "Parking / driver assistance (if fitted)"),
    (0x752, 0x7BC, "53", "Parking brake", "Electromechanical parking brake (if fitted)"),
    (0x72A, 0x794, "—", "SCR / AdBlue", "Diesel selective catalytic reduction (if fitted)"),
    (0x773, 0x7DD, "5F", "Infotainment head unit (MIB)", "Infotainment / navigation (if fitted)"),
    (0x7E5, 0x7ED, "—", "HV battery energy control", "High-voltage battery management (EV/PHEV, if fitted)"),
    (0x7E6, 0x7EE, "—", "Drive motor control", "Electric drive motor (EV, if fitted)"),
]

# request_id -> response_id map handed to Elm327UdsClient so it sets the right
# receive filter per module (VAG body modules answer on request + 0x6A, not +8).
VW_ID_MAP = {tx: rx for tx, rx, _, _, _ in VW_ECU_TABLE}

# The engine also owns the standard OBD slot; 01 00 there is the one request an
# emissions ECU must answer with the ignition on. On VAG the engine IS 0x7E0.
ENGINE = 0x7E0

# Standard UDS identification DIDs (0x22), research §3. Modules support
# different subsets; we read best-effort and keep whatever answers as text.
IDENT_DIDS = [
    (0xF190, "VIN"),
    (0xF187, "VAG part number"),
    (0xF189, "Software"),
    (0xF191, "Hardware"),
    (0xF197, "System name"),
    (0xF18C, "ECU serial"),
]

# UDS NRCs that mean "I might answer in a different session" -- when a plain
# ReadDTC returns one of these we open the UDS extended session (10 03) and
# retry once (research §3).
_SESSION_NRCS = (0x11, 0x22, 0x7E, 0x7F)

# reportDTCByStatusMask masks, laddered. There is no single public "VAG mask"
# (research §3): 0xFF = all DTCs, 0x08 = confirmed only, 0xAF = the broad tool
# mask. A bad/unsupported mask returns NRC 0x31 (out of range) or 0x12 (sub-func
# not supported) -- move to the next mask on those, exactly like the Peugeot code.
_DTC_MASKS = (0xFF, 0x08, 0xAF)
_MASK_RETRY_NRCS = (0x12, 0x31)

_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nrc_text(exc: uds.UdsNegativeResponse) -> str:
    return f"NRC {exc.nrc:02X} ({uds.NRC_NAMES.get(exc.nrc, 'unknown')})"


def _printable(body: bytes) -> str:
    text = "".join(chr(b) for b in body if 32 <= b < 127).strip()
    if text and len(text) >= (len(body) + 1) // 2:
        return text
    return body.hex().upper()


def _probe(client: Elm327UdsClient, req_id: int, trace: List[str]) -> bool:
    """Presence ladder: 3E 00, then a VIN ident read (research §2 recipe) --
    some ECUs never answer a naked TesterPresent but do answer 22 F190. Any
    reply, positive or negative, proves an ECU lives at the id pair."""
    for data in (bytes([0x3E, 0x00]), bytes([0x22, 0xF1, 0x90])):
        try:
            reply = client.request(req_id, data, timeout=1.2)
        except (ObdError, TransportError):
            continue
        if reply:
            return True
    trace.append("3E 00 / 22 F190 -> silence")
    return False


def _open_extended(client: Elm327UdsClient, req_id: int, trace: List[str]) -> bool:
    """Open a UDS extended session (10 03) so a module that refused a read in the
    default session will answer. Returns True on a positive 0x50 reply. Standard
    UDS 10 03 -- NOT PSA's 10 C0."""
    try:
        reply = client.request(req_id, bytes([0x10, 0x03]), timeout=2.0)
    except (ObdError, TransportError):
        trace.append("10 03 -> no answer")
        return False
    if reply and reply[0] == 0x50:
        trace.append("10 03 -> extended session")
        return True
    if reply and reply[0] == 0x7F and len(reply) >= 3:
        trace.append(f"10 03 -> NRC {reply[2]:02X}")
    return False


def _read_ident(client: Elm327UdsClient, req_id: int, trace: List[str]) -> List[dict]:
    """Best-effort module identification via ReadDataByIdentifier (0x22)."""
    out: List[dict] = []
    for did, label in IDENT_DIDS:
        try:
            payload = client.request(req_id, bytes([0x22, did >> 8, did & 0xFF]), timeout=2.0)
        except (ObdError, TransportError):
            if did == 0xF190:
                trace.append("22 F190 -> no answer")
            continue
        # Positive RDBI reply is 0x62 <did_hi> <did_lo> <data...>; anything else
        # (incl. a 0x7F negative for an unsupported DID) we simply skip.
        if len(payload) < 4 or payload[0] != 0x62:
            if did == 0xF190 and payload and payload[0] == 0x7F and len(payload) >= 3:
                trace.append(f"22 F190 -> NRC {payload[2]:02X}")
            continue
        value = _printable(payload[3:])
        if value:
            out.append({"label": label, "value": value})
    return out


def _read_dtcs_uds(client: Elm327UdsClient, req_id: int, trace: List[str]) -> List[dict]:
    """One pass of the 19 02 mask ladder (0xFF -> 0x08 -> 0xAF). Raises on a
    session-family NRC so the caller can escalate to 10 03 and retry."""
    last_error = None
    payload = None
    for mask in _DTC_MASKS:
        try:
            reply = client.request(req_id, bytes([0x19, 0x02, mask]), timeout=3.0)
        except (ObdError, TransportError) as exc:
            trace.append(f"19 02 {mask:02X} -> no answer")
            raise exc
        if reply and reply[0] == 0x59:
            trace.append(f"19 02 {mask:02X} -> OK")
            payload = reply
            break
        if reply and reply[0] == 0x7F and len(reply) >= 3:
            last_error = uds.UdsNegativeResponse(0x19, reply[2])
            if reply[2] in _MASK_RETRY_NRCS:
                continue  # bad/unsupported mask -- try the next one
            trace.append(f"19 02 {mask:02X} -> {_nrc_text(last_error)}")
            raise last_error
    if payload is None:
        if last_error is not None:
            trace.append(f"19 02 -> {_nrc_text(last_error)}")
            raise last_error
        raise ObdError("Fault read failed.", kind="no_data")

    # Reply: 0x59 0x02 <availabilityMask> then 4-byte records [dtc x3][status].
    records = payload[3:]
    dtcs = []
    for i in range(0, len(records) - 3, 4):
        b0, b1, b2, status = records[i : i + 4]
        if b0 == b1 == b2 == 0:
            continue
        entry = uds.decode_dtc(b0, b1, b2)
        # VAG-family numbering: the generic SAE meanings do not
        # apply, so no plain-English label is claimed for it.
        dtc_scheme.mark(entry, "vw")
        entry["status"] = f"{status:02X}"
        entry["flags"] = uds.status_flags(status)
        dtcs.append(entry)
    return dtcs


def _read_dtcs(client: Elm327UdsClient, req_id: int, trace: List[str]) -> List[dict]:
    """Fault memory, laddered. On a session-conditions NRC (0x11/0x22/0x7E/0x7F)
    open the UDS extended session (10 03) once and retry -- some modules only
    answer 19 02 in an active session."""
    try:
        return _read_dtcs_uds(client, req_id, trace)
    except uds.UdsNegativeResponse as exc:
        if exc.nrc in _SESSION_NRCS and _open_extended(client, req_id, trace):
            try:
                return _read_dtcs_uds(client, req_id, trace)
            except uds.UdsNegativeResponse as exc2:
                trace.append(f"19 02 after session -> {_nrc_text(exc2)}")
                raise
        raise


def scan(adapter: Optional[dict], mode: str = "real") -> dict:
    """Walk the VAG car's reachable modules and read each one's fault memory.

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
                    "11-bit/500k CAN VAG UDS module diagnostics use. If this is the VW/Audi, "
                    "turn the ignition on and retry. Pre-2012 (TP2.0/K-line) VAG cars can't be "
                    "module-scanned through a plain OBD dongle."
                ),
                "timestamp": _now(),
            }

        # The car's identity first, while the receive filter is still open:
        # Mode 09 VIN works on any ~2001+ car no matter what UDS does next.
        obd_vin = obd_service._read_vin(session)

        client = Elm327UdsClient(session, VW_ID_MAP)
        client.begin()
        modules: List[dict] = []
        try:
            for req_id, resp_id, code, name, desc in VW_ECU_TABLE:
                trace: List[str] = []
                mod = {
                    "address": f"0x{req_id:03X}", "response": f"0x{resp_id:03X}",
                    "code": code, "name": name, "description": desc,
                    "present": False, "ident": [], "dtcs": [], "error": None,
                    "trace": trace,
                }
                via_standard_obd = False
                present = _probe(client, req_id, trace)
                if not present and req_id == ENGINE:
                    # Mode 01 PID 00 on the standard OBD slot: the one request
                    # an emissions ECU must answer with the ignition on.
                    try:
                        reply = client.request(ENGINE, bytes([0x01, 0x00]), timeout=2.0)
                        present = bool(reply)
                    except (ObdError, TransportError):
                        present = False
                    if present:
                        via_standard_obd = True
                        trace.append("answered on standard OBD 7E0 (01 00)")
                if not present:
                    modules.append(mod)
                    continue
                mod["present"] = True
                try:
                    mod["ident"] = _read_ident(client, req_id, trace)
                    mod["dtcs"] = _read_dtcs(client, req_id, trace)
                except uds.UdsNegativeResponse as exc:
                    mod["error"] = str(exc)
                except (ObdError, TransportError) as exc:
                    if via_standard_obd:
                        # Engine on the plain OBD channel may not speak UDS
                        # fault services -- generic Mode 03 codes still do.
                        try:
                            from backend.obd import dtc as obd_dtc
                            codes = obd_dtc.read_codes(session, 0x03)
                            mod["dtcs"] = [
                                {"code": c, "hex": "", "failureType": "",
                                 "status": "", "flags": ["confirmed"]}
                                for c in codes
                            ]
                            trace.append("fell back to OBD Mode 03")
                        except Exception:
                            mod["error"] = str(exc)
                    else:
                        mod["error"] = str(exc)
                modules.append(mod)
        finally:
            client.restore()

        present = [m for m in modules if m["present"]]
        faults = sum(len(m["dtcs"]) for m in present)

        # Dealer check: every module that knows its VIN should agree. A module
        # reporting a different VIN was swapped in from another car (crash
        # repair, or a cluster swap used to disguise mileage).
        vins = [(m["code"], item["value"][:17]) for m in modules
                for item in m["ident"]
                if item["label"] == "VIN" and len(item["value"]) >= 11]
        vin_warning = ""
        if len({v for _, v in vins}) > 1:
            vin_warning = (
                " WARNING - VIN MISMATCH: "
                + ", ".join(f"{code}={v}" for code, v in vins)
                + " -- a control unit from another car is fitted (crash repair or "
                "clocking risk)."
            )
        vin = (vins[0][1] if vins else None) or obd_vin

        return {
            "ok": True, "simulated": False,
            "vehicle": {"vin": vin, "protocol": session.protocol or "ISO 15765-4 (CAN 11/500)",
                        "adapter": (adapter or {}).get("name")},
            "modules": modules,
            "message": (
                (f"VIN {vin}. " if vin else "")
                + f"{len(present)} module(s) answered, {faults} fault"
                f"{'s' if faults != 1 else ''} stored.{vin_warning} "
                "Read-only scan -- nothing was changed on the car."
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
    """Clear fault memory on one module (address like "0x710") or all present."""
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
                "message": f"Bad module address '{address}' (expected hex like 0x710).",
                "timestamp": _now(),
            }

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "cleared": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k CAN needed to clear VW/Audi module faults."
                ),
                "timestamp": _now(),
            }

        client = Elm327UdsClient(session, VW_ID_MAP)
        client.begin()
        cleared, errors = [], []
        try:
            if wanted is not None:
                targets = [wanted]
            else:
                targets = [rid for rid, _, _, _, _ in VW_ECU_TABLE
                           if _probe(client, rid, [])]
            for rid in targets:
                # UDS 14 FF FF FF (ClearDiagnosticInformation, all groups).
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
            "ident": ident or [], "dtcs": dtcs or [], "error": None, "trace": [],
        }

    modules = [
        mod("0x7E0", "0x7E8", "01", "Engine control (ECM)", "Engine / injection (petrol or diesel)",
            True, ident=[{"label": "VIN", "value": "SIMULATEDVWGOLF00"},
                         {"label": "VAG part number", "value": "04E906027AB"},
                         {"label": "Software", "value": "0004"}],
            dtcs=[
                {"code": "P0300", "hex": "030000", "failureType": "00",
                 "status": "2F", "flags": ["test failed", "confirmed"]},
                {"code": "P0171", "hex": "017100", "failureType": "00",
                 "status": "08", "flags": ["confirmed"]},
            ]),
        mod("0x710", "0x77A", "19", "Gateway (J533)", "CAN gateway / central diagnostic node",
            True, ident=[{"label": "VAG part number", "value": "5Q0907530AB"}]),
        mod("0x714", "0x77E", "17", "Instrument cluster", "Dashboard cluster",
            True, ident=[{"label": "VIN", "value": "SIMULATEDVWGOLF00"}]),
        mod("0x713", "0x77D", "03", "ABS / brakes (ESP)", "ABS anti-lock + ESP stability",
            True),
        mod("0x715", "0x77F", "15", "Airbag / SRS", "Restraint / airbag control",
            True, dtcs=[{"code": "B1004", "hex": "100400", "failureType": "00",
                         "status": "08", "flags": ["confirmed"]}]),
        mod("0x746", "0x7B0", "08", "Climate / A/C", "Air conditioning / heating", False),
        mod("0x72A", "0x794", "—", "SCR / AdBlue", "Diesel selective catalytic reduction (if fitted)",
            False),
    ]
    return {
        "ok": True, "simulated": True,
        "vehicle": {"vin": "SIMULATEDVWGOLF00", "protocol": "ISO 15765-4 (CAN 11/500)",
                    "adapter": "Simulated"},
        "modules": modules,
        "message": "SIMULATED -- VIN SIMULATEDVWGOLF00. 5 modules answered, 3 faults stored. "
                   "No real car connected.",
        "timestamp": _now(),
    }
