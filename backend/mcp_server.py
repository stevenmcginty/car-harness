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
from backend.profiles.bmw import scan as bmw_scan  # noqa: E402

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
