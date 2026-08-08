"""Vauxhall / Opel (GM Global A) module scan over a generic OBD-II dongle.

Generic OBD-II (backend/obd/service.py) only sees the engine's emissions data.
This walks the manufacturer-level modules a Global A car exposes on High Speed
GMLAN and reads each responding module's fault memory + identification -- the
same idea as the Mazda, Peugeot and VAG dongle scans, with two Vauxhall-shaped
differences (docs/private/vauxhall-astra-k-research.md §2, §3):

* **Two address blocks.** Powertrain sits in the legislated ISO 15765-4 block
  (ECM 0x7E0/0x7E8, TCM 0x7E1/0x7E9, response = request + 8). Body and chassis
  modules use GM's own block at 0x24x-0x25x and answer on **request + 0x400**
  (BCM 0x241/0x641, ABS 0x243/0x643, cluster 0x24C/0x64C, ...). Elm327UdsClient
  is handed VAUXHALL_ID_MAP so it sets the right receive filter per module.
* **Two dialects.** Global A speaks GMW3110, not UDS: fault codes are 0xA9,
  clear is 0x04, identification is 0x1A. Some late Global A nodes also answer
  plain UDS. Neither is assumed -- every module is tried UDS-first (one cheap
  request) then GMLAN, and the scan records which dialect answered so the
  result is evidence, not a guess.

SCOPE. Built for the GM Global A era: Astra K, Insignia B, Mokka X, Zafira C,
roughly 2010-2019. Two honest limits ride on every result:

* **Low Speed GMLAN is unreachable.** GM's single-wire 33.3 kbit/s bus (pin 1)
  carries much of the comfort/body traffic and no standard OBD dongle touches
  it -- the same wall the Mazda profile hits with MS-CAN.
* **2019+ Vauxhalls are not GM.** Corsa F, Mokka B, Astra L and the
  Crossland/Grandland are PSA/Stellantis platforms. This profile will find
  little on them; the Peugeot profile is the right one.

The module address table below is **GM-Global-A-generic, not model-verified**:
no public source maps the Astra K's own module list. That is what discover()
is for -- it sweeps both blocks on the actual car and reports what answers.
Treat a table row that stays silent as "not at that address on this model",
not as a fault.

Read-only apart from clear(), which the API layer guards behind a confirm.
"""

import re
from datetime import datetime, timezone
from typing import List, Optional

from backend.core import uds
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

from . import gmlan

# --------------------------------------------------------------------------- #
# Address map. Each row is (request_id, response_id, code, name, description).
# ``code`` is the address in the form a GM tester shows it.
#
# Powertrain rows follow ISO 15765-4 (response = request + 8). Every 0x24x row
# follows GM's +0x400 reply rule and takes its name from the public GMLAN
# 11-bit id header (research §2). "(if fitted)" rows simply time out on cars
# without them, which is harmless.
# --------------------------------------------------------------------------- #
VAUXHALL_ECU_TABLE = [
    (0x7E0, 0x7E8, "7E0", "Engine control (ECM)", "Engine management"),
    (0x7E1, 0x7E9, "7E1", "Transmission (TCM)", "Automatic gearbox control (if fitted)"),
    (0x7E2, 0x7EA, "7E2", "Powertrain 2", "Second powertrain node (if fitted)"),
    (0x7E3, 0x7EB, "7E3", "Powertrain 3", "Third powertrain node (if fitted)"),
    (0x241, 0x641, "241", "Body control (BCM)", "Central body electrics"),
    (0x242, 0x642, "242", "Theft deterrent (TDM)", "Immobiliser / theft deterrent"),
    (0x243, 0x643, "243", "ABS / ESC (EBCM)", "Anti-lock brakes + stability control"),
    (0x244, 0x644, "244", "Infotainment (EHU)", "Radio / head unit (if fitted)"),
    (0x246, 0x646, "246", "Serial data interface (SIC)", "Comms interface module (if fitted)"),
    (0x247, 0x647, "247", "Restraints (SDM/SDC)", "Airbag / restraints control"),
    (0x24C, 0x64C, "24C", "Instrument cluster (IPC)", "Dashboard cluster"),
    (0x251, 0x651, "251", "Climate (HVAC)", "Air conditioning / heating (if fitted)"),
    (0x258, 0x658, "258", "Remote function (RFA)", "Remote locking / keyless (if fitted)"),
]

# request id -> response id, handed to Elm327UdsClient so it sets the correct
# ATCRA per module. Without this the 0x24x block is invisible: those modules do
# NOT answer on request + 8.
VAUXHALL_ID_MAP = {tx: rx for tx, rx, _, _, _ in VAUXHALL_ECU_TABLE}

# The engine also owns the standard OBD slot; 01 00 there is the one request an
# emissions ECU must answer with the ignition on.
ENGINE = 0x7E0

# Address ranges discover() sweeps, as (first, last, response_offset, label).
DISCOVERY_RANGES = [
    (0x7E0, 0x7E7, 0x008, "powertrain (ISO 15765-4, reply +8)"),
    (0x240, 0x25F, 0x400, "GMLAN body/chassis (reply +0x400)"),
]

# Standard UDS identification DIDs, tried on the UDS-dialect path only.
UDS_IDENT_DIDS = [
    (0xF190, "VIN"),
    (0xF188, "Software calibration"),
    (0xF187, "Part number"),
    (0xF191, "Hardware"),
    (0xF194, "Software"),
    (0xF18C, "ECU serial"),
]

# UDS reportDTCByStatusMask masks, laddered; same rationale as the VAG scan.
UDS_DTC_MASKS = (0xFF, 0x08, 0xAF)
UDS_MASK_RETRY_NRCS = (0x12, 0x31)

_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")

_WRONG_BUS = (
    "the 11-bit/500k High Speed GMLAN this scan needs. Turn the ignition on and "
    "retry. If this is a 2019+ Corsa/Mokka/Astra L it is a PSA/Stellantis car, "
    "not a GM one -- use the Peugeot profile."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _printable(body: bytes) -> str:
    text = "".join(chr(b) for b in body if 32 <= b < 127).strip()
    if text and len(text) >= (len(body) + 1) // 2:
        return text
    return body.hex().upper()


# Presence probes, in cost order. Each row is (request, label, dialect a
# POSITIVE reply proves). Most diagnostic services exist in both dialects and
# therefore prove only that something is alive at the address:
#
#   3E 00    TesterPresent -- service 0x3E is in UDS and GMW3110 alike
#   22 F190  service 0x22 is ReadDataByIdentifier in UDS and
#            ReadDataByParameterIdentifier in GMW3110 -- same id, both dialects
#
# Only 0x1A and 0xA9 are GMW3110-only, so only a POSITIVE reply to one of those
# proves the dialect. Nothing here proves UDS: the one UDS-only read service is
# 0x19, and spending a fault read on every swept address to find out would be
# expensive. So probing establishes presence, and _read_module() settles the
# dialect by which reader actually worked. GMLAN is tried first throughout --
# it is the expected dialect on Global A.
PRESENCE_PROBES = [
    (bytes([0x3E, 0x00]), "3E 00", None),
    (bytes([0x1A, 0x90]), "1A 90", "gmlan"),
    (bytes([0x22, 0xF1, 0x90]), "22 F190", None),
    (bytes([0xA9, 0x82, 0xFF]), "A9 82 FF", "gmlan"),
]


def _probe(client: Elm327UdsClient, req_id: int, trace: List[str]) -> Optional[tuple]:
    """Is anything alive here? -> (proving request, dialect proven or None).

    A negative response counts as presence -- something decoded the frame and
    chose to refuse it -- but NOT as dialect proof, since a module refuses an
    unknown service in whatever dialect it does speak.
    """
    presence = None
    for data, label, proves in PRESENCE_PROBES:
        try:
            reply = client.request(req_id, data, timeout=1.0)
        except (ObdError, TransportError):
            continue
        if not reply:
            continue
        if presence is None:
            presence = label
        if proves is not None and reply[0] == (data[0] | 0x40):
            trace.append(f"{label} -> positive, {proves.upper()} dialect proven")
            return label, proves
    if presence is not None:
        trace.append(f"{presence} -> answered (dialect not proven by probing)")
        return presence, None
    trace.append("3E 00 / 1A 90 / 22 F190 / A9 82 -> silence")
    return None


# --------------------------------------------------------------------------- #
# UDS-dialect half (the GMLAN half lives in gmlan.py)
# --------------------------------------------------------------------------- #
def _uds_read_ident(client: Elm327UdsClient, req_id: int, trace: List[str]) -> List[dict]:
    out: List[dict] = []
    for did, label in UDS_IDENT_DIDS:
        try:
            reply = client.request(req_id, bytes([0x22, did >> 8, did & 0xFF]), timeout=2.0)
        except (ObdError, TransportError):
            continue
        if len(reply) < 4 or reply[0] != 0x62:
            continue
        value = _printable(reply[3:])
        if value:
            out.append({"label": label, "value": value, "dialect": "uds"})
    return out


def _uds_read_dtcs(client: Elm327UdsClient, req_id: int, trace: List[str]) -> List[dict]:
    """One pass of the UDS 19 02 mask ladder. Raises so the caller can fall
    through to the GMLAN dialect, which is the expected path on Global A."""
    last_error = None
    payload = None
    for mask in UDS_DTC_MASKS:
        reply = client.request(req_id, bytes([0x19, 0x02, mask]), timeout=3.0)
        if reply and reply[0] == 0x59:
            trace.append(f"19 02 {mask:02X} -> OK")
            payload = reply
            break
        if reply and reply[0] == 0x7F and len(reply) >= 3:
            last_error = uds.UdsNegativeResponse(0x19, reply[2])
            if reply[2] in UDS_MASK_RETRY_NRCS:
                continue
            raise last_error
    if payload is None:
        if last_error is not None:
            raise last_error
        raise ObdError("UDS fault read returned nothing usable.", kind="no_data")

    availability = payload[2] if len(payload) >= 3 else 0xFF
    records = payload[3:]
    dtcs = []
    for i in range(0, len(records) - 3, 4):
        b0, b1, b2, status = records[i : i + 4]
        if b0 == b1 == b2 == 0:
            continue
        entry = gmlan.format_dtc(b0, b1, b2)
        entry["status"] = f"{status:02X}"
        entry["statusAvailability"] = f"{availability:02X}"
        entry["flags"] = uds.status_flags(status, availability)
        entry["state"] = uds.fault_state(status, availability)
        entry["dialect"] = "uds"
        dtcs.append(entry)
    return dtcs


# --------------------------------------------------------------------------- #
# The ladder: GMW3110 first (the expected Global A dialect), then UDS.
# --------------------------------------------------------------------------- #
def _dialect_order(hint: Optional[str]) -> tuple:
    return ("uds", "gmlan") if hint == "uds" else ("gmlan", "uds")


def _read_ident(client: Elm327UdsClient, req_id: int, hint: Optional[str],
                trace: List[str]) -> tuple:
    """(ident, dialect that produced it) -- [] and None if neither answered."""
    for which in _dialect_order(hint):
        reader = _uds_read_ident if which == "uds" else gmlan.read_ident
        try:
            ident = reader(client, req_id, trace)
        except (ObdError, TransportError):
            continue
        if ident:
            return ident, which
    return [], None


def _read_module(client: Elm327UdsClient, req_id: int, hint: Optional[str],
                 trace: List[str]) -> tuple:
    """(ident, dtcs, dialect, error) for one present module.

    ``hint`` is whatever the presence probe managed to prove, which is usually
    nothing -- see PRESENCE_PROBES. So the reported dialect is not a guess
    carried forward from probing: it is the dialect whose FAULT read actually
    succeeded, falling back to whichever produced identification if neither
    fault service worked. A module whose dialect stays unproven says so.
    """
    ident, ident_dialect = _read_ident(client, req_id, hint, trace)

    error = None
    for which in _dialect_order(hint):
        reader = _uds_read_dtcs if which == "uds" else gmlan.read_dtcs
        try:
            return ident, reader(client, req_id, trace), which, None
        except uds.UdsNegativeResponse as exc:
            error = (f"{which.upper()} fault read refused: NRC {exc.nrc:02X} "
                     f"({gmlan.nrc_name(exc.nrc)})")
        except (ObdError, TransportError) as exc:
            error = f"{which.upper()} fault read failed: {exc}"
    return ident, [], ident_dialect, error


# --------------------------------------------------------------------------- #
# discover() -- build a module map for a model nobody has published one for
# --------------------------------------------------------------------------- #
def _discovery_readings(found: List[dict]) -> List[dict]:
    """The sweep's findings in the generic name/value shape the desktop panel
    renders for any brand action -- one chip per answering module."""
    readings = []
    for f in found:
        vin = next((i["value"] for i in f.get("ident", []) if i["label"] == "VIN"), "")
        parts = [f["dialect"].upper()]
        if f.get("known"):
            parts.append(f["known"])
        else:
            parts.append("not in the shipped table")
        if vin:
            parts.append(f"VIN {vin}")
        readings.append({
            "name": f"{f['address']} → {f['response']}",
            "value": " · ".join(parts),
            "unit": "",
            "description": f"Proved by {f.get('provedBy') or 'a probe'} on the "
                           f"{f.get('block', 'diagnostic')} block.",
        })
    return readings


def discover(adapter: Optional[dict], mode: str = "real") -> dict:
    """Sweep both GMLAN diagnostic address blocks and report what answers.

    The shipped table is Global-A-generic. This is how it gets replaced by
    evidence for a specific model: every address in 0x7E0-0x7E7 and
    0x240-0x25F is probed in both dialects, and whatever answers is reported
    with the request that proved it and any name it gives for itself.
    Read-only -- presence probes and identification reads only, no fault
    services, no writes.

    The result carries the findings twice: ``found`` is the structured record
    (what a follow-up commit to VAUXHALL_ECU_TABLE would be written from), and
    ``readings`` is the same thing in the generic name/value shape the desktop
    panel renders for any brand action, so this needs no front-end code.
    """
    if mode == "sim":
        found = [
            {"address": "0x7E0", "response": "0x7E8", "block": DISCOVERY_RANGES[0][3],
             "dialect": "gmlan", "provedBy": "1A 90",
             "ident": [{"label": "VIN", "value": "SIMULATEDVAUXHALL"}],
             "known": "Engine control (ECM)"},
            {"address": "0x7E1", "response": "0x7E9", "block": DISCOVERY_RANGES[0][3],
             "dialect": "gmlan", "provedBy": "3E 00", "ident": [],
             "known": "Transmission (TCM)"},
            {"address": "0x241", "response": "0x641", "block": DISCOVERY_RANGES[1][3],
             "dialect": "gmlan", "provedBy": "3E 00", "ident": [],
             "known": "Body control (BCM)"},
        ]
        return {
            "ok": True, "simulated": True, "found": found,
            "readings": _discovery_readings(found),
            "message": "SIMULATED -- no real car connected.",
            "timestamp": _now(),
        }

    known = {tx: name for tx, _, _, name, _ in VAUXHALL_ECU_TABLE}

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "found": [], "readings": [],
                "message": f"The car answered {session.protocol or 'an unknown protocol'}, not "
                           + _WRONG_BUS,
                "timestamp": _now(),
            }

        # Build the sweep's id map up front -- the client needs the right
        # receive filter for every address it will aim at, not just table rows.
        id_map = {}
        for first, last, offset, _ in DISCOVERY_RANGES:
            for req_id in range(first, last + 1):
                id_map[req_id] = req_id + offset

        client = Elm327UdsClient(session, id_map)
        client.begin()
        found: List[dict] = []
        swept = 0
        try:
            for first, last, offset, label in DISCOVERY_RANGES:
                for req_id in range(first, last + 1):
                    swept += 1
                    trace: List[str] = []
                    hit = _probe(client, req_id, trace)
                    if hit is None:
                        continue
                    proved_by, hint = hit
                    # No fault services here -- the sweep stays cheap and stays
                    # a presence/identification pass. The dialect is whatever
                    # identification proved, or "unproven" if only a shared
                    # service answered; the full scan settles it properly.
                    ident, ident_dialect = _read_ident(client, req_id, hint, trace)
                    found.append({
                        "address": f"0x{req_id:03X}",
                        "response": f"0x{req_id + offset:03X}",
                        "block": label,
                        "dialect": hint or ident_dialect or "unproven",
                        "provedBy": proved_by,
                        "ident": ident,
                        "known": known.get(req_id, ""),
                    })
        finally:
            client.restore()

        extras = [f["address"] for f in found if not f["known"]]
        message = (
            f"Swept {swept} addresses, {len(found)} answered. "
            + (f"Not in the shipped table: {', '.join(extras)} -- worth adding once "
               "identified. " if extras else "")
            + "Read-only sweep; nothing was changed on the car."
        )
        return {
            "ok": True, "simulated": False, "found": found,
            "readings": _discovery_readings(found),
            "sweptAddresses": swept, "message": message, "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "found": [], "readings": [],
                "message": str(exc), "timestamp": _now()}


# --------------------------------------------------------------------------- #
# scan() / clear()
# --------------------------------------------------------------------------- #
def scan(adapter: Optional[dict], mode: str = "real") -> dict:
    """Walk the Vauxhall's modules and read each one's fault memory. Read-only."""
    if mode == "sim":
        return _simulated_scan()

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False,
                "vehicle": {"vin": None, "protocol": session.protocol,
                            "adapter": (adapter or {}).get("name")},
                "modules": [],
                "message": f"The car answered {session.protocol or 'an unknown protocol'}, not "
                           + _WRONG_BUS,
                "timestamp": _now(),
            }

        # The car's identity first, while the receive filter is still open:
        # Mode 09 VIN works on any ~2001+ car whatever the dialect does next.
        obd_vin = obd_service._read_vin(session)

        client = Elm327UdsClient(session, VAUXHALL_ID_MAP)
        client.begin()
        modules: List[dict] = []
        dialects = set()
        try:
            for req_id, resp_id, code, name, desc in VAUXHALL_ECU_TABLE:
                trace: List[str] = []
                mod = {
                    "address": f"0x{req_id:03X}", "response": f"0x{resp_id:03X}",
                    "code": code, "name": name, "description": desc,
                    "present": False, "dialect": None, "ident": [], "dtcs": [],
                    "error": None, "trace": trace,
                }
                hit = _probe(client, req_id, trace)
                if hit is None and req_id == ENGINE:
                    # Mode 01 PID 00 on the standard OBD slot: the one request
                    # an emissions ECU must answer with the ignition on.
                    try:
                        if client.request(ENGINE, bytes([0x01, 0x00]), timeout=2.0):
                            hit = ("01 00", None)
                            trace.append("answered on standard OBD 7E0 (01 00)")
                    except (ObdError, TransportError):
                        pass
                if hit is None:
                    modules.append(mod)
                    continue
                mod["present"] = True
                ident, dtcs, dialect, error = _read_module(client, req_id, hit[1], trace)
                mod["ident"], mod["dtcs"], mod["error"] = ident, dtcs, error
                mod["dialect"] = dialect or "unproven"
                if dialect:
                    dialects.add(dialect)
                modules.append(mod)
        finally:
            client.restore()

        present = [m for m in modules if m["present"]]
        faults = sum(len(m["dtcs"]) for m in present)

        # Dealer check: every module that knows its VIN should agree. A module
        # reporting a different VIN was swapped in from another car.
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

        if not present:
            spoken = "Nothing answered on either dialect."
        elif not dialects:
            spoken = ("Modules answered but none completed a read in either dialect -- "
                      "check the trace on each.")
        elif dialects == {"gmlan"}:
            spoken = "Reads were GMW3110 (GMLAN) -- the expected Global A dialect."
        elif dialects == {"uds"}:
            spoken = "Reads were plain UDS."
        else:
            spoken = "Mixed dialects: some modules read over UDS, some over GMW3110."

        return {
            "ok": True, "simulated": False,
            "vehicle": {"vin": vin, "protocol": session.protocol or "ISO 15765-4 (CAN 11/500)",
                        "adapter": (adapter or {}).get("name")},
            "modules": modules,
            "message": (
                (f"VIN {vin}. " if vin else "")
                + f"{len(present)} module(s) answered, {faults} fault"
                f"{'s' if faults != 1 else ''} stored. {spoken}{vin_warning} "
                "Faults are shown GM-style as code + failure type. Addresses that "
                "stayed silent are not fitted at that address on this model -- run "
                "'Find modules' to map the car for certain. "
                "Read-only scan -- nothing was changed on the car."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "vehicle": None, "modules": [],
                "message": str(exc), "timestamp": _now()}


def clear(adapter: Optional[dict], mode: str = "real", address: Optional[str] = None) -> dict:
    """Clear fault memory on one module (address like "0x7E1") or all present.

    Tries UDS 14 FF FF FF then GMLAN 04. Note the difference: GMLAN's clear
    takes no group argument, so on a GMW3110 module this wipes that module's
    ENTIRE fault memory -- there is no "emissions only" option.
    """
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
            return {"ok": False, "simulated": False, "cleared": [],
                    "message": f"Bad module address '{address}' (expected hex like 0x7E1).",
                    "timestamp": _now()}

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {"ok": False, "simulated": False, "cleared": [],
                    "message": f"The car answered {session.protocol or 'an unknown protocol'}, not "
                               + _WRONG_BUS,
                    "timestamp": _now()}

        client = Elm327UdsClient(session, VAUXHALL_ID_MAP)
        client.begin()
        cleared, errors = [], []
        try:
            if wanted is not None:
                targets = [wanted]
            else:
                targets = [rid for rid, _, _, _, _ in VAUXHALL_ECU_TABLE
                           if _probe(client, rid, []) is not None]
            for rid in targets:
                last = None
                for attempt in (uds.clear_dtcs, gmlan.clear_dtcs):
                    try:
                        attempt(client, rid)
                        cleared.append(f"0x{rid:03X}")
                        last = None
                        break
                    except Exception as exc:
                        last = exc
                if last is not None:
                    errors.append(f"0x{rid:03X}: {last}")
        finally:
            client.restore()

        ok = bool(cleared) and not errors
        message = f"Cleared fault memory on {', '.join(cleared) or 'no modules'}."
        if errors:
            message += " Failed: " + "; ".join(errors)
        return {"ok": ok, "simulated": False, "cleared": cleared,
                "message": message, "timestamp": _now()}

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "cleared": [],
                "message": str(exc), "timestamp": _now()}


def _simulated_scan() -> dict:
    def mod(addr, resp, code, name, desc, present, dialect=None, ident=None, dtcs=None):
        return {
            "address": addr, "response": resp, "code": code, "name": name,
            "description": desc, "present": present, "dialect": dialect,
            "ident": ident or [], "dtcs": dtcs or [], "error": None, "trace": [],
        }

    def fault(code, hexval, ftype, status, flags):
        return {"code": code, "hex": hexval, "failureType": ftype,
                "display": f"{code}-{ftype}", "status": status, "flags": flags,
                "dialect": "gmlan", "recordLayout": "inferred"}

    modules = [
        mod("0x7E0", "0x7E8", "7E0", "Engine control (ECM)", "Engine management", True,
            dialect="gmlan",
            ident=[{"label": "VIN", "value": "SIMULATEDVAUXHALL", "dialect": "gmlan"}]),
        mod("0x7E1", "0x7E9", "7E1", "Transmission (TCM)",
            "Automatic gearbox control (if fitted)", True, dialect="gmlan",
            dtcs=[fault("P0776", "077600", "00", "08", ["confirmed"])]),
        mod("0x241", "0x641", "241", "Body control (BCM)", "Central body electrics",
            True, dialect="gmlan"),
        mod("0x243", "0x643", "243", "ABS / ESC (EBCM)",
            "Anti-lock brakes + stability control", True, dialect="gmlan"),
        mod("0x247", "0x647", "247", "Restraints (SDM/SDC)", "Airbag / restraints control",
            True, dialect="gmlan"),
        mod("0x24C", "0x64C", "24C", "Instrument cluster (IPC)", "Dashboard cluster",
            True, dialect="gmlan"),
        mod("0x251", "0x651", "251", "Climate (HVAC)",
            "Air conditioning / heating (if fitted)", False),
    ]
    return {
        "ok": True, "simulated": True,
        "vehicle": {"vin": "SIMULATEDVAUXHALL", "protocol": "ISO 15765-4 (CAN 11/500)",
                    "adapter": "Simulated"},
        "modules": modules,
        "message": "SIMULATED -- VIN SIMULATEDVAUXHALL. 6 modules answered, 1 fault stored "
                   "(P0776-00 on the TCM). No real car connected.",
        "timestamp": _now(),
    }
