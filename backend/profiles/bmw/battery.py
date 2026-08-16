"""BMW/MINI battery: condition, and registering a replacement.

Fitting a battery to a modern BMW and not telling the car is a real fault, not
a formality. Power management keeps a charge history and a state-of-health
figure for the battery it thinks is fitted; leave that history in place and the
car keeps charging a brand-new battery on an old battery's strategy, holds
start/stop off, and eventually reports a battery fault against a good battery.

Registering is one routine -- STEUERN_BATTERIETAUSCH_REGISTRIEREN, UDS
$31 01 F030 -- on the engine ECU, and it takes no arguments. It was found by
reading the job table straight out of the F56's own DME_BX8.prg (and it is in
the N55/N62/S55/S63/POW_E65 families too), so this is BMW's own function, not
a guessed telegram.

WHAT THIS DOES NOT DO: registering says "the battery was replaced". It does
NOT change the coded battery TYPE or CAPACITY. Fitting a different size, or
swapping lead-acid for AGM, needs the car's vehicle order coding changed as
well -- that is E-Sys/ISTA territory and deliberately out of scope here. Told
plainly in the result rather than silently ignored.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from backend.core import ediabas as core_ediabas
from backend.profiles.bmw import ediabas as bmw_ediabas

MODULE = "dme"
REGISTER_JOB = "STEUERN_BATTERIETAUSCH_REGISTRIEREN"

# (job, result-with-the-value, label, unit-or-None-to-read-the-_EINH-result).
# Every one of these came from the SGBD's own documented result list, with the
# unit BMW states -- no scaling of our own, because EDIABAS has already applied
# the SGBD's conversion by the time we see the value.
READINGS = [
    ("STATUS_MESSWERTE_IBS", "STAT_U_BATT_WERT", "Battery voltage", "V"),
    ("STATUS_MESSWERTE_IBS", "STAT_I_BATT_WERT", "Battery current", "A"),
    ("STATUS_MESSWERTE_IBS", "STAT_T_BATT_WERT", "Battery temperature", "°C"),
    ("STATUS_SYSTEMCHECK_AEP_INFO_2", "STAT_SOH_WERT", "State of health", "%"),
    ("STATUS_SYSTEMCHECK_AEP_INFO_2", "STAT_SOC_FIT_WERT", "State of charge", "%"),
    ("STATUS_SYSTEMCHECK_AEP_INFO_2", "STAT_BATTERIE_KAPAZITAET_WERT", "Rated capacity", "Ah"),
    ("STATUS_SYSTEMCHECK_AEP_INFO_1", "STAT_BATTERIELADUNG_SALDO_WERT", "Charge balance", "Ah"),
    ("STATUS_SYSTEMCHECK_AEP_INFO_1", "STAT_BATTERIETAUSCH_LETZTER_WERT", "Mileage at last battery change", "km"),
    ("STATUS_SYSTEMCHECK_AEP_INFO_1", "STAT_BATTERIETAUSCH_ZWEITLETZTER_WERT", "Mileage at previous battery change", "km"),
]

# The reading that proves a registration landed: the car writes its current
# mileage against "last battery change".
PROOF_RESULT = "STAT_BATTERIETAUSCH_LETZTER_WERT"

WARNING = (
    "Registering tells the car the battery is NEW. It does not change the coded "
    "battery type or capacity -- fitting a different size, or AGM in place of "
    "lead-acid, also needs the car's coding changed, which this harness does not do."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_jobs(host: Optional[str]) -> tuple[dict, list[dict]]:
    """Run each battery-status job once; return {job: results} and the failures."""
    answers: dict[str, dict] = {}
    problems: list[dict] = []
    for job in dict.fromkeys(job for job, *_ in READINGS):
        result = bmw_ediabas.run_job(MODULE, job, mode="real", remote_host=host)
        if result.get("ok"):
            answers[job] = result.get("results", {})
        else:
            problems.append({"job": job, "detail": str(result.get("message", "")).strip()})
    return answers, problems


def _collect(answers: dict) -> list[dict]:
    readings = []
    for job, key, label, unit in READINGS:
        results = answers.get(job)
        if not results or key not in results:
            continue
        readings.append({
            "name": label,
            "value": results[key],
            "unit": results.get(key.replace("_WERT", "_EINH")) or unit,
            "job": job,
            "result": key,
        })
    return readings


def _sim_readings() -> list[dict]:
    values = ["12.4", "-0.6", "18.5", "87", "76", "70", "-1.2", "34120", "0"]
    return [
        {"name": label, "value": value, "unit": unit, "job": job, "result": key, "simulated": True}
        for (job, key, label, unit), value in zip(READINGS, values)
    ]


def status(mode: str = "real", remote_host: Optional[str] = None) -> dict:
    """Read battery condition from the engine's power management. Read-only."""
    if mode == "sim":
        return {
            "ok": True, "simulated": True, "readings": _sim_readings(), "problems": [],
            "warning": WARNING,
            "message": "SIMULATED -- no car was read.", "timestamp": _now(),
        }

    avail = core_ediabas.availability()
    if not avail["available"]:
        return {"ok": False, "simulated": False, "readings": [], "problems": [],
                "message": f"EDIABAS bridge unavailable: {avail['detail']}", "timestamp": _now()}

    host = bmw_ediabas.resolve_host(remote_host)
    answers, problems = _read_jobs(host)
    readings = _collect(answers)
    if not readings:
        return {
            "ok": False, "simulated": False, "readings": [], "problems": problems,
            "message": (
                "The engine ECU gave no battery data. It has to be awake -- ignition "
                "on, ENET cable connected. On a car whose engine ECU speaks a "
                "different SGBD these job names may differ; the raw job browser will "
                "show what it does have."
            ),
            "timestamp": _now(),
        }
    return {
        "ok": True,
        "simulated": False,
        "readings": readings,
        "problems": problems,
        "warning": WARNING,
        "message": f"{len(readings)} battery readings from the engine's power management.",
        "timestamp": _now(),
    }


def register(mode: str = "real", confirm: bool = False, remote_host: Optional[str] = None) -> dict:
    """Register a battery replacement with power management. CHANGES THE CAR.

    Reads the "mileage at last battery change" before and after, so the result
    can show the car's own evidence that it took -- rather than just reporting
    that a job returned OKAY.
    """
    if not confirm:
        return {
            "ok": False,
            "needsConfirm": True,
            "simulated": False,
            "message": (
                "Registering resets the engine's battery charge history. Only do this "
                "if a new battery really has been fitted. Re-run with confirm=true. "
                + WARNING
            ),
            "timestamp": _now(),
        }

    if mode == "sim":
        return {
            "ok": True, "simulated": True, "before": "12500", "after": "34120",
            "jobStatus": "OKAY", "warning": WARNING,
            "message": "SIMULATED -- no battery registration was really sent.",
            "timestamp": _now(),
        }

    avail = core_ediabas.availability()
    if not avail["available"]:
        return {"ok": False, "simulated": False,
                "message": f"EDIABAS bridge unavailable: {avail['detail']}", "timestamp": _now()}

    host = bmw_ediabas.resolve_host(remote_host)
    before = bmw_ediabas.run_job(MODULE, "STATUS_SYSTEMCHECK_AEP_INFO_1", mode="real", remote_host=host)
    before_km = before.get("results", {}).get(PROOF_RESULT) if before.get("ok") else None

    result = bmw_ediabas.run_job(MODULE, REGISTER_JOB, mode="real", confirm=True, remote_host=host)
    status_text = str(result.get("results", {}).get("JOB_STATUS", "")).strip()
    ok = bool(result.get("ok")) and (status_text.upper().startswith("OKAY") or not status_text)
    if not ok:
        return {
            "ok": False,
            "simulated": False,
            "before": before_km,
            "jobStatus": status_text or None,
            "raw": result.get("raw"),
            "message": (
                "The engine ECU did not accept the battery registration, so nothing "
                "was changed: " + str(result.get("message", "")).strip()
            ),
            "timestamp": _now(),
        }

    after = bmw_ediabas.run_job(MODULE, "STATUS_SYSTEMCHECK_AEP_INFO_1", mode="real", remote_host=host)
    after_km = after.get("results", {}).get(PROOF_RESULT) if after.get("ok") else None

    message = "Battery replacement registered with the engine's power management."
    if after_km and after_km != before_km:
        message += f" The car now records the change at {after_km} km (was {before_km})."
    elif after_km:
        message += (
            f" The recorded change mileage still reads {after_km} -- the job was "
            "accepted, but check the figure against the odometer before trusting it."
        )
    return {
        "ok": True,
        "simulated": False,
        "before": before_km,
        "after": after_km,
        "jobStatus": status_text or None,
        "request": result.get("results", {}).get("_REQUEST"),
        "warning": WARNING,
        "message": message,
        "timestamp": _now(),
    }
