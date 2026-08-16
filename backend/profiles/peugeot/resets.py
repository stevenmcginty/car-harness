"""Peugeot / PSA service resets.

Research verdict (docs/private/service-resets-research.md and
docs/private/peugeot-t7-special-functions-research.md): PSA's service-interval
("spanner") reset is a coding write inside the BSI, performed after a
SecurityAccess unlock (0x27 03/04). The exact coding zone is NOT publicly
documented anywhere, so it can't be sent over a dongle honestly today -- but the
official dash-button procedure IS documented and reliable across the whole
2010+ range (including the digital i-Cockpit 208/2008). So every Peugeot reset
here is a ``procedure`` entry: the app shows the driver the exact steps rather
than pretending to send bytes it doesn't have.

One crack in that wall, worth chasing: Klavkarr's EOBD-Facile app demonstrably
performs the maintenance-counter reset over a plain ELM327 dongle on these
years (its "Maintenance Interval ECU" menu). The bytes exist -- they're just
not published. A Bluetooth HCI snoop of that app doing the reset would hand us
the request id + write payload, and then this file gains a real ``uds`` entry.

The T7-era entries below (windows, key fob, roof, BSI battery) are the ones
with owner's-handbook or strong-community provenance -- every "reset" the
forums circulate that is folklore (throttle pedal dances, winter plugs) is
deliberately NOT here; see the research doc for why.
"""

from typing import List, Optional

from backend.obd import uds_reset

RESETS: List[dict] = [
    {
        "id": "peugeot-service",
        "name": "Service indicator (spanner) reset",
        "description": (
            "Clears the service-due spanner after an oil/interval service. On PSA "
            "cars the counter lives in the BSI; there is no public dongle command, "
            "so this is the official trip-button procedure -- it works with no tools "
            "and takes under a minute."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "Peugeot owner's manuals; klavkarr/mrautofixer reset guides",
        "models": "208/2008, 308, 3008/5008, 508 and Citroen/DS equivalents (~2010+)",
        "cautions": [
            "If you disconnect the battery afterwards, lock the car and wait 5 "
            "minutes first, or the reset is lost.",
            "The button resets the displayed counter only -- the BSI's oil-wear "
            "model behind it is separate (DiagBox territory).",
        ],
        "procedure": [
            "Switch the ignition OFF.",
            "Press and hold the trip-distance zero-reset button (end of the wiper "
            "stalk, the lighting stalk, or below the speedometer).",
            "Switch the ignition ON while still holding the button.",
            "The mileage display starts counting down; keep holding.",
            "When it shows '=0' (or the digital cluster says 'Re-initialisation "
            "performed'), release the button.",
            "The spanner is gone. Switch the ignition off to finish.",
        ],
    },
    {
        "id": "peugeot-window-antipinch",
        "name": "Electric window anti-pinch re-initialisation",
        "description": (
            "Re-teaches each window its one-touch travel and anti-pinch stop -- "
            "needed after a battery disconnect, window motor work, or when a "
            "window only inches along. Owner's-handbook procedure (ACCESS section, "
            "~page 70), no tools."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": (
            "Peugeot owner's handbook (ACCESS, ~p70), quoted by GiveMeABreak on "
            "frenchcarforum.co.uk t=65627; aussiefrogs 308 thread"
        ),
        "models": "308 / 308 CC / RCZ and T7-platform Citroens (2007-2013)",
        "cautions": [
            "Do it BEFORE any folding-roof work on a 308 CC -- an un-initialised "
            "window halts the whole roof sequence.",
            "Run per window, driver's side first, ignition on, engine off.",
        ],
        "procedure": [
            "Ignition ON, engine off. Start with the driver's window.",
            "Pull the switch up until the window fully closes.",
            "Keep holding the switch for ~3 seconds after it stops.",
            "Press the switch so the window auto-lowers fully.",
            "When fully down, press the switch again for ~1 second.",
            "Release. Repeat on the passenger window.",
        ],
    },
    {
        "id": "peugeot-key-resync",
        "name": "Remote key fob re-synchronisation",
        "description": (
            "Re-syncs the flip key's RF locking after a fob battery change or a "
            "flat car battery -- the classic 'key starts the car but won't lock "
            "it' fix. No tools, ~6 minutes (most of it waiting)."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": (
            "Peugeot service bulletins via Scribd 'PEUGEOT Key synchronization'; "
            "peugeotforums t=332294; aussiefrogs 104925"
        ),
        "models": "T7-platform cars with the flip key (non-keyless), 2007-2013",
        "cautions": [
            "If the key won't START the car either, that's immobiliser/BSI "
            "pairing -- DiagBox only, this procedure won't help.",
            "A fob dead right after a battery swap is often just a flattened "
            "battery contact inside the fob -- reseat it first.",
            "The 'hold the key head against the steering-column mark' procedure "
            "online is for later keyless cars, not this one.",
        ],
        "procedure": [
            "Ignition ON (do not start the engine).",
            "Press and HOLD the lock button on the fob for ~10 seconds "
            "(some cars want 5 -- try both).",
            "Ignition OFF, remove the key.",
            "Wait ~5 minutes without touching anything, then test the fob.",
        ],
    },
    {
        "id": "peugeot-roof-cc",
        "name": "308 CC folding roof re-sync (after manual close / stuck)",
        "description": (
            "The 308 CC's roof ECU re-syncs itself from its microswitches once "
            "every latch reports properly closed -- there is NO scanner "
            "re-initialisation routine for it (DiagBox can only read/clear the "
            "roof faults). This is the field procedure that un-sticks a roof "
            "that stopped mid-cycle or after a manual close."
        ),
        "method": "procedure",
        "confidence": "community",
        "source": (
            "peugeotforums 308 CC roof threads t=365668 / t=150050; 308 CC "
            "Facebook group (roof-ECU-not-scanner-resettable statement)"
        ),
        "models": "308 CC (T7 convertible, 2009-2015)",
        "cautions": [
            "Windows must be initialised first (see the window procedure) -- an "
            "un-initialised window halts the roof.",
            "If the pump runs but nothing moves, the hydraulic bleed screw (next "
            "to the pump, boot RH side behind the trim) has been left open -- "
            "close it before anything else.",
            "Real roof fault codes seen on these cars: 9884 (RH lock switch), "
            "98A0 (LH lock switch), BF7B -- reading/clearing them needs "
            "DiagBox/Lexia on the pins-3/8 bus.",
        ],
        "procedure": [
            "Check the two front roof locks are actually clamped shut -- if in "
            "doubt, back each one off and re-lock it so the microswitch signals "
            "properly.",
            "If the roof is stuck part-way, manually park it fully into the boot "
            "with the emergency tool (kit under the boot floor).",
            "Close the boot lid fully.",
            "Switch everything off, lock the car, unlock it.",
            "Retry the roof from the switch. The ECU re-learns the latches as "
            "they report closed.",
            "If it still refuses, cycle the roof fully several times while "
            "topping the hydraulic reservoir (13 mm brass screw, ~0.3 L, "
            "G004000M2-class fluid) to bleed the pump.",
        ],
    },
    {
        "id": "peugeot-bsi-battery",
        "name": "BSI-safe battery disconnection / reconnection",
        "description": (
            "The procedure for changing the battery on any BSI-era Peugeot "
            "without corrupting the body computer's config or losing key sync. "
            "Pure discipline, no tools -- the mistake it prevents is cutting "
            "power while the BSI is still awake."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "Peter Cooper Car Repairs BSI reboot procedure; thevehiclecheck.co.uk",
        "models": "All BSI-era Peugeot/Citroen (~2000 onwards)",
        "cautions": [
            "If this was skipped and the car now acts weird: the fix is a "
            "DiagBox session and a window re-init, NOT another battery pull.",
            "Never let a tool write a different VIN into the BSI -- it breaks "
            "key coding and radio pairing.",
        ],
        "procedure": [
            "Disconnect any diagnostic tool from the OBD port.",
            "Driver's window fully down, bonnet up, everything electrical off.",
            "All doors closed, key out of the ignition.",
            "Wait 3 FULL minutes without touching anything -- the BSI goes to "
            "sleep (power-save mode).",
            "Disconnect the battery. Wait 15 seconds.",
            "Reconnect. Wait 10 seconds, and do NOT open the doors.",
            "Switch the side/headlights ON through the open window.",
            "Ignition on, check the systems.",
            "Hold the key's lock button for 10 seconds.",
            "Open and close a door, test the central locking.",
            "Start the engine and do a full check.",
        ],
    },
    {
        "id": "peugeot-dpf-additive",
        "name": "DPF / Eolys additive top-up reset (HDi diesel)",
        "description": (
            "After topping up the Eolys additive on an HDi diesel, the additive "
            "counter is reset in the engine ECU. This is a named DiagBox function "
            "whose routine id isn't public, so it's documented here as a specialist "
            "step, not a dongle command."
        ),
        "method": "procedure",
        "confidence": "community",
        "source": "peugeotforums DiagBox additive-reset threads",
        "models": "HDi / BlueHDi diesels with Eolys additive tank",
        "cautions": [
            "Only needed after refilling additive or replacing the DPF.",
            "Requires DiagBox/Lexia; a generic dongle can't perform this.",
        ],
        "procedure": [
            "Connect DiagBox/Lexia (not a generic dongle).",
            "Repair and test by ECU -> Engine Management ECU -> Repair -> Pack "
            "Repair -> Replacement Parts -> Filling of the Additive Reservoir.",
            "Enter the current mileage in km and follow the prompts.",
            "If the DPF itself was replaced, also run the 'changing FAP' step so "
            "the soot counters are reset.",
            "Switch the ignition off then on to store.",
        ],
    },
]

_BY_ID = {r["id"]: r for r in RESETS}


def list_resets() -> dict:
    """The Peugeot resets as the API/UI JSON shape."""
    return {"brand": "peugeot", "resets": [uds_reset.public_view(r) for r in RESETS]}


def perform(
    adapter: Optional[dict], mode: str, reset_id: str, confirm: bool = False
) -> dict:
    """Peugeot has no runnable dongle resets yet -- every entry is a procedure.

    Kept for API symmetry with the BMW/Mazda perform(): it always returns a
    clear "follow the on-screen steps" result instead of touching the car.
    """
    reset = _BY_ID.get(reset_id)
    if reset is None:
        return {"ok": False, "message": f"Unknown Peugeot reset '{reset_id}'."}
    return {
        "ok": False,
        "runnable": False,
        "reset": uds_reset.public_view(reset),
        "message": (
            f"'{reset['name']}' can't be sent over a dongle -- follow the "
            "on-screen procedure instead."
        ),
    }
