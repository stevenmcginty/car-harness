"""Discovery of adapter TYPES and persistence of adapter INSTANCES.

Adapter types are the built-in classes below. Adapter instances are the
user-configured copies created via the wizard; they persist to adapters.json in
the project root (created on first save). A missing, empty, or corrupt file is
treated as "no instances" -- it never raises.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from backend.adapters.custom import CustomAdapter
from backend.adapters.elm327_ble import Elm327BleAdapter
from backend.adapters.elm327_bluetooth import Elm327BluetoothAdapter
from backend.adapters.elm327_serial import Elm327SerialAdapter
from backend.adapters.elm327_wifi import Elm327WifiAdapter
from backend.adapters.enet_doip import EnetDoipAdapter
from backend.adapters.j2534 import J2534Adapter
from backend.core.models import AdapterInstance, AdapterTestResult, AdapterType

# registry.py -> adapters -> backend -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADAPTERS_JSON = PROJECT_ROOT / "adapters.json"

# Ordered list of built-in adapter types.
_TYPES = [
    EnetDoipAdapter,
    Elm327SerialAdapter,
    Elm327BluetoothAdapter,
    Elm327BleAdapter,
    Elm327WifiAdapter,
    J2534Adapter,
    CustomAdapter,
]
_TYPES_BY_ID = {t.id: t for t in _TYPES}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# adapter types
# --------------------------------------------------------------------------- #
def list_types() -> List[AdapterType]:
    """Every built-in adapter type as an API model."""
    return [t.meta() for t in _TYPES]


def get_type(type_id: str):
    """The adapter class for ``type_id`` (or None if unknown)."""
    return _TYPES_BY_ID.get(type_id)


# --------------------------------------------------------------------------- #
# instance persistence (adapters.json)
# --------------------------------------------------------------------------- #
def _load_raw() -> List[dict]:
    """Read + normalize saved instances. Never raises: a missing / empty /
    corrupt file yields an empty list."""
    if not ADAPTERS_JSON.is_file():
        return []
    try:
        text = ADAPTERS_JSON.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return []
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, dict):  # tolerate {"adapters": [...]}
        data = data.get("adapters", [])
    if not isinstance(data, list):
        return []

    items: List[dict] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("id") or not item.get("typeId"):
            continue
        config = item.get("config")
        last_test = item.get("lastTest")
        items.append(
            {
                "id": item["id"],
                "typeId": item["typeId"],
                "name": item.get("name") or item["id"],
                "config": config if isinstance(config, dict) else {},
                "lastTest": last_test if isinstance(last_test, dict) else None,
            }
        )
    return items


def _save_raw(items: List[dict]) -> None:
    """Persist instances. An empty list removes the file (back to "absent")."""
    if not items:
        try:
            if ADAPTERS_JSON.is_file():
                ADAPTERS_JSON.unlink()
        except OSError:
            pass
        return
    ADAPTERS_JSON.write_text(json.dumps(items, indent=2), encoding="utf-8")


def list_instances() -> List[AdapterInstance]:
    """All saved adapter instances."""
    out: List[AdapterInstance] = []
    for item in _load_raw():
        try:
            out.append(AdapterInstance(**item))
        except Exception:
            continue
    return out


def get_instance(instance_id: str) -> Optional[AdapterInstance]:
    """A single saved instance by id, or None."""
    for item in _load_raw():
        if item.get("id") == instance_id:
            try:
                return AdapterInstance(**item)
            except Exception:
                return None
    return None


# --------------------------------------------------------------------------- #
# testing + create/remove
# --------------------------------------------------------------------------- #
def test_config(type_id: str, config: dict) -> AdapterTestResult:
    """Run an adapter type's read-only connectivity test WITHOUT saving."""
    adapter_cls = get_type(type_id)
    if adapter_cls is None:
        return AdapterTestResult(
            ok=False, detail=f"Unknown adapter type '{type_id}'.", info={}
        )
    try:
        result = adapter_cls.test(config or {})
    except Exception as exc:  # never let a broken adapter crash the API
        return AdapterTestResult(ok=False, detail=f"Adapter test failed: {exc}", info={})
    if isinstance(result, AdapterTestResult):
        return result
    return AdapterTestResult(**result)


def save_instance(type_id: str, name: str, config: dict) -> AdapterInstance:
    """Test then persist a new adapter instance. Raises ValueError on bad type."""
    if get_type(type_id) is None:
        raise ValueError(f"Unknown adapter type '{type_id}'.")
    result = test_config(type_id, config or {})
    instance = {
        "id": uuid.uuid4().hex,
        "typeId": type_id,
        "name": name,
        "config": dict(config or {}),
        "lastTest": {"ok": result.ok, "detail": result.detail, "at": _now()},
    }
    items = _load_raw()
    items.append(instance)
    _save_raw(items)
    return AdapterInstance(**instance)


def remove_instance(instance_id: str) -> bool:
    """Delete a saved instance. Returns True if removed, False if unknown."""
    items = _load_raw()
    remaining = [i for i in items if i.get("id") != instance_id]
    if len(remaining) == len(items):
        return False
    _save_raw(remaining)
    return True
