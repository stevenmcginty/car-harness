"""car-harness MCP server -- lets Claude Code drive the harness directly.

This exposes the harness as MCP tools (over stdio) so an assistant can check the
car connection, manage adapters, and add/remove job modules WITHOUT the HTTP
server running: every tool calls the same backend functions main.py does, so the
behaviour is identical to the REST API (single source of truth).

Wire it into Claude Code via the .mcp.json in the project root. Run standalone
with:  .venv\\Scripts\\python.exe backend\\mcp_server.py
"""

import sys
from pathlib import Path

# Make `import backend.*` work no matter the cwd this is launched from.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402
from serial.tools import list_ports  # noqa: E402

from backend.adapters import registry as adapter_registry  # noqa: E402
from backend.core import interfaces, registry  # noqa: E402
from backend.profiles.bmw import resets as bmw_resets  # noqa: E402
from backend.profiles.bmw import scan as bmw_scan  # noqa: E402
from backend.profiles.mazda import pids as mazda_pids  # noqa: E402
from backend.profiles.mazda import resets as mazda_resets  # noqa: E402
from backend.profiles.mazda import scan as mazda_scan  # noqa: E402
from backend.profiles.mazda import snapshot as mazda_snapshot_mod  # noqa: E402
from backend.profiles.peugeot import pids as peugeot_pids  # noqa: E402
from backend.profiles.peugeot import resets as peugeot_resets  # noqa: E402
from backend.profiles.peugeot import scan as peugeot_scan  # noqa: E402

_RESETS_BY_BRAND = {"bmw": bmw_resets, "mazda": mazda_resets, "peugeot": peugeot_resets}

mcp = FastMCP("car-harness")


# --------------------------------------------------------------------------- #
# Connection status
# --------------------------------------------------------------------------- #
@mcp.tool()
def get_connection_status(
    profile: str = "bmw", mode: str = "real", adapter_id: str | None = None
) -> dict:
    """Check whether the harness is connected to a car and how far it got.

    profile: manufacturer profile id (currently "bmw").
    mode: "real" runs a genuine, read-only probe (for BMW this broadcasts a DoIP
        vehicle-identification request and waits ~2s -- nothing is written to the
        car); "sim" returns a clearly-labelled fake so the flow can be seen with
        no car present.
    adapter_id: optional configured-adapter instance id (from list_adapters); omit
        for the profile's native auto-detected transport.
    Returns the full connection status: overall state, the step-by-step ladder,
        and the vehicle (VIN etc.) if one answered.
    """
    if mode not in ("real", "sim"):
        return {"error": "mode must be 'real' or 'sim'."}
    probe_module = registry.load_profile_probe(profile)
    if probe_module is None or not hasattr(probe_module, "status"):
        return {"error": f"Unknown or invalid profile '{profile}'."}
    if adapter_id is None:
        return probe_module.status(mode).model_dump()
    instance = adapter_registry.get_instance(adapter_id)
    if instance is None:
        return {"error": f"Unknown adapter '{adapter_id}'."}
    return probe_module.status(mode, adapter=instance.model_dump()).model_dump()


# --------------------------------------------------------------------------- #
# BMW deep scan over the ENET cable (HSFZ, F-series)
# --------------------------------------------------------------------------- #
@mcp.tool()
def bmw_discover() -> dict:
    """Find a BMW on the ENET cable (read-only).

    Tries HSFZ identification (F-series, e.g. a 2015 F26 X4) then DoIP
    (G-series). Returns {found, protocol, ip, vin, detail}. Needs the yellow
    ENET cable in the car's OBD port and the laptop's USB-Ethernet adapter;
    the link-local address takes ~30s to settle after plugging in.
    """
    return bmw_scan.discover()


@mcp.tool()
def bmw_scan_modules(mode: str = "real") -> dict:
    """Read every module's fault memory over the ENET cable (read-only).

    Walks the common F-series ECU addresses through the gateway -- engine,
    gearbox, airbag, ABS, steering, body, cluster, head unit, climate -- and
    reads each one's manufacturer-level fault codes (far deeper than generic
    OBD-II). Ignition must be ON. mode="sim" returns a clearly-labelled fake.
    Takes 30-60s for real; absent modules just time out quietly.
    """
    if mode not in ("real", "sim"):
        return {"error": "mode must be 'real' or 'sim'."}
    return bmw_scan.scan(mode)


@mcp.tool()
def bmw_clear_module(confirm: bool = False, address: str | None = None, mode: str = "real") -> dict:
    """Clear fault memory on one BMW module (or all present) -- CHANGES THE CAR.

    confirm: must be True, and only after the user has explicitly agreed.
    address: hex diagnostic address like "0x63" (head unit); omit to clear
        every module that answers. Faults return if the underlying problem
        is still present.
    """
    if confirm is not True:
        return {"error": "Refused: clearing changes the car. Pass confirm=true after the user agrees."}
    if mode not in ("real", "sim"):
        return {"error": "mode must be 'real' or 'sim'."}
    return bmw_scan.clear(mode, address)


# --------------------------------------------------------------------------- #
# Mazda module scan over a generic OBD dongle (HS-CAN, UDS)
# --------------------------------------------------------------------------- #
def _elm_adapter_or_error(mode: str, adapter_id: str | None):
    """Resolve an ELM327 adapter for a real Mazda call, or return an error dict.

    Returns (instance_dict_or_None, error_dict_or_None). For mode='sim' the
    instance is None and there is no error.
    """
    if mode not in ("real", "sim"):
        return None, {"error": "mode must be 'real' or 'sim'."}
    if mode == "sim":
        return None, None
    if not adapter_id:
        return None, {"error": "Pick an ELM327 adapter (adapter_id from list_adapters) or use mode='sim'."}
    instance = adapter_registry.get_instance(adapter_id)
    if instance is None:
        return None, {"error": f"Unknown adapter '{adapter_id}'."}
    return instance.model_dump(), None


@mcp.tool()
def mazda_scan_modules(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read a Mazda's module fault memory over a generic OBD-II dongle (read-only).

    Walks the HS-CAN physical addresses (0x7E0-0x7E7) and reads each responding
    module's manufacturer-level UDS fault codes + identification -- deeper than
    generic OBD-II. Tuned for 2016 SkyActiv cars (e.g. an MX-5 ND). Ignition
    must be ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    Note: body/comfort modules live on Mazda's MS-CAN bus and are NOT reachable
    through a standard OBD dongle -- that needs an MS-CAN switchable adapter.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return mazda_scan.scan(instance, mode)


@mcp.tool()
def mazda_live_values(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read Mazda-specific (mode-22) live values over a generic OBD dongle.

    One read-only pass over the community-documented SkyActiv enhanced DIDs on
    the PCM -- data generic OBD-II doesn't carry. Values this particular
    car/engine doesn't support are skipped silently. Ignition must be ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return mazda_pids.live(instance, mode)


@mcp.tool()
def mazda_snapshot(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Passively decode a Mazda's broadcast CAN frames over a generic dongle.

    Listens to the bus with the ELM327's monitor mode (nothing is sent to any
    module) and decodes the ND-community-verified frames: RPM, speed, pedals,
    coolant/outside temps, wheel speeds, brake pressure, steering angle, gear,
    fuel litres, doors, handbrake, ignition state, odometer. Values this
    car/year doesn't broadcast are skipped. Works with ignition ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return mazda_snapshot_mod.snapshot(instance, mode)


@mcp.tool()
def mazda_clear_module(
    confirm: bool = False, adapter_id: str | None = None,
    address: str | None = None, mode: str = "real",
) -> dict:
    """Clear fault memory on Mazda modules over a dongle -- CHANGES THE CAR.

    confirm: must be True, and only after the user has explicitly agreed.
    adapter_id: a configured ELM327 adapter instance (required for real mode).
    address: hex diagnostic address like "0x7E0" (the engine/PCM); omit to clear
        every HS-CAN module that answers. Faults return if the underlying
        problem is still present.
    """
    if confirm is not True:
        return {"error": "Refused: clearing changes the car. Pass confirm=true after the user agrees."}
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return mazda_scan.clear(instance, mode, address)


# --------------------------------------------------------------------------- #
# Peugeot / PSA module scan over a generic OBD dongle (diagnostic CAN, UDS)
# --------------------------------------------------------------------------- #
@mcp.tool()
def peugeot_scan_modules(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read a Peugeot's module fault memory over a generic OBD-II dongle (read-only).

    Walks PSA's per-module diagnostic CAN id pairs -- BSI (0x752), engine (0x6A8),
    airbag (0x744), ABS/ESP (0x6AD), cluster (0x75F), AdBlue/SCR (0x6A1) and more
    -- and reads each responding module's manufacturer-level UDS fault codes +
    identification, deeper than generic OBD-II. For AEE2010-era PSA cars (~2010+
    Peugeot, Citroen, DS). Ignition must be ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    Note: pre-2004 (VAN bus) and many 2004-2010 (KWP2000) PSA cars can't be
    module-scanned through a standard OBD dongle.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return peugeot_scan.scan(instance, mode)


@mcp.tool()
def peugeot_health_check(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read Peugeot/PSA dealer health-check values over a generic dongle.

    One read-only pass over community-documented PSA mode-22 DIDs: DPF soot
    load and regeneration data (HDi/BlueHDi diesels), AdBlue level, battery
    state, engine temperatures, plus odometer readings from more than one
    module cross-checked against each other -- a mismatch is a clocking red
    flag. Values this car/engine doesn't support are skipped. Ignition ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return peugeot_pids.live(instance, mode)


@mcp.tool()
def peugeot_clear_module(
    confirm: bool = False, adapter_id: str | None = None,
    address: str | None = None, mode: str = "real",
) -> dict:
    """Clear fault memory on Peugeot modules over a dongle -- CHANGES THE CAR.

    confirm: must be True, and only after the user has explicitly agreed.
    adapter_id: a configured ELM327 adapter instance (required for real mode).
    address: hex request id like "0x752" (the BSI) or "0x6A8" (the engine); omit
        to clear every PSA module that answers. Faults return if the underlying
        problem is still present.
    """
    if confirm is not True:
        return {"error": "Refused: clearing changes the car. Pass confirm=true after the user agrees."}
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return peugeot_scan.clear(instance, mode, address)


# --------------------------------------------------------------------------- #
# Service resets (oil / brake / inspection counters) per manufacturer
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_service_resets(brand: str) -> dict:
    """List the service resets available for a brand (read-only catalog).

    brand: "bmw", "mazda" or "peugeot". Returns each reset's name, description,
    method ("uds" = sendable over the dongle, "procedure" = manual dash steps),
    a confidence grade, the source it came from, and either the UDS steps or the
    dash procedure. No car or adapter is needed to list them.
    """
    module = _RESETS_BY_BRAND.get(brand)
    if module is None:
        return {"error": f"No service resets for '{brand}'."}
    return module.list_resets()


@mcp.tool()
def perform_service_reset(
    brand: str, reset_id: str, confirm: bool = False,
    adapter_id: str | None = None, mode: str = "real",
) -> dict:
    """Perform one service reset -- CHANGES THE CAR for "uds" resets.

    brand: "bmw", "mazda" or "peugeot". reset_id: from list_service_resets.
    confirm: must be True, and only after the user has explicitly agreed.
    adapter_id: a configured ELM327 adapter (required for a real "uds" reset).
    mode="sim" shows the steps without touching a car. "procedure" resets never
    touch the car -- they return the manual steps to follow.
    """
    module = _RESETS_BY_BRAND.get(brand)
    if module is None:
        return {"error": f"No service resets for '{brand}'."}
    if mode == "real":
        instance, error = _elm_adapter_or_error(mode, adapter_id)
        if error is not None:
            return error
        return module.perform(instance, mode, reset_id, confirm)
    if mode != "sim":
        return {"error": "mode must be 'real' or 'sim'."}
    return module.perform(None, mode, reset_id, confirm)


# --------------------------------------------------------------------------- #
# Profiles + network interfaces
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_profiles() -> dict:
    """List the manufacturer profiles the harness knows, with their job modules."""
    return {"profiles": registry.list_profiles()}


@mcp.tool()
def list_interfaces() -> dict:
    """List the machine's network adapters (used to spot the ENET link-local one)."""
    return {"interfaces": interfaces.list_interfaces()}


# --------------------------------------------------------------------------- #
# Adapters (how the harness physically talks to a car)
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_adapter_types() -> dict:
    """List the built-in adapter TYPES (ENET/DoIP, ELM327 serial/Bluetooth, J2534,
    custom) and the config fields each needs."""
    return {"types": [t.model_dump(exclude_none=True) for t in adapter_registry.list_types()]}


@mcp.tool()
def list_adapters() -> dict:
    """List the user-configured adapter INSTANCES saved via the wizard."""
    return {"adapters": [i.model_dump() for i in adapter_registry.list_instances()]}


@mcp.tool()
def list_serial_ports() -> dict:
    """List available serial / COM ports (for configuring an ELM327 adapter)."""
    ports = []
    try:
        for p in list_ports.comports():
            ports.append({"device": p.device, "description": p.description or ""})
    except Exception:
        ports = []
    return {"ports": ports}


@mcp.tool()
def test_adapter(type_id: str, config: dict) -> dict:
    """Run an adapter type's read-only connectivity test WITHOUT saving it.

    type_id: one of the ids from list_adapter_types.
    config: the type's config fields (e.g. {"port": "COM3"} for ELM327).
    """
    return adapter_registry.test_config(type_id, config or {}).model_dump()


@mcp.tool()
def add_adapter(type_id: str, name: str, config: dict) -> dict:
    """Test then save a new adapter instance (as the wizard's Save step does)."""
    try:
        return adapter_registry.save_instance(type_id, name, config or {}).model_dump()
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool()
def remove_adapter(adapter_id: str) -> dict:
    """Delete a saved adapter instance by id."""
    if adapter_registry.remove_instance(adapter_id):
        return {"removed": True}
    return {"error": f"Adapter '{adapter_id}' not found."}


# --------------------------------------------------------------------------- #
# Job modules (installable/removable per-manufacturer software)
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_job_modules(profile: str | None = None) -> dict:
    """List job modules (built-in + imported), optionally filtered by profile."""
    return {"modules": registry.list_modules(profile)}


@mcp.tool()
def import_job_module(path: str) -> dict:
    """Import a job module from a folder or .zip on disk into modules_store/.

    Use this to add downloaded software to the harness. The source must contain a
    module.json. Returns the new module record (or an error).
    """
    try:
        return registry.import_module(path)
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}


@mcp.tool()
def remove_job_module(module_id: str) -> dict:
    """Delete an imported job module to reclaim disk. Built-in modules are refused."""
    try:
        return registry.remove_module(module_id)
    except PermissionError as exc:
        return {"error": str(exc)}
    except FileNotFoundError as exc:
        return {"error": str(exc)}


if __name__ == "__main__":
    mcp.run()
