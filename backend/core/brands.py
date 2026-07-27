"""Discovery of per-brand diagnostic capabilities.

A manufacturer profile is a folder under backend/profiles/<id>/ whose optional
submodules provide its diagnostic surface. main.py and mcp_server.py dispatch
to whatever exists, so dropping a new profile folder in is all it takes to
light up its routes:

    scan.py      scan(adapter, mode), clear(adapter, mode, address)
                 -- profiles whose profile.json transport is "enet-doip" use
                    the native-cable forms scan(mode) / clear(mode, address)
                    and may expose discover()
    pids.py      live(adapter, mode)      brand-specific live values
                 -- "enet-doip" profiles use live(mode), like scan/clear
                 -- ALSO feeds the Live Data dashboard automatically; see the
                    contract in backend/obd/livefeed.py. A brand with no
                    pids.py still gets the full standard OBD-II set.
    snapshot.py  snapshot(adapter, mode)  passive broadcast capture
    resets.py    list_resets(), perform(adapter, mode, reset_id, confirm)
    service_history.py
                 catalogue(), build_argument(...)  the car's own service book
                 -- text only, no car contact; see the BMW one for why

profile.json may also carry a "panel" object (title/hint/actions) that the
desktop front-end renders verbatim -- the UI for a new brand is data, not code.
"""

import importlib

from backend.core import registry


_module_cache: dict[tuple[str, str], object | None] = {}


def profile_meta(brand: str) -> dict | None:
    """The parsed profile.json for a brand, or None if it isn't a profile."""
    path = registry.PROFILES_DIR / brand / "profile.json"
    if not path.is_file():
        return None
    try:
        return registry._read_json(path)
    except (OSError, ValueError):
        return None


def brand_module(brand: str, name: str):
    """Import backend.profiles.<brand>.<name>; None if the profile or that
    capability file doesn't exist. A genuine import error inside an existing
    file still raises -- silently hiding a broken scan.py would be dishonest."""
    key = (brand, name)
    if key in _module_cache:
        return _module_cache[key]
    module = None
    if profile_meta(brand) is not None:
        try:
            module = importlib.import_module(f"backend.profiles.{brand}.{name}")
        except ModuleNotFoundError as exc:
            if exc.name != f"backend.profiles.{brand}.{name}":
                raise  # scan.py exists but imports something missing -- surface it
            module = None
    _module_cache[key] = module
    return module


def needs_adapter(brand: str) -> bool:
    """True when the brand talks through a picked ELM327-style adapter;
    False for native-cable profiles (BMW ENET) that find the car themselves."""
    meta = profile_meta(brand) or {}
    return meta.get("transport") != "enet-doip"


def features(brand: str) -> dict:
    """Which diagnostic surfaces this brand actually ships (drives the UI)."""
    scan_mod = brand_module(brand, "scan")
    pids_mod = brand_module(brand, "pids")
    return {
        "scan": scan_mod is not None and hasattr(scan_mod, "scan"),
        "clear": scan_mod is not None and hasattr(scan_mod, "clear"),
        "discover": scan_mod is not None and hasattr(scan_mod, "discover"),
        "live": pids_mod is not None and hasattr(pids_mod, "live"),
        # A printable customer-facing PDF. Opt-in per brand: a profile only
        # advertises one once it has something worth putting a name to.
        "report": pids_mod is not None and hasattr(pids_mod, "report"),
        "snapshot": brand_module(brand, "snapshot") is not None,
        "resets": brand_module(brand, "resets") is not None,
        # Building the car's own service-history entry (BMW/MINI iDrive).
        "serviceHistory": brand_module(brand, "service_history") is not None,
        "needsAdapter": needs_adapter(brand),
    }
