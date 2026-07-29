"""Offline self-test for the EDIABAS bridge (BMW/MINI SGBD job runner).

Run:  cd car-harness; .venv\\Scripts\\python.exe -m backend.tools.ediabas_selftest

No car needed. What it proves:

  * EdiabasTest output parsing: error runs (real captured IFH-0018 output) and
    Tool32-style `NAME = value` result lines both decode correctly,
  * the module -> SGBD table is sane and every mapped file really exists in
    C:\\EDIABAS\\ECU (when the library is present on this machine),
  * availability() is honest about a missing runner/library,
  * the allowlist is enforced: unknown jobs/modules are refused, write jobs
    are confirm-gated, required params are demanded,
  * sim mode is clearly labelled,
  * the HTTP routes and MCP tools serve the catalogue and enforce the gates.

Exits 0 on PASS, 1 on FAIL.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from backend.core import brands, ediabas  # noqa: E402
from backend.main import app  # noqa: E402
from backend.profiles.bmw import ediabas as bmw_ediabas  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not cond:
        line += f" -- {detail}"
    print(line)


# --------------------------------------------------------------------------- #
# 1. Output parsing -- real captured error output from EdiabasTest (no car).
# --------------------------------------------------------------------------- #
ERROR_OUT = """API Version: 7.7.2
JOB: IDENTIFIKATION
Error occured: 0x0000001C IFH-0018: INITIALIZATION ERROR
ResolveSgbdFile failed: Error occurred: EDIABAS_IFH_0018
   at EdiabasLib.EdiabasNet.RaiseError(ErrorCodes error)
"""
p = ediabas.parse_output(ERROR_OUT)
check("parse: job name decoded", p["job"] == "IDENTIFIKATION", p)
check("parse: error code decoded", p["error"] and p["error"]["code"] == "0x0000001C", p)
check("parse: stack frames ignored", p["results"] == {}, p)

OK_OUT = """API Version: 7.7.2
JOB: IDENTIFIKATION
SGBD = NBT
FG_NR = WBAXX120300S30285
BAUSTAND = 2020-03
"""
p = ediabas.parse_output(OK_OUT)
check("parse: results decoded", p["results"].get("FG_NR") == "WBAXX120300S30285", p)
check("parse: no error on success", p["error"] is None, p)

# --------------------------------------------------------------------------- #
# 2. Module table + on-disk SGBD library.
# --------------------------------------------------------------------------- #
avail = ediabas.availability()
check("availability: honest shape", set(avail) >= {"available", "exe", "ecuDir", "detail"}, avail)
if avail["available"]:
    missing = [
        f"{key}:{s}" for key, meta in ediabas.MODULES.items()
        for s in meta["sgbds"] if not (ediabas.ecu_dir() / s).is_file()
        and not any(f.name.lower() == s.lower() for f in ediabas.ecu_dir().iterdir())
    ]
    check("module table: every candidate SGBD exists on disk", not missing, missing)
    check("availability: mapped files counted", avail["sgbdFilesForMappedModules"] >= 20, avail)
    p = ediabas.sgbd_path("g_klima3.grp")  # case-insensitive: on disk it's G_KLIMA3.grp
    check("sgbd_path: case-insensitive", p.name.lower() == "g_klima3.grp", p)
else:
    check("availability: missing stack reported honestly", len(avail["detail"]) > 10, avail)

# --------------------------------------------------------------------------- #
# 3. Catalogue + allowlist enforcement.
# --------------------------------------------------------------------------- #
cat = bmw_ediabas.modules()
check("catalogue: all 20 modules listed", len(cat["modules"]) == 20, len(cat["modules"]))
hu = next(m for m in cat["modules"] if m["key"] == "hu")
job_names = [j["name"] for j in hu["jobs"]]
check("catalogue: HU carries service-history jobs",
      "STEUERN_SERVICEHISTORY_ADD" in job_names and "STEUERN_SERVICEHISTORY_ERASE" in job_names, job_names)
check("catalogue: every module has the 4 common jobs",
      all(len(m["jobs"]) >= 4 for m in cat["modules"]))

try:
    bmw_ediabas.run_job("hu", "FORMAT_C_DRIVE", None, "sim", True)
    check("allowlist: unknown job refused", False)
except bmw_ediabas.EdiabasJobError:
    check("allowlist: unknown job refused", True)
try:
    bmw_ediabas.run_job("flux_capacitor", "IDENTIFIKATION", None, "sim", True)
    check("allowlist: unknown module refused", False)
except bmw_ediabas.EdiabasJobError:
    check("allowlist: unknown module refused", True)

# --------------------------------------------------------------------------- #
# 4. Write gating + required params.
# --------------------------------------------------------------------------- #
r = bmw_ediabas.run_job("hu", "STEUERN_SERVICEHISTORY_ADD", ["1;2;3"], "real", False)
check("gate: write without confirm refused", r["ok"] is False and r.get("needsConfirm") is True, r)
r = bmw_ediabas.run_job("dme", "FS_LOESCHEN", None, "real", False)
check("gate: fault-clear without confirm refused", r.get("needsConfirm") is True, r)
r = bmw_ediabas.run_job("hu", "STEUERN_SERVICEHISTORY_ADD", None, "real", True)
check("gate: required param demanded", r["ok"] is False and "argument" in r["message"], r.get("message"))

# --------------------------------------------------------------------------- #
# 5. Sim mode is clearly labelled; read jobs run unconfirmed.
# --------------------------------------------------------------------------- #
r = bmw_ediabas.run_job("kombi", "IDENTIFIKATION", None, "sim", False)
check("sim: labelled", r["simulated"] is True and r["message"].startswith("SIMULATED"), r)
r = bmw_ediabas.run_job("kombi", "STEUERGERAETE_RESET", None, "sim", True)
check("sim: write job sims when confirmed", r["simulated"] is True and r["ok"] is True, r)

# A real read with no car present must fail honestly, never fake a result.
# (Spawns EdiabasTest for real only when the runner+library exist; either way
# the answer must be an honest dict.)
r = bmw_ediabas.run_job("kombi", "IDENTIFIKATION", None, "real", False)
check("real: honest outcome dict", isinstance(r.get("ok"), bool) and r.get("message"), r)
check("real: failure names the cause", r["ok"] or ("EDIABAS" in r["message"] or "candidate" in r["message"]), r.get("message"))

# --------------------------------------------------------------------------- #
# 6. brands.py discovery + HTTP routes.
# --------------------------------------------------------------------------- #
f_bmw = brands.features("bmw")
f_mazda = brands.features("mazda")
check("features: bmw.ediabas on", f_bmw.get("ediabas") is True, f_bmw)
check("features: mazda.ediabas off", f_mazda.get("ediabas") is False, f_mazda)

client = TestClient(app)  # no `with`: avoids the startup UDP responder
r = client.get("/api/bmw/ediabas/modules")
check("route: modules 200", r.status_code == 200, r.status_code)
check("route: modules shape", "modules" in r.json() and "available" in r.json())
r = client.get("/api/mazda/ediabas/modules")
check("route: non-ediabas brand 404", r.status_code == 404, r.status_code)

r = client.post("/api/bmw/ediabas/run", json={"module": "hu", "job": "STEUERN_SERVICEHISTORY_ERASE"})
body = r.json()
check("route: write gated without confirm", r.status_code == 200 and body.get("needsConfirm") is True, body)
r = client.post("/api/bmw/ediabas/run", json={"module": "hu", "job": "STEUERN_SERVICEHISTORY_ERASE", "confirm": True, "mode": "sim"})
check("route: sim write runs when confirmed", r.status_code == 200 and r.json().get("simulated") is True, r.json())
r = client.post("/api/bmw/ediabas/run", json={"module": "hu"})
check("route: missing job 400", r.status_code == 400, r.status_code)
r = client.post("/api/bmw/ediabas/run", json={"module": "hu", "job": "IDENTIFIKATION", "mode": "banana"})
check("route: bad mode 400", r.status_code == 400, r.status_code)
r = client.post("/api/bmw/ediabas/run", json={"module": "hu", "job": "NOT_A_JOB", "mode": "sim"})
check("route: unlisted job 400", r.status_code == 400, r.status_code)

# --------------------------------------------------------------------------- #
# 7. MCP tools serve the same truth.
# --------------------------------------------------------------------------- #
from backend import mcp_server  # noqa: E402

m = mcp_server.bmw_ediabas_modules()
check("mcp: modules served", "modules" in m and len(m["modules"]) == 20)
m = mcp_server.bmw_ediabas_run("hu", "STEUERN_SERVICEHISTORY_ERASE", None, "real", False)
check("mcp: write gated", m.get("needsConfirm") is True, m)
m = mcp_server.bmw_ediabas_run("hu", "NOPE", None, "sim", True)
check("mcp: bad job errors", "error" in m, m)
m = mcp_server.bmw_ediabas_run("kombi", "IDENTIFIKATION", None, "sim", False)
check("mcp: sim run labelled", m.get("simulated") is True, m)

# --------------------------------------------------------------------------- #
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
print("EDIABAS SELF-TEST PASSED.")
sys.exit(0)
