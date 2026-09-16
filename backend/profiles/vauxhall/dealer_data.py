"""The GDS2 dealer data set for the Astra K T76 transmission controller.

WHAT THIS IS
------------
``data/gds2_t76_tcm.json`` is a static extract of the module node
``Transmission Control Module$TCM - T76 - (Conti) - GME`` out of the GM/Opel
GDS2 2023.3 Opel/Vauxhall deliverable -- the same node the 2017 Astra K menu
tree binds. Every entry in it carries the file offset and object type it was
read from, so any single value can be traced back to a byte range in
``BinaryFile/vatbin.bin`` and re-checked. ``tools/gds2/export_module.py``
regenerates it.

This module is the read side of that file: it loads it, and it turns a raw
answer into a number using the scaling the dealer tool itself uses. Nothing
here sends anything. The device controls in the data are exposed as
*descriptions* only -- see ``procedures.py`` for how a control gets executed,
which is to say, currently not at all.

WHY IT MATTERS TO THIS PROFILE
------------------------------
tcm.py built its map by changing something physical on the car and keeping only
what moved the way it had to. That is a strong method and a slow one, and it
leaves gaps wherever a signal cannot be provoked at a standstill. The dealer
data is the opposite: complete, authoritative about naming and scaling, and
completely untested against this particular car. The two are worth exactly as
much as each other and they answer different questions, so the profile keeps
them as separate axes rather than merging them:

    confidence   what the CAR has shown (tcm.py's existing verified /
                 inferred / unknown vocabulary -- unchanged)
    source       where the NAME came from (on-car, dealer-data, or both)

A signal named only by the dealer data is not "verified" no matter how
official the source, because nothing on Steve's car has yet answered to it.
A signal the car has proven but the dealer data does not name stays proven.
Where the two disagree, the disagreement is recorded and neither is quietly
dropped.

THE GROUND-TRUTH CHECK
----------------------
The whole decode rule rests on one datum that can be checked against
known-good live data. The dealer file gives ``Transmission Fluid Temperature``
as parameter id 0x1940, request ``22 19 40``, scaling ``(X*1)-40 °C`` -- which
is exactly what this harness already reads successfully from the TCM at 0x7E2
on VIN W0LPF2EC8HG072884. That match validates the *read* half of the decode
rule: that the extracted parameter id really is the wire id, and that the
extracted conversion really is the scaling.

It does more than that, and this is worth keeping in view because it settles a
question the profile got wrong once. Several module sets in the same file carry
a parameter of that same name, and they disagree about where it lives: the T76
sets put it on 0x1940 scaled -40, while the AF40 sets (0x05ABE67C and
0x05AC07C4) put it on 0x210A scaled -50. This car answers 0x1940 with values
that make sense under -40. So the car independently agrees with the menu tree
that this gearbox is a T76 and not an AF40 -- from a direction that has nothing
to do with the menu tree at all. An earlier round of this work attributed the
Astra K TCM to the AF40 family and drew a fault-code list from it; that was
wrong, and this is the check that would have caught it.

It validates nothing about the device-control half. The fluid-temperature PID
is a read, built by a different code path in the dealer tool, and it never
exercises the $AE frame assembly -- not the template, not the byte-pattern
append, not the OR-in at the value offset. "The extract's read decoding is
confirmed on the car" and "the control frame is verified" are different
statements and must not be flattened into one.

THE SECOND PACK -- THE E39A ENGINE CONTROLLER
--------------------------------------------
``data/gds2_e39a_ecm.json`` is the same kind of extract taken from the engine
side: module node ``Engine Control Module$ECM - E39A (S3) - GME``, the node the
Astra-J 2.0L (LHU) menu tree binds, which is the ECM in Steve's 2016 Astra VXR.
It is read through the ``ecm_*`` functions below; the TCM functions above are
untouched and still mean the gearbox.

Three things about it decide how engine.py is allowed to use it, and they are
carried in the file itself as machine-readable ``caveats`` rather than being
paraphrased here:

* the PID set the node binds is GM's SHARED "E39 4cyl" set, so the database
  cannot say which of the 970 parameters the A20NFT answers -- every parameter
  is marked ``applicability: "UNVERIFIED"`` and NRC 31 is expected on a fair
  number of them;
* there are no pass/fail thresholds anywhere in it. The ``range`` on a
  conversion is the display limit GDS2 draws with, and some are plainly wrong;
* the 70 device controls WRITE to the car. They are exposed here as
  descriptions, exactly like the T76 ones, and nothing in this profile sends
  one.

The read half needs no new decoder: the ECM pack uses the same ``item`` /
``read`` shape as the T76 pack, so ``decode()`` and ``apply_equation()`` below
work on an ECM entry unchanged. What is new is the indexing -- the ECM pack
keys parameters by request AND name, because one request frequently carries
several named signals and, unlike the T76 set, several different requests carry
the SAME name (``Boost Pressure Sensor`` is a volts reading on 22 30 88 and a
kPa reading on 22 32 98).

Nothing in the ECM pack has been seen on a car. Not one parameter, not one
fault row, not one identifier -- and the 40 identification entries carry
requests this exporter RECONSTRUCTED rather than read, which is a second and
separate reason to distrust them until a car answers.

EQUATIONS
---------
Only the equation forms the T76 set actually uses are implemented, and an
unrecognised form returns None rather than a plausible-looking number:

    (X(0)*M)+K            linear scaling
    (X(0)>>N)&0xM         bit field
    X(0)<<N               shift (write side)
    (X(0)<A)?0:(...)      the fluid-temperature window classifier
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

DATA_FILE = Path(__file__).resolve().parent / "data" / "gds2_t76_tcm.json"

# The one parameter whose extracted decode is confirmed against live data from
# this car. Quoted here so the claim travels with the code that relies on it.
GROUND_TRUTH = {
    "ref": "8442_6736",
    "request": "22 19 40",
    "equation": "(X(0)*1)-40",
    "unit": "°C",
    "evidence": (
        "Read 63 raw with the engine cold and stopped, which is 23 °C and "
        "matched generic OBD coolant on a car that had been standing; rose to "
        "30 °C over several minutes of idling. The dealer file names the same "
        "parameter id with the same scaling."
    ),
    "discriminates": (
        "The AF40 module sets in the same deliverable put this parameter on "
        "0x210A scaled -50. The car answering 0x1940 under -40 therefore also "
        "confirms this gearbox is a T76 rather than an AF40, independently of "
        "the menu tree."
    ),
    "doesNotCover": (
        "The device-control frame assembly, which is a different code path in "
        "the dealer tool and is exercised by nothing here."
    ),
}


@lru_cache(maxsize=1)
def load() -> dict:
    """The whole extract. Cached -- it is a quarter of a megabyte."""
    with DATA_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _index() -> Dict[str, List[dict]]:
    """Parameters grouped by the request frame that reads them.

    One request can carry several named signals: the T76 set packs four
    transmission fluid pressure switches into the one byte at 22 28 1D, and
    reads thirteen adaptive pressure cells out of the single 1A 50 block. So
    the value is always a list.
    """
    out: Dict[str, List[dict]] = {}
    for entry in load().get("parameters", []):
        req = entry.get("request")
        if req:
            out.setdefault(req, []).append(entry)
    return out


def by_request(request: str) -> List[dict]:
    """Every named signal carried by one request frame, e.g. '22 19 40'."""
    return _index().get(request.upper(), [])


def by_did(did: int) -> List[dict]:
    """Every named signal on one service 0x22 parameter id."""
    return by_request("22 %02X %02X" % (did >> 8, did & 0xFF))


@lru_cache(maxsize=1)
def _by_ref() -> Dict[str, dict]:
    out = {}
    for group in ("parameters", "deviceControls"):
        for entry in load().get(group, []):
            out.setdefault(entry.get("ref", ""), entry)
    return out


def by_ref(ref: str) -> Optional[dict]:
    """One parameter or control by its GDS2 reference name, e.g. '8682_51909'."""
    return _by_ref().get(ref)


def states_for(ref: str) -> Dict[int, str]:
    """The enumerated state table of one parameter, as {value: text}."""
    entry = by_ref(ref) or {}
    conv = entry.get("read") or entry.get("write") or {}
    return {s["value"]: s["text"] for s in conv.get("states", [])}


def condition(name: str) -> Optional[dict]:
    for c in load().get("conditions", []):
        if c.get("name") == name:
            return c
    return None


def device_control(ref: str) -> Optional[dict]:
    for c in load().get("deviceControls", []):
        if c.get("ref") == ref:
            return c
    return None


def dtc_name(code: str) -> Optional[str]:
    """GM's own wording for a code, out of the master text dictionary.

    Being in that dictionary means GM has assigned the code somewhere in its
    range -- it is NOT evidence that this module supports it. The dictionary is
    a corpus-wide lookup of 8885 rows covering every module GDS2 knows.
    """
    names = (load().get("dtc") or {}).get("masterNames") or {}
    for row in names.get("codes", []):
        if row.get("code") == code.upper():
            return row.get("name")
    return None


# --------------------------------------------------------------------------- #
# equation evaluation
# --------------------------------------------------------------------------- #
_LINEAR = re.compile(r"^\(X\(0\)\*(-?[\d.]+)\)([+-])(-?[\d.]+)$")
_BITFIELD = re.compile(r"^\(X\(0\)>>(\d+)\)&0[xX]([0-9a-fA-F]+)$")
_SHIFT = re.compile(r"^X\(0\)<<(\d+)$")
_WINDOW = re.compile(r"^\(X\(0\)<(\d+)\)\?0:\(\(X\(0\)>(\d+)\)\?1:2\)$")


def apply_equation(equation: str, raw: int) -> Optional[float]:
    """Turn a raw integer into the value the dealer tool would display.

    Returns None for any form without an implemented rule. That is deliberate:
    a wrong scaling looks exactly like a right one on screen, so the profile
    would rather show nothing than show a number it cannot justify.
    """
    if not equation:
        return None
    m = _LINEAR.match(equation)
    if m:
        scale, sign, offset = float(m.group(1)), m.group(2), float(m.group(3))
        return raw * scale + (offset if sign == "+" else -offset)
    m = _BITFIELD.match(equation)
    if m:
        return float((raw >> int(m.group(1))) & int(m.group(2), 16))
    m = _SHIFT.match(equation)
    if m:
        return float(raw << int(m.group(1)))
    m = _WINDOW.match(equation)
    if m:
        low, high = int(m.group(1)), int(m.group(2))
        return 0.0 if raw < low else (1.0 if raw > high else 2.0)
    return None


def decode(entry: dict, payload: bytes) -> Optional[dict]:
    """Decode one answer for one named signal.

    ``payload`` is the response body after the echoed identifier. The signal's
    own byte offset and length select its slice, which is how thirteen adaptive
    pressure cells come out of one 1A 50 block.
    """
    item = entry.get("item") or {}
    conv = entry.get("read") or {}
    off, length = item.get("payloadOffset", 0), item.get("length", 1)
    if length < 1 or off + length > len(payload):
        return None
    raw = int.from_bytes(payload[off:off + length], "big")
    value = apply_equation(conv.get("equation", ""), raw)
    out = {
        "name": entry.get("name"),
        "ref": entry.get("ref"),
        "raw": raw,
        "unit": "" if conv.get("unit") in (None, "_") else conv.get("unit"),
        "offset": entry.get("offset"),
    }
    states = {s["value"]: s["text"] for s in conv.get("states", [])}
    if states:
        key = int(value) if value is not None else raw
        out["value"] = states.get(key)
        out["state"] = key
        if out["value"] is None:
            out["value"] = f"unnamed state {key}"
    elif value is None:
        out["value"] = None
        out["note"] = (f"scaling {conv.get('equation')!r} has no implemented "
                       "rule; raw value only")
    else:
        out["value"] = round(value, 4)
    return out


# --------------------------------------------------------------------------- #
# The E39A engine controller pack
#
# Kept as its own set of functions rather than a module argument on the ones
# above, so that nothing reading the gearbox can accidentally be handed engine
# data. The two packs are different modules on different addresses and the only
# thing they share is the file format.
# --------------------------------------------------------------------------- #
ECM_DATA_FILE = Path(__file__).resolve().parent / "data" / "gds2_e39a_ecm.json"


@lru_cache(maxsize=1)
def load_ecm() -> dict:
    """The whole E39A extract. Cached -- it is about three megabytes."""
    with ECM_DATA_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _ecm_by_request() -> Dict[str, List[dict]]:
    """ECM parameters grouped by the request frame that reads them."""
    out: Dict[str, List[dict]] = {}
    for entry in load_ecm().get("parameters", []):
        req = entry.get("request")
        if req:
            out.setdefault(req.upper(), []).append(entry)
    return out


def ecm_by_request(request: str) -> List[dict]:
    """Every named ECM signal carried by one request frame, e.g. '22 00 0C'."""
    return _ecm_by_request().get(request.upper(), [])


def ecm_parameter(request: str, name: str) -> Optional[dict]:
    """One ECM parameter, keyed by request AND name.

    Name alone is not a key in this pack and neither is the request. There are
    two ``Boost Pressure Sensor`` rows on different requests in different units,
    and 22 32 98 carries both the boost reading and a freeze-frame slot. A
    caller that means one specific signal has to say both, and gets None rather
    than a near-miss if the pack does not have it.
    """
    for entry in ecm_by_request(request):
        if entry.get("name") == name:
            return entry
    return None


def ecm_read_requests() -> List[str]:
    """Every distinct service 0x22 request in the ECM pack, in pack order.

    This is the full sweep list -- several hundred frames. It is deliberately
    a separate call from the curated block in engine.py: sweeping it is a
    deliberate act, not something a routine read should ever do by accident.
    """
    seen: List[str] = []
    known = set()
    for entry in load_ecm().get("parameters", []):
        req = entry.get("request")
        if entry.get("service") != "$22" or not req or req in known:
            continue
        known.add(req)
        seen.append(req)
    return seen


def ecm_ident_parameters() -> List[dict]:
    """The 40 GMLAN $1A identification rows.

    Their request bytes were RECONSTRUCTED by the exporter rather than read out
    of the database, which each row records in its own ``requestSource``. They
    are offered so an identification read has GM's names for what comes back;
    they are not evidence that this ECM answers any of them.
    """
    return [e for e in load_ecm().get("parameters", []) if e.get("service") == "$1A"]


def ecm_caveats() -> List[dict]:
    """The pack's own machine-readable limits, as written by the exporter.

    Surfaced verbatim rather than restated: a caveat that gets paraphrased at
    each hop is a caveat that eventually goes missing.
    """
    return list(load_ecm().get("caveats", []))


def ecm_device_controls() -> List[dict]:
    """The 70 $AE device controls, as DESCRIPTIONS.

    Every one of these writes to the car. Nothing in this profile transmits
    one, and this function exists so the operator can see what the dealer tool
    is able to command without the harness being able to command it.
    """
    return list(load_ecm().get("deviceControls", []))


def ecm_tests() -> List[dict]:
    """The 61 named dealer tests, in their 7 folders.

    The pack's ``test-bindings-dead`` caveat applies to all of them: there is
    no test-to-frame join anywhere in the database, so a test here is a name
    and nothing more. Pairing one with a parameter is a guess.
    """
    return list(load_ecm().get("tests", []))


@lru_cache(maxsize=1)
def _ecm_dtc_names() -> Dict[str, tuple]:
    """code -> (name, which list it came from). Local overrides win."""
    out: Dict[str, tuple] = {}
    dtc = load_ecm().get("dtc") or {}
    for row in (dtc.get("masterList") or {}).get("rows", []):
        code = (row.get("code") or "").upper()
        if code:
            out[code] = (row.get("name"), "master")
    for local in dtc.get("localLists") or []:
        for row in local.get("rows", []):
            code = (row.get("code") or "").upper()
            if code:
                out[code] = (row.get("name"), "local")
    return out


def ecm_dtc_name(code: str) -> Optional[dict]:
    """GM's own wording for a code, with which list it came from.

    ``local`` means this module node names the code itself, which is as close
    to "this ECM supports it" as the database gets. ``master`` means it is in
    the corpus-wide dictionary, which says only that GM has assigned the code
    somewhere -- exactly the same caveat the T76 dtc_name() carries.
    """
    hit = _ecm_dtc_names().get((code or "").upper())
    if hit is None:
        return None
    return {"name": hit[0], "list": hit[1]}
