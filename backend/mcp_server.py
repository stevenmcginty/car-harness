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
from backend.core import brands, interfaces, registry  # noqa: E402
from backend.profiles.bmw import ediabas as bmw_ediabas  # noqa: E402
from backend.profiles.bmw import battery as bmw_battery  # noqa: E402
from backend.profiles.bmw import cbs as bmw_cbs  # noqa: E402
from backend.profiles.bmw import scan as bmw_scan  # noqa: E402
from backend.profiles.fiat import pids as fiat_pids  # noqa: E402
from backend.profiles.fiat import scan as fiat_scan  # noqa: E402
from backend.profiles.jlr import pids as jlr_pids  # noqa: E402
from backend.profiles.jlr import scan as jlr_scan  # noqa: E402
from backend.profiles.mazda import pids as mazda_pids  # noqa: E402
from backend.profiles.mazda import scan as mazda_scan  # noqa: E402
from backend.profiles.mazda import snapshot as mazda_snapshot_mod  # noqa: E402
from backend.profiles.mercedes import pids as mercedes_pids  # noqa: E402
from backend.profiles.mercedes import scan as mercedes_scan  # noqa: E402
from backend.profiles.peugeot import pids as peugeot_pids  # noqa: E402
from backend.profiles.peugeot import scan as peugeot_scan  # noqa: E402
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
    return jlr_scan.scan(instance, mode)


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
