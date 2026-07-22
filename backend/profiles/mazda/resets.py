"""Mazda (SkyActiv-era) service resets.

Research (docs/private/service-resets-research.md): there is NO publicly
documented dongle/UDS reset for the SkyActiv gasoline maintenance monitor --
the "wrench" counters live in the cluster/infotainment and are reset only from
the dash. The diesel PCM oil-deterioration and DPF resets ARE real UDS routines
(FORScan exposes them) but their hex has never been published, and each has an
official no-tool fallback. So every Mazda reset here is a ``procedure``: the app
walks the driver through the exact official steps.

If a SkyActiv-D PCM routine id is ever captured, add it as a ``uds`` reset
(target PCM 0x7E0, standard 11-bit) and perform() below runs it unchanged.

Mirror of android/.../obd/BrandResetData.kt (Mazda section) -- keep both in sync.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.obd import service as obd_service
from backend.obd import uds_reset
from backend.obd.uds_elm327 import Elm327UdsClient

RESETS: List[dict] = [
    {
        "id": "mazda-maintenance-monitor",
        "name": "Maintenance monitor reset (oil / service / tyre rotation)",
        "description": (
            "Resets the SkyActiv 'wrench' maintenance monitor after a service. "
            "The monitor tracks three things — Scheduled Maintenance, Oil Change "
            "and Tyre Rotation — and there's no dongle command for it, so this is "
            "the built-in dash reset."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "Mazda owner's manuals; Service Alert SA-071/17",
        "models": "SkyActiv MX-5 ND, 2/DJ, 3/BM/BN/BP, 6/GJ/GL, CX-3, CX-5 KE/KF",
        "cautions": [
            "With no centre display (Type A audio) the trip A/B and average fuel "
            "economy reset too if a trip is on screen — clear it first.",
            "After resetting, cycle the ignition and check the wrench is actually "
            "gone; a few 2020+ owners report it needing a second try.",
        ],
        "procedure": [
            "Cars WITH the centre display (MZD Connect):",
            "  1. Ignition ON, engine off (press Start twice, no brake).",
            "  2. Home/Applications → Vehicle Status Monitor → Maintenance.",
            "  3. Pick the item (Oil Change / Scheduled Maintenance / Tyre "
            "Rotation) → Reset → Yes.",
            "  4. Ignition off.",
            "Cars WITHOUT a centre display (base trims):",
            "  1. Ignition OFF.",
            "  2. Press and hold the trip/selector button, then switch the "
            "ignition ON, keeping it held.",
            "  3. Keep holding 7+ seconds until the master warning light flashes "
            "— that confirms the reset. Release.",
        ],
    },
    {
        "id": "mazda-ieloop-battery",
        "name": "Battery / i-ELOOP reset after battery change",
        "description": (
            "After replacing the battery on an i-stop / i-ELOOP car, the charging "
            "system has to relearn. There's no dongle reset — this is Mazda's "
            "official pedal procedure, done with the OLD battery still fitted."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "Mazda Service Bulletin 01-002/14; i-stop initialisation (WSM)",
        "models": "SkyActiv i-stop / i-ELOOP (e.g. Mazda3, Mazda6, CX-5)",
        "cautions": [
            "On i-ELOOP cars disconnect the CURRENT SENSOR connector BEFORE the "
            "negative terminal, or you'll get false readings / DTCs.",
            "Do the pedal steps before swapping the battery, then reinitialise "
            "windows, sunroof, clock and TPMS after.",
        ],
        "procedure": [
            "With the old battery still fitted: ignition ON (engine off), gear in "
            "N/P, foot on the brake.",
            "Depress the accelerator fully for 5 seconds or more.",
            "Check the charging-system and master warning lights flash.",
            "Depress and release the accelerator 3 times.",
            "Check the charging light comes on steady and the master warning light "
            "goes out — the relearn is armed.",
            "Ignition OFF, then change the battery (sensor connector first on "
            "i-ELOOP). Reconnect, ignition ON, and reinitialise windows/sunroof/"
            "clock/TPMS.",
        ],
    },
    {
        "id": "mazda-diesel-oil-data",
        "name": "Engine oil data reset (SkyActiv-D diesel)",
        "description": (
            "SkyActiv-D diesels hold an oil-deterioration/dilution figure in the "
            "PCM that must be reset at every oil change. FORScan/M-MDS can do it "
            "over the port but the routine id isn't public, so this is Mazda's "
            "official no-tool jumper method."
        ),
        "method": "procedure",
        "confidence": "community",
        "source": "Mazda WSM 'Engine oil data reset [SkyActiv-D 2.2]'; FORScan",
        "models": "SkyActiv-D 1.5 / 2.2 diesels (UK/EU stock)",
        "cautions": [
            "Diesel only — petrol SkyActiv has no PCM oil-data counter.",
            "Reset AFTER the oil + filter change, not before.",
        ],
        "procedure": [
            "Do the oil and filter change first.",
            "Ignition ON, engine off.",
            "Jumper the diagnostic test terminal (TEN) to body ground.",
            "Within 5 seconds, fully depress and release the accelerator 5 times.",
            "The glow-plug indicator flashes 5 times = reset done.",
            "Remove the jumper. (Or, with FORScan: Service Functions → Reset "
            "Engine Oil Deterioration Learned Value on the PCM.)",
        ],
    },
]

_BY_ID = {r["id"]: r for r in RESETS}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_resets() -> dict:
    return {"brand": "mazda", "resets": [uds_reset.public_view(r) for r in RESETS]}


def perform(
    adapter: Optional[dict], mode: str, reset_id: str, confirm: bool = False
) -> dict:
    """Run one Mazda reset. Every shipped entry is a dash/pedal procedure, so
    this returns the steps rather than touching the car. The UDS run path below
    is ready for any SkyActiv-D PCM routine whose id gets captured."""
    reset = _BY_ID.get(reset_id)
    if reset is None:
        return {"ok": False, "message": f"Unknown Mazda reset '{reset_id}'.", "timestamp": _now()}
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
                    "11-bit/500k HS-CAN Mazda diagnostics use. Ignition on and retry."
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
