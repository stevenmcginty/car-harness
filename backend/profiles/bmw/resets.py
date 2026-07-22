"""BMW / MINI service (CBS) resets.

Research (docs/private/service-resets-research.md) settled this:

  * CBS lives in the instrument cluster KOMBI (diagnostic address 0x60).
  * The reset is UDS `0x2E` WriteDataByIdentifier (F-series "CBS v6"), needs
    only an extended session -- NO SecurityAccess. Cheap apps do it over an
    ELM327, so it's exactly the "over the VeePeak" job Steve wants.
  * The item IDs and the `0x2E` payload structure are fully documented (below).
  * THE ONE GAP: the DID that follows `0x2E` is compiled into the cluster and
    is not in any public source. Without it we will NOT send a guessed frame.

So today BMW ships the *dash* CBS reset (verified, no tool) and the `0x2E`
machinery is STAGED: CBS_ITEMS + build_cbs_reset_payload() are here and unit
tested, so the moment the DID is captured from one BimmerLink/ISTA trace on the
F26/F60, add it as CBS_RESET_DID and turn the oil entry into a "uds" reset --
the executor (backend/obd/uds_reset.py) and UI already handle it.

Transport when it goes live: backend/obd/bmw_uds_elm327.py (6F1 gateway
addressing over the ELM327), target KOMBI 0x60. Mirror of
android/.../obd/BrandResetData.kt (BMW section) -- keep both in sync.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.obd import uds_reset

KOMBI = 0x60  # instrument cluster: the CBS master on F-series / UKL

# CBSKENNUNG item ids (confirmed from the KOMB_G11 / KOMB_J29 cluster SGBD).
# Microfilter is deliberately absent: on F-series CBS v6 it is not a standalone
# resettable item (it folds into the vehicle check); only pre-F KWP clusters
# had a distinct 0x04 Filt.
CBS_ITEMS = {
    "oil": 0x01,          # Motoroel
    "front_brakes": 0x02,  # Bremsbelaege vorne
    "brake_fluid": 0x03,   # Bremsfluessigkeit
    "rear_brakes": 0x06,   # Bremsbelaege hinten
    "spark_plugs": 0x0A,   # Zuendkerzen
    "vehicle_check": 0x11,  # Fahrzeug-Check / Sichtpruefung
}

# The DID for the 0x2E CBS_RESET write. UNKNOWN until captured on-car -- keep
# None so nothing can accidentally send a guessed frame.
CBS_RESET_DID: Optional[int] = None


def build_cbs_reset_payload(
    item_id: int,
    availability: int = 0x64,   # 100% remaining (0xFF = no change; brake fluid may be 0x96)
    service_count: int = 0x1F,  # 31 = increment services-done by +1
) -> bytes:
    """Assemble the documented CBS_RESET argument block (the bytes that follow
    the DID in a 0x2E write). Every field except the item, availability and
    service counter is set to its "no change" sentinel, i.e. a normal
    "service done" reset. This is verified against the SGBD field table; only
    the leading DID is missing to make a complete on-wire frame."""
    return bytes([
        item_id & 0xFF,       # CBS_KENNUNG
        availability & 0xFF,  # CBS_VERFUEGBARKEIT
        service_count & 0xFF,  # CBS_ANZAHL_SERVICE
        0x0F,                 # CBS_ZIEL_MONAT   = no change
        0x3F,                 # CBS_ZIEL_JAHR    = no change
        0x80, 0x00,           # RMM_CBS_WERT     = no change (0x8000)
        0x0F,                 # ST_UN_CBS_RSTG   = don't care
        0xFF,                 # FRC_INTM_WAY      = no change
        0xFF,                 # FRC_INTM_T        = no change
        0x00,                 # CHOV_CBS_CBR_VIEW
        0x00,                 # RES_BYTE
    ])


RESETS: List[dict] = [
    {
        "id": "bmw-cbs-dash",
        "name": "CBS service reset (oil / brakes / vehicle check)",
        "description": (
            "Resets a Condition Based Service item from the dash — engine oil, "
            "brake fluid, front/rear pads, vehicle check. The dongle route needs "
            "one manufacturer byte that isn't public yet (see below), so this is "
            "the on-cluster method, which needs no tools and is what BMW's own "
            "menu does."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "BMW F-series CBS reset (BC-stalk method); ISTA CBS_RESET job",
        "models": "F-series BMW + UKL MINI (F20/F30/F26 X4, F54/F55/F56/F60), ~2011–2019",
        "cautions": [
            "Scroll to the specific item (oil, brakes…) before confirming — you "
            "reset one item at a time.",
            "Only reset an item you've actually serviced; the cluster records the "
            "reset date and mileage.",
        ],
        "procedure": [
            "Get in, don't start the engine. Press Start/Stop once WITHOUT a foot "
            "on the brake to wake the car (terminal 15 / ignition on).",
            "Press and hold the BC button on the end of the indicator stalk until "
            "the service (CBS) menu appears in the cluster.",
            "Short-press the BC button to step through the CBS items to the one "
            "you serviced (e.g. Engine oil).",
            "Press and hold the BC button again — a 'Reset?' prompt appears.",
            "Confirm with another press/hold. The item's remaining life returns to "
            "100% and the service date/mileage are stamped.",
            "Switch the ignition off to finish.",
        ],
    },
]

_BY_ID = {r["id"]: r for r in RESETS}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_resets() -> dict:
    return {"brand": "bmw", "resets": [uds_reset.public_view(r) for r in RESETS]}


def perform(
    adapter: Optional[dict], mode: str, reset_id: str, confirm: bool = False
) -> dict:
    """Run one BMW reset. Every shipped entry is a dash procedure today, so
    this returns the steps rather than touching the car. When CBS_RESET_DID is
    captured and an entry becomes ``method: "uds"``, add the UDS run path here
    (see backend/profiles/mazda/resets.py.perform for the shape)."""
    reset = _BY_ID.get(reset_id)
    if reset is None:
        return {"ok": False, "message": f"Unknown BMW reset '{reset_id}'.", "timestamp": _now()}
    if reset["method"] != "uds":
        return {
            "ok": False, "runnable": False, "reset": uds_reset.public_view(reset),
            "message": (
                f"'{reset['name']}' is done on the dash — follow the on-screen steps. "
                "(A dongle reset needs a BMW byte that isn't public yet.)"
            ),
            "timestamp": _now(),
        }
    # No uds entries ship yet; kept for when CBS_RESET_DID lands.
    return {"ok": False, "message": "No runnable BMW UDS reset is enabled yet.", "timestamp": _now()}
