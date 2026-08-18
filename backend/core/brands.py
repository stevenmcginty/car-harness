"""Discovery of per-brand diagnostic capabilities.

A manufacturer profile is a folder under backend/profiles/<id>/ whose optional
submodules provide its diagnostic surface. main.py and mcp_server.py dispatch
to whatever exists, so dropping a new profile folder in is all it takes to
light up its routes:

    scan.py      scan(adapter, mode), clear(adapter, mode, address)
                 -- profiles whose profile.json transport is "enet-doip" use
                    the native-cable forms scan(mode) / clear(mode, address)
                 -- may also expose discover(): discover() on a native-cable
                    profile (it finds the car itself), discover(adapter, mode)
                    on a dongle profile (it sweeps diagnostic addresses)
    discover.py  discover(adapter, mode)  the same dongle address sweep as a
                 separate file when the profile keeps its scan lean (JLR);
                 main.py's /discover dispatch accepts either home
    sweep.py     sweep(adapter, mode, ...)  walk a 0x22 DID range on one module
                 keeping every answer raw -- the DID-mapping instrument
    adblue.py    health(...), drive_log(...)  aftertreatment-focused passes
                 with their results kept raw until decoded against the car.
                 May also expose presets() -- named short DID watch lists the
                 drive log accepts by name instead of a hex list
    dealer_data.py
                 reference(adapter, mode)  what the manufacturer's OWN dealer
                 database says about this family: module address map, named
                 identifiers with factory scaling and units, routine ids.
                 Touches no car -- both arguments are accepted and ignored so
                 the front-end can call it like any other brand action
    pids.py      live(adapter, mode)      brand-specific live values
                 -- "enet-doip" profiles use live(mode), like scan/clear
                 -- ALSO feeds the Live Data dashboard automatically; see the
                    contract in backend/obd/livefeed.py. A brand with no
                    pids.py still gets the full standard OBD-II set.
    snapshot.py  snapshot(adapter, mode)  passive broadcast capture
    software.py  versions(adapter, mode)  the flashed software / calibration
                 part numbers each module reports for itself -- the read that
                 says whether a manufacturer update is even relevant. Read-only
                 by construction; the harness never writes a calibration.
    gearbox.py   analyze(adapter, mode)   TCM-focused diagnosis (ident +
                 faults + live + clutch map). Read-only.
    resets.py    list_resets(), perform(adapter, mode, reset_id, confirm)
    service_history.py
                 catalogue(), build_argument(...)  the car's own service book
                 -- text only, no car contact; see the BMW one for why
    ediabas.py   modules(), run_job(...)  BMW/MINI only: BMW's own SGBD job
                 library driven over ENET via core/ediabas.py (EdiabasTest).
                 Write jobs are confirm-gated inside the profile.
    cbs.py       catalogue(), read(...), reset(...)  service-interval counters
                 read and reset natively; reset() is confirm-gated
    battery.py   status(...), register(...)  battery condition and registering
                 a replacement; register() is confirm-gated

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
        "discover": (scan_mod is not None and hasattr(scan_mod, "discover"))
        or brand_module(brand, "discover") is not None,
        "live": pids_mod is not None and hasattr(pids_mod, "live"),
        # 0x22 DID-range walk on one module (the DID-mapping instrument).
        "sweep": brand_module(brand, "sweep") is not None
        and hasattr(brand_module(brand, "sweep"), "sweep"),
        # AdBlue/SCR/DPF health + drive logging.
        "adblue": brand_module(brand, "adblue") is not None,
        # The manufacturer's own dealer database, loaded as a reference view:
        # module address map, named identifiers with factory scaling, routine
        # identifiers. Touches no car (JLR's is the decrypted SDD 169 extract).
        "dealerData": brand_module(brand, "dealer_data") is not None
        and hasattr(brand_module(brand, "dealer_data"), "reference"),
        # A printable customer-facing PDF. Opt-in per brand: a profile only
        # advertises one once it has something worth putting a name to.
        "report": pids_mod is not None and hasattr(pids_mod, "report"),
        "snapshot": brand_module(brand, "snapshot") is not None,
        # Flashed software / calibration part numbers, read from each module.
        "software": brand_module(brand, "software") is not None,
        # Gearbox-focused read (ident + faults + live + diagnosis).
        "gearbox": brand_module(brand, "gearbox") is not None
        and hasattr(brand_module(brand, "gearbox"), "analyze"),
        "resets": brand_module(brand, "resets") is not None,
        # Building the car's own service-history entry (BMW/MINI iDrive).
        "serviceHistory": brand_module(brand, "service_history") is not None,
        # BMW's own EDIABAS job library driven from the harness (BMW/MINI).
        "ediabas": brand_module(brand, "ediabas") is not None,
        # Service-interval counters read and reset natively (BMW/MINI CBS).
        "cbs": brand_module(brand, "cbs") is not None,
        # Battery condition + registering a replacement (BMW/MINI).
        "battery": brand_module(brand, "battery") is not None,
        "needsAdapter": needs_adapter(brand),
    }
