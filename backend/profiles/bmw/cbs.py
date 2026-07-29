"""BMW/MINI CBS -- Condition Based Service -- read and reset over the ENET cable.

This is the daily job on a dealer's forecourt: the car has been serviced, and
the service indicator has to be told. Until now the harness could only show
Steve the dash procedure (backend/profiles/bmw/resets.py) because the raw CBS
reset telegram has never been published. It doesn't need to be: BMW's own SGBD
job library does it, and the harness can drive that library -- see
backend/core/ediabas.py.

THREE THINGS ABOUT CBS THAT COST TIME IF YOU DON'T KNOW THEM

1. CBS is NOT the iDrive service history. CBS lives in the modules (and is
   mirrored onto the key) and drives the "service due" display; the history is
   a separate record in the head unit -- backend/profiles/bmw/service_history.py.
   A full service wants both, and doing one does nothing to the other.

2. CBS is spread across several modules, not held in one. On the F56 the
   cluster (KOMB25), the engine (DME_BX8) and the ABS unit (DSC_I1) each carry
   the CBS jobs; the SGBD calls engine oil and brake pads "external" items,
   owned by the engine and ABS respectively, with the cluster holding copies.
   So a reset is aimed at the module that owns the item, with the cluster as
   the fallback.

3. The argument list depends on the CBS VERSION, and versions are mixed on one
   car. Verified from the F56's own SGBD files: cluster and engine are CBS
   version 5 (ten arguments), the ABS unit is version 6 (eleven -- it inserts
   CHOV_CBS_CBR_VIEW before the reserved byte). Sending a version-5 vector to a
   version-6 module shifts every value by one position. That is why every reset
   here asks CBS_INFO first instead of assuming.

Item table, argument lists and defaults were read from the decompiled SGBD
documentation for the exact files on this machine (docs/private/sgbd/), not
from forum tables. Nothing here is invented; anything unverified says so.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from backend.core import ediabas as core_ediabas
from backend.profiles.bmw import ediabas as bmw_ediabas

# Table CBSKENNUNG, read verbatim from komb25.prg's documentation. `code` is
# BMW's own numbering, which is also the service-history item code for the
# items the two records share -- so a CBS reset can offer to write the matching
# history line (see service_history.SERVICE_ITEMS).
ITEMS = [
    {"kennung": "Oel",   "code": 0x01, "label": "Engine oil",              "owner": "dme",   "german": "Motoroel"},
    {"kennung": "Br_v",  "code": 0x02, "label": "Front brake pads",        "owner": "dsc",   "german": "Bremsbelag vorne"},
    {"kennung": "Brfl",  "code": 0x03, "label": "Brake fluid",             "owner": "kombi", "german": "Bremsfluessigkeit",
     "note": "Brake fluid is the one item that accepts 150% availability -- BMW uses the extra headroom for a two-year interval."},
    {"kennung": "Bsi",   "code": 0x05, "label": "Service Inclusive",       "owner": "kombi", "german": "Service Inclusive"},
    {"kennung": "Br_h",  "code": 0x06, "label": "Rear brake pads",         "owner": "dsc",   "german": "Bremsbelag hinten"},
    {"kennung": "Sic",   "code": 0x11, "label": "Vehicle check",           "owner": "kombi", "german": "Sichtpruefung/Fahrzeug-Check"},
    {"kennung": "Ueb",   "code": 0x14, "label": "Pre-delivery inspection", "owner": "kombi", "german": "Uebergabedurchsicht"},
    {"kennung": "Efk",   "code": 0x15, "label": "Running-in check",        "owner": "kombi", "german": "Einfahrkontrolle"},
    {"kennung": "TUV",   "code": 0x20, "label": "Statutory inspection (MOT)", "owner": "kombi", "german": "Fahrzeuguntersuchung",
     "note": "Date-based: set the target month and year rather than a percentage."},
    {"kennung": "AU",    "code": 0x21, "label": "Emissions test",          "owner": "kombi", "german": "Abgasuntersuchung",
     "note": "Date-based: set the target month and year rather than a percentage."},
    {"kennung": "NOx_a", "code": 0x0D, "label": "NOx additive (AdBlue)",   "owner": "dme",   "german": "NOx-Additiv"},
    {"kennung": "Sic_v", "code": 0x64, "label": "Vehicle check (linked)",  "owner": "kombi", "german": "Sichtpruefung/Fahrzeug-Check verknuepft"},
]

# Modules that host the CBS jobs. Confirmed by reading the job tables out of
# the F56's own SGBD binaries: komb25, DME_BX8 and DSC_I1 have CBS_RESET /
# CBS_DATEN_LESEN / CBS_INFO; FEM_20, IHX_I1, NBT, ACSM4I and FZD_F15 do not.
CBS_MODULES = ("kombi", "dme", "dsc")

# Ordered argument vectors with BMW's documented "no change" defaults.
# Version 5 = F-series (komb25, DME_BX8); version 6 = the newer set (DSC_I1,
# KOMB_G11, KOMB_J29) which adds CHOV_CBS_CBR_VIEW in position 10.
_V5 = [
    ("CBS_KENNUNG", None),
    ("CBS_VERFUEGBARKEIT", "100"),
    ("CBS_ANZAHL_SERVICE", "31"),
    ("CBS_ZIEL_MONAT", "15"),
    ("CBS_ZIEL_JAHR", "63"),
    ("RMM_CBS_WERT", "32768"),
    ("ST_UN_CBS_RSTG", "15"),
    ("FRC_INTM_WAY_CBS_MESS", "255"),
    ("FRC_INTM_T_CBS_MESS", "255"),
    ("RES_BYTE", "0"),
]
ARGUMENTS = {
    5: _V5,
    6: _V5[:-1] + [("CHOV_CBS_CBR_VIEW", "0"), ("RES_BYTE", "0")],
}

# Sentinels, by name, so the code reads like the SGBD comments do.
NO_CHANGE_PERCENT = "255"
INCREMENT_COUNTER = "31"
NO_CHANGE_MONTH = "15"
NO_CHANGE_YEAR = "63"


class CbsError(Exception):
    """A bad request to this module -- unknown item, unusable value."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_item(name: str) -> dict:
    """Resolve an item by CBS identifier ("Oel"), code (1, "0x01") or label."""
    text = str(name).strip()
    if not text:
        raise CbsError("Name a service item, e.g. 'Oel' for engine oil.")
    low = text.lower()
    for item in ITEMS:
        if low in (item["kennung"].lower(), item["label"].lower()):
            return item
    for base in (16, 10):
        try:
            value = int(text, base)
        except ValueError:
            continue
        for item in ITEMS:
            if item["code"] == value:
                return item
    raise CbsError(
        f"'{name}' is not a CBS item. Known items: "
        + ", ".join(f"{i['kennung']} ({i['label']})" for i in ITEMS)
    )


def _item_by_code(value, base: int) -> Optional[dict]:
    """An item by its numeric CBS code, read in the given base. None if the
    value is absent or isn't a code we know."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if base == 16:
        text = text.removeprefix("0x")
    try:
        code = int(text, base)
    except ValueError:
        return None
    for item in ITEMS:
        if item["code"] == code:
            return item
    return None


def catalogue() -> dict:
    """What can be reset, and on which modules. No car contact."""
    avail = core_ediabas.availability()
    return {
        "available": avail["available"],
        "detail": avail["detail"],
        "items": [
            {
                "kennung": i["kennung"],
                "code": f"0x{i['code']:02X}",
                "label": i["label"],
                "owner": i["owner"],
                "german": i["german"],
                "note": i.get("note"),
            }
            for i in ITEMS
        ],
        "modules": [
            {"key": k, "code": core_ediabas.MODULES[k]["code"], "name": core_ediabas.MODULES[k]["name"]}
            for k in CBS_MODULES
        ],
        "versions": {str(v): [name for name, _ in spec] for v, spec in ARGUMENTS.items()},
    }


def parse_version(results: dict) -> Optional[int]:
    """The CBS version out of a CBS_INFO answer, or None if it didn't say one
    we know. Deliberately strict: guessing the version picks the wrong
    argument vector, which shifts every value the reset writes."""
    text = str(results.get("CBS_VERSION_TEXT") or "")
    digits = "".join(c for c in text if c.isdigit())
    if digits and int(digits[:2]) in ARGUMENTS:
        return int(digits[:2])
    hexed = str(results.get("CBS_VERSION_HEX") or "").strip().lower()
    if hexed:
        try:
            value = int(hexed.removeprefix("0x"), 16)
        except ValueError:
            return None
        if value in ARGUMENTS:
            return value
    return None


def _version_of(module_key: str, remote_host: Optional[str]) -> tuple[Optional[int], dict]:
    """Ask a module which CBS version it speaks. (None, result) if it won't say."""
    result = bmw_ediabas.run_job(module_key, "CBS_INFO", mode="real", remote_host=remote_host)
    if not result.get("ok"):
        return None, result
    return parse_version(result.get("results", {})), result


def _decode_sets(result: dict, module_key: str) -> list[dict]:
    """Turn a CBS_DATEN_LESEN answer into one row per service item."""
    rows: list[dict] = []
    for entry in result.get("sets") or []:
        res = entry.get("results", {})
        kennung_text = res.get("ID_FN_CBS_MESS_TEXT")
        kennung_hex = res.get("ID_FN_CBS_MESS_HEX")
        kennung_val = res.get("ID_FN_CBS_MESS_WERT")
        if kennung_text is None and kennung_hex is None and kennung_val is None:
            continue  # job-wide set
        # Identify by BMW's own code, hex string first (that is how the SGBD
        # writes it); the _WERT result is decimal and must not be read as hex,
        # or 20 decimal ("pre-delivery") reads as 0x20 ("MOT").
        item = _item_by_code(kennung_hex, base=16) or _item_by_code(kennung_val, base=10)
        if item is None and kennung_text:
            try:
                item = find_item(str(kennung_text))
            except CbsError:
                item = None
        rows.append({
            "kennung": item["kennung"] if item else (kennung_text or kennung_hex),
            "label": item["label"] if item else f"Unrecognised CBS item ({kennung_text or kennung_hex})",
            "code": f"0x{item['code']:02X}" if item else None,
            "known": item is not None,
            "module": module_key,
            "percent": res.get("AVAI_CBS_WERT"),
            "counter": res.get("COU_RSTG_CBS_MESS_WERT"),
            "remaining": res.get("RMMI_CBS_WERT"),
            "remainingUnit": res.get("ST_UN_CBS_TEXT") or res.get("RMMI_CBS_EINH"),
            "raw": res,
        })
    return rows


def read(mode: str = "real", remote_host: Optional[str] = None) -> dict:
    """Read every CBS item the car holds, across all modules that carry them.

    Read-only. Run it before a reset to see what the car thinks, and after one
    to prove the reset landed.
    """
    if mode == "sim":
        rows = [
            {"kennung": i["kennung"], "label": i["label"], "code": f"0x{i['code']:02X}", "known": True,
             "module": i["owner"], "percent": "100", "counter": "0", "remaining": "30000",
             "remainingUnit": "km", "raw": {"SIMULATED": "no car was read"}}
            for i in ITEMS[:5]
        ]
        return {
            "ok": True, "simulated": True, "items": rows,
            "modules": [{"module": m, "version": 5, "answered": True} for m in CBS_MODULES],
            "message": "SIMULATED -- no car was read.", "timestamp": _now(),
        }

    avail = core_ediabas.availability()
    if not avail["available"]:
        return {"ok": False, "simulated": False, "items": [], "modules": [],
                "message": f"EDIABAS bridge unavailable: {avail['detail']}", "timestamp": _now()}

    host = bmw_ediabas.resolve_host(remote_host)
    items: list[dict] = []
    module_states: list[dict] = []
    for module_key in CBS_MODULES:
        version, info = _version_of(module_key, host)
        data = bmw_ediabas.run_job(module_key, "CBS_DATEN_LESEN", mode="real", remote_host=host)
        rows = _decode_sets(data, module_key) if data.get("ok") else []
        module_states.append({
            "module": module_key,
            "code": core_ediabas.MODULES[module_key]["code"],
            "version": version,
            "answered": bool(data.get("ok")),
            "detail": None if data.get("ok") else str(data.get("message", "")).strip(),
            "sgbd": data.get("sgbd") or info.get("sgbd"),
        })
        items.extend(rows)

    answered = [m for m in module_states if m["answered"]]
    if not answered:
        return {
            "ok": False, "simulated": False, "items": [], "modules": module_states,
            "message": (
                "No module answered its CBS data. Check the ENET cable and turn the "
                "ignition on -- CBS reads need the car awake."
            ),
            "timestamp": _now(),
        }
    return {
        "ok": True,
        "simulated": False,
        "items": items,
        "modules": module_states,
        "message": (
            f"{len(items)} service item{'s' if len(items) != 1 else ''} read from "
            f"{len(answered)} module{'s' if len(answered) != 1 else ''}. Read-only -- "
            "nothing was changed."
        ),
        "timestamp": _now(),
    }


def _candidates(item: dict, module: Optional[str]) -> list[str]:
    """Which modules to aim the reset at, in order."""
    if module:
        if module not in CBS_MODULES:
            raise CbsError(
                f"'{module}' does not carry CBS jobs. Try one of: {', '.join(CBS_MODULES)}."
            )
        return [module]
    order = [item["owner"], "kombi"]
    return [m for i, m in enumerate(order) if m in CBS_MODULES and m not in order[:i]]


def reset(
    item: str,
    mode: str = "real",
    confirm: bool = False,
    percent: Optional[int] = None,
    target_month: Optional[int] = None,
    target_year: Optional[int] = None,
    module: Optional[str] = None,
    remote_host: Optional[str] = None,
) -> dict:
    """Reset one CBS item on the car. CHANGES THE CAR -- confirm required.

    percent: remaining life to write, default 100 (150 allowed for brake
    fluid). target_month/target_year: for the date-based MOT and emissions
    items -- year is the real year, converted to BMW's 2000-based number here.
    module: force a specific module; by default the item's owner is tried and
    the cluster is the fallback.
    """
    entry = find_item(item)

    if percent is not None:
        limit = 150 if entry["kennung"] == "Brfl" else 100
        if not 0 <= int(percent) <= limit:
            raise CbsError(
                f"Availability for {entry['label']} must be 0-{limit}%"
                + (" (brake fluid is the one item allowed 150)." if limit == 150 else ".")
            )

    year_arg = NO_CHANGE_YEAR
    if target_year is not None:
        year = int(target_year)
        if 2000 <= year <= 2239:
            year -= 2000
        if not 0 <= year <= 239:
            raise CbsError("Target year must be between 2000 and 2239.")
        year_arg = str(year)
    month_arg = NO_CHANGE_MONTH
    if target_month is not None:
        if not 1 <= int(target_month) <= 12:
            raise CbsError("Target month must be 1-12.")
        month_arg = str(int(target_month))

    targets = _candidates(entry, module)

    if not confirm:
        return {
            "ok": False,
            "needsConfirm": True,
            "simulated": False,
            "item": entry["kennung"],
            "label": entry["label"],
            "message": (
                f"Resetting '{entry['label']}' writes to the car's "
                f"{core_ediabas.MODULES[targets[0]]['name'].lower()}. Re-run with confirm=true. "
                "This is the service counter only -- it does not write the iDrive service history."
            ),
            "timestamp": _now(),
        }

    if mode == "sim":
        return {
            "ok": True, "simulated": True, "item": entry["kennung"], "label": entry["label"],
            "module": targets[0], "version": 5,
            "arguments": _build_arguments(entry, 5, percent, month_arg, year_arg),
            "message": f"SIMULATED -- '{entry['label']}' was not really reset.",
            "timestamp": _now(),
        }

    avail = core_ediabas.availability()
    if not avail["available"]:
        return {"ok": False, "simulated": False, "item": entry["kennung"],
                "message": f"EDIABAS bridge unavailable: {avail['detail']}", "timestamp": _now()}

    host = bmw_ediabas.resolve_host(remote_host)
    attempts: list[dict] = []
    for module_key in targets:
        version, info = _version_of(module_key, host)
        if version is None:
            attempts.append({
                "module": module_key,
                "version": None,
                "detail": (
                    "This module would not report its CBS version, so the argument "
                    "list can't be chosen safely -- not attempted. "
                    + str(info.get("message", "")).strip()
                ),
            })
            continue
        args = _build_arguments(entry, version, percent, month_arg, year_arg)
        result = bmw_ediabas.run_job(
            module_key, "CBS_RESET", params=args, mode="real", confirm=True, remote_host=host
        )
        status = str(result.get("results", {}).get("JOB_STATUS", "")).strip()
        ok = bool(result.get("ok")) and (status.upper().startswith("OKAY") or not status)
        attempts.append({
            "module": module_key,
            "version": version,
            "arguments": args,
            "jobStatus": status or None,
            "request": result.get("results", {}).get("_REQUEST"),
            "response": result.get("results", {}).get("_RESPONSE"),
            "detail": None if ok else str(result.get("message", "")).strip(),
            "raw": result.get("raw"),
        })
        if ok:
            return {
                "ok": True,
                "simulated": False,
                "item": entry["kennung"],
                "label": entry["label"],
                "module": module_key,
                "version": version,
                "arguments": args,
                "jobStatus": status or None,
                "request": attempts[-1]["request"],
                "attempts": attempts,
                "message": (
                    f"'{entry['label']}' reset on the "
                    f"{core_ediabas.MODULES[module_key]['name'].lower()} (CBS version {version}). "
                    "Check the car's Service Requirements screen. The iDrive service "
                    "history is a separate record and has NOT been written."
                ),
                "timestamp": _now(),
            }

    return {
        "ok": False,
        "simulated": False,
        "item": entry["kennung"],
        "label": entry["label"],
        "attempts": attempts,
        "message": (
            f"'{entry['label']}' was not reset -- no module accepted the job. "
            "Nothing was written. Common causes: the car needs to be fully awake "
            "(ignition on, and some clusters want driving-ready), or this item is "
            "carried by a module the harness hasn't mapped on this car."
        ),
        "timestamp": _now(),
    }


def _build_arguments(
    entry: dict,
    version: int,
    percent: Optional[int],
    month_arg: str,
    year_arg: str,
) -> list[str]:
    """The positional argument vector for one CBS_RESET, per CBS version."""
    spec = ARGUMENTS.get(version)
    if spec is None:
        raise CbsError(
            f"CBS version {version} isn't one this harness has an argument list for "
            f"(known: {', '.join(str(v) for v in sorted(ARGUMENTS))})."
        )
    overrides = {
        "CBS_KENNUNG": entry["kennung"],
        "CBS_ZIEL_MONAT": month_arg,
        "CBS_ZIEL_JAHR": year_arg,
    }
    if percent is not None:
        overrides["CBS_VERFUEGBARKEIT"] = str(int(percent))
    return [str(overrides.get(name, default)) for name, default in spec]
