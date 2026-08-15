"""JLR diagnostic-address discovery over a generic OBD-II dongle.

The shipped table (scan.JLR_ECU_TABLE) is the three publicly-VERIFIED driveline
rows -- engine 0x7E0, ZF gearbox 0x7E1, rear e-diff 0x795 -- captured on
X351/XK petrols. Everything else on the reachable HS-CAN (ABS, EPB, and the
question that started the X260 AdBlue job: is there a separate reagent/SCR
dosing controller?) has an UNKNOWN request id (research §2b: "probe-sweep on
the first real car"). This module IS that probe-sweep.

Method (the Vauxhall-discover pattern, JLR edition): walk the 0x700-0x7FF
request-id block one id at a time with a quick presence probe (3E 00, then
22 F190 -- the same ladder style as scan.py), and for anything that answers,
read identification over UDS 0x22 (the scan.py IDENT_DIDS table). The KWP 1A
ident rung scan.py falls back to is deliberately NOT walked here -- this
sweep sends only 3E 00 and 22 xx -- so an older Ford-era module that refuses
every UDS read is reported honestly as "unproven" rather than probed in its
own dialect. JLR's verified
convention is response = request + 8 for every module (research §2a), so no
id_map is needed. Eight ids at the top of the block (0x7F8-0x7FF) cannot be
JLR request ids at all -- their +8 response would fall outside the 11-bit id
space -- and are skipped with a note rather than probed with an impossible
receive filter.

COST, stated honestly: ~248 short probes. A silent address costs one short
timeout, so expect roughly 2-5 minutes over a Bluetooth dongle (the result
repeats the estimate). Silent ids get the cheap probe only -- never the full
identification ladder -- so the walk stays proportionate.

Read-only: presence probes and identification reads only. No fault services,
no sessions beyond what ident needs, no writes. Body modules on MS-CAN and
MY17+ DoIP cars are as unreachable here as they are to scan() -- those walls
are reported, not presented as faults.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient
from backend.profiles.jlr.scan import IDENT_DIDS, JLR_ECU_TABLE, _printable

# The swept request-id block. 0x7E0/0x7E1/0x795 -- the known table -- sit
# inside it, so the walk re-probes them anyway and they get their table name
# in the result (that is the "verify the X260 PCM/TCM addresses" half of the
# job). Response = request + 8 throughout (research §2a).
SWEEP_FIRST = 0x700
SWEEP_LAST = 0x7FF

# A request id whose +8 response would leave the 11-bit id space cannot be a
# JLR module address under the verified convention -- skip, don't mis-probe.
def _sweepable(req_id: int) -> bool:
    return req_id + 8 <= 0x7FF


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _quick_probe(client, req_id: int) -> Optional[str]:
    """Cheap presence ladder for the walk: 3E 00, then 22 F190.

    Same two-rung style as scan._probe but with sweep timeouts -- a silent
    address must cost one short timeout, not a full ident ladder. Any reply,
    positive or negative, proves an ECU lives at the address; the label of
    the proving request is returned, or None for silence."""
    for data, label in ((bytes([0x3E, 0x00]), "3E 00"),
                        (bytes([0x22, 0xF1, 0x90]), "22 F190")):
        try:
            reply = client.request(req_id, data, timeout=0.4)
        except (ObdError, TransportError):
            continue
        if reply:
            return label
    return None


def _read_ident_uds(client, req_id: int) -> List[dict]:
    """Best-effort identification over UDS 0x22 only (scan.py's IDENT_DIDS).
    The KWP 1A fallback is scan.py's, not this sweep's -- see the module
    docstring -- so a module that refuses every UDS read simply returns []."""
    out: List[dict] = []
    for did, label in IDENT_DIDS:
        try:
            payload = client.request(
                req_id, bytes([0x22, did >> 8, did & 0xFF]), timeout=1.5)
        except (ObdError, TransportError):
            continue
        if len(payload) < 4 or payload[0] != 0x62:
            continue
        value = _printable(payload[3:])
        if value:
            out.append({"label": label, "value": value})
    return out


def _dialect_of(ident: List[dict]) -> str:
    """What identification proved. Only a positive 0x22 read proves UDS;
    presence without it is reported as unproven, never guessed."""
    return "uds" if ident else "unproven"


def _readings(found: List[dict]) -> List[dict]:
    """The findings as generic name/value chips -- the shape the desktop panel
    renders for any brand action, so discovery needs no front-end code."""
    readings = []
    for f in found:
        vin = next((i["value"] for i in f.get("ident", []) if i["label"] == "VIN"), "")
        parts = [f["dialect"].upper()]
        parts.append(f["known"] if f.get("known") else "not in the shipped table")
        if vin:
            parts.append(f"VIN {vin}")
        readings.append({
            "name": f"{f['address']} → {f['response']}",
            "value": " · ".join(parts),
            "unit": "",
            "description": f"Proved by {f.get('provedBy') or 'a probe'} answering.",
        })
    return readings


def discover(adapter: Optional[dict], mode: str = "real") -> dict:
    """Sweep the JLR PT-CAN request-id block and report what answers.

    Purpose: find the SCR/reagent controller if the X260 has a separate one,
    and verify the PCM/TCM addresses the shipped table claims. Every id in
    0x700-0x7FF (minus the eight a +8 response makes impossible) gets a quick
    presence probe; whatever answers gets the UDS identification reads.
    Read-only -- presence probes and identification reads only, no fault
    services, no writes.
    """
    if mode == "sim":
        # A fake X260-shaped bus: PCM + TCM on their table addresses, plus one
        # unexpected UDS answerer at 0x730 to show what discovery is FOR.
        found = [
            {"address": "0x730", "response": "0x738", "dialect": "uds",
             "provedBy": "3E 00",
             "ident": [{"label": "Part number", "value": "XR8Q-5K698-BC"}],
             "known": ""},
            {"address": "0x7E0", "response": "0x7E8", "dialect": "uds",
             "provedBy": "3E 00",
             "ident": [{"label": "VIN", "value": "SIMULATEDX260DIE"},
                       {"label": "Part number", "value": "AA52-12A650-EC"}],
             "known": "PCM (Engine control (ECM/PCM))"},
            {"address": "0x7E1", "response": "0x7E9", "dialect": "uds",
             "provedBy": "22 F190",
             "ident": [{"label": "VIN", "value": "SIMULATEDX260DIE"}],
             "known": "TCM (Transmission (ZF 6HP/8HP))"},
        ]
        return {
            "ok": True, "simulated": True, "sweptAddresses": 248, "skipped": 8,
            "found": found, "readings": _readings(found),
            "message": "SIMULATED -- swept 248 addresses, 3 answered: the PCM and TCM "
                       "on their table addresses plus one unexpected answerer at "
                       "0x730 (not in the shipped table -- the SCR/reagent question "
                       "this tool exists to answer). No real car connected.",
            "timestamp": _now(),
        }

    known = {addr: f"{code} ({name})" for addr, code, name, _ in JLR_ECU_TABLE}

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "sweptAddresses": 0, "skipped": 0,
                "found": [], "readings": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not "
                    "the 11-bit/500k CAN the JLR address sweep needs."
                ),
                "timestamp": _now(),
            }

        client = Elm327UdsClient(session)   # JLR: response = request + 8, no map
        client.begin()
        found: List[dict] = []
        swept = 0
        skipped = 0
        try:
            for req_id in range(SWEEP_FIRST, SWEEP_LAST + 1):
                if not _sweepable(req_id):
                    skipped += 1
                    continue
                swept += 1
                proved_by = _quick_probe(client, req_id)
                if proved_by is None:
                    continue
                # Only an answering address pays for identification.
                ident = _read_ident_uds(client, req_id)
                found.append({
                    "address": f"0x{req_id:03X}",
                    "response": f"0x{req_id + 8:03X}",
                    "dialect": _dialect_of(ident),
                    "provedBy": proved_by,
                    "ident": ident,
                    "known": known.get(req_id, ""),
                })
        finally:
            client.restore()

        extras = [f["address"] for f in found if not f["known"]]
        message = (
            f"Swept {swept} addresses ({skipped} skipped -- a +8 response would leave "
            f"the 11-bit id space), {len(found)} answered. "
            + (f"Not in the shipped table: {', '.join(extras)} -- candidates for the "
               "SCR/reagent controller or the never-published HS-CAN modules; "
               "identify before trusting. " if extras else "")
            + "Expect this sweep to take roughly 2-5 minutes over a Bluetooth dongle "
              "(each silent address costs one short timeout). Read-only sweep; "
              "nothing was changed on the car."
        )
        return {
            "ok": True, "simulated": False, "sweptAddresses": swept,
            "skipped": skipped, "found": found, "readings": _readings(found),
            "message": message, "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {
            "ok": False, "simulated": False, "sweptAddresses": 0, "skipped": 0,
            "found": [], "readings": [], "message": str(exc), "timestamp": _now(),
        }
