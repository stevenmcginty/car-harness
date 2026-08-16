"""car-harness FastAPI application.

Serves the REST API under /api and (defensively) mounts the front-end static
site at /. The API is registered first so it always works even when frontend/
is missing or empty. Runs on 127.0.0.1:8765.

Manufacturer routes are GENERIC: /api/{brand}/scan, /live, /snapshot, /clear,
/resets, /reset and /discover dispatch through backend.core.brands to whatever
capability files exist under backend/profiles/<brand>/ -- adding a manufacturer
is a folder drop-in, not an edit here. Profiles whose profile.json transport is
"elm327" get the picked adapter passed in; native-cable profiles (BMW ENET)
find the car themselves.

Anything that talks to hardware (or disk, in the middleware) runs off the
event loop: sync `def` handlers use FastAPI's threadpool, and the async write
handlers wrap their blocking calls in run_in_threadpool -- one slow BLE
connect must never freeze the whole API.
"""

import mimetypes
import re
import shutil
import sys
import tempfile
from pathlib import Path

# Make the project root importable so `import backend.*` works no matter how the
# process was launched (python run.py, uvicorn, double-clicked start.bat).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.concurrency import run_in_threadpool  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from serial.tools import list_ports  # noqa: E402

from backend.adapters import registry as adapter_registry  # noqa: E402
from backend.core import brands, captures, interfaces, pairing, registry  # noqa: E402
from backend.core import vin as vin_decode  # noqa: E402
from backend.core.models import ConnectionStatus  # noqa: E402
from backend.obd import freeze, livefeed  # noqa: E402
from backend.obd import raw as obd_raw  # noqa: E402
from backend.obd import service as obd_service  # noqa: E402

FRONTEND_DIR = PROJECT_ROOT / "frontend"

# Windows has no registry entry for .webmanifest, so StaticFiles would hand it
# out as application/octet-stream and Chrome would refuse to install the PWA.
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")

app = FastAPI(title="car-harness", version="0.3.0")


@app.on_event("startup")
def _startup():
    pairing.start_discovery_responder()


@app.on_event("shutdown")
def _shutdown():
    obd_service.close_all()  # release COM ports / BLE links cleanly


# --------------------------------------------------------------------------- #
# LAN security: localhost is trusted (it's Steve at the desktop UI); anything
# else must carry a bearer token issued by the pairing flow. The only
# non-localhost endpoint reachable without a token is the pairing claim itself.
# --------------------------------------------------------------------------- #
_LOCAL_CLIENTS = {"127.0.0.1", "::1", "testclient"}


# --------------------------------------------------------------------------- #
# Front-end files carry no cache headers of their own, so a browser is free to
# invent a heuristic freshness window and serve yesterday's index.html without
# even asking -- a shipped fix then looks like it never landed (this happened).
# "no-cache" still lets the browser keep the file; it just has to revalidate,
# which costs a 304. The service worker's offline cache is unaffected.
# --------------------------------------------------------------------------- #
@app.middleware("http")
async def _no_stale_frontend(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.middleware("http")
async def _phone_auth(request: Request, call_next):
    client = request.client.host if request.client else ""
    if client in _LOCAL_CLIENTS:
        return await call_next(request)
    if request.url.path == "/api/pair/claim" and request.method == "POST":
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    # token_valid reads (and touches) paired_devices.json -- keep that disk
    # I/O off the event loop.
    if await run_in_threadpool(pairing.token_valid, token):
        return await call_next(request)
    return JSONResponse(
        status_code=401,
        content={"detail": "Not paired. Pair this device from the desktop app first."},
    )


# --------------------------------------------------------------------------- #
# API routes (registered BEFORE the static mount at "/")
# --------------------------------------------------------------------------- #
@app.get("/api/profiles")
def get_profiles() -> dict:
    profiles = registry.list_profiles()
    for p in profiles:
        p["features"] = brands.features(p["id"])
    return {"profiles": profiles}


@app.get("/api/status", response_model=ConnectionStatus)
def get_status(
    profile: str = "bmw", mode: str = "real", adapter: str | None = None
) -> ConnectionStatus:
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    probe_module = registry.load_profile_probe(profile)
    if probe_module is None or not hasattr(probe_module, "status"):
        raise HTTPException(status_code=404, detail=f"Unknown or invalid profile '{profile}'.")
    # No adapter -> the exact v1 call (auto ENET/DoIP). Unchanged behaviour.
    if adapter is None:
        return _tag_make(probe_module.status(mode), adapter_used=False)
    instance = adapter_registry.get_instance(adapter)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"Unknown adapter '{adapter}'.")
    return _tag_make(
        probe_module.status(mode, adapter=instance.model_dump()), adapter_used=True
    )


def _tag_make(status: ConnectionStatus, adapter_used: bool) -> ConnectionStatus:
    """Name the make from the VIN and suggest the profile that should drive it.

    Done here, once, rather than in each profile: every profile's status goes
    through this endpoint, and the mismatch it catches -- a car answering a
    probe belonging to a different manufacturer -- is possible on any of them.
    """
    vin_value = getattr(getattr(status, "vehicle", None), "vin", None)
    if not vin_value:
        return status
    status.detectedMake = vin_decode.make_from_vin(vin_value)
    # A car found without a configured adapter came over the native cable, so a
    # cable-only profile is a legitimate suggestion; otherwise it is not.
    protocol = None if adapter_used else "DoIP"
    try:
        available = [p["id"] for p in registry.list_profiles() if p.get("id")]
    except Exception:
        available = None
    suggestion = vin_decode.suggest(vin_value, protocol=protocol, available=available)
    if suggestion and suggestion != status.profile:
        status.suggestedProfile = suggestion
    return status


@app.get("/api/interfaces")
def get_interfaces() -> dict:
    return {"interfaces": interfaces.list_interfaces()}


@app.get("/api/modules")
def get_modules(profile: str | None = None) -> dict:
    return {"modules": registry.list_modules(profile)}


@app.post("/api/modules/import")
async def import_module_endpoint(request: Request) -> dict:
    """Import a job module from an uploaded .zip (multipart) OR JSON {"path": ...}."""
    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="No 'file' provided in the multipart upload.")
        tmp_dir = Path(tempfile.mkdtemp(prefix="carharness_upload_"))
        tmp_file = tmp_dir / (getattr(upload, "filename", None) or "module.zip")
        try:
            tmp_file.write_bytes(await upload.read())
            record = await run_in_threadpool(registry.import_module, str(tmp_file))
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return record

    # Otherwise treat the body as JSON {"path": "<folder or .zip on disk>"}.
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Send a .zip file (multipart) or JSON {"path": "<folder or zip>"}.',
        )
    path = (body or {}).get("path")
    if not path:
        raise HTTPException(status_code=400, detail='JSON body must include a "path".')
    try:
        return await run_in_threadpool(registry.import_module, path)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/modules/{module_id}")
def delete_module(module_id: str) -> dict:
    try:
        return registry.remove_module(module_id)
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# --------------------------------------------------------------------------- #
# Adapter layer: types (built-in kinds) + instances (user-configured, wizard).
# --------------------------------------------------------------------------- #
@app.get("/api/adapter-types")
def get_adapter_types() -> dict:
    # exclude_none drops optional field keys (options/help) that don't apply.
    return {"types": [t.model_dump(exclude_none=True) for t in adapter_registry.list_types()]}


@app.get("/api/adapters")
def get_adapters() -> dict:
    return {"adapters": [i.model_dump() for i in adapter_registry.list_instances()]}


@app.get("/api/serial-ports")
def get_serial_ports() -> dict:
    ports = []
    try:
        for p in list_ports.comports():
            ports.append({"device": p.device, "description": p.description or ""})
    except Exception:
        ports = []
    return {"ports": ports}


@app.post("/api/adapters/test")
async def test_adapter(request: Request) -> dict:
    """Run an adapter type's connectivity test WITHOUT saving anything."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail='Send JSON {"typeId": "...", "config": {...}}.')
    type_id = (body or {}).get("typeId")
    config = (body or {}).get("config") or {}
    if not type_id:
        raise HTTPException(status_code=400, detail='JSON body must include a "typeId".')
    # A BLE test can scan for tens of seconds -- never on the event loop.
    result = await run_in_threadpool(adapter_registry.test_config, type_id, config)
    return result.model_dump()


@app.post("/api/adapters")
async def create_adapter(request: Request) -> dict:
    """Test then persist a new adapter instance; returns the AdapterInstance."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Send JSON {"typeId": "...", "name": "...", "config": {...}}.',
        )
    type_id = (body or {}).get("typeId")
    name = (body or {}).get("name")
    config = (body or {}).get("config") or {}
    if not type_id:
        raise HTTPException(status_code=400, detail='JSON body must include a "typeId".')
    if not name:
        raise HTTPException(status_code=400, detail='JSON body must include a "name".')
    try:
        saved = await run_in_threadpool(adapter_registry.save_instance, type_id, name, config)
        return saved.model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/adapters/{adapter_id}")
def delete_adapter(adapter_id: str) -> dict:
    if adapter_registry.remove_instance(adapter_id):
        return {"removed": True}
    raise HTTPException(status_code=404, detail=f"Adapter '{adapter_id}' not found.")


# --------------------------------------------------------------------------- #
# Generic OBD-II (all cars): fault codes, live data, VIN -- over any
# ELM327-style adapter. mode=sim returns clearly-labelled fakes.
# --------------------------------------------------------------------------- #
def _obd_adapter_or_400(adapter_id: str | None, mode: str) -> dict | None:
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    if mode == "sim":
        return None
    if not adapter_id:
        raise HTTPException(
            status_code=400,
            detail="Pick an ELM327-style adapter (or use mode=sim).",
        )
    instance = adapter_registry.get_instance(adapter_id)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"Unknown adapter '{adapter_id}'.")
    return instance.model_dump()


@app.get("/api/obd/scan")
def obd_scan(adapter: str | None = None, mode: str = "real") -> dict:
    instance = _obd_adapter_or_400(adapter, mode)
    try:
        return obd_service.scan(instance, mode)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "message": str(exc)}


@app.post("/api/obd/clear")
async def obd_clear(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Send JSON {"adapter": "...", "mode": "real"|"sim", "confirm": true}.',
        )
    body = body or {}
    if body.get("confirm") is not True:
        raise HTTPException(
            status_code=400,
            detail="Clearing fault codes changes the car. Send confirm: true.",
        )
    mode = body.get("mode") or "real"
    instance = _obd_adapter_or_400(body.get("adapter"), mode)
    try:
        return await run_in_threadpool(obd_service.clear, instance, mode)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "message": str(exc)}


@app.get("/api/obd/live")
def obd_live(adapter: str | None = None, mode: str = "real", pids: str | None = None) -> dict:
    instance = _obd_adapter_or_400(adapter, mode)
    keys = [k.strip() for k in (pids or "").split(",") if k.strip()] or None
    try:
        return obd_service.live(instance, mode, keys)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "readings": [], "message": str(exc)}


@app.get("/api/obd/pids")
def obd_pids(adapter: str | None = None, mode: str = "real") -> dict:
    instance = _obd_adapter_or_400(adapter, mode)
    try:
        return obd_service.supported_pids(instance, mode)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "keys": [], "message": str(exc)}


# --------------------------------------------------------------------------- #
# Live data -- the one place every car's readable values come from, standard
# OBD-II and manufacturer extras merged into a single set of channels. See
# backend/obd/livefeed.py for the contract a new manufacturer has to meet.
# --------------------------------------------------------------------------- #
def _live_brand_or_400(brand: str | None) -> str | None:
    """An optional brand overlay. Unknown brand is a 404 like everywhere else;
    a brand with no pids.py is simply no overlay, which is not an error."""
    if not brand:
        return None
    if brands.profile_meta(brand) is None:
        raise HTTPException(status_code=404, detail=f"Unknown manufacturer '{brand}'.")
    return brand


@app.get("/api/live/catalog")
def live_catalog(
    adapter: str | None = None, mode: str = "real", brand: str | None = None
) -> dict:
    """Ask the car what it can actually give. Slow (it reads support bitmaps
    and does one manufacturer pass), so the front end calls it once and then
    streams through /api/live/read."""
    instance = _obd_adapter_or_400(adapter, mode)
    try:
        return livefeed.catalog(instance, mode, _live_brand_or_400(brand))
    except obd_service.ObdServiceError as exc:
        return {
            "ok": False, "simulated": False, "channels": [], "groups": [],
            "message": str(exc),
        }


@app.get("/api/live/read")
def live_read(
    adapter: str | None = None,
    mode: str = "real",
    keys: str | None = None,
    brand: str | None = None,
) -> dict:
    """Read the selected channels once. This is the call the dashboard polls."""
    instance = _obd_adapter_or_400(adapter, mode)
    wanted = [k.strip() for k in (keys or "").split(",") if k.strip()]
    try:
        return livefeed.read(instance, mode, wanted, _live_brand_or_400(brand))
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "readings": [], "message": str(exc)}


@app.get("/api/obd/freeze")
def obd_freeze(adapter: str | None = None, mode: str = "real", frame: int = 0) -> dict:
    """The live data as it was at the moment a fault stored (mode 02)."""
    instance = _obd_adapter_or_400(adapter, mode)
    try:
        return freeze.read(instance, mode, frame)
    except obd_service.ObdServiceError as exc:
        return {
            "ok": False, "simulated": False, "readings": [], "groups": [],
            "trigger": None, "message": str(exc),
        }


# --------------------------------------------------------------------------- #
# Raw diagnostic channel (the Claude Code / MCP escape hatch).
# --------------------------------------------------------------------------- #
@app.post("/api/raw/did")
async def raw_did(request: Request) -> dict:
    """Read arbitrary mode-22 DIDs from one module (read-only)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Send JSON {"adapter": "...", "target": "7E0", "dids": ["1310"], ...}.',
        )
    body = body or {}
    mode = body.get("mode") or "real"
    instance = _obd_adapter_or_400(body.get("adapter"), mode)
    return await run_in_threadpool(
        obd_raw.read_dids, instance, mode,
        body.get("target") or "", body.get("dids") or [],
        body.get("addressing") or "11bit", body.get("resp_id"),
        bool(body.get("extended_session")),
    )


@app.post("/api/raw/elm")
async def raw_elm(request: Request) -> dict:
    """Run a raw ELM327 command sequence -- arbitrary, so confirm-gated."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Send JSON {"adapter": "...", "commands": ["..."], "confirm": true}.',
        )
    body = body or {}
    if body.get("confirm") is not True:
        raise HTTPException(
            status_code=400,
            detail="Raw ELM commands can write to the car. Send confirm: true.",
        )
    mode = body.get("mode") or "real"
    instance = _obd_adapter_or_400(body.get("adapter"), mode)
    return await run_in_threadpool(
        obd_raw.elm_commands, instance, mode,
        body.get("commands") or [], float(body.get("timeout") or 3.0),
    )


# --------------------------------------------------------------------------- #
# Phone pairing.
# --------------------------------------------------------------------------- #
@app.post("/api/pair/start")
def pair_start(request: Request) -> dict:
    client = request.client.host if request.client else ""
    if client not in _LOCAL_CLIENTS:
        raise HTTPException(status_code=403, detail="Pairing can only be started from the desktop.")
    return pairing.start_pairing()


@app.post("/api/pair/claim")
async def pair_claim(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail='Send JSON {"code": "123456", "deviceName": "..."}.')
    body = body or {}
    try:
        return await run_in_threadpool(
            pairing.claim, str(body.get("code") or ""), str(body.get("deviceName") or "")
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@app.get("/api/pair/devices")
def pair_devices() -> dict:
    return {"devices": pairing.list_devices()}


@app.delete("/api/pair/devices/{device_id}")
def pair_remove(device_id: str) -> dict:
    if pairing.remove_device(device_id):
        return {"removed": True}
    raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found.")


@app.get("/api/ping")
def ping() -> dict:
    return {"app": "car-harness", "version": app.version}


# --------------------------------------------------------------------------- #
# Manufacturer routes -- GENERIC over backend/profiles/<brand>/.
#
# Registered LAST among /api routes so every literal path above (obd, pair,
# adapters, ...) wins before {brand} can match. A brand only resolves if
# backend/profiles/<brand>/profile.json exists; everything else is 404.
#
# Read-only except /clear and /reset, which demand confirm: true.
# --------------------------------------------------------------------------- #
def _brand_capability_or_404(brand: str, name: str):
    module = brands.brand_module(brand, name)
    if module is None:
        raise HTTPException(
            status_code=404, detail=f"No '{name}' capability for '{brand}'."
        )
    return module


def _stamp_capture(brand: str, kind: str, result: dict, instance) -> dict:
    """Persist a real run to captures/<brand>/ and stamp the paths onto the
    result. Sim runs and brands not opted in are left untouched (see
    backend/core/captures.py for why simulated data is never saved)."""
    try:
        saved = captures.save(brand, kind, result, instance)
    except OSError:
        # A full disk or a locked file must never lose the diagnostic itself.
        return result
    if saved:
        result["savedTo"] = saved["json"]
        if saved.get("csv"):
            result["savedCsv"] = saved["csv"]
    return result


@app.get("/api/{brand}/discover")
def brand_discover(brand: str, adapter: str | None = None, mode: str = "real") -> dict:
    """Find the car on a brand's native cable (BMW ENET), or -- for a dongle
    brand that ships a discover.py -- sweep its diagnostic address block and
    report which modules answer (JLR PT-CAN walk, Vauxhall GM map). Read-only
    either way."""
    scan_mod = brands.brand_module(brand, "scan")
    if scan_mod is not None and hasattr(scan_mod, "discover"):
        if brands.needs_adapter(brand):
            instance = _obd_adapter_or_400(adapter, mode)
            return _stamp_capture(
                brand, "discover", scan_mod.discover(instance, mode), instance
            )
        return _stamp_capture(brand, "discover", scan_mod.discover(), None)
    module = brands.brand_module(brand, "discover")
    if module is None or not hasattr(module, "discover"):
        raise HTTPException(status_code=404, detail=f"'{brand}' has no discover step.")
    instance = _obd_adapter_or_400(adapter, mode)
    return _stamp_capture(brand, "discover", module.discover(instance, mode), instance)


@app.get("/api/{brand}/sweep")
def brand_sweep(
    brand: str,
    adapter: str | None = None,
    mode: str = "real",
    first: str = "0300",
    last: str = "05FF",
    target: str = "7E0",
    extended: bool = False,
) -> dict:
    """Walk a 0x22 DID range on one module, keeping every answer raw -- the
    DID-mapping instrument for values no public table carries (JLR SCR/DPF
    today). Read-only."""
    module = _brand_capability_or_404(brand, "sweep")
    instance = _obd_adapter_or_400(adapter, mode)
    return _stamp_capture(
        brand, "sweep", module.sweep(instance, mode, first, last, target, extended),
        instance,
    )


@app.get("/api/{brand}/adblue-health")
def brand_adblue_health(brand: str, adapter: str | None = None, mode: str = "real") -> dict:
    """One read-only AdBlue/SCR + DPF health pass (JLR X260 3.0d today):
    Mode-01 baseline, fault codes through the scan ladder, known DIDs raw,
    sourced assessment, SCR/DPF values honestly flagged capture-pending."""
    module = _brand_capability_or_404(brand, "adblue")
    instance = _obd_adapter_or_400(adapter, mode)
    return _stamp_capture(brand, "adblue-health", module.health(instance, mode), instance)


@app.get("/api/{brand}/adblue-drive-log")
def brand_adblue_drive_log(
    brand: str,
    adapter: str | None = None,
    mode: str = "real",
    seconds: float = 300,
    dids: str | None = None,
) -> dict:
    """Timed read-only log pairing rpm with the AdBlue/DPF candidate DIDs,
    payloads kept raw until decoded against the car. dids is a comma-separated
    hex list (sweep-captured candidates) replacing the default engine set."""
    module = _brand_capability_or_404(brand, "adblue")
    instance = _obd_adapter_or_400(adapter, mode)
    if not 5 <= seconds <= 3600:
        raise HTTPException(
            status_code=400,
            detail="seconds must be between 5 and 3600 (a regen needs ~1500).",
        )
    did_list: list[str] = []
    if dids:
        parts = [d.strip().upper() for d in dids.split(",") if d.strip()]
        if len(parts) > 24:
            raise HTTPException(
                status_code=400, detail="at most 24 dids per drive log."
            )
        for part in parts:
            if not re.fullmatch(r"[0-9A-F]{1,4}", part):
                raise HTTPException(
                    status_code=400,
                    detail="dids must be comma-separated hex like 042C,F40C.",
                )
        did_list = parts
    return _stamp_capture(
        brand, "drive-log", module.drive_log(instance, mode, seconds, did_list or None),
        instance,
    )


@app.get("/api/{brand}/scan")
def brand_scan(brand: str, adapter: str | None = None, mode: str = "real") -> dict:
    """Walk the brand's modules and read each one's own fault memory."""
    module = _brand_capability_or_404(brand, "scan")
    if brands.needs_adapter(brand):
        instance = _obd_adapter_or_400(adapter, mode)
        return _stamp_capture(brand, "scan", module.scan(instance, mode), instance)
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    return _stamp_capture(brand, "scan", module.scan(mode), None)


@app.get("/api/{brand}/software")
def brand_software(brand: str, adapter: str | None = None, mode: str = "real") -> dict:
    """What software is flashed into each of the car's modules. Read-only.

    The answer to "is there an update for my gearbox?" starts here: the module
    reports its own calibration part numbers, and those are what a manufacturer
    programming system matches against the VIN. The harness reads them and
    stops there -- it does not write calibrations, and the profile module
    records why not.
    """
    module = _brand_capability_or_404(brand, "software")
    if brands.needs_adapter(brand):
        instance = _obd_adapter_or_400(adapter, mode)
        return module.versions(instance, mode)
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    return module.versions(mode)


@app.get("/api/{brand}/gearbox")
def brand_gearbox(brand: str, adapter: str | None = None, mode: str = "real") -> dict:
    """Gearbox-focused read: TCM identity, faults, live values, diagnosis.

    Read-only. Brands that do not ship gearbox.py 404 the same way a missing
    software.py does.
    """
    module = _brand_capability_or_404(brand, "gearbox")
    if not hasattr(module, "analyze"):
        raise HTTPException(status_code=404, detail=f"'{brand}' has no gearbox analyse step.")
    if brands.needs_adapter(brand):
        instance = _obd_adapter_or_400(adapter, mode)
        return module.analyze(instance, mode)
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    return module.analyze(mode)


@app.get("/api/{brand}/live")
def brand_live(brand: str, adapter: str | None = None, mode: str = "real") -> dict:
    """Brand-specific live values (Mazda mode-22 extras, Peugeot health check,
    Porsche Taycan battery health). Native-cable brands find the car themselves,
    the same way scan and clear already do."""
    module = _brand_capability_or_404(brand, "pids")
    if brands.needs_adapter(brand):
        instance = _obd_adapter_or_400(adapter, mode)
        return module.live(instance, mode)
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    return module.live(mode)


@app.get("/api/{brand}/report.pdf")
async def brand_report(
    brand: str,
    mode: str = "real",
    dealer: str = "",
    customer: str = "",
    mileage: str = "",
):
    """A printable, customer-facing PDF for brands that publish one.

    The read is taken fresh inside the profile rather than reusing whatever is
    on screen, so the document's timestamp belongs to a measurement that
    actually happened. Read-only, like everything it reports on.
    """
    module = _brand_capability_or_404(brand, "pids")
    if not hasattr(module, "report"):
        raise HTTPException(
            status_code=404, detail=f"'{brand}' does not publish a PDF report."
        )
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    try:
        filename, data = await run_in_threadpool(
            module.report, mode, dealer, customer, mileage
        )
    except RuntimeError as exc:
        # The car could not be read -- that is a 409, not a server fault, and the
        # message is written to be shown to a human.
        raise HTTPException(status_code=409, detail=str(exc))
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/{brand}/snapshot")
def brand_snapshot(brand: str, adapter: str | None = None, mode: str = "real") -> dict:
    """Passive broadcast-CAN capture (ATMA sniff) on dongle brands; the profile's
    second deep-read action on native-cable brands. Read-only either way."""
    module = _brand_capability_or_404(brand, "snapshot")
    if brands.needs_adapter(brand):
        instance = _obd_adapter_or_400(adapter, mode)
        return module.snapshot(instance, mode)
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    return module.snapshot(mode)


@app.post("/api/{brand}/clear")
async def brand_clear(brand: str, request: Request) -> dict:
    module = _brand_capability_or_404(brand, "scan")
    if not hasattr(module, "clear"):
        raise HTTPException(status_code=404, detail=f"'{brand}' has no clear.")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Send JSON {"adapter": "..."?, "mode": "real"|"sim", "confirm": true, "address": "0x..."?}.',
        )
    body = body or {}
    if body.get("confirm") is not True:
        raise HTTPException(
            status_code=400,
            detail="Clearing module fault memory changes the car. Send confirm: true.",
        )
    mode = body.get("mode") or "real"
    if brands.needs_adapter(brand):
        instance = _obd_adapter_or_400(body.get("adapter"), mode)
        return await run_in_threadpool(module.clear, instance, mode, body.get("address"))
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    return await run_in_threadpool(module.clear, mode, body.get("address"))


@app.get("/api/{brand}/resets")
def get_resets(brand: str) -> dict:
    """The brand's service-reset catalogue (no car needed to browse it)."""
    module = _brand_capability_or_404(brand, "resets")
    return module.list_resets()


@app.post("/api/{brand}/reset")
async def run_reset(brand: str, request: Request) -> dict:
    module = _brand_capability_or_404(brand, "resets")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Send JSON {"reset_id": "...", "adapter": "...", "mode": "real"|"sim", "confirm": true}.',
        )
    body = body or {}
    reset_id = body.get("reset_id")
    if not reset_id:
        raise HTTPException(status_code=400, detail='JSON body must include a "reset_id".')
    mode = body.get("mode") or "real"
    # Procedure lookups and sim runs need no adapter; a real UDS run does.
    adapter = None
    if mode == "real":
        adapter = _obd_adapter_or_400(body.get("adapter"), mode)
    elif mode != "sim":
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    return await run_in_threadpool(
        module.perform, adapter, mode, reset_id, bool(body.get("confirm"))
    )


@app.get("/api/{brand}/service-history")
def get_service_history(brand: str) -> dict:
    """The service-history catalogue: items, statuses, head units, limits.

    No car needed -- this is the data a dealer fills a form in with.
    """
    module = _brand_capability_or_404(brand, "service_history")
    return module.catalogue()


@app.post("/api/{brand}/service-history/build")
async def build_service_history(brand: str, request: Request) -> dict:
    """Build the diagnostic-job argument for one service record.

    Builds text only. It never touches the car -- the write is a Tool32 job,
    see backend/profiles/bmw/service_history.py for why.
    """
    module = _brand_capability_or_404(brand, "service_history")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Send JSON {"day":.., "month":.., "year":.., "odometer":.., '
                   '"units":"mi"|"km", "dealer_name":"RADLETT", "items":[...]}.',
        )
    body = body or {}
    try:
        return module.build_argument(
            day=body.get("day"),
            month=body.get("month"),
            year=body.get("year"),
            odometer=body.get("odometer", 0),
            units=body.get("units", "km"),
            dealer_name=body.get("dealer_name", ""),
            bmw_dealer=bool(body.get("bmw_dealer")),
            status=int(body.get("status", 1)),
            items=body.get("items") or [],
            correction_counter=int(body.get("correction_counter", 0)),
            pad=body.get("pad", True),
        )
    except module.ServiceHistoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Bad service record: {exc}")


@app.post("/api/{brand}/service-history/write")
async def write_service_history(brand: str, request: Request) -> dict:
    """Write one service-history entry to the car over ENET. CHANGES THE CAR.

    The body is the build form's fields plus "confirm": true (required --
    entries cannot be edited or deleted afterwards) and optional
    "reboot": false to skip the head-unit restart that makes the entry show.
    mode="sim" validates and returns the telegram without touching a car.
    """
    module = _brand_capability_or_404(brand, "service_history")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Send JSON {"day":.., "month":.., "year":.., "odometer":.., '
                   '"units":"mi"|"km", "dealer_name":"RADLETT", "items":[...], '
                   '"confirm": true}.',
        )
    body = body or {}
    mode = body.get("mode") or "real"
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    fields = {
        "day": body.get("day"),
        "month": body.get("month"),
        "year": body.get("year"),
        "odometer": body.get("odometer", 0),
        "units": body.get("units", "km"),
        "dealer_name": body.get("dealer_name", ""),
        "bmw_dealer": bool(body.get("bmw_dealer")),
        "status": int(body.get("status", 1)),
        "items": body.get("items") or [],
        "correction_counter": int(body.get("correction_counter", 0)),
    }
    try:
        return await run_in_threadpool(
            module.write, mode, bool(body.get("confirm")), bool(body.get("reboot", True)), **fields
        )
    except module.ServiceHistoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Bad service record: {exc}")


@app.get("/api/{brand}/ediabas/modules")
def get_ediabas_modules(brand: str) -> dict:
    """The EDIABAS bridge catalogue: availability + per-module job menus.

    No car contact -- this is the data a UI renders the Jobs browser from.
    """
    module = _brand_capability_or_404(brand, "ediabas")
    return module.modules()


@app.get("/api/{brand}/cbs")
def get_cbs_catalogue(brand: str) -> dict:
    """Which service items can be reset, and on which modules. No car contact."""
    module = _brand_capability_or_404(brand, "cbs")
    return module.catalogue()


@app.get("/api/{brand}/cbs/read")
async def read_cbs(brand: str, mode: str = "real") -> dict:
    """Read the car's service-interval counters. Read-only."""
    module = _brand_capability_or_404(brand, "cbs")
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    return await run_in_threadpool(module.read, mode)


@app.post("/api/{brand}/cbs/reset")
async def reset_cbs(brand: str, request: Request) -> dict:
    """Reset one service item on the car. CHANGES THE CAR -- needs confirm.

    Resets the counter only. The iDrive service history is a separate record
    written through /api/{brand}/service-history/write.
    """
    module = _brand_capability_or_404(brand, "cbs")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Send JSON {"item": "Oel", "confirm": true, "mode": "real"|"sim"}.',
        )
    body = body or {}
    if not body.get("item"):
        raise HTTPException(status_code=400, detail='JSON body must include "item".')
    mode = body.get("mode") or "real"
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    try:
        return await run_in_threadpool(
            module.reset,
            str(body["item"]),
            mode,
            bool(body.get("confirm")),
            body.get("percent"),
            body.get("target_month"),
            body.get("target_year"),
            body.get("module"),
        )
    except module.CbsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Bad reset request: {exc}")


@app.get("/api/{brand}/battery")
async def get_battery(brand: str, mode: str = "real") -> dict:
    """Battery condition from the car's own power management. Read-only."""
    module = _brand_capability_or_404(brand, "battery")
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    return await run_in_threadpool(module.status, mode)


@app.post("/api/{brand}/battery/register")
async def register_battery(brand: str, request: Request) -> dict:
    """Register a battery replacement. CHANGES THE CAR -- needs confirm."""
    module = _brand_capability_or_404(brand, "battery")
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    mode = body.get("mode") or "real"
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    return await run_in_threadpool(module.register, mode, bool(body.get("confirm")))


@app.get("/api/{brand}/ediabas/installed")
async def get_ediabas_installed(brand: str, mode: str = "real") -> dict:
    """Ask the car's gateway which modules are actually fitted.

    Talks to the car (read-only): the module scan's fixed address list can
    only infer "not fitted" from silence, the gateway knows.
    """
    module = _brand_capability_or_404(brand, "ediabas")
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    if not hasattr(module, "installed_modules"):
        raise HTTPException(
            status_code=404,
            detail=f"'{brand}' has no gateway installed-ECU list.",
        )
    return await run_in_threadpool(module.installed_modules, mode)


@app.post("/api/{brand}/ediabas/run")
async def run_ediabas_job(brand: str, request: Request) -> dict:
    """Run one allowlisted SGBD job over ENET.

    kind=write jobs are refused unless the JSON body has "confirm": true --
    the profile layer enforces that, not just this route.
    """
    module = _brand_capability_or_404(brand, "ediabas")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Send JSON {"module": "hu", "job": "IDENTIFIKATION", '
                   '"params": [...], "mode": "real"|"sim", "confirm": true}.',
        )
    body = body or {}
    if not body.get("module") or not body.get("job"):
        raise HTTPException(status_code=400, detail='JSON body must include "module" and "job".')
    mode = body.get("mode") or "real"
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    params = body.get("params")
    if params is not None and not isinstance(params, list):
        raise HTTPException(status_code=400, detail='"params" must be a list of strings.')
    try:
        return await run_in_threadpool(
            module.run_job,
            str(body["module"]),
            str(body["job"]),
            params,
            mode,
            bool(body.get("confirm")),
            body.get("remote_host"),
        )
    except module.EdiabasJobError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------------------------------------- #
# Static front-end mount at "/" (LAST, and only if the dir exists).
# A missing or empty frontend/ must not crash startup -- the API still works.
# --------------------------------------------------------------------------- #
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:
    @app.get("/")
    def _frontend_placeholder() -> dict:
        return {
            "message": "car-harness API is running, but frontend/ is not installed.",
            "api": "/api/profiles",
        }
