"""Vauxhall / Opel (GM Global A) service functions.

Every entry here is a ``procedure``: the harness walks the operator through the
official steps rather than sending bytes. That is not laziness, and on the
gearbox routine it is not reversible if got wrong -- the reasons are recorded
in docs/private/vauxhall-astra-k-research.md §5 and summarised here because
anyone reading this file is the person who would otherwise try to implement it:

  1. **Fast Learn is DeviceControl, not RoutineControl.** GM Global A speaks
     GMW3110. There is no UDS 0x31 routine to call. The equivalent service is
     0xAE DeviceControl, which addresses a per-module CPID (Control Packet
     Identifier) naming the function being driven.
  2. **The CPID is not public.** No open source documents the Astra K TCM's
     Fast Learn CPID or its control bytes. Nothing in this repository should
     guess at a byte sequence whose job is to command clutches.
  3. **It is gated behind SecurityAccess.** 0xAE on a powertrain module needs
     0x27 sub-functions 03/04 (DevCtrl requestSeed / sendKey). The seed-to-key
     algorithm is per-module and GM-proprietary. Without it every request comes
     back NRC 0x33.
  4. **An ELM327 cannot hold the channel anyway.** The routine runs for
     minutes, keeps the box in neutral, streams operator prompts back to the
     tool, and needs 0x28 DisableNormalCommunication plus periodic 0x3E
     TesterPresent throughout. An ELM327 is a request/response text bridge with
     no scheduler.

So the honest surface is: the harness MEASURES (pids.py reads transmission
fluid temperature, current gear and commanded gear over any dongle) and
DOCUMENTS (this file), and the write is done on GDS2/MDI2 or an equivalent
aftermarket tool with GM guided functions. If a genuine GDS2 or J2534 capture
of the DeviceControl sequence is ever obtained, it does not belong here as a
``uds`` reset either -- uds_reset.execute() speaks 0x31/0x2E, not 0xAE, and
would need a GMLAN executor first.

Mirror of android/.../obd/BrandResetData.kt (Vauxhall section) -- keep in sync.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.obd import uds_reset

RESETS: List[dict] = [
    {
        "id": "vauxhall-tcm-calibration-check",
        "name": "TCM software / calibration check",
        "description": (
            "Before touching adapts, find out what software the gearbox is "
            "running. GM issues transmission calibration updates that cure shift "
            "quality complaints outright, and a flash invalidates the adapts "
            "afterwards -- so this is the step that decides whether the adapt "
            "relearn is worth doing yet."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "GM GDS2 / TIS2Web SPS calibration lookup by VIN",
        "models": "GM Global A Vauxhall/Opel with an automatic (Astra K, Insignia B, Mokka X, Zafira C)",
        "cautions": [
            "Reading the calibration id is harmless. FLASHING it is not: a "
            "TCM reprogram over SPS needs a stable supply (a proper charger, "
            "not a jump pack) and an interrupted flash can leave the module "
            "unusable.",
            "If the TCM is reflashed, the clutch adapts it had learned are no "
            "longer valid. The adaptive values learn below stops being optional "
            "and becomes a required follow-on step.",
        ],
        "procedure": [
            "Read what is fitted now: run 'Software versions' in the harness "
            "(API /api/vauxhall/software, MCP vauxhall_software_versions). It "
            "asks the TCM at 0x7E1 for its GMW3110 identification block -- End "
            "Model Part Number ($CB), Software Module Identifiers 01-10 "
            "($C1-$CA) with their alpha codes, and the date it was last "
            "programmed ($99) -- and prints a worksheet to compare. On a module "
            "that answers the UDS dialect the same read comes back as DID F188. "
            "Note the numbers with the VIN.",
            "Have the dealer or an equipped independent compare that calibration "
            "id against the latest available for the VIN in TIS2Web / SPS. Only "
            "the VIN lookup is authoritative -- a calibration that is current for "
            "one build of the same model may not be for this one.",
            "If a newer transmission calibration exists, that is the cheapest "
            "thing to try first for a shift-quality complaint.",
            "If it is flashed, go straight to the adaptive values learn below, "
            "then the relearn drive. Do not judge the shift quality on the old "
            "adapts after a flash.",
        ],
    },
    {
        "id": "vauxhall-trans-adaptive-values-learn",
        "name": "Transmission Adaptive Values Learn (Service Fast Learn Adapts)",
        "description": (
            "The routine that makes the TCM relearn each clutch's fill "
            "characteristics and write them into the adaptive cells it uses for "
            "shift control. This is the fix for a shift flare that survives a "
            "fluid service, and the mandatory follow-on after a TCM reflash, a "
            "valve body or a gearbox rebuild. It is a scan-tool routine -- the "
            "harness cannot run it over a dongle (see the module docstring) -- so "
            "these are the steps to hand the person with the tool."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": (
            "GM bulletin 14876 (GDS2 menu path + ignition-off shutdown); GM "
            "'Service Fast Learn Adapts' doc id 1688788; Sonnax / Transmission "
            "Digest fast-learn guidance"
        ),
        "models": "GM 6T30 / 6T35 / 6T40 / 6T45 family -- includes the Astra K 1.4T automatic (6T35)",
        "cautions": [
            "The car is driven against its own brakes with the wheels blocked "
            "and the engine running. Chock the wheels, park brake on, foot on "
            "the service brake, and keep everyone clear of the vehicle.",
            "The gearbox stays in neutral until the controller powers down. If "
            "it is still in neutral a couple of minutes after the routine, "
            "disconnect the battery for 5-10 minutes.",
            "Published fluid-temperature windows disagree (roughly 64-76 C for "
            "Gen 2 units, 75-85 C for the wider 6T40 family) because the real "
            "limit lives in the calibration in this TCM. Take the window from "
            "the tool's own on-screen prompt for the VIN. The harness live "
            "values read fluid temperature so you can time the attempt.",
            "Adapts learned here are not final. TCC (torque converter clutch) "
            "adapts in particular can take a few hundred miles to settle -- do "
            "not re-judge the car on the first drive out of the workshop.",
        ],
        "procedure": [
            "TOOL. GDS2 (Vauxhall/Opel) with an MDI or MDI2: Module Diagnostics "
            "→ Transmission Control Module → Configuration/Reset Functions → "
            "Transmission Adaptive Values Learn.",
            "Tech2 / Tech2Win: Diagnostics → Transmission → F2 Special Functions "
            "→ F0 TCM → F5 Fast Learn Adapt.",
            "Capable aftermarket alternatives: Autel MaxiSys, or Launch X431 "
            "tiers that carry GM guided functions. A basic code reader cannot do "
            "this -- if the tool has no GM 'guided'/'special functions' menu for "
            "the TCM, it is the wrong tool.",
            "PREPARE. Fluid level and condition correct first. A fast learn on "
            "low or burnt fluid teaches the TCM the wrong numbers.",
            "Block the drive wheels, park brake on, foot on the service brake, "
            "zero throttle, no external rpm control.",
            "Cycle the selector P → R three times before starting, to purge air "
            "from the reverse clutches.",
            "Bring transmission fluid temperature into the window the tool asks "
            "for. The routine will refuse to start outside it.",
            "RUN. Start the routine and follow the tool's on-screen operator "
            "instructions exactly -- the display drives the sequence.",
            "If fluid temperature climbs past the limit mid-routine it aborts. "
            "Key off 30 seconds, let it cool, retry.",
            "FINISH. When the routine completes, switch the engine OFF and let "
            "the TCM power down fully (you will lose communication with the "
            "tool) before restarting. Skipping the power-down is the most common "
            "reason a fast learn does not take.",
            "RELEARN DRIVE. About 45 minutes on smooth level roads, light to "
            "moderate throttle, letting it shift up and down through every "
            "ratio. Do not labour it and do not thrash it.",
            "ASSESS. Flare gone after the relearn drive → monitor for 500 miles. "
            "Unchanged → the problem is mechanical/hydraulic, not adaptive, and "
            "the next step is pressure testing and the valve body.",
        ],
    },
    {
        "id": "vauxhall-service-oil-life-reset",
        "name": "Service / remaining oil life reset (Astra K era)",
        "description": (
            "Resets the service reminder after an oil service. On the Astra K "
            "generation this is a dashboard menu action -- there is no dongle "
            "command for it, and none is invented here."
        ),
        "method": "procedure",
        "confidence": "community",
        "source": "Astra K owner/technician reports; Vauxhall Astra K owner's manual (Info menu)",
        "models": "Astra K (2016-2021) and near siblings with the same cluster menus",
        "cautions": [
            "Reset AFTER the oil and filter change, not before -- the counter is "
            "what tells the next person when the oil was actually done.",
            "The exact menu wording depends on which cluster/display is fitted "
            "(mid-level vs navigation). If the Info menu route is missing, use "
            "the trip-button method below.",
        ],
        "procedure": [
            "Ignition ON, engine off.",
            "Using the steering wheel buttons, open the cluster INFO menu.",
            "Scroll to 'Remaining oil life'.",
            "Press and hold OK (or open the sub-menu with ► and choose Reset) "
            "until the value returns to 100%.",
            "Ignition off, then back on, and check the reminder has gone.",
            "If the menu is not available on this cluster: ignition OFF, hold "
            "the trip/SET-CLR button, switch the ignition ON while still "
            "holding, and keep holding until the service indication flashes and "
            "clears.",
        ],
    },
]

_BY_ID = {r["id"]: r for r in RESETS}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_resets() -> dict:
    return {"brand": "vauxhall", "resets": [uds_reset.public_view(r) for r in RESETS]}


def perform(
    adapter: Optional[dict], mode: str, reset_id: str, confirm: bool = False
) -> dict:
    """Return the steps for one Vauxhall service function.

    Every shipped entry is ``method: "procedure"``, so nothing here touches the
    car -- the response is the walkthrough. There is deliberately no ``uds``
    execution path: GM Global A's write-side service is GMW3110 DeviceControl
    (0xAE), which uds_reset.execute() does not speak. Adding one means writing a
    GMLAN executor and obtaining the CPID plus the security-access algorithm
    first; until then, refusing is the correct behaviour.
    """
    reset = _BY_ID.get(reset_id)
    if reset is None:
        return {"ok": False, "message": f"Unknown Vauxhall service function '{reset_id}'.",
                "timestamp": _now()}
    return {
        "ok": False, "runnable": False, "reset": uds_reset.public_view(reset),
        "message": (
            f"'{reset['name']}' is a documented procedure, not a dongle command -- "
            "follow the steps. GM Global A write functions run over GMW3110 "
            "DeviceControl behind GM's security access, which no ELM327 can do; "
            "the harness will not pretend otherwise."
        ),
        "timestamp": _now(),
    }
