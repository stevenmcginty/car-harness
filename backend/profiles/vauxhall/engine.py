"""Astra J VXR (A20NFT / E39A) -- engine diagnostic over a Veepeak / ELM327.

This is the engine-side twin of gearbox.py: find the ECM, read its
identification, read its faults with GM's own wording, read a curated live
block, and turn that into findings. It does not code, it does not clear, and
it does not actuate.

WHAT THE CAR HAS SHOWN SO FAR: NOTHING
--------------------------------------
This is the first thing to say and the most important. gearbox.py can point at
a VIN and a verified address because somebody plugged into that gearbox and
watched values move. Nothing in this file has that. The Astra VXR has not been
connected. Every parameter below is named, scaled and addressed out of GM's own
GDS2 dealer database and has never been asked of an engine, so the correct
reading of any number this produces is "this is what GDS2 would call that byte",
not "this is what the engine said".

That is not a reason to wait. It is a reason to build the run that settles it,
which is what the applicability report in the result is for: which requests
answered, which came back NRC 31, which went silent. On a first real run that
split is worth more than any individual reading, because it is the only thing
that can turn GM's shared parameter list into this engine's parameter list.

WHERE THE NUMBERS AND NAMES COME FROM
-------------------------------------
* **The data.** data/gds2_e39a_ecm.json, the module node
  ``Engine Control Module$ECM - E39A (S3) - GME`` out of the 2023.3
  Opel/Vauxhall deliverable -- the node the Astra-J 2.0L (LHU) menu tree binds.
  970 parameters, 70 device controls, 61 named tests, the module's own 28-row
  DTC override list and 6,223 rows of GM's master fault dictionary. Read
  through dealer_data.py's ``ecm_*`` half. The strings VXR, OPC and A20NFT
  appear nowhere in the database; Astra-J plus 2.0L (LHU) plus E39A (S3) are
  the only handles there are.
* **Address.** 0x7E0, response 0x7E8, the legislated powertrain slot, which the
  pack's own connection record agrees with. That is the one address in this
  file nobody has to take on trust -- an emissions ECU has to be there.
* **Protocol.** GM Global A, so GMW3110 as well as UDS: faults 0xA9,
  identification 0x1A. Neither is assumed; scan.py's dialect ladder is reused
  as it stands and reports which one actually answered.
* **What is NOT in the data**, each carried in the pack as a machine-readable
  caveat and repeated in every result rather than summarised away:

  - the PID set is GM's SHARED "E39 4cyl" list, not an A20NFT list, so NRC 31
    on a fair number of these is the expected outcome and not a fault;
  - there are no pass/fail thresholds. The lo/hi on a conversion is the limit
    GDS2 draws its gauge between, and some are wrong -- Brake Booster Pressure
    scales to kPa and carries hi = 1. Nothing here judges a reading against
    them, and nothing here should be changed to;
  - there is no test-to-frame join anywhere in the database, so the 61 named
    tests are names. Pairing one to a parameter would be invention;
  - there are no security-access rows for this ECM.

THE 70 DEVICE CONTROLS
----------------------
The pack carries $AE DeviceControl frames for the wastegate, the bypass valve,
the cam actuators, the oil pressure solenoid, ignition retard and the crank
variation learn, each with its ready-made command bytes, plus Return To Normal
(``20``) and Null CPID (``AE 00``). They are listed in the result as
information -- what the dealer tool can command on this engine -- and this file
contains no code path that transmits one. Commanding an actuator on a running
engine is a different job with different gates, the way the T76 learn is in
procedures.py rather than in gearbox.py.

Read-only throughout. There is no clear, no coding and no adaptation reset in
this feature.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Callable, Dict, List, Optional

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

from . import dealer_data
from . import gmlan
from . import software
from .scan import (
    VAUXHALL_ID_MAP,
    _WRONG_BUS,
    _probe,
    _read_module,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CAPTURES_DIR = PROJECT_ROOT / "captures"

# The engine keeps the legislated powertrain slot. Unlike the TCM there is no
# second candidate to ladder: an emissions ECU that is not at 0x7E0 would not
# pass type approval. Kept as a tuple anyway so this file reads like its twin.
ECM_CANDIDATES = (
    (0x7E0, 0x7E8, "7E0", "Engine control (ECM) -- E39A (S3), GM Global A"),
)

# --------------------------------------------------------------------------- #
# The curated core block.
#
# 35 of the pack's 970, chosen for what actually decides a complaint on a
# direct-injection turbo petrol: what the engine is doing, what it WANTED to
# do, and the gap between the two. Desired-versus-actual pairs are the whole
# point -- boost, rail pressure, cam phase and throttle each appear twice for
# that reason, because a single number cannot tell a weak turbo from a turbo
# that was never asked for boost.
#
# Each row is (request, name, group). The name is the second half of the key
# because one request often carries several signals and, in this pack, several
# requests carry the same name. NOTHING about scaling, unit or length is
# written here: it is all looked up in the pack at read time, so this table
# cannot drift away from the data it indexes.
# --------------------------------------------------------------------------- #
CORE_BLOCK = [
    ("22 00 0C", "Engine Speed", "engine"),
    ("22 00 04", "Engine Load", "engine"),
    ("22 00 0D", "Vehicle Speed", "engine"),
    ("22 00 05", "ECT Sensor", "engine"),
    ("22 00 0F", "IAT Sensor", "engine"),
    ("22 11 54", "Calculated Engine Oil Temperature", "engine"),
    ("22 14 70", "Engine Oil Pressure Sensor", "engine"),
    ("22 00 10", "MAF Sensor", "air"),
    ("22 20 01", "Intake Manifold Pressure", "air"),
    ("22 12 B1", "Throttle Position", "air"),
    ("22 00 4C", "Desired Throttle Position", "air"),
    ("22 13 1F", "Accelerator Pedal Position", "air"),
    ("22 00 06", "Short Term Fuel Trim", "fuel"),
    ("22 00 07", "Long Term Fuel Trim", "fuel"),
    ("22 00 44", "Air/Fuel Equivalence Ratio Command", "fuel"),
    ("22 23 20", "HO2S 1", "fuel"),
    ("22 23 21", "HO2S 2", "fuel"),
    ("22 00 23", "Fuel Rail Pressure Sensor", "fuel"),
    ("22 30 8A", "Desired Fuel Rail Pressure", "fuel"),
    ("22 32 98", "Boost Pressure Sensor", "boost"),
    ("22 32 8A", "Desired Boost Pressure", "boost"),
    ("22 32 8B", "Turbocharger Wastegate Solenoid Valve Command", "boost"),
    ("22 32 8D", "Turbocharger Bypass Solenoid Valve Command", "boost"),
    ("22 00 0E", "Ignition Timing", "knock"),
    ("22 12 D9", "Total Knock Retard", "knock"),
    ("22 11 A6", "Knock Retard", "knock"),
    ("22 16 8E", "Intake Camshaft Position", "cam"),
    ("22 16 92", "Desired Intake Camshaft Position", "cam"),
    ("22 16 8D", "Exhaust Camshaft Position", "cam"),
    ("22 16 91", "Desired Exhaust Camshaft Position", "cam"),
    ("22 12 00", "Total Misfire", "misfire"),
    ("22 12 01", "Cylinder 1 History Misfire Counter", "misfire"),
    ("22 12 02", "Cylinder 2 History Misfire Counter", "misfire"),
    ("22 12 03", "Cylinder 3 History Misfire Counter", "misfire"),
    ("22 12 04", "Cylinder 4 History Misfire Counter", "misfire"),
]

# The subset a moving car is worth polling fast: the desired/actual pairs that
# only mean anything under load, plus the two speeds that put them in context.
# Everything else is read on the slow tick -- see capture().
FAST_BLOCK = [
    "Engine Speed", "Vehicle Speed", "Engine Load",
    "Boost Pressure Sensor", "Desired Boost Pressure",
    "Turbocharger Wastegate Solenoid Valve Command",
    "Fuel Rail Pressure Sensor", "Desired Fuel Rail Pressure",
    "Total Knock Retard", "Throttle Position",
]

# Desired/actual pairs, as (desired, actual, what the gap would mean). The
# wording stops at naming the divergence: the pack has no threshold that could
# say how big a gap is too big, so neither does this.
DIVERGENCE_PAIRS = [
    ("Desired Boost Pressure", "Boost Pressure Sensor",
     "the turbo is not delivering the boost the ECM asked for, or the boost "
     "sensor disagrees with it"),
    ("Desired Fuel Rail Pressure", "Fuel Rail Pressure Sensor",
     "the high-pressure fuel system is not holding the rail the ECM asked for"),
    ("Desired Intake Camshaft Position", "Intake Camshaft Position",
     "the intake cam phaser is not reaching its commanded angle"),
    ("Desired Exhaust Camshaft Position", "Exhaust Camshaft Position",
     "the exhaust cam phaser is not reaching its commanded angle"),
    ("Desired Throttle Position", "Throttle Position",
     "the throttle body is not following its command"),
]

MISFIRE_CYLINDERS = [
    ("Cylinder 1 History Misfire Counter", 1),
    ("Cylinder 2 History Misfire Counter", 2),
    ("Cylinder 3 History Misfire Counter", 3),
    ("Cylinder 4 History Misfire Counter", 4),
]

# GMLAN $1A identification, in the order a workshop wants it. The VIN is not
# here: gmlan.read_ident already asks for 1A 90 as one of the six identifiers
# whose meaning is certain, and asking twice would cost a round trip to learn
# nothing. What the pack adds is the rest of the calibration block -- the base
# model part number, the six software module identifiers, the calibration
# history buffer and the oil life counter.
#
# The requests come from the pack, where the exporter RECONSTRUCTED them rather
# than reading them out of the database, so this whole block is two steps away
# from evidence and is labelled that way wherever it is shown.
IDENT_REQUESTS = [
    "1A CB", "1A CC",
    "1A C1", "1A C2", "1A C3", "1A C4", "1A C5", "1A C6",
    "1A 29", "1A 6D",
]

IDENT_CAVEAT = (
    "GMLAN $1A identification. The pack's request bytes for these were "
    "reconstructed by the exporter, not read from the database, so an "
    "unanswered row here is at least as likely to be a wrong request as a "
    "missing identifier."
)

NRC_OUT_OF_RANGE = 0x31

# gmlan.read_ident reads $CB as printable text, which is the wrong reading for
# a 32-bit binary part number and comes out as punctuation. Where the dealer
# block below re-reads the same identifier and decodes it the way GM stores it,
# the raw row is dropped rather than shown next to it: two values for one
# identifier, one of them nonsense, is worse than one. Mapping is
# {gmlan.py's label: the dealer pack's name for the same identifier}.
IDENT_SUPERSEDED = {"End model part number": "End Model Part Number"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _printable(body: bytes) -> str:
    """Text if it reads as text, otherwise hex. Same rule as the GMLAN scan."""
    text = "".join(chr(b) for b in body if 32 <= b < 127).strip()
    if text and len(text) >= (len(body) + 1) // 2:
        return text
    return body.hex().upper()


def _frame(request: str) -> bytes:
    return bytes.fromhex(request.replace(" ", ""))


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def _read_request(client, req_id: int, request: str,
                  timeout: float = 1.0) -> dict:
    """One request frame -> what happened to it.

    Three outcomes, kept apart on purpose, because they are three different
    facts about the calibration:

        answered   the ECM carries this parameter -- payload attached
        refused    the ECM decoded the frame and said NRC 31 (or another NRC).
                   That is the useful negative: the module is listening and
                   this parameter simply is not in this calibration
        silent     nothing came back at all, which says nothing about the
                   parameter and something about the link

    Building the applicability report out of this split is the whole reason the
    first real run is worth doing.
    """
    frame = _frame(request)
    row = {"request": request, "state": "silent", "raw": None,
           "nrc": None, "nrcName": None}
    try:
        reply = client.request(req_id, frame, timeout=timeout)
    except (ObdError, TransportError) as exc:
        row["error"] = str(exc)
        return row
    if not reply:
        return row
    if reply[0] == 0x7F and len(reply) >= 3:
        row["state"] = "refused"
        row["nrc"] = f"{reply[2]:02X}"
        row["nrcName"] = gmlan.nrc_name(reply[2])
        return row
    echo = len(frame) - 1
    if reply[0] != (frame[0] | 0x40) or reply[1:1 + echo] != frame[1:]:
        row["state"] = "unexpected"
        row["raw"] = reply.hex().upper()
        return row
    payload = reply[1 + echo:]
    row["state"] = "answered"
    row["raw"] = payload.hex().upper()
    row["payload"] = payload
    return row


def _reading_from(entry: dict, decoded: dict, request: str,
                  group: str, address: int) -> dict:
    """One decoded signal in the display shape the panel and the worksheet use."""
    unit = decoded.get("unit") or ""
    value = decoded.get("value")
    return {
        "name": entry.get("name"),
        "value": "" if value is None else str(value),
        "unit": unit,
        "group": group,
        "raw": decoded.get("raw"),
        "request": request,
        "applicability": entry.get("applicability") or "UNVERIFIED",
        "source": "dealer-data",
        "description": (
            f"ECM 0x{address:03X}, {request}, GDS2 ref {entry.get('ref')}. "
            f"Scaling and unit are GM's own; applicability on this engine is "
            f"{entry.get('applicability') or 'UNVERIFIED'} -- the name has "
            f"never been checked against a running A20NFT."
        ),
    }


def _read_block(client, req_id: int, block: List[tuple]) -> tuple:
    """Read a (request, name, group) block -> (readings, applicability rows).

    A request carrying two wanted signals is sent once. A request that refuses
    marks every signal on it refused, which is the honest attribution: the
    module refused the frame, not the name.
    """
    readings: List[dict] = []
    applicability: List[dict] = []
    wanted: Dict[str, List[tuple]] = {}
    order: List[str] = []
    for request, name, group in block:
        if request not in wanted:
            wanted[request] = []
            order.append(request)
        wanted[request].append((name, group))

    for request in order:
        row = _read_request(client, req_id, request)
        names = [n for n, _ in wanted[request]]
        record = {"request": request, "names": names, "state": row["state"],
                  "nrc": row.get("nrc"), "nrcName": row.get("nrcName"),
                  "raw": row.get("raw")}
        applicability.append(record)
        if row["state"] != "answered":
            continue
        decoded_any = False
        for name, group in wanted[request]:
            entry = dealer_data.ecm_parameter(request, name)
            if entry is None:
                continue
            decoded = dealer_data.decode(entry, row["payload"])
            if decoded is None:
                continue
            decoded_any = True
            readings.append(_reading_from(entry, decoded, request, group, req_id))
        if not decoded_any:
            record["state"] = "undecoded"
    return readings, applicability


def _read_sweep_block(client, req_id: int) -> tuple:
    """Every service 0x22 request in the pack. Explicit, never the default."""
    block = []
    for request in dealer_data.ecm_read_requests():
        for entry in dealer_data.ecm_by_request(request):
            block.append((request, entry.get("name"), "sweep"))
    return _read_block(client, req_id, block)


def _merge_ident(base: List[dict], dealer: List[dict]) -> List[dict]:
    """The six certain GMLAN identifiers plus the dealer pack's block, with any
    identifier the pack decoded properly shown once rather than twice."""
    have = {row.get("label") for row in dealer}
    kept = [row for row in base
            if IDENT_SUPERSEDED.get(row.get("label")) not in have]
    return kept + dealer


def _read_dealer_ident(client, req_id: int) -> List[dict]:
    """The pack's $1A identification block, shown raw with GM's names beside it.

    Deliberately not decoded through the parameter scalings: a 17-byte VIN or a
    4-byte part number is text or a big-endian number, not a scaled reading, and
    running an equation over it would produce a confident wrong figure.
    """
    out: List[dict] = []
    for request in IDENT_REQUESTS:
        entries = dealer_data.ecm_by_request(request)
        row = _read_request(client, req_id, request, timeout=2.0)
        if row["state"] != "answered":
            continue
        payload = row["payload"]
        names = [e.get("name") or "" for e in entries]
        # GMW3110 stores a part number as a 32-bit binary value, not as text,
        # so $CB and the software module identifiers have to be read the way
        # software.py reads them or they come out as punctuation.
        part = None
        if any("Part Number" in n or "Identifier" in n for n in names):
            part = software._gm_part_number(payload)
        out.append({
            "label": entries[0].get("name") if entries else request,
            "request": request,
            "value": part or _printable(payload),
            "hex": payload.hex().upper(),
            "carries": [e.get("name") for e in entries],
            "dialect": "gmlan",
            "applicability": "UNVERIFIED",
            "note": IDENT_CAVEAT,
        })
    return out


# --------------------------------------------------------------------------- #
# interpretation
# --------------------------------------------------------------------------- #
def _by_name(readings: List[dict]) -> Dict[str, dict]:
    return {r["name"]: r for r in readings}


def _number(reading: Optional[dict]) -> Optional[float]:
    if not reading:
        return None
    try:
        return float(reading.get("value"))
    except (TypeError, ValueError):
        return None


def _boost_view(readings: List[dict]) -> dict:
    rows = _by_name(readings)
    wastegate = rows.get("Turbocharger Wastegate Solenoid Valve Command")
    desired = rows.get("Desired Boost Pressure")
    actual = rows.get("Boost Pressure Sensor")
    out = {
        "desired": desired, "actual": actual, "wastegate": wastegate,
        "bypass": rows.get("Turbocharger Bypass Solenoid Valve Command"),
        "difference": None, "wastegateRailed": None,
    }
    d, a = _number(desired), _number(actual)
    if d is not None and a is not None:
        out["difference"] = round(a - d, 3)
    if wastegate is not None and wastegate.get("raw") is not None:
        # The command is one byte. 0x00 and 0xFF are the ends of what the byte
        # can express, which is a fact about the frame and not a threshold out
        # of the pack -- the pack has none.
        out["wastegateRailed"] = wastegate["raw"] in (0x00, 0xFF)
    return out


def _fuel_view(readings: List[dict]) -> dict:
    rows = _by_name(readings)
    desired = rows.get("Desired Fuel Rail Pressure")
    actual = rows.get("Fuel Rail Pressure Sensor")
    out = {
        "railDesired": desired, "railActual": actual,
        "shortTermTrim": rows.get("Short Term Fuel Trim"),
        "longTermTrim": rows.get("Long Term Fuel Trim"),
        "commandedEquivalenceRatio": rows.get("Air/Fuel Equivalence Ratio Command"),
        "difference": None, "totalTrim": None,
    }
    d, a = _number(desired), _number(actual)
    if d is not None and a is not None:
        out["difference"] = round(a - d, 3)
    st, lt = _number(out["shortTermTrim"]), _number(out["longTermTrim"])
    if st is not None and lt is not None:
        out["totalTrim"] = round(st + lt, 3)
    return out


def _misfire_view(readings: List[dict]) -> dict:
    rows = _by_name(readings)
    total = rows.get("Total Misfire")
    cylinders = []
    for name, number in MISFIRE_CYLINDERS:
        row = rows.get(name)
        if row is None:
            continue
        cylinders.append({"cylinder": number, "counts": _number(row),
                          "reading": row})
    counted = [c for c in cylinders if c["counts"] is not None]
    worst = max(counted, key=lambda c: c["counts"]) if counted else None
    others = sum(c["counts"] for c in counted if worst and c is not worst)
    return {
        "total": total,
        "cylinders": cylinders,
        "worstCylinder": worst["cylinder"] if worst and worst["counts"] else None,
        "worstCounts": worst["counts"] if worst else None,
        "otherCounts": others if counted else None,
    }


def _diagnose(dtcs: List[dict], readings: List[dict], ident: List[dict],
              applicability: List[dict], boost: dict, fuel: dict,
              misfire: dict, dialect: Optional[str]) -> List[str]:
    """Plain-language findings. Every line says what was read and stops there:
    the pack carries no pass/fail threshold, so neither does this."""
    findings: List[str] = []

    answered = [a for a in applicability if a["state"] == "answered"]
    refused = [a for a in applicability if a["state"] == "refused"]
    silent = [a for a in applicability if a["state"] == "silent"]
    out_of_range = [a for a in refused if a.get("nrc") == "31"]
    findings.append(
        f"{len(answered)} of {len(applicability)} requested frame(s) answered, "
        f"{len(refused)} refused ({len(out_of_range)} with NRC 31, request out "
        f"of range), {len(silent)} silent. That split is the deliverable of "
        "this run: GM's list is the shared E39 4-cylinder set, so the refusals "
        "are how this engine's real parameter list gets established."
    )

    if not dtcs:
        findings.append(
            "ECM fault memory is empty. That is not a clean bill of health -- "
            "a boost or misfire complaint that has not yet set a code is still "
            "a complaint. Capture it under load (vauxhall_engine capture)."
        )
    else:
        named = [d for d in dtcs if d.get("gmName")]
        local = [d for d in dtcs if d.get("gmNameList") == "local"]
        findings.append(
            f"ECM reports {len(dtcs)} fault(s), {len(named)} with GM's own "
            f"wording from the dealer dictionary, {len(local)} of those from "
            "this module node's own override list -- which is the closest the "
            "database comes to saying the ECM supports the code."
        )
        for d in dtcs:
            if d.get("gmName"):
                findings.append(
                    f"{d.get('display') or d.get('code')}: {d['gmName']} "
                    f"(GM {d.get('gmNameList')} list)."
                )

    rows = _by_name(readings)
    for desired_name, actual_name, meaning in DIVERGENCE_PAIRS:
        desired, actual = rows.get(desired_name), rows.get(actual_name)
        d, a = _number(desired), _number(actual)
        if d is None or a is None:
            continue
        unit = desired.get("unit") or actual.get("unit") or ""
        gap = a - d
        findings.append(
            f"{actual_name} {a:g} {unit} against {desired_name} {d:g} {unit} "
            f"-- a difference of {gap:+g} {unit}. A sustained gap would mean "
            f"{meaning}; there is no threshold in GM's data that says how big "
            "is too big, so this is a measurement and not a verdict."
        )

    if boost.get("wastegateRailed"):
        wg = boost["wastegate"]
        findings.append(
            f"Turbocharger Wastegate Solenoid Valve Command is at raw "
            f"0x{wg['raw']:02X} ({wg['value']} {wg['unit']}), which is one end "
            "of what that single command byte can express. Sitting at an end "
            "stop is what boost control does when it has run out of authority "
            "-- worth watching through a pull rather than read once at idle."
        )

    if fuel.get("totalTrim") is not None:
        findings.append(
            f"Fuel trims: short term {_number(fuel['shortTermTrim']):+g} %, "
            f"long term {_number(fuel['longTermTrim']):+g} %, "
            f"{fuel['totalTrim']:+g} % together. Reported, not judged -- the "
            "dealer pack carries no trim limit for this ECM."
        )

    worst, other = misfire.get("worstCounts"), misfire.get("otherCounts")
    if worst is not None and misfire.get("worstCylinder"):
        findings.append(
            f"Misfire history: cylinder {misfire['worstCylinder']} carries "
            f"{worst:g} count(s) against {other:g} on the other three "
            "together. A split like that points at that cylinder's coil, plug "
            "or injector before it points at anything shared -- swap the "
            "suspect part with a neighbour and re-read to prove it."
        )
    elif misfire.get("total") is not None and _number(misfire["total"]) == 0:
        findings.append(
            "Every misfire counter that answered reads zero at this instant. "
            "History counters reset with the fault memory, so zero shortly "
            "after a clear means nothing yet."
        )

    end_model = next((i.get("value") for i in ident
                      if i.get("label") == "End Model Part Number"), None)
    if end_model:
        findings.append(
            f"ECM end-model part {end_model}. Take this and the VIN to TIS2Web "
            "/ SPS before changing parts -- a calibration is cheaper than a "
            "turbo."
        )

    for caveat in dealer_data.ecm_caveats():
        findings.append(
            f"Dealer-data caveat [{caveat.get('id')}, {caveat.get('severity')}]: "
            f"{caveat.get('text')}"
        )

    controls = dealer_data.ecm_device_controls()
    findings.append(
        f"GM's data carries {len(controls)} DeviceControl ($AE) commands for "
        "this ECM -- wastegate, bypass valve, cam actuators, oil pressure "
        "solenoid, ignition retard, crankshaft variation learn. They are "
        "listed in this result for information. This feature does not "
        "transmit one and has no code path that could: actuating an engine is "
        "a separate, gated job."
    )
    findings.append(
        f"Dialect: {dialect or 'unproven'}. Everything above is read-only -- "
        "no clear, no coding, no actuation, nothing written to the car."
    )
    return findings


def _readings_from(dtcs, readings, ident, findings, applicability,
                   address, dialect) -> List[dict]:
    """The flat name/value list the desktop panel renders."""
    out: List[dict] = [
        {"name": "ECM address", "value": f"0x{address:03X}", "unit": "",
         "description": f"Dialect {dialect or 'unproven'}."},
    ]
    for item in ident:
        out.append({
            "name": item.get("label") or "ident",
            "value": item.get("value") or "",
            "unit": "",
            "description": " -- ".join(
                p for p in (item.get("request"), item.get("note"),
                            item.get("dialect") and
                            f"read over {item['dialect']}") if p),
        })
    if not dtcs:
        out.append({
            "name": "ECM faults", "value": "none stored", "unit": "",
            "description": "Fault memory empty -- not the same as a healthy engine.",
        })
    for d in dtcs:
        out.append({
            "name": d.get("display") or d.get("code") or "fault",
            "value": d.get("gmName") or d.get("description")
                     or "no GM wording for this code",
            "unit": "",
            "description": " ".join(
                p for p in (
                    d.get("failureTypeName") and f"Failure type: {d['failureTypeName']}.",
                    d.get("gmNameList") and
                    f"GM {d['gmNameList']} fault dictionary.",
                ) if p
            ),
        })
    out.extend(readings)
    answered = sum(1 for a in applicability if a["state"] == "answered")
    out.append({
        "name": "Parameter applicability", "value":
            f"{answered}/{len(applicability)} frame(s) answered", "unit": "",
        "description": "Which of GM's shared E39 list this engine carries. "
                       "The refusals are the finding.",
    })
    for i, line in enumerate(findings, 1):
        out.append({
            "name": f"Finding {i}", "value": line, "unit": "",
            "description": "From GM's E39A dealer data and what this ECM just said.",
        })
    return out


def _worksheet(vin, address, dialect, ident, dtcs, readings,
               applicability, findings) -> str:
    lines = [
        "Vauxhall Astra J VXR / A20NFT engine report (read-only, ELM327)",
        f"VIN: {vin or 'not reported'}",
        f"ECM: 0x{address:03X}  dialect {dialect or 'unproven'}",
        f"Read: {_now()}",
        "",
        "NOTHING IN THIS REPORT HAS BEEN VERIFIED ON THIS ENGINE. Every name,",
        "scaling and unit comes from GM's GDS2 dealer database for the E39A",
        "node, read statically. The A20NFT has never answered any of them.",
        "",
        "Identification (GMLAN $1A). A row printing its request came from the",
        "dealer pack, whose request bytes the exporter RECONSTRUCTED rather",
        "than read -- those are UNVERIFIED twice over.",
    ]
    if ident:
        for item in ident:
            lines.append(f"    {item.get('label')}: {item.get('value')}")
            # The six certain identifiers come from gmlan.read_ident, which
            # carries no request or raw payload; only the dealer-block rows do.
            if item.get("request"):
                lines.append(f"        {item['request']} raw {item.get('hex')}")
    else:
        lines.append("    (nothing answered)")
    lines.append("")
    lines.append("Faults (GM's own wording where the dictionary has it)")
    if not dtcs:
        lines.append("    none stored")
    for d in dtcs:
        lines.append(f"    {d.get('display') or d.get('code')}")
        if d.get("gmName"):
            lines.append(f"        {d['gmName']}  [GM {d.get('gmNameList')} list]")
        if d.get("description"):
            lines.append(f"        SAE: {d['description']}")
        if d.get("failureTypeName"):
            lines.append(f"        failure type: {d['failureTypeName']}")
    lines.append("")
    lines.append("Live block (GDS2 names and scalings, applicability UNVERIFIED)")
    if not readings:
        lines.append("    (nothing in the core block answered)")
    for r in readings:
        unit = f" {r['unit']}" if r.get("unit") else ""
        lines.append(f"    [{r.get('group')}] {r['name']}: {r['value']}{unit}"
                     f"   ({r.get('request')} raw {r.get('raw')})")
    lines.append("")
    lines.append("Parameter applicability -- the point of the first real run")
    for a in applicability:
        if a["state"] == "answered":
            continue
        detail = a.get("nrcName") or ""
        nrc = f" NRC {a['nrc']}" if a.get("nrc") else ""
        lines.append(f"    {a['request']} {a['state']}{nrc} {detail}"
                     f"   [{', '.join(n for n in a['names'] if n)}]")
    lines.append("")
    lines.append("Findings")
    for line in findings:
        lines.append(f"    - {line}")
    lines.append("")
    lines.append(
        "Sources: GDS2 2023.3 Opel/Vauxhall deliverable, module node "
        "'Engine Control Module$ECM - E39A (S3) - GME', extracted to "
        "backend/profiles/vauxhall/data/gds2_e39a_ecm.json with a file offset "
        "on every row; GMW3110 fault and identification services via "
        "backend/profiles/vauxhall/gmlan.py. Nothing was written to the car, "
        "and no DeviceControl frame was transmitted."
    )
    return "\n".join(lines)


def _device_control_catalogue() -> List[dict]:
    """What GDS2 can command on this engine, as text. Never sent."""
    out = []
    for control in dealer_data.ecm_device_controls():
        out.append({
            "name": control.get("name"),
            "service": control.get("service"),
            "request": control.get("request"),
            "commands": [c.get("text") for c in control.get("commands") or []],
            "writesToCar": control.get("writesToCar"),
            "warning": control.get("warning"),
            "applicability": control.get("applicability"),
        })
    return out


def _enrich_dtcs(dtcs: List[dict]) -> List[dict]:
    """Attach GM's own wording, and say which dictionary it came from."""
    for entry in dtcs:
        hit = dealer_data.ecm_dtc_name(entry.get("code") or "")
        if hit:
            entry["gmName"] = hit["name"]
            entry["gmNameList"] = hit["list"]
    return dtcs


def _result(address, response, code, name, vin, protocol, adapter_name,
            dialect, error, trace, ident, dtcs, readings, applicability,
            simulated, message_prefix="", scenario=None) -> dict:
    boost = _boost_view(readings)
    fuel = _fuel_view(readings)
    misfire = _misfire_view(readings)
    findings = _diagnose(dtcs, readings, ident, applicability,
                         boost, fuel, misfire, dialect)
    flat = _readings_from(dtcs, readings, ident, findings, applicability,
                          address, dialect)
    worksheet = _worksheet(vin, address, dialect, ident, dtcs, readings,
                           applicability, findings)
    answered = sum(1 for a in applicability if a["state"] == "answered")
    refused = sum(1 for a in applicability if a["state"] == "refused")
    unread = " FAULT MEMORY UNREAD -- " + error if error and not dtcs else ""
    return {
        "ok": True, "simulated": simulated,
        "vehicle": {"vin": vin, "protocol": protocol, "adapter": adapter_name},
        "ecm": {"address": f"0x{address:03X}", "response": f"0x{response:03X}",
                "code": code, "name": name, "dialect": dialect or "unproven",
                "error": error, "trace": trace, "scenario": scenario},
        "ident": ident, "dtcs": dtcs, "live": readings, "findings": findings,
        "didReport": {
            "answered": [a for a in applicability if a["state"] == "answered"],
            "refused": [a for a in applicability if a["state"] == "refused"],
            "silent": [a for a in applicability if a["state"] == "silent"],
            "other": [a for a in applicability
                      if a["state"] not in ("answered", "refused", "silent")],
            "note": "Which of GM's shared E39 4-cylinder parameter list this "
                    "engine actually carries. NRC 31 means the module decoded "
                    "the frame and does not have that parameter.",
        },
        "misfire": misfire, "boost": boost, "fuel": fuel,
        "deviceControls": _device_control_catalogue(),
        "caveats": dealer_data.ecm_caveats(),
        "readings": flat, "worksheet": worksheet,
        "message": (
            message_prefix
            + (f"VIN {vin}. " if vin else "")
            + f"ECM at 0x{address:03X} ({dialect or 'unproven'}). "
            + f"{len(dtcs)} fault(s). "
            + f"{answered}/{len(applicability)} parameter frame(s) answered, "
            + f"{refused} refused. "
            + f"{len(findings)} finding(s).{unread} "
            + "Every name and scaling is GM dealer data that has never been "
              "confirmed on this engine. Read-only -- nothing was changed on "
              "the car and no DeviceControl was sent."
        ),
        "timestamp": _now(),
    }


def _empty(message: str, simulated: bool = False) -> dict:
    return {
        "ok": False, "simulated": simulated, "readings": [], "worksheet": "",
        "dtcs": [], "ident": [], "live": [], "findings": [],
        "message": message, "timestamp": _now(),
    }


# --------------------------------------------------------------------------- #
# analyse
# --------------------------------------------------------------------------- #
def analyze(adapter: Optional[dict], mode: str = "real",
            sweep: bool = False, scenario: str = "healthy") -> dict:
    """One read-only pass over the engine controller.

    ``sweep`` swaps the 35-parameter core block for every service 0x22 request
    in GM's list -- several hundred frames, minutes over a Bluetooth dongle.
    It is never the default, because the answer it gives is a map of the
    calibration rather than a diagnosis, and it is the kind of thing a panel
    button should not be able to start by accident.
    """
    if mode == "sim":
        return _simulated(scenario, sweep)

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return _empty(
                f"The car answered {session.protocol or 'an unknown protocol'}, "
                "not " + _WRONG_BUS
            )

        vin = obd_service._read_vin(session)
        id_map = dict(VAUXHALL_ID_MAP)
        for req, resp, _, _ in ECM_CANDIDATES:
            id_map[req] = resp

        client = Elm327UdsClient(session, id_map)
        client.begin()
        try:
            chosen = None
            ident: List[dict] = []
            dtcs: List[dict] = []
            dialect = None
            error = None
            trace: List[str] = []
            for req, resp, code, name in ECM_CANDIDATES:
                hit = _probe(client, req, trace)
                if hit is None:
                    continue
                ident, dtcs, dialect, error = _read_module(client, req, hit[1], trace)
                chosen = (req, resp, code, name)
                break

            readings: List[dict] = []
            applicability: List[dict] = []
            if chosen:
                req = chosen[0]
                ident = _merge_ident(ident, _read_dealer_ident(client, req))
                if sweep:
                    readings, applicability = _read_sweep_block(client, req)
                else:
                    readings, applicability = _read_block(client, req, CORE_BLOCK)
        finally:
            client.restore()

        if chosen is None:
            return _empty(
                "No engine controller answered at 0x7E0. Ignition off, the "
                "dongle on the wrong bus, or a car this profile does not cover "
                "-- run Find modules. Nothing was changed."
            )

        req, resp, code, name = chosen
        return _result(req, resp, code, name, vin,
                       session.protocol or "ISO 15765-4 (CAN 11/500)",
                       (adapter or {}).get("name"), dialect, error, trace,
                       ident, _enrich_dtcs(dtcs), readings, applicability,
                       simulated=False)

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return _empty(str(exc))


def _simulated(scenario: str = "healthy", sweep: bool = False) -> dict:
    """The same read pipeline, run against the fake ECM in sim_ecm.py.

    Not a canned dictionary. gearbox.py can afford one because its numbers came
    off a car; this file has no numbers off a car, so a canned result would be
    fiction dressed as a demonstration. Running the real reader against a
    module that answers frames at least proves the reader: the decode, the
    NRC-31 path, the divergence findings and the applicability split are all
    genuinely exercised, and the only invented thing is the engine.
    """
    from . import sim_ecm

    fake = sim_ecm.SimulatedEcm(scenario)
    trace: List[str] = []
    req, resp, code, name = ECM_CANDIDATES[0]
    hit = _probe(fake, req, trace)
    if hit is None:
        return _empty("SIMULATED -- the fake ECM did not answer the probe.",
                      simulated=True)
    ident, dtcs, dialect, error = _read_module(fake, req, hit[1], trace)
    ident = _merge_ident(ident, _read_dealer_ident(fake, req))
    if sweep:
        readings, applicability = _read_sweep_block(fake, req)
    else:
        readings, applicability = _read_block(fake, req, CORE_BLOCK)
    result = _result(req, resp, code, name, fake.vin,
                     "ISO 15765-4 (CAN 11/500)", "Simulated", dialect,
                     error, trace, ident, _enrich_dtcs(dtcs), readings,
                     applicability, simulated=True,
                     message_prefix=f"SIMULATED ({scenario}) -- ",
                     scenario=scenario)
    result["message"] += (
        f" No real car connected; the scenario is '{scenario}' "
        f"({sim_ecm.SCENARIOS.get(scenario, '')})"
    )
    return result


# --------------------------------------------------------------------------- #
# faults on their own
# --------------------------------------------------------------------------- #
def faults(adapter: Optional[dict], mode: str = "real",
           scenario: str = "healthy") -> dict:
    """Fault memory only -- the cheap read, for when the engine is not running.

    Same dialect ladder as the full analyse, GM's own wording attached, and no
    live block, so it costs a handful of frames instead of forty.
    """
    if mode == "sim":
        from . import sim_ecm
        fake = sim_ecm.SimulatedEcm(scenario)
        trace: List[str] = []
        req = ECM_CANDIDATES[0][0]
        hit = _probe(fake, req, trace)
        ident, dtcs, dialect, error = (
            _read_module(fake, req, hit[1], trace) if hit else ([], [], None, None)
        )
        dtcs = _enrich_dtcs(dtcs)
        return {
            "ok": True, "simulated": True, "dtcs": dtcs, "ident": ident,
            "dialect": dialect or "unproven", "error": error, "trace": trace,
            "message": (f"SIMULATED ({scenario}) -- {len(dtcs)} fault(s) on the "
                        "fake ECM. No real car connected."),
            "timestamp": _now(),
        }

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return _empty(
                f"The car answered {session.protocol or 'an unknown protocol'}, "
                "not " + _WRONG_BUS
            )
        id_map = dict(VAUXHALL_ID_MAP)
        for req, resp, _, _ in ECM_CANDIDATES:
            id_map[req] = resp
        client = Elm327UdsClient(session, id_map)
        client.begin()
        trace: List[str] = []
        try:
            req = ECM_CANDIDATES[0][0]
            hit = _probe(client, req, trace)
            if hit is None:
                return _empty("No engine controller answered at 0x7E0.")
            ident, dtcs, dialect, error = _read_module(client, req, hit[1], trace)
        finally:
            client.restore()
        dtcs = _enrich_dtcs(dtcs)
        return {
            "ok": True, "simulated": False, "dtcs": dtcs, "ident": ident,
            "dialect": dialect or "unproven", "error": error, "trace": trace,
            "message": (f"ECM at 0x7E0 ({dialect or 'unproven'}): "
                        f"{len(dtcs)} fault(s). Read-only."),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return _empty(str(exc))


# --------------------------------------------------------------------------- #
# capture -- the road test
# --------------------------------------------------------------------------- #
def _run_dir(mode: str) -> Path:
    stamp = "sim" if mode == "sim" else datetime.now().strftime("%Y-%m-%d")
    path = CAPTURES_DIR / f"vauxhall-engine-{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _hhmm() -> str:
    return datetime.now().strftime("%H%M")


def _capture_block(fast: bool) -> tuple:
    """(fast rows, slow rows) of the core block."""
    if not fast:
        return CORE_BLOCK, []
    quick = [row for row in CORE_BLOCK if row[1] in FAST_BLOCK]
    rest = [row for row in CORE_BLOCK if row[1] not in FAST_BLOCK]
    return quick, rest


def _sample(client, req_id: int, block: List[tuple]) -> dict:
    readings, applicability = _read_block(client, req_id, block)
    out = {}
    for r in readings:
        out[r["name"]] = r["value"]
        out[r["name"] + " (raw)"] = r["raw"]
    out["_refused"] = [a["request"] for a in applicability
                       if a["state"] == "refused"]
    return out


def capture(adapter: Optional[dict], seconds: float = 600.0,
            mode: str = "real", fast: bool = True, label: str = "drive",
            scenario: str = "healthy",
            should_stop: Optional[Callable[[], bool]] = None,
            on_line: Optional[Callable[[str], None]] = None) -> dict:
    """Log the engine through a drive, one JSON object per line.

    This is where the desired/actual pairs earn their place. Boost control and
    rail pressure cannot be judged at idle: the ECM is not asking for anything,
    so nothing can fail to deliver it. The shape that matters is a full-throttle
    pull in third or fourth, and that is a series, not a reading.

    ``fast=True`` (the default here, unlike analyse) polls only the ten
    parameters that change under load every tick and sweeps the rest every
    fifth, because every read over a Bluetooth dongle is its own round trip.

    Read-only. The log is raw bytes plus GDS2's reading of them, side by side,
    so a later correction to a scaling does not cost the run.
    """
    quick, slow = _capture_block(fast)

    def loop(client, req_id, clock, path, is_sim) -> dict:
        rows = 0
        started = clock()
        tick = 0
        with path.open("w", encoding="utf-8") as fh:
            while clock() - started < seconds:
                if should_stop is not None and should_stop():
                    break
                block = list(quick)
                if slow and tick % 5 == 0:
                    block += slow
                sample = _sample(client, req_id, block)
                sample["t"] = round(clock() - started, 3)
                sample["tick"] = tick
                sample["label"] = label
                sample["simulated"] = is_sim
                line = json.dumps(sample, ensure_ascii=False)
                fh.write(line + "\n")
                rows += 1
                tick += 1
                if on_line is not None:
                    on_line(line)
        elapsed = max(clock() - started, 1e-9)
        return {
            "ok": True, "simulated": is_sim, "rows": rows, "label": label,
            "sampleRate": round(rows / elapsed, 2), "savedTo": str(path),
            "message": (
                f"{'SIMULATED -- ' if is_sim else ''}{rows} row(s) over "
                f"{elapsed:.0f} s at {rows / elapsed:.2f} row(s)/s, written to "
                f"{path}. Every value is GDS2's reading of the raw bytes, which "
                "are logged beside it; nothing here has been confirmed on this "
                "engine."
            ),
            "timestamp": _now(),
        }

    if mode == "sim":
        from . import sim_ecm
        fake = sim_ecm.SimulatedEcm(scenario)
        path = _run_dir(mode) / f"capture-{label}-{scenario}.jsonl"
        return loop(fake, ECM_CANDIDATES[0][0], fake.clock, path, True)

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            result = _empty(
                f"The car answered {session.protocol or 'an unknown protocol'}, "
                "not " + _WRONG_BUS
            )
            result["rows"] = 0
            return result
        id_map = dict(VAUXHALL_ID_MAP)
        for req, resp, _, _ in ECM_CANDIDATES:
            id_map[req] = resp
        client = Elm327UdsClient(session, id_map)
        client.begin()
        try:
            path = _run_dir(mode) / f"capture-{label}-{_hhmm()}.jsonl"
            return loop(client, ECM_CANDIDATES[0][0], monotonic, path, False)
        finally:
            client.restore()

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        result = _empty(str(exc))
        result["rows"] = 0
        return result


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def _capture_summary(path: Path) -> dict:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    if not rows:
        return {"ok": False, "message": f"{path} has no rows.",
                "timestamp": _now()}

    duration = rows[-1].get("t", 0.0)
    lines = [
        "Vauxhall Astra J VXR / A20NFT engine capture summary",
        f"Source: {path}",
        f"Generated {_now()}",
        "",
        f"Rows: {len(rows)}   Duration: {duration:.0f} s   "
        f"Rate: {len(rows) / duration if duration else 0:.2f} row(s)/s",
        f"Label: {rows[0].get('label')}   "
        f"Simulated: {bool(rows[0].get('simulated'))}",
        "",
        "Every figure is GDS2's reading of raw bytes from an engine that has",
        "never confirmed any of them. No threshold below is a pass/fail limit,",
        "because GM's data carries none for this ECM.",
        "",
        "Extremes seen in this run",
    ]
    tracked = [name for _, name, _ in CORE_BLOCK]
    for name in tracked:
        values = []
        for row in rows:
            try:
                values.append(float(row[name]))
            except (KeyError, TypeError, ValueError):
                continue
        if not values:
            continue
        lines.append(f"    {name}: min {min(values):g}, max {max(values):g}, "
                     f"last {values[-1]:g}  ({len(values)} sample(s))")
    refused = sorted({r for row in rows for r in row.get("_refused") or []})
    lines.append("")
    lines.append(f"Frames refused during the run: {len(refused)}")
    for request in refused:
        lines.append(f"    {request}")
    lines.append("")
    return {"ok": True, "rows": len(rows), "text": "\n".join(lines),
            "stem": f"vauxhall-engine-{path.stem}", "timestamp": _now()}


def report(source=None, adapter: Optional[dict] = None, mode: str = "real",
           scenario: str = "healthy", out_dir: Optional[Path] = None) -> dict:
    """Write the worksheet to captures/reports/.

    With no ``source`` this runs an analyse and writes its worksheet -- the
    thing to print and take to the car. With a capture .jsonl it writes that
    run's summary instead.
    """
    if source is not None:
        path = Path(source)
        if not path.exists():
            return {"ok": False, "message": f"No such capture: {path}",
                    "timestamp": _now()}
        built = _capture_summary(path)
        if not built.get("ok"):
            return built
        text, stem = built["text"], built["stem"]
        extra = {"rows": built["rows"]}
    else:
        result = analyze(adapter, mode, scenario=scenario)
        if not result.get("ok"):
            return result
        text = result["worksheet"]
        stamp = "sim" if mode == "sim" else datetime.now().strftime("%Y-%m-%d")
        stem = f"vauxhall-engine-{stamp}" + (f"-{scenario}" if mode == "sim" else "")
        extra = {"findings": len(result.get("findings") or []),
                 "simulated": result.get("simulated", False)}

    reports = out_dir or (CAPTURES_DIR / "reports")
    reports.mkdir(parents=True, exist_ok=True)
    dest = reports / f"{stem}.txt"
    dest.write_text(text, encoding="utf-8")
    out = {"ok": True, "savedTo": str(dest),
           "message": f"Worksheet written to {dest}.",
           "timestamp": _now()}
    out.update(extra)
    return out
