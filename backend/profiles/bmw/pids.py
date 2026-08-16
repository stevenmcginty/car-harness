"""BMW/MINI live data over the ENET cable.

BMW was the last brand in the harness with no live-data layer, and the reason
was honest: unlike Mazda or Fiat, BMW's measured values are not published as
mode-22 identifiers, and the standing rule here is that we do not invent DIDs.

The way in is BMW's own job library. Every ECU's SGBD carries a table of the
statuses it can report -- the argument name to ask for, the identifier, the
result it comes back as, its unit and its scaling -- and the ECU answers those
through the standard STATUS_LESEN job. So the harness asks the car, in BMW's
own vocabulary, and EDIABAS does the decoding. Nothing here computes a physical
value from raw bytes: whatever reaches the screen is what the car said.

TWO SOURCES OF CHANNELS
1. `live_pack.json` -- the big one, generated on this machine by
   backend/tools/bmw_live_extract.py from the installed SGBDs. Engine coolant
   and oil temperature, engine and road speed, boost, intake air temperature,
   ignition angles, lambda... 700+ on a modern petrol DME alone. NOT in the
   repo (it is derived from BMW's data files and this repo is public), so a
   fresh clone has to run the extractor. Its absence is reported, not hidden.
2. JOB_CHANNELS below -- our own small curated list of no-argument jobs whose
   results are documented physical values: battery voltage/current/temperature
   and state of health from the engine's power management, supply voltage and
   the displayed odometer from the cluster, the stored mileage in the body
   module. These ship with the harness.

WHAT IS STILL A GAP: the cluster, climate and ABS modules on this platform do
not carry the argument-indexed status table the engine does -- their live
values sit behind job-specific result decoders. So BMW live data is engine-deep
and thin elsewhere. Said plainly rather than padded out with guesses.

SPEED: every read is an EdiabasTest subprocess, about a second. Reading 700
channels one at a time would be useless, so this implements livefeed's rich
interface -- only the channels on screen are read, batched into as few job
calls as possible (STATUS_LESEN takes many statuses at once).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.core import ediabas as core_ediabas
from backend.profiles.bmw import ediabas as bmw_ediabas

_PACK_FILE = Path(__file__).with_name("live_pack.json")

# How many statuses to ask for in one STATUS_LESEN call. Keeps the command
# line sane and one unlucky status from spoiling a big batch.
BATCH = 12

# Curated channels from no-argument jobs, with the module that carries them.
# (key, module, job, result, label, unit, group). Every one is a documented
# physical value from that module's own SGBD.
JOB_CHANNELS = [
    ("batt_volts",    "dme", "STATUS_MESSWERTE_IBS", "STAT_U_BATT_WERT", "Battery voltage", "V", "Battery"),
    ("batt_amps",     "dme", "STATUS_MESSWERTE_IBS", "STAT_I_BATT_WERT", "Battery current", "A", "Battery"),
    ("batt_temp",     "dme", "STATUS_MESSWERTE_IBS", "STAT_T_BATT_WERT", "Battery temperature", "°C", "Battery"),
    ("batt_soh",      "dme", "STATUS_SYSTEMCHECK_AEP_INFO_2", "STAT_SOH_WERT", "Battery state of health", "%", "Battery"),
    ("batt_soc",      "dme", "STATUS_SYSTEMCHECK_AEP_INFO_2", "STAT_SOC_FIT_WERT", "Battery state of charge", "%", "Battery"),
    ("batt_capacity", "dme", "STATUS_SYSTEMCHECK_AEP_INFO_2", "STAT_BATTERIE_KAPAZITAET_WERT", "Battery rated capacity", "Ah", "Battery"),
    ("batt_balance",  "dme", "STATUS_SYSTEMCHECK_AEP_INFO_1", "STAT_BATTERIELADUNG_SALDO_WERT", "Battery charge balance", "Ah", "Battery"),
    ("batt_lastswap", "dme", "STATUS_SYSTEMCHECK_AEP_INFO_1", "STAT_BATTERIETAUSCH_LETZTER_WERT", "Mileage at last battery change", "km", "Battery"),
    ("kombi_volts",   "kombi", "STATUS_VERSORGUNGSSPANNUNG", "STAT_MILLI_VOLT_WERT", "Supply voltage at the cluster", "mV", "Electrical"),
    ("odometer",      "kombi", "STATUS_GWSZ_ANZEIGE", "STAT_GWSZ_ANZEIGE_WERT", "Odometer (as displayed)", "km", "Vehicle"),
    ("odometer_body", "fem_bdc", "STATUS_KILOMETERSTAND", "STAT_KILOMETERSTAND_WERT", "Odometer (stored in the body module)", "km", "Vehicle"),
]

# Result names worth putting an English label on -- the ones a dealer reads.
# Anything not listed keeps a tidied version of BMW's own result name, which is
# honest and searchable; a wrong translation would not be.
LABELS = {
    "STAT_MOTORDREHZAHL_WERT": ("Engine speed", "Engine", "1/min"),
    "STAT_KUEHLMITTELTEMPERATUR_WERT": ("Coolant temperature", "Temperatures", "°C"),
    "STAT_OELTEMPERATUR_WERT": ("Oil temperature", "Temperatures", "°C"),
    "STAT_ANSAUGLUFTTEMPERATUR_WERT": ("Intake air temperature", "Temperatures", "°C"),
    "STAT_UMGEBUNGSTEMPERATUR_WERT": ("Ambient temperature", "Temperatures", "°C"),
    "STAT_GESCHWINDIGKEIT_WERT": ("Vehicle speed", "Vehicle", "km/h"),
    "STAT_LADEDRUCK_WERT": ("Boost pressure (before throttle)", "Pressures", "hPa"),
    "STAT_UMGEBUNGSDRUCK_WERT": ("Ambient pressure", "Pressures", "hPa"),
    "STAT_OELDRUCK_WERT": ("Oil pressure", "Pressures", ""),
    "STAT_KRAFTSTOFFDRUCK_WERT": ("Fuel pressure", "Pressures", ""),
    "STAT_RAILDRUCK_WERT": ("Fuel rail pressure", "Pressures", ""),
    "STAT_BATTERIESPANNUNG_WERT": ("Battery voltage (engine ECU)", "Electrical", "V"),
    "STAT_MOTORMOMENT_WERT": ("Engine torque", "Engine", "Nm"),
    "STAT_LUFTMASSE_WERT": ("Air mass flow", "Engine", "kg/h"),
    "STAT_DROSSELKLAPPE_WERT": ("Throttle position", "Engine", "%"),
    "STAT_PEDALWERT_WERT": ("Accelerator pedal", "Engine", "%"),
    "STAT_FAHRPEDAL_WERT": ("Accelerator pedal", "Engine", "%"),
    "STAT_LAMBDA_BANK1_WERT": ("Lambda, bank 1", "Emissions", ""),
    "STAT_LAMBDA_BANK2_WERT": ("Lambda, bank 2", "Emissions", ""),
    "STAT_KATALYSATORTEMPERATUR_WERT": ("Catalyst temperature", "Emissions", "°C"),
    "STAT_GETRIEBETMP_WERT": ("Gearbox temperature", "Temperatures", "°C"),
}

# The channels a live view should open on, in order, when the user has not
# chosen any. Keys are looked up in both sources; missing ones are skipped.
FEATURED = [
    "batt_volts", "batt_soh", "odometer",
    "dme_stat_motordrehzahl_wert", "dme_stat_kuehlmitteltemperatur_wert",
    "dme_stat_oeltemperatur_wert", "dme_stat_ansauglufttemperatur_wert",
    "dme_stat_geschwindigkeit_wert", "dme_stat_ladedruck_wert",
]

_GROUP_HINTS = (
    ("temperatur", "Temperatures"),
    ("druck", "Pressures"),
    ("spannung", "Electrical"),
    ("strom", "Electrical"),
    ("batterie", "Battery"),
    ("lambda", "Emissions"),
    ("kat", "Emissions"),
    ("abgas", "Emissions"),
    ("drehzahl", "Engine"),
    ("moment", "Engine"),
    ("kilometer", "Vehicle"),
    ("geschwindigkeit", "Vehicle"),
    ("cbs", "Service"),
)


class LivePackMissing(Exception):
    """The generated pack isn't on this machine."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_pack() -> dict:
    try:
        return json.loads(_PACK_FILE.read_text(encoding="utf-8")).get("sgbds", {})
    except (OSError, ValueError):
        return {}


def _pretty(result: str) -> str:
    """A readable label from a BMW result name, when we have no better one."""
    words = result.removeprefix("STAT_").removesuffix("_WERT").split("_")
    return " ".join(w.capitalize() if w.isupper() else w for w in words if w) or result


def _group(result: str, unit: str) -> str:
    low = result.lower()
    for hint, group in _GROUP_HINTS:
        if hint in low:
            return group
    if unit in ("°C", "C", "degreeC"):
        return "Temperatures"
    if unit in ("hPa", "bar", "mbar", "kPa"):
        return "Pressures"
    if unit in ("V", "mV", "A", "mA"):
        return "Electrical"
    return "Engine"


# Which SGBD in the pack belongs to which harness module. The pack is keyed by
# SGBD file because that is what it was read from; a car tells us its variant
# at scan time, and until it does the engine pack is the one worth offering.
PACK_MODULES = {"dme_bx8": "dme", "komb25": "kombi", "dsc_i1": "dsc",
                "fem_20": "fem_bdc", "ihx_i1": "ihka"}


def _pack_channels() -> list[dict]:
    out: list[dict] = []
    for sgbd, entries in _load_pack().items():
        module = PACK_MODULES.get(sgbd)
        if module is None:
            continue
        for entry in entries:
            result = entry.get("result") or ""
            if not result.endswith("_WERT") or not entry.get("arg"):
                continue
            label, group, known_unit = LABELS.get(result, (None, None, ""))
            out.append({
                "key": f"{module}_{result.lower()}",
                "label": label or _pretty(result),
                # BMW leaves the unit column empty on most rows; for the
                # curated few the unit is not in doubt (a coolant temperature
                # is degrees), and on a real read the car's own _EINH wins.
                "unit": entry.get("unit") or known_unit,
                "group": group or _group(result, entry.get("unit") or ""),
                "module": module,
                "job": "STATUS_LESEN",
                "arg": entry["arg"],
                "result": result,
                "description": entry.get("label") or None,
                "sgbd": sgbd,
            })
    return out


def _job_channels() -> list[dict]:
    return [
        {"key": key, "label": label, "unit": unit, "group": group, "module": module,
         "job": job, "arg": None, "result": result, "description": None, "sgbd": None}
        for key, module, job, result, label, unit, group in JOB_CHANNELS
    ]


def _all_channels() -> list[dict]:
    channels, seen = [], set()
    for channel in _job_channels() + _pack_channels():
        if channel["key"] in seen:
            continue
        seen.add(channel["key"])
        channels.append(channel)
    return channels


CHANNELS = _all_channels()
_BY_KEY = {c["key"]: c for c in CHANNELS}


def pack_state() -> dict:
    """Whether the generated pack is present, and what it holds."""
    pack = _load_pack()
    return {
        "present": bool(pack),
        "file": str(_PACK_FILE),
        "sgbds": {name: len(entries) for name, entries in pack.items() if entries},
        "channels": len(CHANNELS),
        "detail": (
            f"{len(CHANNELS)} live channels available."
            if pack else
            "Only the built-in channels are available. The full engine pack is "
            "generated from the SGBD files on the machine with BMW Standard Tools "
            "installed: python -m backend.tools.bmw_live_extract"
        ),
    }


def _sim_value(channel: dict, seed: int) -> str:
    unit, key = channel["unit"], channel["key"]
    if "soh" in key or "soc" in key:
        return str(80 + seed % 15)
    if unit == "V":
        return f"{12.2 + (seed % 9) / 10:.1f}"
    if unit == "mV":
        return str(12400 + seed % 600)
    if unit in ("°C", "C"):
        return str(20 + seed % 70)
    if unit == "1/min":
        return str(700 + seed % 900)
    if unit == "km":
        return str(34120 + seed % 5)
    if unit == "hPa":
        return str(980 + seed % 60)
    return str(seed % 100)


def read_channels(adapter=None, mode: str = "real", keys: Optional[list] = None) -> list[dict]:
    """livefeed's fast path: read only the requested channels.

    `adapter` is part of livefeed's shared signature and is unused here -- BMW
    talks over its own ENET cable, not through a picked dongle.
    """
    wanted = [_BY_KEY[k] for k in (keys or []) if k in _BY_KEY]
    if not wanted:
        return []

    if mode == "sim":
        return [
            {"key": c["key"], "value": _sim_value(c, i * 7 + len(c["key"])), "unit": c["unit"]}
            for i, c in enumerate(wanted)
        ]

    if not core_ediabas.availability()["available"]:
        return []

    host = bmw_ediabas.resolve_host(None)
    out: list[dict] = []

    # One call per (module, job); STATUS_LESEN carries a batch of statuses.
    batches: dict[tuple[str, str], list[dict]] = {}
    for channel in wanted:
        batches.setdefault((channel["module"], channel["job"]), []).append(channel)

    for (module, job), group in batches.items():
        chunks = ([group[i:i + BATCH] for i in range(0, len(group), BATCH)]
                  if job == "STATUS_LESEN" else [group])
        for chunk in chunks:
            params = ["ARG"] + [c["arg"] for c in chunk] if job == "STATUS_LESEN" else None
            try:
                result = bmw_ediabas.run_job(module, job, params=params, mode="real", remote_host=host)
            except bmw_ediabas.EdiabasJobError:
                continue
            if not result.get("ok"):
                continue
            results = result.get("results", {})
            for channel in chunk:
                if channel["result"] not in results:
                    continue
                unit = results.get(channel["result"].replace("_WERT", "_EINH"))
                out.append({
                    "key": channel["key"],
                    "value": results[channel["result"]],
                    # The car's own unit wins; the table's is the fallback.
                    "unit": unit or channel["unit"],
                })
    return out


def live(mode: str = "real") -> dict:
    """The brand contract: a readable set of live values.

    Reads the featured channels rather than all of them -- every EDIABAS call
    is a subprocess, and a 700-channel sweep would take ten minutes. The Live
    Data page reads exactly what is on screen through read_channels().
    """
    featured = [k for k in FEATURED if k in _BY_KEY]
    rows = read_channels(None, mode, featured)
    readings = [
        {
            "name": _BY_KEY[row["key"]]["label"],
            "value": row["value"],
            "unit": row["unit"],
        }
        for row in rows
    ]
    state = pack_state()
    if mode == "sim":
        return {"ok": True, "simulated": True, "readings": readings,
                "message": f"SIMULATED -- {len(readings)} BMW values, no car connected.",
                "timestamp": _now()}
    if not readings:
        return {
            "ok": False, "simulated": False, "readings": [],
            "message": (
                "No BMW live values answered. The car has to be awake on the ENET "
                "cable with the ignition on. " + state["detail"]
            ),
            "timestamp": _now(),
        }
    return {
        "ok": True,
        "simulated": False,
        "readings": readings,
        "message": (
            f"{len(readings)} live values from the car's own modules "
            f"({len(CHANNELS)} channels selectable)."
        ),
        "timestamp": _now(),
    }
