"""Fiat / Alfa Romeo service resets.

Research (docs/private/fiat-alfa-research.md §5): no public raw dongle reset frame
exists for any Fiat/Alfa service counter -- MultiECUScan/AlfaOBD implement the
Service Reminder reset and the EPB "brake service" routine, but the wire-level
2E/31 ids live inside those tools and are not published (the same situation as
BMW's 2E CBS, PSA's BSI and VAG's ESI). So every entry here is a ``procedure``:
the app walks the driver through the exact dash/menu steps and touches nothing on
the car.

The headline entry is the Alfa Giulia/Stelvio EPB "Brake Service Mode" rear-pad
procedure (§6): the retract lives in the ABS/ESC module (Continental MK C1,
address 0x28) as a bi-directional actuation, so on a 2018+ SGW car it is blocked
over a bare ELM327 -- which is exactly why the built-in Uconnect dash menu is the
right route, and why this ships as a verified procedure, not a dongle command.

If a real MES/AlfaOBD transaction is ever captured, add it as a ``uds`` reset
(target the relevant module, 29-bit) and perform() below runs it unchanged.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.obd import service as obd_service
from backend.obd import uds_reset
from backend.obd.uds_elm327 import Elm327Uds29Client

RESETS: List[dict] = [
    {
        "id": "fiat-500-pedal",
        "name": "Oil service reset (Fiat 500, 312, pre-2016) -- accelerator pedal",
        "description": (
            "Resets the oil-service reminder on the pre-2016 Fiat 500 (type 312) with "
            "the accelerator-pedal trick. No dongle command exists for the Fiat service "
            "reset, so this is the built-in pedal procedure."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "scegliauto Fiat 500; klavkarr Fiat oil reset; seventrumpet 2012-2015 500",
        "models": "Fiat 500 (type 312, pre-2016)",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
            "The three pedal presses must be full and within 10 s or it won't register.",
        ],
        "procedure": [
            "Ignition ON, engine OFF.",
            "Depress the accelerator fully, slowly, 3 times within 10 seconds.",
            "Ignition OFF.",
            "Start the engine and check the service message has cleared.",
        ],
    },
    {
        "id": "fiat-cluster-menu",
        "name": "Oil service reset (Fiat 2016+ / Jeep Renegade) -- cluster menu",
        "description": (
            "Resets the oil-service reminder on the 2016-on Fiat 500 / 500X / Tipo / "
            "Panda and the Jeep Renegade through the cluster / infotainment menu. Manual "
            "dash procedure -- no dongle command exists for the Fiat service reset."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "scegliauto 500X; klavkarr Fiat oil reset; oilreset 500X",
        "models": "Fiat 500 / 500X / Tipo / Panda (2016+), Jeep Renegade",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
            "The accelerator-pedal 3x trick still works on many of these cars if the menu "
            "path differs on yours.",
        ],
        "procedure": [
            "Ignition ON, engine OFF.",
            "Open the cluster / infotainment menu with the steering-wheel arrows and OK.",
            "Go to Settings -> Service / Maintenance -> Oil Reset.",
            "Confirm the reset with OK.",
            "Ignition OFF, then back ON and check the service message is gone.",
        ],
    },
    {
        "id": "alfa-giulia-stelvio-oil",
        "name": "Oil service reset (Alfa Giulia / Stelvio) -- wheel menu or pedal",
        "description": (
            "Resets the oil-service reminder on the Alfa Giulia / Stelvio via the "
            "steering-wheel Service menu, or the accelerator-pedal 3x trick. Manual dash "
            "procedure -- no dongle command exists."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "Bosch Brisbane Giulia/Stelvio 2017-2021; scegliauto Stelvio; giuliaforums reset",
        "models": "Alfa Romeo Giulia (952), Stelvio",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
            "This clears only the DASH oil-change message. The Uconnect service reminder "
            "needs MES/AlfaOBD or a dealer -- an actuation blocked by the Security Gateway "
            "on 2018+ cars without a bypass.",
        ],
        "procedure": [
            "Ignition ON, engine OFF (do not start).",
            "Use the steering-wheel controls to open the Service / Oil Change menu.",
            "Hold OK until the reset completes; OR press the accelerator fully 3 times "
            "within 10 s.",
            "Ignition OFF, then back ON and check the dash message has cleared.",
        ],
    },
    {
        "id": "fiat-punto-pedal",
        "name": "Oil service reset (Fiat Punto family) -- accelerator pedal",
        "description": (
            "Resets the oil-service reminder on the Fiat Punto mk2 / Grande Punto / Punto "
            "Evo family with the accelerator-pedal procedure (same family as the 500). "
            "Manual dash procedure -- no dongle command exists."
        ),
        "method": "procedure",
        "confidence": "community",
        "source": "klavkarr Fiat oil reset (Punto family); community threads",
        "models": "Fiat Punto mk2 / Grande Punto / Punto Evo",
        "cautions": [
            "Community-sourced wording -- confirm against the car before relying on it.",
            "Reset AFTER the oil + filter change, not before.",
        ],
        "procedure": [
            "Ignition ON, engine OFF.",
            "Depress the accelerator fully 3 times within 10 seconds.",
            "Ignition OFF.",
            "Start and check the service message has cleared (or use MES if it hasn't).",
        ],
    },
    {
        "id": "alfa-epb-brake-service",
        "name": "Rear brake pads -- EPB Brake Service Mode (Giulia / Stelvio)",
        "description": (
            "The Alfa Giulia and Stelvio have an electric parking brake on the REAR "
            "calipers (motor-on-caliper, screw-type piston). To change the rear pads the "
            "EPB motors must be wound back via Brake Service Mode. The best part: it is a "
            "built-in Uconnect dashboard function -- you do NOT need a scan tool or the "
            "dongle for it. Front pads are conventional and need no special mode."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": ("giuliaforums brake-service-mode threads; stelvioforum rear-brake DIY; "
                   "alfabb brake service; instructables clear-errors MY2020 SGW"),
        "models": ("Alfa Giulia (952) & Stelvio -- rear pads only (fronts are conventional; "
                   "MiTo/Giulietta and CAN-era Fiats have a normal handbrake)"),
        "cautions": [
            "The scan-tool EPB command (MES 'EPB maintenance mode') is an actuation and is "
            "Security-Gateway-blocked over a dongle on 2018+ cars -- the dash menu is the "
            "right route, and it is free.",
            "Any latched codes (e.g. C1301, U1706, EPB 1407-13) can be READ over this "
            "dongle -- reads pass the SGW -- but CLEARING them on a 2018+ car needs an SGW "
            "bypass plug or a dealer.",
            "Keep everything away from the parking sensors during the job: an object near "
            "them triggers 'to exit service mode the vehicle must not be in motion' and the "
            "brake won't release.",
        ],
        "procedure": [
            "Car in ignition-on / accessory, engine OFF, stationary, wheels chocked, and "
            "NOTHING near the parking sensors (or it refuses to exit service mode).",
            "Enter Brake Service Mode via Uconnect: Settings -> Driver Assistance -> Brake "
            "Service. The EPB motors retract.",
            "Disconnect the NEGATIVE battery lead (put a towel over the boot latch so it "
            "can't lock) -- this stops the system logging a fluid-loss fault.",
            "Change the rear pads; wind back the screw-type piston with an EPB wind-back tool.",
            "PUMP THE BRAKE PEDAL to seat the pistons BEFORE reconnecting the battery "
            "(skipping this throws fluid-loss codes).",
            "Reconnect the battery. Ignition on (do not start) -- service mode should exit; "
            "if not, toggle the EPB switch a couple of times.",
            "Start the engine, turn the steering lock-to-lock then centre (clears the cluster "
            "self-check), and test the parking brake.",
        ],
    },
]

_BY_ID = {r["id"]: r for r in RESETS}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_resets() -> dict:
    return {"brand": "fiat", "resets": [uds_reset.public_view(r) for r in RESETS]}


def perform(
    adapter: Optional[dict], mode: str, reset_id: str, confirm: bool = False
) -> dict:
    """Run one Fiat / Alfa reset. Every shipped entry is a dash/menu procedure, so
    this returns the steps rather than touching the car. The UDS run path below is
    ready for any FCA routine whose id gets captured (29-bit addressing)."""
    reset = _BY_ID.get(reset_id)
    if reset is None:
        return {"ok": False, "message": f"Unknown Fiat / Alfa reset '{reset_id}'.", "timestamp": _now()}
    if reset["method"] != "uds":
        return {
            "ok": False, "runnable": False, "reset": uds_reset.public_view(reset),
            "message": f"'{reset['name']}' is a manual procedure -- follow the on-screen steps.",
            "timestamp": _now(),
        }
    if mode == "sim":
        log = [uds_reset.describe_step(s) for s in reset["steps"]]
        return {
            "ok": True, "simulated": True, "reset_id": reset_id, "log": log,
            "message": f"SIMULATED -- '{reset['name']}' would run these steps; no real car touched.",
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
                    "11-bit/500k CAN Fiat / Alfa diagnostics use. Ignition on and retry."
                ),
                "timestamp": _now(),
            }
        session.at("ATSP7")
        session.at("ATCAF1")
        client = Elm327Uds29Client(session)
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
            try:
                session.at(f"ATSP{session.protocol_num:X}" if session.protocol_num is not None else "ATSP0")
                session.at("ATSH7DF")
            except Exception:
                pass
        return {
            "ok": True, "simulated": False, "reset_id": reset_id, "log": log,
            "message": f"'{reset['name']}' sent. Cycle the ignition and check the dash to confirm.",
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "message": str(exc), "timestamp": _now()}
