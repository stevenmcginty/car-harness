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
from backend.core import captures  # noqa: E402
from backend.core import brands, interfaces, registry  # noqa: E402
from backend.obd import raw as obd_raw  # noqa: E402
from backend.profiles.bmw import ediabas as bmw_ediabas  # noqa: E402
from backend.profiles.bmw import battery as bmw_battery  # noqa: E402
from backend.profiles.bmw import cbs as bmw_cbs  # noqa: E402
from backend.profiles.bmw import scan as bmw_scan  # noqa: E402
from backend.profiles.fiat import pids as fiat_pids  # noqa: E402
from backend.profiles.fiat import scan as fiat_scan  # noqa: E402
from backend.profiles.ford import pids as ford_pids  # noqa: E402
from backend.profiles.ford import scan as ford_scan  # noqa: E402
from backend.profiles.jlr import adblue as jlr_adblue  # noqa: E402
from backend.profiles.jlr import dealer_data as jlr_dealer_data_mod  # noqa: E402
from backend.profiles.jlr import discover as jlr_discover  # noqa: E402
from backend.profiles.jlr import pids as jlr_pids  # noqa: E402
from backend.profiles.jlr import scan as jlr_scan  # noqa: E402
from backend.profiles.jlr import sweep as jlr_sweep  # noqa: E402
from backend.profiles.mazda import pids as mazda_pids  # noqa: E402
from backend.profiles.mazda import scan as mazda_scan  # noqa: E402
from backend.profiles.mazda import snapshot as mazda_snapshot_mod  # noqa: E402
from backend.profiles.mercedes import misfire as mercedes_misfire  # noqa: E402
from backend.profiles.mercedes import pids as mercedes_pids  # noqa: E402
from backend.profiles.mercedes import scan as mercedes_scan  # noqa: E402
from backend.profiles.peugeot import oil as peugeot_oil  # noqa: E402
from backend.profiles.peugeot import pids as peugeot_pids  # noqa: E402
from backend.profiles.peugeot import scan as peugeot_scan  # noqa: E402
from backend.profiles.vauxhall import pids as vauxhall_pids  # noqa: E402
from backend.profiles.vauxhall import scan as vauxhall_scan  # noqa: E402
from backend.profiles.vauxhall import software as vauxhall_software  # noqa: E402
from backend.profiles.vauxhall import tcm as vauxhall_tcm  # noqa: E402
from backend.profiles.vauxhall import gearbox as vauxhall_gearbox  # noqa: E402
from backend.profiles.vauxhall import procedures as vauxhall_procedures  # noqa: E402
from backend.profiles.vw import pids as vw_pids  # noqa: E402
from backend.profiles.vw import scan as vw_scan  # noqa: E402

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
# BMW/MINI EDIABAS bridge -- BMW's own SGBD job library over ENET
# --------------------------------------------------------------------------- #
@mcp.tool()
def bmw_ediabas_modules() -> dict:
    """List the EDIABAS bridge catalogue for BMW/MINI (no car contact).

    Returns whether the bridge is usable on this machine (EdiabasTest runner +
    C:\\EDIABAS\\ECU SGBD library present) and, per module (engine, gearbox,
    cluster, head unit...), the curated allowlist of jobs that can be run:
    idents, fault memory, service-history write, CBS resets. Jobs marked
    verify=true are mapped but not yet confirmed on a real car.
    """
    return bmw_ediabas.modules()


@mcp.tool()
def bmw_cbs_read(mode: str = "real") -> dict:
    """Read a BMW/MINI's service-interval (CBS) counters over the ENET cable.

    Returns every service item the car holds -- engine oil, brake pads, brake
    fluid, MOT, emissions -- with remaining life in percent, remaining
    distance and the service counter, plus which module carried each and which
    CBS version it speaks. Read-only; ignition ON.
    """
    if mode not in ("real", "sim"):
        return {"error": "mode must be 'real' or 'sim'."}
    return bmw_cbs.read(mode)


@mcp.tool()
def bmw_cbs_reset(item: str, confirm: bool = False, mode: str = "real") -> dict:
    """Reset one BMW/MINI service item. CHANGES THE CAR -- confirm required.

    item: "Oel" (engine oil), "Br_v"/"Br_h" (front/rear brake pads), "Brfl"
        (brake fluid), "TUV" (MOT), "AU" (emissions) -- or the English label.
    Aims at the module that owns the item (engine for oil, ABS for pads,
    cluster otherwise) and asks that module its CBS version first, because the
    argument list differs between versions on the same car.
    Resets the counter ONLY -- the iDrive service history is separate, see
    the service-history tools.
    """
    if mode not in ("real", "sim"):
        return {"error": "mode must be 'real' or 'sim'."}
    try:
        return bmw_cbs.reset(item, mode, confirm)
    except bmw_cbs.CbsError as exc:
        return {"error": str(exc)}


@mcp.tool()
def bmw_battery_status(mode: str = "real") -> dict:
    """Read a BMW/MINI's battery condition from the engine's power management.

    State of health, state of charge, rated capacity, live voltage/current/
    temperature from the IBS sensor, and the mileage at the last two battery
    changes. Read-only; ignition ON with the ENET cable connected.
    """
    if mode not in ("real", "sim"):
        return {"error": "mode must be 'real' or 'sim'."}
    return bmw_battery.status(mode)


@mcp.tool()
def bmw_battery_register(confirm: bool = False, mode: str = "real") -> dict:
    """Register a replacement battery on a BMW/MINI. CHANGES THE CAR.

    Only after a new battery has actually been fitted: it resets the engine's
    charge history so charging strategy and start/stop suit a new battery.
    Does NOT change the coded battery type or capacity -- a different size or
    AGM-for-lead-acid swap needs coding this harness doesn't do.
    """
    if mode not in ("real", "sim"):
        return {"error": "mode must be 'real' or 'sim'."}
    return bmw_battery.register(mode, confirm)


@mcp.tool()
def bmw_ediabas_installed(mode: str = "real") -> dict:
    """Ask a BMW/MINI's gateway which control units the car actually has.

    Runs STATUS_VCM_GET_ECU_LIST_ALL on the ZGW over the ENET cable and maps
    each reported diagnostic address back to a harness module key, so the
    answer comes from the car's own stored list instead of probing addresses
    and guessing from silence. Ignition ON, cable connected; mode="sim"
    returns a clearly-labelled fake.
    """
    if mode not in ("real", "sim"):
        return {"error": "mode must be 'real' or 'sim'."}
    try:
        return bmw_ediabas.installed_modules(mode)
    except bmw_ediabas.EdiabasJobError as exc:
        return {"error": str(exc)}


@mcp.tool()
def bmw_ediabas_run(
    module: str,
    job: str,
    params: list[str] | None = None,
    mode: str = "real",
    confirm: bool = False,
) -> dict:
    """Run one allowlisted EDIABAS job on a BMW/MINI module over the ENET cable.

    module: key from bmw_ediabas_modules (e.g. "dme", "kombi", "hu").
    job: job name from that module's allowlist (e.g. "IDENTIFIKATION",
        "FS_LESEN"). Anything not allowlisted is refused.
    params: job arguments as strings, semicolon-order per the SGBD docs.
    confirm: REQUIRED True for kind=write jobs (fault-clear, module reset,
        service-history add/erase, CBS reset) -- only after the user agrees.
    Ignition must be ON with the ENET cable connected. mode="sim" returns a
    clearly-labelled fake without touching a car.
    """
    if mode not in ("real", "sim"):
        return {"error": "mode must be 'real' or 'sim'."}
    try:
        return bmw_ediabas.run_job(module, job, params, mode, confirm)
    except bmw_ediabas.EdiabasJobError as exc:
        return {"error": str(exc)}


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
# Ford module scan over a generic OBD dongle (HS-CAN, UDS) -- Ranger T6 P375
# --------------------------------------------------------------------------- #
@mcp.tool()
def ford_scan_modules(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read a Ford's module fault memory over a generic OBD-II dongle (read-only).

    Walks the Ford global module map on HS-CAN (PCM 0x7E0, TCM 0x7E1, ABS 0x760,
    BCM 0x726, IPC 0x720, RCM 0x737, PSCM 0x730, APIM 0x7D0, ...) and reads each
    responding module's UDS fault codes + identification (F190 VIN, F188/F111/
    F113/F124 part numbers). Built for the 2018 Ranger T6 3.2 Duratorq auto.
    Ignition must be ON. Works over the phone relay adapter (elm327-remote).

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    Note: MS-CAN body modules need a switchable adapter and are not reachable
    through a standard OBD dongle.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return ford_scan.scan(instance, mode)


@mcp.tool()
def ford_live_values(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read Ford IDS-table (mode-22) live values from the PCM over a dongle.

    One read-only pass over the DIDs Ford's own IDS 133.01 MDX tables define
    for the P375 Ranger diesel PCM: rail pressure control deviation (050D),
    soot load / DPF test / soot-in-oil (057B/0380/0508 -- delete evidence on a
    DPF-deleted truck), pilot correction (FD00), and the Bosch 0xDExx
    adjustment blocks (IMA, EGR, throttle, learn counters). Every reading keeps
    its raw hex beside the decode. Unanswered DIDs are skipped. Ignition ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return ford_pids.live(instance, mode)


@mcp.tool()
def ford_clear_module(
    confirm: bool = False, adapter_id: str | None = None,
    address: str | None = None, mode: str = "real",
) -> dict:
    """Clear fault memory on Ford modules over a dongle -- CHANGES THE CAR.

    confirm: must be True, and only after the user has explicitly agreed.
    adapter_id: a configured ELM327 adapter instance (required for real mode).
    address: hex diagnostic address like "0x7E0" (the PCM); omit to clear every
        HS-CAN module that answers. Faults return if the cause is still there.
    """
    if confirm is not True:
        return {"error": "Refused: clearing changes the car. Pass confirm=true after the user agrees."}
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return ford_scan.clear(instance, mode, address)


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


@mcp.tool()
def peugeot_engine_probe(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Map a PSA petrol engine ECU's diagnostic surface (read-only, EP6/THP work).

    Reports, for both the PSA pair (0x6A8/0x688) and the standard OBD slot
    (0x7E0): does the engine answer, does service 0x22 work, do the 10 C0 /
    10 03 sessions open. Adds VIN + calibration id (tells a THP 156 "5FV" from
    a THP 200 "5FF"), the live generic PIDs (incl. whether oil temp 0x5C is
    implemented), and any 0xF4xx mirror DIDs. Run this FIRST on a car, then
    aim peugeot_engine_sweep / peugeot_oil_capture at the address that
    answered. Ignition ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return peugeot_oil.probe(instance, mode)


@mcp.tool()
def peugeot_oil_coldstart(
    adapter_id: str | None = None, seconds: float = 180.0,
    blocks: list[str] | None = None, mode: str = "real",
) -> dict:
    """Capture a PSA petrol engine's whole cold start (read-only, EP6/THP).

    For the oil-pressure-warning-at-startup complaint. Takes a generic-OBD
    baseline (rpm, coolant, voltage, fuel trims) BEFORE opening the diagnostic
    session -- on these ECUs the two are mutually exclusive -- then logs every
    KWP measurement block raw for the whole run, decoding oil pressure live
    from block C2 (u16 at offset 7, hundredths of a bar, identified on a known
    good RCZ).

    Procedure: ignition ON, engine OFF and cold, start this, then crank and let
    it idle untouched. No throttle -- the idle curve is the evidence.

    blocks: hex block ids; omit for all ten that a THP answers.
    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return peugeot_oil.coldstart(instance, seconds, blocks, mode)


@mcp.tool()
def peugeot_engine_blocks(
    adapter_id: str | None = None, seconds: float = 90.0,
    blocks: list[str] | None = None, mode: str = "real",
) -> dict:
    """Log PSA KWP measurement blocks against a rev change (read-only).

    The discovery counterpart to peugeot_oil_coldstart, for engine ECUs that
    refuse UDS service 0x22 so the published oil DIDs are unreachable. Logs
    blocks raw while the engine is revved; a signal is then identified by which
    bytes move with rpm. Block C2's oil pressure is decoded live.

    blocks: hex block ids like ["C2","CB"]; omit for all ten. Fewer blocks
        means a faster sample rate, which matters when chasing a transient.
    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return peugeot_oil.block_capture(instance, seconds, blocks, mode)


@mcp.tool()
def peugeot_engine_sweep(
    first: str, last: str, adapter_id: str | None = None, address: str = "psa",
    extended: bool = False, mode: str = "real",
) -> dict:
    """Walk a mode-22 DID range on a PSA engine ECU and keep every answer (read-only).

    Walk a mode-22 DID range and keep every raw answer (read-only).

    Catalog already has oil pressure at D429 (and D47D / D41D). Sweep is for
    the two still-missing signals -- OilPressInstr and OilPumpValve -- plus
    a 5FV that stays silent on D429. Diff idle vs ~2500 rpm. NRC 0x31
    refusals are counted, not listed (ECU listens, id not implemented).

    first/last: hex DID bounds like "D400"/"D4FF"; max 4096 ids per call.
        Start at D400-D4FF, then 0100-04FF, 1000-13FF, 2000-21FF, F400-F4FF.
    address: "psa" (0x6A8 pair, what Diagbox uses) or "obd" (0x7E0) -- pick
        whichever peugeot_engine_probe showed answering service 0x22.
    extended: open the read-safe PSA session ladder first (some DIDs only
        answer there).
    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    try:
        first_i, last_i = int(first, 16), int(last, 16)
    except (TypeError, ValueError):
        return {"ok": False, "message": "first/last must be hex like '1000'."}
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return peugeot_oil.sweep(instance, first_i, last_i, address, extended, mode=mode)


@mcp.tool()
def peugeot_oil_capture(
    adapter_id: str | None = None, seconds: float = 120.0,
    dids: list[str] | None = None, address: str = "psa",
    extended: bool = False, mode: str = "real",
) -> dict:
    """Timestamped cold-start capture on a PSA petrol engine (read-only, crank-proof).

    Built for the EP6/THP oil-pressure-at-startup complaint: connect with
    ignition ON engine OFF, start the capture, wait ~5 s, crank, idle, and near
    the end rev slowly to ~3000 rpm (pump stage switch). Every tick logs engine
    rpm plus each DID in ``dids`` raw; every fifth tick adds coolant, oil temp
    and module voltage (the voltage dip timestamps the crank). Survives the
    ECU dropping off the bus during cranking -- gaps are recorded, not fatal.
    Same capture on the known-good RCZ and the problem 308, then compare.

    dids: omit to use the imported catalog (D429 pressure, D47D oil temp,
        D41D sensor mV). Pass [] for the rpm/coolant/voltage baseline only.
        Result includes decoded bar/C plus an assessment vs the 5FV bands.
    address: "psa" (0x6A8) or "obd" (0x7E0), per peugeot_engine_probe.
    extended: open the read-safe PSA session first for DIDs that need it.
    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return peugeot_oil.capture(instance, seconds, dids, address, extended, mode=mode)


# --------------------------------------------------------------------------- #
# VW / Audi (VAG) module scan over a generic OBD dongle (pure UDS)
# --------------------------------------------------------------------------- #
@mcp.tool()
def vw_scan_modules(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read a VW/Audi's module fault memory over a generic OBD-II dongle (read-only).

    Walks VAG's diagnostic address table -- powertrain on the standard block
    (engine 0x7E0, gearbox 0x7E1), body/chassis/comfort on VAG's own block
    (gateway 0x710, cluster 0x714, ABS 0x713, airbag 0x715 and more, answered on
    request + 0x6A) -- and reads each responding module's manufacturer-level UDS
    fault codes + identification, deeper than generic OBD-II. For VW/Škoda/SEAT
    ~2012+ (MQB, Golf 7 on) and Audi ~2008+ (B8 on) -- pure UDS, no KWP dialect.
    Ignition must be ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    Note: pre-2012 VAG cars (TP2.0/K-line) can't be module-scanned through a
    standard OBD dongle.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return vw_scan.scan(instance, mode)


@mcp.tool()
def vw_live_values(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read VW/Audi-specific live "extras" over a generic OBD dongle.

    One read-only pass over the engine ECU's remapped-OBD DIDs (coolant, oil
    temp, battery voltage, fuel level, ambient temp, load) plus community-
    sourced diesel DPF and EV state-of-charge DIDs shown as raw values only
    (their scaling isn't publicly confirmed). Values this particular car/engine
    doesn't support are skipped silently. Ignition must be ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return vw_pids.live(instance, mode)


@mcp.tool()
def vw_clear_module(
    confirm: bool = False, adapter_id: str | None = None,
    address: str | None = None, mode: str = "real",
) -> dict:
    """Clear fault memory on VW/Audi modules over a dongle -- CHANGES THE CAR.

    confirm: must be True, and only after the user has explicitly agreed.
    adapter_id: a configured ELM327 adapter instance (required for real mode).
    address: hex request id like "0x7E0" (the engine) or "0x710" (the gateway);
        omit to clear every VAG module that answers. Faults return if the
        underlying problem is still present.
    """
    if confirm is not True:
        return {"error": "Refused: clearing changes the car. Pass confirm=true after the user agrees."}
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return vw_scan.clear(instance, mode, address)


# --------------------------------------------------------------------------- #
# Mercedes-Benz engine + gearbox read over a generic OBD dongle (UDS + KWP2000)
# --------------------------------------------------------------------------- #
@mcp.tool()
def mercedes_scan_modules(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read a Mercedes' engine + gearbox fault memory over a generic OBD-II dongle (read-only).

    Reads the two modules a plain dongle reaches at manufacturer depth on a
    Mercedes -- engine (0x7E0/0x7E8) and automatic transmission (0x7E1/0x7E9),
    both on the standard +8 block -- and each one's fault codes + identification,
    laddering UDS then Mercedes' KWP2000-over-CAN dialect. VIN first over Mode 09.
    For mainstream Mercedes ~2004-2018 (petrol OBD ~2001+, EU diesel ~2004+).
    Ignition must be ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    Note: this is NOT a VCDS/ISTA-style all-modules walk. Body/chassis/comfort
    modules (ABS/ESP, SRS, SAM, EZS, cluster, Airmatic, climate) use Mercedes'
    proprietary addressing behind the gateway and are STAR/XENTRY-only -- no OBD
    dongle reaches them. ~2018+ cars are DoIP/Ethernet and give generic OBD only.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return mercedes_scan.scan(instance, mode)


@mcp.tool()
def mercedes_live_values(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read Mercedes-specific live "extras" over a generic OBD dongle.

    One read-only pass: the one verified Mercedes-specific value -- the 722.9/9G
    automatic-transmission (ATF) oil temperature on the gearbox ECU 0x7E1 (tried
    as KWP 21 30 then UDS 22 2130; its -50 vs -40 offset needs a real-car confirm)
    -- plus a curated generic Mode-01 set (coolant, oil temp, module voltage,
    ambient, fuel level) on the engine. Values this car doesn't support are
    skipped. Ignition must be ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    Note: DPF soot / AdBlue level have no public DID for Mercedes, so DPF/SCR
    health shows through the fault codes, not a live value.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return mercedes_pids.live(instance, mode)


@mcp.tool()
def mercedes_clear_module(
    confirm: bool = False, adapter_id: str | None = None,
    address: str | None = None, mode: str = "real",
) -> dict:
    """Clear fault memory on Mercedes engine/gearbox modules over a dongle -- CHANGES THE CAR.

    confirm: must be True, and only after the user has explicitly agreed.
    adapter_id: a configured ELM327 adapter instance (required for real mode).
    address: hex request id like "0x7E0" (the engine) or "0x7E1" (the gearbox);
        omit to clear both modules that answer. Clears via UDS then the KWP
        14 FF 00 fallback. Faults return if the underlying problem is still present.
    """
    if confirm is not True:
        return {"error": "Refused: clearing changes the car. Pass confirm=true after the user agrees."}
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return mercedes_scan.clear(instance, mode, address)


@mcp.tool()
def mercedes_slk_identify(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Identify which engine a Mercedes SLK R172 (or similar M271/M276 car) has.

    slk.md step 0: reads the engine ECU's ident DIDs (VIN, part numbers) on
    0x7E0 and probes one SIM271DE20-only marker DID vs the MED177-only markers,
    so the rough-running capture knows which mined dealer-DID table applies --
    M271 EVO turbo 4-cyl (SLK200/250) or M276 V6 (SLK350). Read-only.
    Ignition must be ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return mercedes_misfire.identify(instance, mode)


@mcp.tool()
def mercedes_slk_misfire_capture(
    seconds: float = 300.0, ecu: str | None = None,
    adapter_id: str | None = None, mode: str = "real",
) -> dict:
    """Capture the SLK rough-running drive log over a generic OBD dongle (read-only).

    captures/mercedes/slk.md step 4. Start this BEFORE the drive profile:
    cold start + untouched idle 3 min -> warm idle 2 min -> hold ~2000 rpm ->
    hold ~3000 rpm (-> road loop if load-dependent). Every tick logs rpm, the
    per-cylinder misfire counters and upstream lambda; every 5th tick adds
    coolant, oil temp, voltage, load, MAF, fuel pressures and adaptations --
    all from XENTRY-mined dealer DIDs (graded candidate until they answer).
    Returns the assessment: which cylinder climbed, cold-vs-warm split, trim
    drift, rail-pressure sag, matched known issues. Read-only.

    seconds: capture length 30-3600. ecu: omit to auto-identify, or
        "sim271de20_m271_turbo" / "med177_m276_v6" to skip that step.
    adapter_id: a configured ELM327 adapter instance; required for real mode.
    """
    if ecu is not None and ecu not in (
            "sim271de20_m271_turbo", "med177_m276_v6"):
        return {"error": "ecu must be sim271de20_m271_turbo or med177_m276_v6."}
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return mercedes_misfire.capture(instance, seconds, ecu, mode)


@mcp.tool()
def mercedes_slk_known_issues(ecu: str | None = None) -> dict:
    """The researched rough-running known-issues reference for the SLK engines.

    What online sources (Mercedes technical instructions, workshop guides,
    owner forums -- each row carries its links) document for M271 EVO and
    M276 rough running/misfires, with the live-data signature hint each one
    would show in mercedes_slk_misfire_capture output. A reading aid ranked
    against a capture -- never a diagnosis on its own.

    ecu: filter to "sim271de20_m271_turbo" or "med177_m276_v6"; omit for both.
    """
    return mercedes_misfire.known_issues(ecu)


# --------------------------------------------------------------------------- #
# Vauxhall / Opel (GM Global A) over a generic OBD dongle
# --------------------------------------------------------------------------- #
@mcp.tool()
def vauxhall_scan_modules(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read a Vauxhall / Opel car's modules over a generic OBD-II dongle (read-only).

    Built for the GM Global A era (Astra K, Insignia B, Mokka X, Zafira C --
    roughly 2010-2019). Global A does NOT speak plain UDS: fault codes are
    GMW3110 ReadDiagnosticInformation (0xA9), clear is 0x04 and identification
    is 0x1A. Every module is tried UDS-first then GMLAN, and the result records
    which dialect answered. Two address blocks are walked: powertrain
    0x7E0-0x7E3 (reply +8) and GM's body/chassis block 0x241-0x258 (reply
    +0x400). Faults come back GM-style as code + failure type ("P0716-71").
    Ignition must be ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.

    Two honest limits. Low Speed GMLAN (single-wire, 33.3k) carries much of the
    comfort/body traffic and NO standard dongle reaches it. And 2019+ Corsa,
    Mokka and Astra L are PSA/Stellantis cars, not GM -- use peugeot_scan_modules
    for those. The shipped address table is generic to GM Global A rather than
    confirmed per model, so run vauxhall_discover_modules on an unfamiliar car.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return vauxhall_scan.scan(instance, mode)


@mcp.tool()
def vauxhall_discover_modules(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Sweep a Vauxhall's GMLAN diagnostic addresses and report what answers (read-only).

    No public source maps any specific Vauxhall model's module list -- the
    shipped table is generic to GM Global A. This probes every address in the
    powertrain block (0x7E0-0x7E7, reply +8) and GM's body/chassis block
    (0x240-0x25F, reply +0x400) in both dialects and reports which answered,
    what proved it, and any identification they give. Use it to build a
    verified module map before trusting the scan's "not present" answers.
    Presence probes and identification reads only -- no fault services, no
    writes. Ignition must be ON. Takes noticeably longer than a scan (~40
    addresses).

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return vauxhall_scan.discover(instance, mode)


@mcp.tool()
def vauxhall_software_versions(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read the software / calibration part numbers flashed into each Vauxhall module (read-only).

    This is how you find out whether a manufacturer software update is relevant
    to a given car -- including a gearbox recalibration, which is the common
    reason to ask. Every module that answers is asked for its GMW3110
    identification block: End Model Part Number ($CB), Base Model Part Number
    ($CC), Boot Software Part Number ($C0), Software Module Identifiers 01-10
    ($C1-$CA) with their two-character alpha codes, the date it was last
    programmed ($99) and the tester/shop code that programmed it ($98). Modules
    that answer plain UDS instead are read over the ISO 14229-1 F18x block.
    Part numbers are decoded from GMW3110's 4-byte big-endian form into the
    decimal number a parts catalogue quotes.

    The result includes a plain-text ``worksheet`` listing every module's
    numbers, meant to be compared line by line against what the manufacturer's
    own programming system offers for the VIN.

    Reading is all this does. The harness does not and will not write a
    calibration: an ELM327 cannot hold a programming session, the calibration
    binaries are not publicly available, and programming is gated behind a
    proprietary SecurityAccess algorithm. See backend/profiles/vauxhall/
    software.py for the full reasoning.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return vauxhall_software.versions(instance, mode)


@mcp.tool()
def vauxhall_gearbox_analyze(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Analyse the Astra K / GM 6T35 gearbox over a Veepeak / ELM327 (read-only).

    Finds the TCM (0x7E2 on the verified 2017 Astra K, 0x7E1 fallback), reads
    its calibration part numbers, reads its fault memory with GM 6T35/6T40
    names (P2723 is the 1-2-3-4 clutch, not SAE 'Shift Solenoid E'), reads the
    19xx live block (fluid temp, selector, range switch, shaft speeds) and
    writes a worksheet that maps codes onto clutches. Same job ScanMyOpelCAN
    calls 'AT' and GDS2 calls Transmission Controls -- the half a V-Peak can
    do. Does not Fast Learn and does not flash.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a labelled 6T35 flare case.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return vauxhall_gearbox.analyze(instance, mode)


@mcp.tool()
def vauxhall_live_values(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read Vauxhall gearbox + engine live values over a generic OBD dongle (read-only).

    On the Astra K the TCM is at 0x7E2 and the live block is 0x19xx -- fluid
    temperature (22 1940, same PID Torque ships as Pontiac/GM/Opel/Vauxhall
    header 7E2), selector (194F), range switch (1951), shaft speeds (1941/1942).
    Cars that actually implement the OBDb 0x7E1 set (04FE / 5503 / 5504) still
    get current vs commanded gear from there. Fluid temperature also tells you
    when the box is near the Service Fast Learn window. Plus a curated Mode-01
    set on the engine. Ignition ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.

    GENUINE GAP: no public DID exposes the TCM's clutch adaptive cells, line
    pressure or clutch slip. Those are GDS2-only, and nothing is invented for
    them.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return vauxhall_pids.live(instance, mode)


@mcp.tool()
def vauxhall_tcm_capture(
    adapter_id: str | None = None, seconds: float = 120.0, mode: str = "real",
    fast: bool = False,
) -> dict:
    """Log the Astra K transmission controller through a drive (read-only).

    A shift flare is a shape over time, not a value: the gearbox failing to
    deliver a ratio while engine rpm climbs into the gap. A single reading
    cannot show one. This polls the TCM's answering parameter block at 0x7E2
    together with engine rpm, road speed and throttle, timestamps every sample,
    and returns the series.

    It also finishes a discovery job the standstill tests could not. No gear
    register has been identified on this calibration -- every candidate reads
    zero at a standstill, which is exactly what a gear register does when the
    car is not moving. Drive with this running and the gear register identifies
    itself: it is the parameter that steps 1-2-3-4-5-6 in time with road speed.
    Every parameter is therefore logged RAW, not decoded.

    Expect roughly one sample per second over a Bluetooth dongle. That resolves
    a flare lasting a second or more -- which is what a complained-about flare
    does -- and does NOT resolve the internals of a clean 200 ms shift. The
    result reports its own achieved sample rate so the rate is never mistaken
    for precision.

    fast=True trims each tick to the three reads a flare needs (input shaft
    speed, output shaft speed, engine rpm), roughly tripling the sample rate
    over Bluetooth, and adds derived per-sample values: gear ratio, turbine
    rpm (ISS/8, scaling proven on this car 2026-08-13) and converter slip.
    Fluid temp and throttle still land every fifth tick. Use fast=True when
    hunting the flare itself; use the default full set when the goal is
    discovering what an unknown parameter does.

    seconds: how long to log for. Drive the shift that flares during it.
    adapter_id: a configured ELM327 adapter instance (required for real mode).
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return vauxhall_tcm.capture(instance, seconds, mode, fast)


@mcp.tool()
def vauxhall_tcm_sweep(
    adapter_id: str | None = None, first: str = "1900", last: str = "19FF",
    mode: str = "real",
) -> dict:
    """Sweep a parameter-id range on the Astra K TCM at 0x7E2 (read-only).

    This is the tool that found the transmission block in the first place. The
    OBDb DIDs (04FE/5503/5504) return NRC 31 on this car at every address --
    the module speaks service 0x22 fine, those parameter ids just do not exist
    in this calibration. Sweeping found a live block at 0x1940-0x1951 instead.

    An id that answers NRC 31 is reported as absent-by-refusal, which is a more
    useful fact than silence: it proves the module is listening and the
    parameter is simply not implemented. Use it to widen the map beyond the
    range already verified.

    first / last: hex parameter ids as strings, e.g. "1900" and "19FF".
    adapter_id: a configured ELM327 adapter instance (required for real mode).
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    try:
        lo, hi = int(first, 16), int(last, 16)
    except ValueError:
        return {"ok": False, "message": f"first/last must be hex, got {first!r}/{last!r}."}
    return vauxhall_tcm.sweep(instance, lo, hi, mode)


@mcp.tool()
def vauxhall_clear_module(
    confirm: bool = False, adapter_id: str | None = None,
    address: str | None = None, mode: str = "real",
) -> dict:
    """Clear fault memory on Vauxhall modules over a dongle -- CHANGES THE CAR.

    confirm: must be True, and only after the user has explicitly agreed.
    adapter_id: a configured ELM327 adapter instance (required for real mode).
    address: hex request id like "0x7E1" (the gearbox) or "0x241" (the BCM);
        omit to clear every module that answers. Tries UDS 14 FF FF FF then
        GMLAN 04. Note the GMLAN clear takes NO group argument -- on a GMW3110
        module it wipes that module's entire fault memory, with no
        emissions-only option. Faults return if the underlying problem is still
        present, and clearing destroys the freeze-frame evidence a diagnosis may
        still need.
    """
    if confirm is not True:
        return {"error": "Refused: clearing changes the car. Pass confirm=true after the user agrees."}
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return vauxhall_scan.clear(instance, mode, address)


@mcp.tool()
def vauxhall_adaptive_learn_preflight(
    adapter_id: str | None = None, mode: str = "real",
    scenario: str = "complete",
) -> dict:
    """Check whether the Astra K TCM would accept a Transmission Adaptive
    Values Learn right now. Read-only -- service 0x22 only, nothing is sent.

    Reads the live fluid temperature, the module's own 17-state readiness
    parameter (22 28 23) and every precondition GDS2's own condition set
    checks, then says what is still in the way. This is the step to run before
    even discussing the learn: it costs nothing and it answers "is the car warm
    enough and in the right state" with numbers.

    adapter_id: a configured ELM327 adapter instance (required for real mode).
    mode="sim" reads a simulated TCM; scenario picks which one (complete, cold,
        not-in-park, engine-off, not-ready, ...).
    """
    if mode == "sim":
        return vauxhall_procedures.preflight(
            None, "vauxhall-t76-adaptive-values-learn", mode, scenario
        )
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return vauxhall_procedures.preflight(
        instance, "vauxhall-t76-adaptive-values-learn", mode, scenario
    )


@mcp.tool()
def vauxhall_adaptive_learn(
    confirm: bool = False, temperature_ack: float | None = None,
    adapter_id: str | None = None, mode: str = "real",
    scenario: str = "complete",
) -> dict:
    """Run the Astra K T76 Transmission Adaptive Values Learn -- CHANGES THE CAR.

    THIS HAS NEVER BEEN RUN ON A CAR. The GMW3110 DeviceControl frames were
    recovered by static analysis of the GDS2 dealer database, independently
    re-derived from raw file offsets and reproduced across five deliverables.
    That is a confirmed static decode and it is not the same as evidence from a
    car. Whether the module demands SecurityAccess (NRC 0x33), and whether a
    Bluetooth ELM327 can hold TesterPresent for the length of the routine, are
    both still open. Say so to the user before running it.

    The failure that matters is an aborted learn, not a refused one: the
    routine clears the gearbox's adaptive cells when it starts and only writes
    new ones when it finishes, so an interrupted run leaves a worse gearbox
    than it found. The harness releases the device control on every failure
    path, but an interrupted learn still has to be run again to completion.

    confirm: must be True, and only after the user has explicitly agreed.
    temperature_ack: the live transmission fluid temperature in °C, as reported
        by vauxhall_adaptive_learn_preflight. Required. It exists because the
        dealer data's threshold has two defensible readings (84 °C converted,
        44 °C raw); the harness gates on the conservative one and will not pick
        it behind the user's back.
    adapter_id: a configured ELM327 adapter instance (required for real mode).
    mode="sim" runs the whole exchange against a simulated TCM with no car;
        scenario picks the outcome to rehearse (complete, nrc33, module-abort,
        keepalive-loss, exception, release-refused, ...).

    The car must be on level ground with its wheels chocked, the park brake on
    and a foot on the service brake: it is driven against its own brakes with
    the engine running.
    """
    if confirm is not True:
        return {"error": (
            "Refused: this drives the gearbox's clutches and has never been "
            "run on a car. Pass confirm=true only after the user agrees, and "
            "after telling them it is unverified."
        )}
    if mode == "sim":
        return vauxhall_procedures.execute(
            None, "vauxhall-t76-adaptive-values-learn", True, mode,
            temperature_ack, scenario,
        )
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return vauxhall_procedures.execute(
        instance, "vauxhall-t76-adaptive-values-learn", True, mode,
        temperature_ack, scenario,
    )


# --------------------------------------------------------------------------- #
# Fiat / Alfa Romeo (FCA / Stellantis) module read over a generic OBD dongle
# --------------------------------------------------------------------------- #
@mcp.tool()
def fiat_scan_modules(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read a Fiat / Alfa Romeo (FCA) car's modules over a generic OBD-II dongle (read-only).

    Two tiers. On a UDS-era FCA (Alfa Giulia/Stelvio, Fiat 500X/Tipo/124, Jeep
    Renegade, ~2014+) it walks the modules over 29-bit CAN (18DAxxF1) -- engine
    0x10, transmission 0x18, ABS/ESC 0x28 (which hosts the EPB), steering column
    0x30 and body computer 0x40 -- reading each one's fault codes + identification.
    VIN first over Mode 09. On a CAN-era Fiat (~2005-2016) it falls back to the
    11-bit engine and says the body modules need a MultiECUScan B-CAN pin adapter.
    Ignition must be ON.

    SGW honesty: the 2018+ FCA Security Gateway blocks CLEARING and actuations,
    NOT reads -- so this read-only scan works fully even on an SGW car. TPMS
    (0xC7) is NEVER addressed: polling it can engage the immobilizer and stall the
    car.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return fiat_scan.scan(instance, mode)


@mcp.tool()
def fiat_live_values(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read Fiat / Alfa-specific live "extras" over a generic OBD dongle.

    One read-only pass. On a UDS-era Alfa Giulia/Stelvio (2.2 diesel) it reads the
    danardi-captured DPF soot mass + regeneration status, oil temp/level, coolant,
    boost, current gear and battery on the engine (0x10) and the gearbox (TCT/ZF)
    oil temperature on the TCM (0x18), over 29-bit CAN, one DID at a time. These
    are a single on-car capture, so verify the scaling against the car. A CAN-era
    Fiat falls back to a curated generic Mode-01 set. Ignition must be ON.

    TPMS (0xC7) is NEVER polled -- it can engage the immobilizer.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return fiat_pids.live(instance, mode)


@mcp.tool()
def fiat_clear_module(
    confirm: bool = False, adapter_id: str | None = None,
    address: str | None = None, mode: str = "real",
) -> dict:
    """Clear fault memory on Fiat / Alfa (FCA) modules over a dongle -- CHANGES THE CAR.

    confirm: must be True, and only after the user has explicitly agreed.
    adapter_id: a configured ELM327 adapter instance (required for real mode).
    address: one-byte FCA module address like "0x10" (engine) or "0x28" (ABS);
        omit to clear every FCA module that answers. TPMS "0xC7" is refused
        outright (immobilizer trap). On a 2018+ Security-Gateway car the clear is
        blocked over a bare dongle -- reads still work, but clearing needs an SGW
        bypass plug or an AutoAuth-registered tool. Faults return if the
        underlying problem is still present.
    """
    if confirm is not True:
        return {"error": "Refused: clearing changes the car. Pass confirm=true after the user agrees."}
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return fiat_scan.clear(instance, mode, address)


# --------------------------------------------------------------------------- #
# Jaguar / Land Rover module read over a generic OBD dongle
# --------------------------------------------------------------------------- #
def _jlr_capture(kind: str, result: dict, instance) -> dict:
    """Persist a real JLR run to captures/jlr/ so a farm-day session driven
    through Claude accumulates the same dataset the web UI does. Sim runs and
    save failures leave the result untouched (backend/core/captures.py)."""
    try:
        saved = captures.save("jlr", kind, result, instance)
    except OSError:
        return result
    if saved:
        result["savedTo"] = saved["json"]
        if saved.get("csv"):
            result["savedCsv"] = saved["csv"]
    return result


@mcp.tool()
def jlr_scan_modules(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read a Jaguar / Land Rover car's driveline modules over a generic OBD-II dongle (read-only).

    Honest per-era scope. On a 2006-2016 CAN-era JLR (XF X250, XK, XJ X351,
    F-Type, Disco 3/4, RR Sport L320, Freelander 2, Ford-era L322) it reads the
    publicly-verified driveline modules on 11-bit CAN -- engine 0x7E0, ZF gearbox
    0x7E1 and (where fitted) the rear e-diff 0x795 -- each one's fault codes +
    identification, laddering UDS then KWP2000-over-CAN for the older cars. VIN
    first over Mode 09. Ignition must be ON.

    The two walls, reported honestly rather than as faults: body modules sit on
    an MS-CAN not wired to the OBD pins (physically unreachable bare), and MY17+
    new-architecture cars (Disco 5, Velar, 2018+ Jaguars, Defender L663, I-Pace)
    plus the gateway-filtered Evoque L538 / Disco Sport L550 give generic OBD
    only -- the scan detects that pattern and says so. Dash-procedure service
    resets (jlr in list_service_resets) still work on every era.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return _jlr_capture("scan", jlr_scan.scan(instance, mode), instance)


@mcp.tool()
def jlr_live_values(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Read Jaguar / Land Rover-specific live "extras" over a generic OBD dongle.

    One read-only pass over the Old Jaguar Group on-car captures: engine oil
    temperature, measured oil level (mm) and calculated oil volume (the
    dipstick-less cars carry these), boost, charge-air temp, ZF gearbox oil
    temperature (0x7E1) and rear e-diff oil temperature (0x795), one DID at a
    time on 11-bit CAN. Single-source capture (X351/XK petrol) -- verify the
    scaling against the car. A DoIP-era or gateway-filtered car falls back to a
    curated generic Mode-01 set. Ignition must be ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return jlr_pids.live(instance, mode)


@mcp.tool()
def jlr_clear_module(
    confirm: bool = False, adapter_id: str | None = None,
    address: str | None = None, mode: str = "real",
) -> dict:
    """Clear fault memory on Jaguar / Land Rover modules over a dongle -- CHANGES THE CAR.

    confirm: must be True, and only after the user has explicitly agreed.
    adapter_id: a configured ELM327 adapter instance (required for real mode).
    address: an 11-bit module id like "0x7E0" (engine), "0x7E1" (ZF gearbox) or
        "0x795" (rear e-diff); omit to clear every JLR module that answers.
        Ladder matches the read side: UDS 14 FF FF FF, then the KWP clear an
        older Ford-era module wants. Faults return if the underlying problem is
        still present.
    """
    if confirm is not True:
        return {"error": "Refused: clearing changes the car. Pass confirm=true after the user agrees."}
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return jlr_scan.clear(instance, mode, address)


@mcp.tool()
def jlr_discover_modules(adapter_id: str | None = None, mode: str = "real") -> dict:
    """Sweep a JLR car's PT-CAN request-id block and report what answers (read-only).

    Walks 0x700-0x7FF (response = request + 8, the verified JLR convention) one id
    at a time with a quick presence probe (3E 00, then 22 F190); whatever answers
    gets its identification read. Purpose: build the module map nobody published --
    find the SCR/reagent controller if a diesel has a separate one, and verify the
    PCM/TCM addresses the shipped table claims. No fault services, no writes:
    presence probes and identification reads only. Eight ids at the top of the
    block are skipped (a +8 response would leave the 11-bit id space). Expect
    roughly 2-5 minutes over a Bluetooth dongle. Ignition must be ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return _jlr_capture("discover", jlr_discover.discover(instance, mode), instance)


@mcp.tool()
def jlr_engine_sweep(
    adapter_id: str | None = None, mode: str = "real", first: str = "0300",
    last: str = "05FF", target: str = "7E0", extended: bool = False,
) -> dict:
    """Walk a 0x22 DID range on one JLR module and keep every answer raw (read-only).

    This is the DID mapping tool for values with no public JLR DID (DPF soot mass,
    regen status, DEF level/quality, NOx ppm, EGTs). One DID at a time; every
    answer is kept as raw hex -- NRC 0x31 refusals are counted (module listening,
    id not implemented), other NRCs are listed by name, the rest are silent. A
    signal is identified by sweeping once at idle and once warm/loaded and seeing
    which payloads MOVE -- nothing is decoded or invented here.

    first/last: hex DID bounds like "0300" and "05FF"; max 4096 ids per call.
        Research seed ranges on the PCM: 0300-05FF, 2100-21FF, 2200-22FF,
        F400-F4FF.
    target: the module's hex request id, default the PCM ("7E0").
    extended: open the UDS extended session (10 03) first -- best-effort.
    The target must answer a presence probe or the sweep refuses (no blind
    spraying of a dead bus). Read-only.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return _jlr_capture(
        "sweep", jlr_sweep.sweep(instance, mode, first, last, target, extended), instance
    )


@mcp.tool()
def jlr_adblue_health(adapter_id: str | None = None, mode: str = "real") -> dict:
    """One read-only AdBlue/SCR + DPF health pass on a Jaguar X260 3.0d (read-only).

    Built for the 2016 XF (X260) 3.0d V6 diesel, EU6 with SCR. One pass: the
    generic Mode-01 baseline (rpm, coolant, module voltage) FIRST, then the
    known-table modules' fault codes through the existing UDS/KWP scan ladder,
    then the known engine DIDs read RAW with their provenance marked (Old Jaguar
    Group petrol capture -- unverified on the diesel). The SCR/DPF values are
    flagged capture-pending: no public JLR DID exists for soot mass / DEF level /
    NOx ppm, so jlr_engine_sweep must map them on the real car first and nothing
    here invents a decoding. The assessment block is sourced text -- the ranked
    AdBlue fault space (crystallised injector, NOx sensor, tank level sensor,
    contaminated fluid) and the regen thresholds (auto ~26 g soot, forced
    permissible below ~46 g) -- conditional on the codes actually read. Ignition
    must be ON.

    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return _jlr_capture("adblue-health", jlr_adblue.health(instance, mode), instance)


@mcp.tool()
def jlr_adblue_drive_log(
    adapter_id: str | None = None,
    mode: str = "real",
    seconds: float = 300,
    dids: list[str] | None = None,
) -> dict:
    """Log a JLR diesel drive pairing rpm with the AdBlue/DPF candidate DIDs (read-only).

    Every tick reads engine rpm (Mode 01) plus each DID's RAW payload with a
    timestamp; every fifth tick adds coolant and module voltage; the result
    reports the sample rate it actually achieved. Run it through warm-up or a
    regen (a regen is ~20 min at 500 °C+ -- pass seconds=1500+ to span one).
    Payloads stay raw: a signal is identified by how it moves against rpm, then
    decoded against the car. Default DIDs are the known Old Jaguar Group engine
    set; once jlr_engine_sweep has captured aftertreatment candidates on the
    real car, pass them as dids (hex strings like ["042C", "F40C"], max 24) --
    they replace the default set for that run.

    A watch preset may be passed instead of a hex list: dids=["urea"] logs only
    the SDD-named urea/SCR cluster 05FA-05FD, which is the short focused list
    that samples fast enough to calibrate 05FC (total mass fluid injected -- a
    monotonic total). Drive the car warm for it: reagent dosing does not start
    until the SCR catalyst is up to temperature.

    seconds: how long to log for. Drive normally while it runs.
    dids: hex DID strings, or a single watch-preset name, to log instead of
        the default engine set.
    adapter_id: a configured ELM327 adapter instance (from list_adapters);
        required for real mode. mode="sim" returns a clearly-labelled fake.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return _jlr_capture(
        "drive-log", jlr_adblue.drive_log(instance, mode, seconds, dids), instance
    )


@mcp.tool()
def jlr_dealer_data() -> dict:
    """What JLR's own SDD 169 dealer database says about the 306DT family (no car).

    A pure reference view, decrypted from SDD 169 (3DES-ECB; method and key
    recorded in backend/profiles/jlr/data/sdd_dids.json, reproducible with
    backend/tools/jlr_sdd_decrypt.py). Three things:

      * the 57-module L494 MY16 CAN address map -- this is what lets
        jlr_discover_modules report ABS at 0x7E6 and DSM at 0x744 instead of a
        bare id, and it is what refuted the "0x7E6 is the SCR controller"
        hypothesis: there is no separate SCR ECU, SCR is PCM-managed at 0x7E0;
      * 37 diesel-PCM data identifiers with SDD's own names, byte sizes,
        scaling and units, each graded verified or candidate;
      * 24 PCM/SCR routine identifiers, including the SCR prime (0x4076) and
        the three reductant dosing tests (0x4094-0x4096). REFERENCE ONLY --
        the harness sends none of them and has no way to.

    Rows marked [candidate] have an authoritative manufacturer name against a
    value this car has not proved; SDD publishes no scaling for the urea
    cluster 05FA-05FD, so those stay raw. SDD names a curated 37 of the ~191
    DIDs the PCM answers, so absence here does NOT mean a DID is dead.

    Touches no car and needs no adapter.
    """
    return jlr_dealer_data_mod.reference(None, "real")


# --------------------------------------------------------------------------- #
# Raw diagnostic channel -- the escape hatch beyond the curated brand packs
# --------------------------------------------------------------------------- #
@mcp.tool()
def read_dids(
    target: str,
    dids: list[str],
    addressing: str = "11bit",
    adapter_id: str | None = None,
    resp_id: str | None = None,
    extended_session: bool = False,
    mode: str = "real",
) -> dict:
    """Read arbitrary mode-22 (ReadDataByIdentifier) DIDs from any module -- the
    read-only escape hatch for values no curated brand pack carries (soot mass,
    odd body DIDs, anything researched on the fly).

    target: the module's hex id. addressing="11bit" (default): a standard CAN
        request id like "7E0" (engine), "7E1" (gearbox), "720"...; the answer is
        expected on target+8 unless resp_id overrides it (VAG body modules
        answer at +0x6A, e.g. target "714" cluster -> resp_id "77E").
        addressing="29bit" (UDS-era Fiat/Alfa/Jeep): the ONE-BYTE ECU address
        like "10" (engine), "18" (TCM), "28" (ABS); pairing is fixed
        (18DAxxF1/18DAF1xx). 0xC7 (FCA TPMS) is refused -- immobilizer trap.
    dids: hex DID strings, e.g. ["1310", "F190"]; max 24 per call.
    extended_session: open UDS 10 03 first for modules that refuse reads in the
        default session (best-effort, never fatal).
    adapter_id: a configured ELM327 adapter (from list_adapters); required for
        real mode. mode="sim" returns a clearly-labelled fake.
    Returns per-DID raw reply hex + payload, printable ASCII and big-endian int
    where they apply, and named NRCs for negatives. Decode/scale the payload
    yourself against researched formulas -- the raw bytes are the truth here.
    Read-only: only services 0x22 (and 10 03 when asked) are ever sent.
    """
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return obd_raw.read_dids(
        instance, mode, target, dids, addressing, resp_id, extended_session
    )


@mcp.tool()
def elm_commands(
    commands: list[str],
    confirm: bool = False,
    adapter_id: str | None = None,
    timeout: float = 3.0,
    mode: str = "real",
) -> dict:
    """Run a raw ELM327 command sequence -- AT setup + arbitrary hex requests --
    for anything read_dids can't express. CAN WRITE TO THE CAR, so confirm-gated.

    confirm: must be True, and only after the user has explicitly agreed.
    commands: executed in order in one session, e.g. ["ATSH7E0", "ATCRA7E8",
        "ATFCSD300000", "ATFCSM1", "221310"]; max 32. Monitor (ATMA -- use the
        brand snapshot feature), reset (ATZ/ATWS), defaults (ATD), low-power and
        baud-rate commands are refused: they'd strand the dongle session.
    timeout: per-command seconds (0.5-10).
    adapter_id: a configured ELM327 adapter (required for real mode);
        mode="sim" returns a clearly-labelled fake.
    The ELM is ALWAYS restored to generic-OBD defaults afterwards (headers,
    filters, flow control, timing, and the protocol if the sequence switched
    it), so a stray state can never leak into the next scan.
    """
    if confirm is not True:
        return {"error": "Refused: raw ELM commands can write to the car. Pass confirm=true after the user agrees."}
    instance, error = _elm_adapter_or_error(mode, adapter_id)
    if error is not None:
        return error
    return obd_raw.elm_commands(instance, mode, commands, timeout)


# --------------------------------------------------------------------------- #
# Service resets (oil / brake / inspection counters) per manufacturer
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_service_resets(brand: str) -> dict:
    """List the service resets available for a brand (read-only catalog).

    brand: a manufacturer profile id (see list_profiles). Returns each reset's
    name, description, method ("uds" = sendable over the dongle, "procedure" =
    manual dash steps), a confidence grade, the source it came from, and either
    the UDS steps or the dash procedure. No car or adapter is needed.
    """
    module = brands.brand_module(brand, "resets")
    if module is None:
        return {"error": f"No service resets for '{brand}'."}
    return module.list_resets()


@mcp.tool()
def perform_service_reset(
    brand: str, reset_id: str, confirm: bool = False,
    adapter_id: str | None = None, mode: str = "real",
) -> dict:
    """Perform one service reset -- CHANGES THE CAR for "uds" resets.

    brand: a manufacturer profile id. reset_id: from list_service_resets.
    confirm: must be True, and only after the user has explicitly agreed.
    adapter_id: a configured ELM327 adapter (required for a real "uds" reset).
    mode="sim" shows the steps without touching a car. "procedure" resets never
    touch the car -- they return the manual steps to follow.
    """
    module = brands.brand_module(brand, "resets")
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
