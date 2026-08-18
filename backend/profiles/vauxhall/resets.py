"""Vauxhall / Opel (GM Global A) service functions.

Every entry here is a ``procedure``: the harness walks the operator through the
official steps rather than sending bytes. That is not laziness, and on the
gearbox routine it is not reversible if got wrong -- the reasons are recorded
in docs/private/vauxhall-astra-k-research.md §5 and summarised here because
anyone reading this file is the person who would otherwise try to implement it:

  1. **Fast Learn is DeviceControl, not RoutineControl.** GM Global A speaks
     GMW3110. There is no UDS 0x31 routine to call. The equivalent service is
     0xAE DeviceControl, which addresses a per-module CPID (Control Packet
     Identifier) naming the function being driven. This one is still true and
     is the reason a ``uds`` reset entry would be the wrong shape here:
     uds_reset.execute() speaks 0x31 and 0x2E, not 0xAE.

Reasons 2, 3 and 4 have been overtaken by the GDS2 data extraction, and are
kept below with what actually became of them, because a stale objection is
worse than no objection -- somebody will act on it.

  2. **"The CPID is not public."** No longer the situation. The CPID is 0x30
     and the control packet is recovered, traceable field by field to offsets
     in the dealer deliverable, and reproduced across five separately built
     copies of it. See procedures.py and data/gds2_t76_tcm.json. What has NOT
     changed is the principle underneath the objection: nothing here guesses at
     a byte sequence whose job is to command clutches. The bytes are now read
     rather than guessed, and they are still not sent.
  3. **"It is gated behind SecurityAccess."** Stated too strongly. GDS2 sends
     no 0x27 at all for this test: the security step exists in its state
     machine but sits behind a flag that is 0 in every copy. So the claim that
     "without it every request comes back NRC 0x33" is not supported. The
     honest version is that the dealer tool does not offer a key, which is not
     the same as the module not demanding one -- a live attempt may still meet
     NRC 0x33, and there is no working seed/key algorithm in the deliverable to
     answer with if it does. Unresolved, and only a car resolves it.
  4. **"An ELM327 cannot hold the channel anyway."** Half wrong. The recovered
     sequence does not use 0x28 DisableNormalCommunication; it is 0x10 in,
     0x3E kept alive, 0x20 out, and both the control and its release are seven
     bytes, which fits an ISO-TP single frame. None of that is beyond an
     ELM327. What survives is narrower and real: nobody has measured whether a
     Bluetooth ELM327's round-trip latency can keep 0x3E inside the module's
     timeout for a routine that runs minutes.

So the honest surface today is: the harness MEASURES (pids.py and tcm.py read
fluid temperature, the shaft speeds, commanded gear and the fast-learn status
parameter over any dongle), DESCRIBES (procedures.py holds the full exchange
with its provenance), DOCUMENTS (this file) -- and now, separately and
deliberately, RUNS it. learn.py and gmlan_control.py implement the GMW3110
DeviceControl transport that objection 4 above said did not exist, with the
control release owned by a ``finally`` on every path out, live preconditions
read before anything is sent, and a simulated module to rehearse every failure
path against with no car attached.

That is a different capability from this file, and the split is on purpose.
The entry below stays a *procedure*: it is the walkthrough for the person
standing in front of the car with GDS2, an Autel or a Launch, and that person
still exists and still needs it. Running it from the harness is
``/api/vauxhall/procedure`` and ``procedures.execute``, which is confirm-gated
twice over and carries, on every surface, the fact that none of it has ever
been seen on a wire. Do not merge the two: a documented procedure that always
prints the same steps and an unverified routine that drives clutches deserve
different words and different gates.

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
            "Read what is fitted now: run 'Software versions' or 'Gearbox "
            "analyse' in the harness (API /api/vauxhall/software or /gearbox, "
            "MCP vauxhall_software_versions / vauxhall_gearbox_analyze). The "
            "Astra K TCM is at 0x7E2 (0x7E1 is silent on the verified car). "
            "It asks for the GMW3110 identification block -- End "
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
            "valve body or a gearbox rebuild. These are the steps to hand the "
            "person with the scan tool. The harness can now also run the "
            "exchange itself over a dongle -- see procedures.py and "
            "/api/vauxhall/procedure -- but that path has never been used on a "
            "car, so this walkthrough remains the one to follow when a proper "
            "tool is available."
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
            "The GDS2 data for this exact module says something different "
            "again, and it is worth knowing which number is which. The "
            "condition GDS2 checks before it will start is simply fluid above "
            "84 C. The module's own calibration classifies below 86 C as "
            "'below operating temperature' and above 126 C as 'above', which "
            "makes its working window 86-126 C -- wider at the top than any "
            "published figure. Neither of those is a workshop recommendation; "
            "they are what the tool and the module test for. If they disagree "
            "with the prompt in front of you, believe the prompt.",
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
            "CHECK BEFORE YOU START, over the dongle, costing nothing: the "
            "module publishes its own readiness on parameter 2823 (request "
            "22 28 23) and the harness now decodes it through GDS2's own "
            "17-state table. It will say 'Fast Learn Disabled' when it is "
            "willing, and otherwise name what it is waiting for -- 'Brake Must "
            "be Applied', 'Vehicle Must be in Park', 'Transmission Temperature "
            "is Below Operating Temperature'. Reading it writes nothing. If it "
            "reports a blocking condition, fix that before the tool is even "
            "connected.",
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
    (0xAE), which uds_reset.execute() does not speak.

    ``confirm`` is accepted and ignored, and that is the point: there is no
    value of it that makes THIS send anything. The runnable adaptive-learn
    exchange lives in procedures.py behind its own gates and its own route;
    routing a write through the reset catalogue as well would give the same
    dangerous routine two front doors with two different sets of warnings, and
    the one that got clicked would be whichever was closer to hand.
    """
    reset = _BY_ID.get(reset_id)
    if reset is None:
        return {"ok": False, "message": f"Unknown Vauxhall service function '{reset_id}'.",
                "timestamp": _now()}
    return {
        "ok": False, "runnable": False, "reset": uds_reset.public_view(reset),
        "message": (
            f"'{reset['name']}' is a documented procedure, not a dongle "
            "command -- follow the steps. The adaptive values learn can also be "
            "run by the harness itself over GMW3110 DeviceControl, through "
            "/api/vauxhall/procedure rather than through this catalogue; that "
            "path has never been used on a car, and whether the module demands "
            "security access is still open."
        ),
        "timestamp": _now(),
    }
