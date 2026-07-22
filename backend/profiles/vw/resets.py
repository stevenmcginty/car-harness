"""VW / Audi (VAG) service resets.

Research (docs/private/vag-research.md §6): the UDS service-reset over the
dongle is NOT PUBLICLY SOURCED. VCDS resets the Service Reminder Interval on
module 17 (Instruments) by selecting the 'ESI: Resetting ESI' adaptation from a
drop-down -- but the underlying adaptation DID / 0x2E / 0x31 id is inside VCDS
and has never been published, the same situation as BMW's 0x2E CBS DID and
PSA's BSI zone. So every VAG reset here is a ``procedure``: the app walks the
driver through the exact dash/menu steps and touches nothing on the car.

If a real ODIS/VCDS 'Resetting ESI' transaction is ever captured, add it as a
``uds`` reset (target cluster 0x714, standard 11-bit) and perform() below runs
it unchanged.

Only the Golf 6/7 button procedure is directly CONFIRMED by multiple sources;
the digital-cockpit/MMI menu path is the same VAG family but per-model wording
varies, so it ships at "community" confidence.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.obd import service as obd_service
from backend.obd import uds_reset
from backend.obd.uds_elm327 import Elm327UdsClient

RESETS: List[dict] = [
    {
        "id": "vw-sri-button",
        "name": "Oil / inspection service reset (button cluster)",
        "description": (
            "Resets the Service Reminder Interval on VAG cars with a trip-reset "
            "(0.0) button in the cluster. There's no public dongle command for "
            "the UDS 'Resetting ESI' adaptation, so this is the built-in "
            "button-through-ignition procedure."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": ("Golf 6/7 oil-service reset guides (scegliauto, seventrumpet, "
                   "erwinsalarda, carworklog MK7, golfmk7 forum)"),
        "models": ("Most 2008–2019 VAG with a button cluster — Golf 6/7 CONFIRMED; "
                   "same family on Polo, Passat, Tiguan, T-Roc, Škoda, SEAT"),
        "cautions": [
            "Directly confirmed on Golf 6/7. On other models the wording and "
            "hold-time vary — check the owner's manual wording matches before "
            "relying on it.",
            "Reset AFTER the oil + filter change, not before.",
        ],
        "procedure": [
            "Ignition OFF.",
            "Press and hold the trip-reset (0.0) button in the cluster.",
            "Switch the ignition ON, keeping the button held.",
            "\"RESET OIL CHANGE SERVICE?\" appears — for the longer INSPECTION "
            "service instead, keep holding ~10–20 s until \"RESET INSPECT. "
            "SERVICE?\" shows.",
            "Release the button.",
            "Press the button again to confirm — \"SERVICE WAS RESET\" is shown.",
            "Ignition OFF.",
        ],
    },
    {
        "id": "vw-sri-menu",
        "name": "Service reset via digital cockpit / MMI menu",
        "description": (
            "Cars with a full TFT digital cockpit or central touchscreen "
            "(Golf 8 and most 2019+ VAG, Audi MMI) reset the service reminder "
            "from the on-screen menu rather than the trip button."
        ),
        "method": "procedure",
        "confidence": "community",
        "source": ("Ross-Tech SRI reset wiki; VAG digital-cockpit / MMI service "
                   "menu (family pattern, per-model wording varies)"),
        "models": "Golf 8+ and 2019+ VAG with digital cockpit; Audi MMI (A3 8Y, A4 B9, Q3, …)",
        "cautions": [
            "The exact menu wording and path vary by model and MMI/cockpit "
            "generation — confirm against the car's own menu.",
            "Some early builds still honour the button-through-ignition method.",
        ],
        "procedure": [
            "Ignition ON, engine off.",
            "Open the infotainment / cluster menu: Car (or Vehicle) → Service "
            "(& Inspection).",
            "Select the service item you replaced (e.g. Oil change / Inspection).",
            "Choose Reset and confirm.",
            "Ignition OFF, then back ON and check the service message is gone.",
        ],
    },
]

_BY_ID = {r["id"]: r for r in RESETS}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_resets() -> dict:
    return {"brand": "vw", "resets": [uds_reset.public_view(r) for r in RESETS]}


def perform(
    adapter: Optional[dict], mode: str, reset_id: str, confirm: bool = False
) -> dict:
    """Run one VW/Audi reset. Every shipped entry is a dash/menu procedure, so
    this returns the steps rather than touching the car. The UDS run path below
    is ready for any VAG cluster routine whose id gets captured."""
    reset = _BY_ID.get(reset_id)
    if reset is None:
        return {"ok": False, "message": f"Unknown VW/Audi reset '{reset_id}'.", "timestamp": _now()}
    if reset["method"] != "uds":
        return {
            "ok": False, "runnable": False, "reset": uds_reset.public_view(reset),
            "message": f"'{reset['name']}' is a manual procedure — follow the on-screen steps.",
            "timestamp": _now(),
        }
    if mode == "sim":
        log = [uds_reset.describe_step(s) for s in reset["steps"]]
        return {
            "ok": True, "simulated": True, "reset_id": reset_id, "log": log,
            "message": f"SIMULATED — '{reset['name']}' would run these steps; no real car touched.",
            "timestamp": _now(),
        }
    if confirm is not True:
        return {"ok": False, "message": "Refused: a reset changes the car. Confirm first.",
                "timestamp": _now()}

    address = reset["target"]["address"]

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False,
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k CAN VAG diagnostics use. Ignition on and retry."
                ),
                "timestamp": _now(),
            }
        client = Elm327UdsClient(session)
        client.begin()
        try:
            log = uds_reset.execute(client, address, reset["steps"])
        except Exception as exc:
            return {
                "ok": False, "simulated": False, "reset_id": reset_id,
                "message": f"'{reset['name']}' failed: {exc}", "timestamp": _now(),
            }
        finally:
            client.restore()
        return {
            "ok": True, "simulated": False, "reset_id": reset_id, "log": log,
            "message": f"'{reset['name']}' sent. Cycle the ignition and check the dash to confirm.",
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "message": str(exc), "timestamp": _now()}
