"""BMW/MINI EDIABAS capability: BMW's own job library, driven from the harness.

Instead of hand-writing every UDS exchange, this plugs the brand into the
SGBD job files that ship with BMW Standard Tools (C:\\EDIABAS\\ECU) -- the
same data Tool32/INPA use -- via core/ediabas.py (EdiabasTest subprocess over
the ENET cable).

Which jobs exist is DATA, not code: backend/profiles/bmw/ediabas_jobs.json.
"Add this function" = add an entry there. kind=write jobs always require
confirm:true at this layer, regardless of what the API caller asked for.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.core import ediabas

_JOBS_FILE = Path(__file__).with_name("ediabas_jobs.json")


class EdiabasJobError(Exception):
    pass


def _catalogue() -> dict:
    try:
        return json.loads(_JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EdiabasJobError(f"Job catalogue {_JOBS_FILE.name} is unreadable: {exc}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jobs_for(module_key: str) -> list[dict]:
    cat = _catalogue()
    jobs = list(cat.get("common", []))
    jobs.extend(cat.get("modules", {}).get(module_key, []))
    return jobs


def _find_job(module_key: str, job_name: str) -> dict:
    for entry in _jobs_for(module_key):
        if entry["name"].upper() == job_name.upper():
            return entry
    raise EdiabasJobError(
        f"Job '{job_name}' is not in the allowlist for module '{module_key}'. "
        "Only curated jobs from ediabas_jobs.json can run."
    )


def modules() -> dict:
    """What the EDIABAS bridge can see: availability + per-module job menus.

    No car contact -- this is the catalogue a UI renders.
    """
    avail = ediabas.availability()
    out = []
    for key, meta in ediabas.MODULES.items():
        jobs = _jobs_for(key)
        out.append({
            "key": key,
            "code": meta["code"],
            "name": meta["name"],
            "sgbds": meta["sgbds"],
            "verify": bool(meta.get("verify")),
            "jobs": [
                {
                    "name": j["name"],
                    "kind": j["kind"],
                    "description": j["description"],
                    "params": j.get("params", []),
                    "confirm": j.get("confirm"),
                    "verify": bool(j.get("verify")),
                }
                for j in jobs
            ],
        })
    return {
        "available": avail["available"],
        "detail": avail["detail"],
        "exe": avail["exe"],
        "ecuDir": avail["ecuDir"],
        "modules": out,
    }


def run_job(
    module_key: str,
    job_name: str,
    params: Optional[list[str]] = None,
    mode: str = "real",
    confirm: bool = False,
    remote_host: Optional[str] = None,
) -> dict:
    """Run one allowlisted job. Write jobs are refused without confirm=true."""
    if module_key not in ediabas.MODULES:
        raise EdiabasJobError(f"Unknown module '{module_key}'.")
    entry = _find_job(module_key, job_name)

    if entry["kind"] == "write" and not confirm:
        return {
            "ok": False,
            "needsConfirm": True,
            "simulated": False,
            "module": module_key,
            "job": entry["name"],
            "message": (
                f"'{entry['name']}' writes to the car. Re-run with confirm=true. "
                + (entry.get("confirm") or "")
            ),
            "timestamp": _now(),
        }

    for p in entry.get("params", []):
        if p.get("required") and (not params or not any(str(x).strip() for x in params)):
            return {
                "ok": False,
                "simulated": False,
                "module": module_key,
                "job": entry["name"],
                "message": f"Job needs parameter '{p['name']}': {p.get('hint', '')}",
                "timestamp": _now(),
            }

    if mode == "sim":
        return _simulated(module_key, entry, params)

    avail = ediabas.availability()
    if not avail["available"]:
        return {
            "ok": False,
            "simulated": False,
            "module": module_key,
            "job": entry["name"],
            "message": f"EDIABAS bridge unavailable: {avail['detail']}",
            "timestamp": _now(),
        }

    # Pin the gateway IP: the ENET interface's auto discovery proved
    # unreliable on the real car (IFH-0056 ILLEGAL CHANNEL) where the pinned
    # IP works. Discover once per call -- cheap, and honest about a lost car.
    host = remote_host
    if host is None:
        from backend.profiles.bmw import scan as _scan
        found = _scan.discover()
        if found["found"] and found["protocol"] == "HSFZ":
            host = found["ip"]

    # Read jobs may walk the SGBD candidates until one answers (a wrong group
    # file just errors harmlessly); WRITE jobs stay pinned to the first
    # candidate so a fallback can never write to the wrong variant.
    candidates = ediabas.MODULES[module_key]["sgbds"]
    if entry["kind"] == "write":
        candidates = candidates[:1]

    # Some SGBD variants name the same job differently (the F56's KOMB25
    # answers IDENT, not IDENTIFIKATION) -- the catalogue carries aliases.
    job_names = [entry["name"], *entry.get("aliases", [])]

    last = None
    for sgbd in candidates:
        for jn in job_names:
            try:
                result = ediabas.run_job(sgbd, jn, params=params, remote_host=host)
            except ediabas.EdiabasError as exc:
                last = {"sgbd": sgbd, "error": str(exc)}
                continue
            if result["ok"]:
                used = f" (as {jn})" if jn != entry["name"] else ""
                result.update({
                    "simulated": False,
                    "module": module_key,
                    "kind": entry["kind"],
                    "message": f"{entry['name']}{used} on {ediabas.MODULES[module_key]['code']} via {result['sgbd']}.",
                    "timestamp": _now(),
                })
                return result
            last = result

    detail = "no SGBD candidate answered"
    raw = None
    if last:
        err = last.get("error")
        detail = f"{last.get('sgbd')}: {err.get('detail') or err.get('code')}" if isinstance(err, dict) else str(last)
        raw = last.get("raw")
    return {
        "ok": False,
        "simulated": False,
        "module": module_key,
        "job": entry["name"],
        "raw": raw,
        "message": (
            f"Job failed on every candidate ({detail}). If the car is awake on the "
            "ENET cable, this module's SGBD mapping needs verifying."
        ),
        "timestamp": _now(),
    }


def _simulated(module_key: str, entry: dict, params: Optional[list[str]]) -> dict:
    fake_results = {"SIMULATED_RESULT": "no real car was contacted"}
    if entry["name"] == "IDENTIFIKATION":
        fake_results = {
            "SIM_SGBD": ediabas.MODULES[module_key]["sgbds"][0],
            "SIM_BAUSTAND": "SIMULATED",
            "SIM_VIN": "SIMULATED00000BMW",
        }
    return {
        "ok": True,
        "simulated": True,
        "module": module_key,
        "job": entry["name"],
        "kind": entry["kind"],
        "sgbd": ediabas.MODULES[module_key]["sgbds"][0],
        "results": fake_results,
        "raw": "SIMULATED -- job not run.",
        "message": f"SIMULATED -- {entry['name']} on {ediabas.MODULES[module_key]['code']} was not really run.",
        "timestamp": _now(),
    }
