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
