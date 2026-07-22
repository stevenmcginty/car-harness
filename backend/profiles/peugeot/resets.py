"""Peugeot / PSA service resets.

Research verdict (docs/private/service-resets-research.md): PSA's service-interval
("spanner") reset is a coding write inside the BSI, performed after a
SecurityAccess unlock (0x27 03/04). The exact coding zone is NOT publicly
documented anywhere, so it can't be sent over a dongle honestly today -- but the
official dash-button procedure IS documented and reliable across the whole
2010+ range (including the digital i-Cockpit 208/2008). So every Peugeot reset
here is a ``procedure`` entry: the app shows the driver the exact steps rather
than pretending to send bytes it doesn't have.

If the BSI coding zone is ever captured from DiagBox, add a ``uds`` reset with
the step list and this same file serves it -- the executor and UI already
handle both kinds.
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
