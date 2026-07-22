"""car-harness FastAPI application.

Serves the REST API under /api and (defensively) mounts the front-end static
site at /. The API is registered first so it always works even when frontend/
is missing or empty. Runs on 127.0.0.1:8765.
"""

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
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from serial.tools import list_ports  # noqa: E402

from backend.adapters import registry as adapter_registry  # noqa: E402
from backend.core import interfaces, pairing, registry  # noqa: E402
from backend.core.models import ConnectionStatus  # noqa: E402
from backend.obd import service as obd_service  # noqa: E402
from backend.profiles.bmw import scan as bmw_scan  # noqa: E402
from backend.profiles.mazda import pids as mazda_pids  # noqa: E402
from backend.profiles.mazda import scan as mazda_scan  # noqa: E402
from backend.profiles.mazda import snapshot as mazda_snapshot_mod  # noqa: E402
from backend.profiles.peugeot import scan as peugeot_scan  # noqa: E402

FRONTEND_DIR = PROJECT_ROOT / "frontend"

app = FastAPI(title="car-harness", version="0.2.0")


@app.on_event("startup")
def _startup():
    pairing.start_discovery_responder()


# --------------------------------------------------------------------------- #
# LAN security: localhost is trusted (it's Steve at the desktop UI); anything
# else must carry a bearer token issued by the pairing flow. The only
# non-localhost endpoint reachable without a token is the pairing claim itself.
# --------------------------------------------------------------------------- #
_LOCAL_CLIENTS = {"127.0.0.1", "::1", "testclient"}


@app.middleware("http")
async def _phone_auth(request: Request, call_next):
    client = request.client.host if request.client else ""
    if client in _LOCAL_CLIENTS:
        return await call_next(request)
    if request.url.path == "/api/pair/claim" and request.method == "POST":
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if pairing.token_valid(token):
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
    return {"profiles": registry.list_profiles()}


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
        return probe_module.status(mode)
    instance = adapter_registry.get_instance(adapter)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"Unknown adapter '{adapter}'.")
    return probe_module.status(mode, adapter=instance.model_dump())


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
            record = registry.import_module(str(tmp_file))
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
        return registry.import_module(path)
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
    return adapter_registry.test_config(type_id, config).model_dump()


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
        return adapter_registry.save_instance(type_id, name, config).model_dump()
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
        return obd_service.clear(instance, mode)
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
# BMW ENET deep scan: every module's fault memory over the ENET cable
# (HSFZ, F-series). Read-only except /clear, which demands confirm: true.
# --------------------------------------------------------------------------- #
@app.get("/api/bmw/discover")
def bmw_discover() -> dict:
    return bmw_scan.discover()


@app.get("/api/bmw/scan")
def bmw_full_scan(mode: str = "real") -> dict:
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    return bmw_scan.scan(mode)


@app.post("/api/bmw/clear")
async def bmw_clear(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Send JSON {"mode": "real"|"sim", "confirm": true, "address": "0x63"?}.',
        )
    body = body or {}
    if body.get("confirm") is not True:
        raise HTTPException(
            status_code=400,
            detail="Clearing module fault memory changes the car. Send confirm: true.",
        )
    mode = body.get("mode") or "real"
    if mode not in ("real", "sim"):
        raise HTTPException(status_code=400, detail="mode must be 'real' or 'sim'.")
    return bmw_scan.clear(mode, body.get("address"))


# --------------------------------------------------------------------------- #
# Mazda module scan over a generic OBD dongle (HS-CAN, UDS). Read-only except
# /clear, which demands confirm: true. Needs an ELM327-style adapter (there is
# no native Mazda cable); mode=sim returns clearly-labelled fakes.
# --------------------------------------------------------------------------- #
@app.get("/api/mazda/scan")
def mazda_full_scan(adapter: str | None = None, mode: str = "real") -> dict:
    instance = _obd_adapter_or_400(adapter, mode)
    return mazda_scan.scan(instance, mode)


@app.get("/api/mazda/live")
def mazda_live(adapter: str | None = None, mode: str = "real") -> dict:
    """Mazda-specific mode-22 values (one pass). Read-only."""
    instance = _obd_adapter_or_400(adapter, mode)
    return mazda_pids.live(instance, mode)


@app.get("/api/mazda/snapshot")
def mazda_snapshot(adapter: str | None = None, mode: str = "real") -> dict:
    """Passive broadcast-CAN snapshot (ATMA sniff). Read-only."""
    instance = _obd_adapter_or_400(adapter, mode)
    return mazda_snapshot_mod.snapshot(instance, mode)


@app.post("/api/mazda/clear")
async def mazda_clear(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Send JSON {"adapter": "...", "mode": "real"|"sim", "confirm": true, "address": "0x7E0"?}.',
        )
    body = body or {}
    if body.get("confirm") is not True:
        raise HTTPException(
            status_code=400,
            detail="Clearing module fault memory changes the car. Send confirm: true.",
        )
    mode = body.get("mode") or "real"
    instance = _obd_adapter_or_400(body.get("adapter"), mode)
    return mazda_scan.clear(instance, mode, body.get("address"))


# --------------------------------------------------------------------------- #
# Peugeot / PSA module scan over a generic OBD dongle (diagnostic CAN, UDS).
# Read-only except /clear, which demands confirm: true. Needs an ELM327-style
# adapter (no native PSA cable here); mode=sim returns clearly-labelled fakes.
# --------------------------------------------------------------------------- #
@app.get("/api/peugeot/scan")
def peugeot_full_scan(adapter: str | None = None, mode: str = "real") -> dict:
    instance = _obd_adapter_or_400(adapter, mode)
    return peugeot_scan.scan(instance, mode)


@app.post("/api/peugeot/clear")
async def peugeot_clear(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail='Send JSON {"adapter": "...", "mode": "real"|"sim", "confirm": true, "address": "0x752"?}.',
        )
    body = body or {}
    if body.get("confirm") is not True:
        raise HTTPException(
            status_code=400,
            detail="Clearing module fault memory changes the car. Send confirm: true.",
        )
    mode = body.get("mode") or "real"
    instance = _obd_adapter_or_400(body.get("adapter"), mode)
    return peugeot_scan.clear(instance, mode, body.get("address"))


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
        return pairing.claim(str(body.get("code") or ""), str(body.get("deviceName") or ""))
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
