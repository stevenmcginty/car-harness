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


def _fill_defaults(entry: dict, params: Optional[list[str]]) -> Optional[list[str]]:
    """Pad a positional argument list out with the catalogue's defaults.

    EDIABAS arguments are positional, and some jobs take a lot of them --
    CBS_RESET wants ten, of which nine are almost always the documented
    "no change" sentinel. Rather than make a caller type nine sentinels (or,
    worse, send a short list and have the SGBD read rubbish off the end), the
    catalogue carries each argument's default and this fills the gaps. Jobs
    whose params declare no defaults are passed through untouched.
    """
    spec = entry.get("params") or []
    if not spec or not any("default" in p for p in spec):
        return params
    given = list(params or [])
    out: list[str] = []
    for i, p in enumerate(spec):
        value = given[i].strip() if i < len(given) and given[i] is not None else ""
        out.append(value if value else str(p.get("default", "")))
    out.extend(str(x) for x in given[len(spec):])
    return out


def resolve_host(remote_host: Optional[str]) -> Optional[str]:
    """The gateway IP to talk to: the caller's, or one live discovery.

    Pinning matters -- EdiabasTest's own `auto:all` discovery answered
    IFH-0056 ILLEGAL CHANNEL on the real car where the pinned IP worked.
    """
    if remote_host:
        return remote_host
    from backend.profiles.bmw import scan as _scan
    found = _scan.discover()
    if found["found"] and found["protocol"] == "HSFZ":
        return found["ip"]
    return None


def installed_modules(mode: str = "real", remote_host: Optional[str] = None) -> dict:
    """Ask the gateway which control units this car actually has.

    The module scan otherwise probes a fixed address list and infers "not
    fitted" from silence -- which cannot tell a missing ECU from a sleeping
    one (the F56's manual gearbox vs a dead EGS looked identical). The ZGW
    holds the car's own SVT list, so this is the car's answer rather than
    ours: one result set per module, name and diagnostic address, mapped back
    to the job menus we know how to drive.
    """
    if mode == "sim":
        rows = [
            {"name": "ZGW_01", "address": "0x10", "module": "zgw"},
            {"name": "DME_BX8", "address": "0x12", "module": "dme"},
            {"name": "KOMB25", "address": "0x60", "module": "kombi"},
            {"name": "NBT", "address": "0x63", "module": "hu"},
        ]
        for r in rows:
            meta = ediabas.MODULES[r["module"]]
            r.update({"code": meta["code"], "label": meta["name"], "sgbds": meta["sgbds"], "known": True})
        return {
            "ok": True,
            "simulated": True,
            "count": len(rows),
            "modules": rows,
            "unmapped": 0,
            "message": "SIMULATED -- no gateway was asked.",
            "timestamp": _now(),
        }

    avail = ediabas.availability()
    if not avail["available"]:
        return {
            "ok": False, "simulated": False, "count": 0, "modules": [], "unmapped": 0,
            "message": f"EDIABAS bridge unavailable: {avail['detail']}",
            "timestamp": _now(),
        }

    result = run_job("zgw", "STATUS_VCM_GET_ECU_LIST_ALL", mode="real", remote_host=remote_host)
    if not result.get("ok"):
        return {
            "ok": False,
            "simulated": False,
            "count": 0,
            "modules": [],
            "unmapped": 0,
            "raw": result.get("raw"),
            "message": (
                "The gateway did not answer its installed-ECU list: "
                + str(result.get("message", "")).strip()
            ),
            "timestamp": _now(),
        }

    rows: list[dict] = []
    for entry in result.get("sets") or []:
        res = entry.get("results", {})
        name = res.get("STAT_SG_NAME_TEXT")
        address = res.get("STAT_SG_DIAG_ADRESSE")
        if not name and not address:
            continue  # set 0 is the job-wide set (JOB_STATUS etc.)
        key = ediabas.module_for_address(address)
        row = {
            "name": name,
            "address": address,
            "module": key,
            "known": key is not None,
            "code": None,
            "label": None,
            "sgbds": [],
        }
        if key:
            meta = ediabas.MODULES[key]
            row.update({"code": meta["code"], "label": meta["name"], "sgbds": meta["sgbds"]})
        rows.append(row)

    declared = result.get("results", {}).get("STAT_SG_ANZAHL")
    unmapped = sum(1 for r in rows if not r["known"])
    message = f"The gateway lists {len(rows)} control unit{'s' if len(rows) != 1 else ''} on this car"
    if declared and str(declared).strip() not in ("", str(len(rows))):
        message += f" (it reported a count of {declared})"
    if unmapped:
        message += f"; {unmapped} have no job menu in the harness yet"
    return {
        "ok": True,
        "simulated": False,
        "count": len(rows),
        "modules": rows,
        "unmapped": unmapped,
        "raw": result.get("raw"),
        "message": message + ".",
        "timestamp": _now(),
    }


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
                    "note": j.get("note"),
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

    params = _fill_defaults(entry, params)

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
    host = resolve_host(remote_host)

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
