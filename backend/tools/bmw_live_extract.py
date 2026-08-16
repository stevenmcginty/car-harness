"""Build the BMW/MINI live-data pack from the SGBD files on this machine.

WHY THIS EXISTS
BMW was the one brand in the harness with no live-data layer. Its measured
values are not published as DIDs the way Mazda's or Fiat's are -- every write-up
says so, and the honest answer up to now was "standard OBD-II only, and we will
not invent DIDs". But the values are not secret either: every BMW ECU's own
SGBD file carries a table (SG_FUNKTIONEN) listing each readable status with the
argument name to ask for it, the identifier it lives at, the result it comes
back as, its unit and its scaling. This reads that table.

WHAT IT PRODUCES
`backend/profiles/bmw/live_pack.json` -- keyed by SGBD file, one entry per
readable status: {arg, did, result, label, unit}. `profiles/bmw/pids.py` reads
it and asks the car through EDIABAS's own STATUS_LESEN job, so the CAR's answer
(and EDIABAS's own decoding) is what reaches the screen -- this table only
decides what to ask for and what to call it.

THE PACK IS NOT COMMITTED. The harness repo is public and this is derived from
BMW's data files; the extractor (this file, our code) is committed, the output
is gitignored. Anyone with BMW Standard Tools installed regenerates it in a
second. Without the pack, pids.py still ships its own small curated set and
says plainly that the big pack is missing.

USAGE
    .venv\\Scripts\\python.exe -m backend.tools.bmw_live_extract
    .venv\\Scripts\\python.exe -m backend.tools.bmw_live_extract DME_BX8.prg ...

HOW THE TABLE IS READ
Literals in a .prg are stored XOR 0xF7, in file order, one row after another:

    ARG    DID      RESULT                     COMMENT          [UNIT]  A2L   ORDER  TYPE            [MUL DIV OFFSET]  SERVICES
    ITKUM  0x4300   STAT_KUEHLMITTELTEMPERATUR_WERT  Motor-Temperatur      tmot  High   unsigned char   0.75 1.0 -48.0   22;2C
    IPLAD  0x4205   STAT_LADEDRUCK_WERT        Druck vor Drossel  hPa    pvd_w High   unsigned int    20.0 256.0 0.0    22;2C

Rows are variable length -- the unit and the scaling triple are both optional --
so nothing is read by position. The identifier anchors a row, and each field is
recognised by shape. Anything that does not parse cleanly is skipped rather than
guessed at: a mislabelled gauge is worse than a missing one.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ECU_DIR = Path(r"C:\EDIABAS\ECU")
OUT_FILE = Path(__file__).resolve().parents[1] / "profiles" / "bmw" / "live_pack.json"

# The SGBDs worth packing: the variants verified on Steve's own cars, plus the
# group files' common members. Extending this list is free.
DEFAULT_SGBDS = [
    "DME_BX8.prg",   # F56 MINI B38/B48 petrol -- verified on the car
    "komb25.prg",    # F-series UKL cluster -- verified
    "DSC_I1.prg",    # F-series UKL ABS -- verified
    "FEM_20.prg",    # F-series UKL body -- verified
    "IHX_I1.prg",    # F-series UKL climate -- verified
    "zgw_01.PRG",    # gateway -- verified
    "ACSM4I.prg",    # airbag -- verified
    "FZD_F15.prg",   # roof centre -- verified
    "nbt.prg",       # NBT head unit -- verified
]

_STRING_RE = re.compile(rb"[\x20-\x7e]{2,}")
_DID_RE = re.compile(r"^0x[0-9A-Fa-f]{4}$")
_SERVICE_RE = re.compile(r"^[0-9A-Fa-f]{2}(;[0-9A-Fa-f]{2})*$")
_FLOAT_RE = re.compile(r"^-?\d+(\.\d+)?$")
_ARG_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
_BYTE_ORDER = {"High", "Low"}
_TYPES = {
    "unsigned char", "unsigned int", "unsigned long", "signed char",
    "signed int", "signed long", "char", "int", "long", "real", "string",
    "data", "binary",
}

# Units seen in these tables. Used only to tell a unit apart from a comment or
# an internal a2l name in a variable-length row -- an unrecognised unit costs a
# blank unit label, never a wrong one.
_UNITS = {
    "%", "V", "A", "Ah", "W", "hPa", "mbar", "bar", "kPa", "Pa", "°C", "C",
    "degreeC", "K", "1/min", "rpm", "km/h", "m/s", "km", "m", "mm", "s", "ms",
    "min", "h", "kg/h", "g/s", "l/h", "l", "Nm", "°", "°KW", "deg", "kW", "Hz",
    "mV", "mA", "µs", "us", "ppm", "mg/Hub", "kg", "g", "mg", "cd", "lx",
}


def strings_in_order(path: Path) -> list[str]:
    """Every literal in the file, decoded and in file order."""
    plain = bytes(c ^ 0xF7 for c in path.read_bytes())
    return [m.group(0).decode("latin-1") for m in _STRING_RE.finditer(plain)]


def _rows(words: list[str]) -> list[list[str]]:
    """Split the literal stream into candidate table rows.

    A row starts at the ARG immediately before an identifier and runs to the
    word before the next row's ARG.
    """
    starts = [
        i - 1
        for i, w in enumerate(words)
        if i and _DID_RE.match(w) and _ARG_RE.match(words[i - 1])
    ]
    out = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else min(start + 16, len(words))
        row = words[start:end]
        if len(row) <= 16:
            out.append(row)
    return out


def _parse(row: list[str]) -> dict | None:
    """One table row -> a channel, or None if it doesn't parse cleanly."""
    arg, did = row[0], row[1]
    rest = row[2:]
    if not rest:
        return None

    result = rest[0]
    if not result.startswith("STAT_") or not result.endswith(("_WERT", "_TEXT", "_HEX")):
        return None

    # The byte-order marker separates the human fields from the machine ones.
    try:
        order_at = next(i for i, w in enumerate(rest) if w in _BYTE_ORDER)
    except StopIteration:
        return None
    human = rest[1:order_at]
    machine = rest[order_at + 1:]

    unit = ""
    label = ""
    if human:
        # [comment], [unit], [a2l-name] -- any of which may be absent. Take the
        # unit only from the recognised set, and the label from the first field
        # that reads like prose rather than an internal symbol.
        units = [w for w in human if w in _UNITS]
        unit = units[0] if units else ""
        for word in human:
            if word in _UNITS:
                continue
            if len(word) > 3 and not word.startswith("STAT_"):
                label = word
                break

    scale = None
    numbers = [w for w in machine if _FLOAT_RE.match(w)]
    if len(numbers) >= 3:
        mul, div, offset = (float(x) for x in numbers[:3])
        if div:
            scale = {"mul": mul, "div": div, "offset": offset}

    kind = next((w for w in machine if w in _TYPES), None)
    services = [w for w in machine if _SERVICE_RE.match(w) and not _FLOAT_RE.match(w)]

    return {
        "arg": arg,
        "did": did,
        "result": result,
        "label": label,
        "unit": unit,
        "type": kind,
        "scale": scale,
        "services": services[-1] if services else None,
    }


def extract(path: Path) -> list[dict]:
    """Every readable status this SGBD documents, de-duplicated by argument."""
    channels: dict[str, dict] = {}
    for row in _rows(strings_in_order(path)):
        parsed = _parse(row)
        if parsed and parsed["arg"] not in channels:
            channels[parsed["arg"]] = parsed
    return list(channels.values())


def build(names: list[str] | None = None) -> dict:
    pack: dict[str, list[dict]] = {}
    missing: list[str] = []
    for name in names or DEFAULT_SGBDS:
        path = ECU_DIR / name
        if not path.is_file():
            matches = [f for f in ECU_DIR.glob("*") if f.name.lower() == name.lower()]
            if not matches:
                missing.append(name)
                continue
            path = matches[0]
        pack[path.stem.lower()] = extract(path)
    return {"sgbds": pack, "missing": missing}


def main() -> int:
    if not ECU_DIR.is_dir():
        print(f"No SGBD library at {ECU_DIR} -- nothing to extract.")
        return 1
    result = build(sys.argv[1:] or None)
    payload = {
        "_comment": (
            "Generated by backend/tools/bmw_live_extract.py from the SGBD files on "
            "this machine. Derived from BMW's data -- gitignored, never committed. "
            "Regenerate with: .venv\\Scripts\\python.exe -m backend.tools.bmw_live_extract"
        ),
        "sgbds": result["sgbds"],
    }
    OUT_FILE.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    total = sum(len(v) for v in result["sgbds"].values())
    for name, channels in sorted(result["sgbds"].items()):
        withunit = sum(1 for c in channels if c["unit"])
        print(f"  {name:14s} {len(channels):5d} channels ({withunit} with a unit)")
    if result["missing"]:
        print("  not on this machine: " + ", ".join(result["missing"]))
    print(f"{total} channels -> {OUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
