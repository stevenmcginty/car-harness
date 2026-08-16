"""EDIABAS engine: run BMW/MINI SGBD jobs from the harness over ENET.

BMW's own diagnostic data library (the .prg/.grp "SGBD" files that ship with
BMW Standard Tools, conventionally at C:\\EDIABAS\\ECU) is the official job
catalogue for every module: idents, fault memory, live values, service
functions -- each with the exact telegrams and result decoders baked in. The
harness drives those jobs through EdiabasTest.exe (the scriptable Tool32
replacement from the open-source ediabaslib project, already vetted on disk)
as a subprocess; the car side is the same ENET cable core/hsfz.py uses.

This module is deliberately just the pipe: WHICH jobs may run (and which need
confirmation) is decided one layer up, in backend/profiles/<brand>/ediabas.py,
against a curated allowlist. Nothing here flashes, codes, or does
SecurityAccess -- same rule as core/uds.py.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

_DEFAULT_EXE = Path(r"C:\BMW\ediabaslib\EdiabasTest\EdiabasTest.exe")
_DEFAULT_ECU_DIR = Path(r"C:\EDIABAS\ECU")

# Module key -> ordered SGBD candidates. A .grp group file resolves the exact
# variant ON THE CAR (its init job asks the ECU which .prg it is), so it is
# always tried first; bare .prg files are fallbacks for modules with no group.
# "verify" flags the candidates not yet confirmed against a real car -- read
# jobs may walk candidates harmlessly, writes stay pinned (see profiles layer).
# Verified on the real MINI F56 (VIN WMWXS72030T850490) 2026-07-29:
# zgw=ZGW_01, dme=DME_BX8, dsc=DSC_I1, fem_bdc=FEM_20, ihka=IHX_I1, hu=NBT,
# fzd=FZD_F15, acsm=ACSM4I, kombi=KOMB25. egs gave NO RESPONSE because that car
# is a MANUAL (owner-confirmed -- no gearbox ECU fitted); eps mapping is still
# unconfirmed (UKL EPS variant unknown) -- both stay flagged verify.
#
# "addr" is the module's diagnostic address, the same number
# profiles/bmw/scan.py walks -- it is what lets the gateway's installed-ECU
# list (STATUS_VCM_GET_ECU_LIST_ALL) be matched back to a job menu.
MODULES = {
    "zgw":    {"code": "ZGW",     "name": "Central gateway",                 "addr": 0x10, "sgbds": ["g_zgw.grp"]},
    "dme":    {"code": "DME/DDE", "name": "Engine",                          "addr": 0x12, "sgbds": ["g_motor.grp"]},
    "egs":    {"code": "EGS",     "name": "Automatic transmission",          "addr": 0x18, "sgbds": ["g_egs.grp"], "verify": True},
    "acsm":   {"code": "ACSM",    "name": "Airbag / crash safety",           "addr": 0x01, "sgbds": ["g_airbag.grp"]},
    "dsc":    {"code": "DSC",     "name": "ABS / stability control",         "addr": 0x29, "sgbds": ["g_dsc.grp"]},
    "eps":    {"code": "EPS",     "name": "Power steering",                  "addr": 0x30, "sgbds": ["g_eps.grp"], "verify": True},
    "fem_bdc": {"code": "FEM/BDC", "name": "Body electronics",               "addr": 0x40, "sgbds": ["FEM_20.prg", "BDC.prg", "g_fxx.grp"]},
    "rem":    {"code": "REM",     "name": "Rear body electronics",           "addr": 0x72, "sgbds": ["g_remabf.grp"], "verify": True},
    "kombi":  {"code": "KOMBI",   "name": "Instrument cluster",              "addr": 0x60, "sgbds": ["g_kombi.grp"]},
    "hu":     {"code": "HU",      "name": "Head unit (nav / radio)",         "addr": 0x63, "sgbds": ["g_mmi.grp"]},
    "ihka":   {"code": "IHKA",    "name": "Climate control",                 "addr": 0x78, "sgbds": ["g_klima.grp", "g_klima2.grp", "G_KLIMA3.grp"]},
    "kafas":  {"code": "KAFAS",   "name": "Camera / driver assistance",      "addr": 0x5D, "sgbds": ["g_kafas.grp"], "verify": True},
    "gws":    {"code": "GWS",     "name": "Gear selector",                   "addr": 0x5E, "sgbds": ["g_gws.grp"], "verify": True},
    "fzd":    {"code": "FZD",     "name": "Roof function centre",            "addr": 0x56, "sgbds": ["g_fzd.grp"]},
    "zbe":    {"code": "ZBE",     "name": "iDrive controller",               "addr": 0x67, "sgbds": ["g_zbe.grp", "g_zbef.grp"], "verify": True},
    "hud":    {"code": "HUD",     "name": "Head-up display",                 "addr": 0x3D, "sgbds": ["g_hud.grp"], "verify": True},
    "ahm":    {"code": "AHM",     "name": "Trailer module",                  "addr": 0x71, "sgbds": ["g_ahm.grp"], "verify": True},
    "tcb":    {"code": "TCB",     "name": "Telematics",                      "addr": 0x36, "sgbds": ["g_tel.grp"], "verify": True},
    "vdm":    {"code": "VDM",     "name": "Adaptive suspension",             "addr": 0x21, "sgbds": ["g_vdc.grp"], "verify": True},
    "pma":    {"code": "PMA",     "name": "Parking assistant",               "addr": 0x64, "sgbds": ["g_pma.grp", "g_pdc.grp"], "verify": True},
}


def module_for_address(address) -> Optional[str]:
    """Module key for a diagnostic address (int, "0x63", "63" or "99" decimal).

    The gateway reports addresses as strings and BMW's own tooling is
    inconsistent about the base, so both readings are tried -- hex first,
    because that is how every BMW document writes them.
    """
    if address is None:
        return None
    candidates: list[int] = []
    if isinstance(address, int):
        candidates = [address]
    else:
        text = str(address).strip()
        if not text:
            return None
        try:
            candidates.append(int(text, 16))
        except ValueError:
            pass
        if text.isdigit():
            candidates.append(int(text, 10))
    for value in candidates:
        for key, meta in MODULES.items():
            if meta.get("addr") == value:
                return key
    return None


class EdiabasError(Exception):
    """Honest failure: runner missing, timeout, or the tool itself errored."""


def exe_path() -> Path:
    return Path(os.environ.get("CARHARNESS_EDIABASTEST", str(_DEFAULT_EXE)))


def ecu_dir() -> Path:
    return Path(os.environ.get("CARHARNESS_EDIABAS_ECU", str(_DEFAULT_ECU_DIR)))


def availability() -> dict:
    """Is the EDIABAS stack usable from this machine? Never fakes it."""
    exe, ecu = exe_path(), ecu_dir()
    missing = []
    if not exe.is_file():
        missing.append(f"job runner not found at {exe}")
    if not ecu.is_dir():
        missing.append(f"SGBD library not found at {ecu}")
    on_disk = 0
    if ecu.is_dir():
        on_disk = sum(1 for m in MODULES.values() for s in m["sgbds"] if (ecu / s).is_file())
    return {
        "available": not missing,
        "exe": str(exe),
        "ecuDir": str(ecu),
        "sgbdFilesForMappedModules": on_disk,
        "detail": "; ".join(missing) if missing else "EDIABAS runner + SGBD library present.",
    }


def sgbd_path(filename: str) -> Path:
    """Resolve an SGBD filename against the ECU dir (case-insensitive: the
    install mixes g_klima.grp and G_KLIMA3.grp styles)."""
    direct = ecu_dir() / filename
    if direct.is_file():
        return direct
    lower = filename.lower()
    if ecu_dir().is_dir():
        for f in ecu_dir().iterdir():
            if f.name.lower() == lower:
                return f
    raise EdiabasError(f"SGBD '{filename}' not found in {ecu_dir()}.")


_RESULT_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]{1,63})\s*[=:]\s+(.*\S)\s*$")
_ERROR_RE = re.compile(r"Error occured:\s*(0x[0-9A-Fa-f]+)\s*(.*)")
_DATASET_RE = re.compile(r"^DATASET:\s*(\d+)", re.IGNORECASE)


def parse_output(text: str) -> dict:
    """Turn EdiabasTest stdout into {job, results, sets, error}.

    Result lines are Tool32-style `NAME = value` (also tolerates `NAME: value`).
    Anything unrecognised stays in `raw` -- the caller always gets the full
    text, so a parser miss can never hide what the car actually said.

    EDIABAS answers a job with one result SET per row -- one per fault in a
    fault read, one per ECU in the gateway's installed-ECU list -- and
    EdiabasTest prints each behind a `DATASET: n` line. Those go in `sets`,
    in order. `results` stays a flat name->value map (later sets win) because
    single-set jobs are the common case and every existing caller reads it;
    anything that can return a list MUST read `sets` or it sees only the last
    row. Set 0 is EDIABAS's job-wide set (JOB_STATUS and friends).
    """
    job, results, error = None, {}, None
    sets: list[dict] = []
    current: Optional[dict] = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("API Version") or line.startswith("Usage:"):
            continue
        if line.startswith("JOB:"):
            job = line[4:].strip()
            continue
        m = _DATASET_RE.match(line)
        if m:
            current = {"set": int(m.group(1)), "results": {}}
            sets.append(current)
            continue
        m = _ERROR_RE.search(line)
        if m:
            error = {"code": m.group(1), "detail": m.group(2).strip()}
            continue
        if line.startswith("ResolveSgbdFile failed") or line.startswith("at "):
            continue
        m = _RESULT_RE.match(line)
        if m:
            results[m.group(1)] = m.group(2)
            if current is not None:
                current["results"][m.group(1)] = m.group(2)
    return {"job": job, "results": results, "sets": sets, "error": error}


def run_job(
    sgbd: str,
    job: str,
    params: Optional[list[str]] = None,
    results: Optional[list[str]] = None,
    remote_host: Optional[str] = None,
    timeout_s: int = 90,
) -> dict:
    """Run one SGBD job over ENET. Returns {ok, job, results, error, raw, cmd}.

    `job` must already be allowlisted by the caller -- this function trusts its
    input and runs exactly one EdiabasTest invocation.
    """
    if not exe_path().is_file():
        raise EdiabasError(f"EdiabasTest.exe not found at {exe_path()}.")
    path = sgbd_path(sgbd)

    cfg = "Interface=ENET;RemoteHost=" + (remote_host or "auto:all")
    job_str = job
    if params:
        job_str += "#" + ";".join(str(p) for p in params)
        if results:
            job_str += "#" + ";".join(results)
    elif results:
        job_str += "##" + ";".join(results)

    cmd = [str(exe_path()), "-s", str(path), "--cfg", cfg, "-j", job_str]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        raise EdiabasError(f"Job {job} timed out after {timeout_s}s (car asleep or link down).")

    raw = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    parsed = parse_output(raw)
    ok = proc.returncode == 0 and parsed["error"] is None
    return {
        "ok": ok,
        "job": parsed["job"] or job,
        "sgbd": path.name,
        "results": parsed["results"],
        "sets": parsed["sets"],
        "error": parsed["error"] or ({"code": f"exit {proc.returncode}", "detail": ""} if not ok else None),
        "raw": raw.strip(),
        "cmd": " ".join(cmd),
    }
