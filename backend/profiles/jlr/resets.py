"""Jaguar / Land Rover service resets.

Research (docs/private/jlr-research-resets.md): JLR reset *actuations* live
inside proprietary tools (SDD/Pathfinder, GAP IIDTool) and no raw dongle 2E/31
frame is public -- the same situation as BMW's CBS, PSA's BSI and VAG's ESI. So
every entry here is a ``procedure``: the app walks the driver through the exact
dash/menu/pedal steps and touches nothing on the car. That is JLR's superpower
in this harness: dash procedures don't care about the DoIP wall, so they work
on EVERY era including the MY17+ cars the module scan can't reach.

Three oil-reset generations run through the range: the legacy trip-button hold
(Disco 3, early L322, Freelander 2), the bonnet-open + brake+accelerator pedal
dance (Disco Sport, L494, L405, modern Jaguars), and the steering-wheel menu
(Evoque L538, RR Sport L320). The flagship is the EPB service mode: modern
motor-on-caliper cars have a no-tool switch dance for rear pad changes, and
compressing one of those pistons WITHOUT service mode destroys the in-caliper
actuator -- the card exists as much for the warning as the steps.

Honest tool-only verdicts (battery/BMS registration, forced DPF regen, air
suspension calibration, transport-mode exit) ship as WARN-style cards: the
steps explain the situation instead of pretending a dongle can do it.

If a real SDD/IIDTool transaction is ever captured, add it as a ``uds`` reset
(11-bit addressing) and perform() below runs it unchanged.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.obd import service as obd_service
from backend.obd import uds_reset
from backend.obd.uds_elm327 import Elm327UdsClient

RESETS: List[dict] = [
    # ------------------------------------------------------------ oil / service
    {
        "id": "jlr-oil-xf-x250",
        "name": "Oil service reset (Jaguar XF X250 2008-15) -- fog-switch dance",
        "description": (
            "Resets the service-interval reminder on the Jaguar XF X250 with the "
            "rear-fog-switch sequence. Fiddly but real -- no dongle command exists "
            "for any JLR service reset, so this is the built-in method."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "scegliauto 132196; jaguarforums X250 threads; enginepartsdiagram",
        "models": "Jaguar XF X250 (2008-2015)",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
            "The switch sequence must be done briskly (within ~3 s) -- memorise it first.",
        ],
        "procedure": [
            "Ignition ON without touching the brake (no engine).",
            "Hold the REAR FOG LAMP switch, press START (still no brake), release the "
            "fog switch.",
            "Now hold the trip-computer button (end of the stalk) AND the rear fog "
            "switch together until the message centre shows 'resetting service mode' "
            "then 'service mode reset'.",
            "Release both, ignition OFF. Restart and check the reminder is gone.",
        ],
    },
    {
        "id": "jlr-oil-modern-jaguar",
        "name": "Oil service reset (Jaguar XE / XF X260 / F-Pace) -- pedals or menu",
        "description": (
            "Resets the service reminder on the 2015-on Jaguars. Two built-in routes: "
            "the bonnet-open pedal dance, or the steering-wheel Service menu where "
            "fitted. No dongle command exists."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "scegliauto 107146/132196; fpaceforums 7582/5393; justanswer F-Pace",
        "models": "Jaguar XE (X760), XF (X260), F-Pace (X761), 2015-2021",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
            "2021+ facelift cars: sources are split -- try brake ~3 s then accelerator "
            "~10 s until 'service counter resetting'; some 2021+ cars only reset with "
            "dealer/JLR software.",
            "Manual resets on modern JLR often clear the oil counter but not the "
            "time/days counter -- if a date-based reminder returns, it needs a tool.",
        ],
        "procedure": [
            "Route A (pedals): bonnet OPEN, all doors closed, then open the driver's "
            "door. Ignition ON without the brake (no engine).",
            "Press brake AND accelerator fully together and hold ~10 s until the "
            "cluster shows 'service resetting'.",
            "Release, ignition OFF then ON to confirm the message is gone.",
            "Route B (menu, where fitted): steering-wheel arrows -> Service / Oil "
            "Change -> hold OK/reset until it confirms.",
        ],
    },
    {
        "id": "jlr-oil-evoque-l538",
        "name": "Oil service reset (Range Rover Evoque L538) -- wheel menu",
        "description": (
            "Resets the service reminder on the 2011-2018 Evoque through the "
            "steering-wheel cluster menu. No dongle command exists."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "scegliauto 106248/32686; seventrumpet; Land Rover SSM60837",
        "models": "Range Rover Evoque L538 (2011-2018)",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
            "The facelift/L551 Evoque moves to the bonnet-open pedal method (see the "
            "Disco Sport entry).",
        ],
        "procedure": [
            "Ignition ON (no engine).",
            "Steering-wheel OK button -> Vehicle Information -> Oil Service.",
            "Press RES (lower-centre wheel button) to reset; alternatively Settings -> "
            "Service menu, hold the left-stalk-end button ~10 s.",
            "Ignition OFF then ON and check the reminder is gone.",
        ],
    },
    {
        "id": "jlr-oil-disco-sport",
        "name": "Oil service reset (Discovery Sport L550) -- bonnet + pedals",
        "description": (
            "Resets the service reminder on the 2014-2019 Discovery Sport with the "
            "bonnet-open pedal dance. No dongle command exists."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "seventrumpet; discosportforums 14079; GAP diagnostic notes",
        "models": "Discovery Sport L550 (2014-2019); same method on late Evoque",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
            "KNOWN LIMITATION: on some model years the cluster won't clear the message "
            "even after a valid reset -- that needs an IPC software update or a JLR "
            "tool, not a repeat of the procedure.",
        ],
        "procedure": [
            "Bonnet OPEN, close all doors, ignition ON without the brake (no engine), "
            "then open the driver's door.",
            "Press brake AND accelerator fully together and hold ~30 s until the "
            "cluster shows 'resetting' then 'reset complete'.",
            "Release, ignition OFF then ON and check the reminder is gone.",
        ],
    },
    {
        "id": "jlr-oil-disco3",
        "name": "Service interval reset (Discovery 3 / LR3) -- trip-button hold",
        "description": (
            "Resets the service-interval (SIA) reminder on the 2004-2009 Discovery 3 "
            "with the classic cluster-button hold. No dongle command exists."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "4x4community 111739; scegliauto 108490; enginepartsdiagram",
        "models": "Discovery 3 / LR3 (2004-2009)",
        "cautions": [
            "Reset AFTER the service, not before.",
        ],
        "procedure": [
            "Ignition OFF.",
            "Press and HOLD the left cluster (trip) reset button.",
            "Turn the ignition to position II, keeping the button held ~5 s until the "
            "LCD shows 'RESET SIA'.",
            "Release, then press the button again to confirm ('rst').",
            "Ignition OFF, restart, check the reminder is gone.",
        ],
    },
    {
        "id": "jlr-oil-disco4",
        "name": "Oil service reset (Discovery 4 / LR4) -- pedals (or trip button)",
        "description": (
            "Resets the service reminder on the 2010-2016 Discovery 4. Two variants "
            "exist across the run -- the pedal dance and the earlier trip-button hold; "
            "try the pedals first. No dongle command exists."
        ),
        "method": "procedure",
        "confidence": "community",
        "source": "aulro 286061; enginepartsdiagram; scegliauto (variants disputed by MY)",
        "models": "Discovery 4 / LR4 (2010-2016)",
        "cautions": [
            "Sources genuinely disagree on which model years take which variant -- if "
            "the pedal route doesn't take, try the Disco 3 trip-button hold.",
            "Reset AFTER the service, not before.",
        ],
        "procedure": [
            "Bonnet OPEN, driver's door open, ignition ON (no engine).",
            "Hold accelerator AND brake fully together until the cluster responds.",
            "Cycle the trip display to END then TRIP to exit.",
            "Ignition OFF, start the engine to confirm the reminder is gone.",
        ],
    },
    {
        "id": "jlr-oil-rrs-l320",
        "name": "Oil service reset (Range Rover Sport L320) -- cruise CANCEL menu",
        "description": (
            "Resets the service reminder on the 2005-2013 RR Sport through the cluster "
            "menu with the cruise CANCEL double-press. No dongle command exists."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "rangerovers.net 17848/277762; seventrumpet",
        "models": "Range Rover Sport L320 (2005-2013)",
        "cautions": [
            "Post-2010 diesels may need diagnostics if the menu route refuses.",
            "Reset AFTER the service, not before.",
        ],
        "procedure": [
            "Ignition ON, engine OFF.",
            "Steering-wheel controls -> Service Menu -> Oil Level Display "
            "(or Vehicle Info -> Service Interval on some years).",
            "Press cruise CANCEL twice within 2 seconds (or hold reset on the "
            "Service Interval screen).",
            "Ignition OFF then ON and check the reminder is gone.",
        ],
    },
    {
        "id": "jlr-oil-rrs-l494",
        "name": "Oil service reset (Range Rover Sport L494) -- bonnet + pedals",
        "description": (
            "Resets the service reminder on the 2013-2022 RR Sport with the modern "
            "bonnet-open pedal dance (or the cluster menu where fitted). No dongle "
            "command exists."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "scegliauto 103663; rangerovers.net; seventrumpet",
        "models": "Range Rover Sport L494 (2013-2022)",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
        ],
        "procedure": [
            "Bonnet OPEN, doors closed, then open the driver's door. Ignition ON "
            "without the brake (no engine).",
            "Press brake AND accelerator fully together and hold until the cluster "
            "shows 'service resetting'.",
            "Release, ignition OFF then ON to confirm.",
            "Menu alternative (where fitted): Vehicle Info -> Service Interval -> "
            "hold reset.",
        ],
    },
    {
        "id": "jlr-oil-l405",
        "name": "Oil service reset (Range Rover L405) -- pedals (MY14+, petrol)",
        "description": (
            "Resets the service reminder on the full-size L405 with the pedal dance. "
            "Real caveats: it works from MY14 on, and several sources say PETROL only "
            "-- diesel L405s (and pre-MY14 cars) need a JLR-capable tool."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "rangerovers.net 110361/277762; landroverworld 25667",
        "models": "Range Rover L405 (MY14-2022) -- petrol; diesel/pre-MY14 = tool only",
        "cautions": [
            "Pre-MY14 cars: no dash route exists -- tool only.",
            "Diesel: multiple sources report the pedal reset doesn't take -- expect to "
            "need a JLR-capable tool (GAP IIDTool / iCarsoft LR / SDD).",
            "Reset AFTER the oil + filter change, not before.",
        ],
        "procedure": [
            "Bonnet OPEN, doors closed, then open the driver's door. Ignition ON "
            "without the brake (no engine).",
            "Press brake AND accelerator fully together and hold ~10 s until the "
            "cluster shows 'service resetting'.",
            "Release, ignition OFF then ON to confirm.",
        ],
    },
    {
        "id": "jlr-oil-l322",
        "name": "Service reset (Range Rover L322) -- three systems by year",
        "description": (
            "The L322 changed reset systems twice across one badge. 2002-2005 "
            "(BMW-era) and 2006-2009 (Ford-era) both use a trip-button hold with "
            "different displays; the 2010-2013 facelift has NO dash route at all -- "
            "tool only (IIDTool/SDD)."
        ),
        "method": "procedure",
        "confidence": "community",
        "source": "scegliauto 42778; aulro 205836; enginepartsdiagram (variants by year)",
        "models": "Range Rover L322: 2002-05 and 2006-09 dash; 2010-13 TOOL ONLY",
        "cautions": [
            "2010-2013 facelift: genuinely tool-only -- don't keep trying the buttons.",
            "Year variants are community-sourced and disputed -- if one display "
            "doesn't match, try the other era's steps.",
        ],
        "procedure": [
            "2002-2005 (BMW-era): ignition OFF. Hold the LEFT trip button, turn the "
            "ignition to position I, keep held ~5 s until 'OIL SERVICE' or "
            "'INSPECTION' shows with a reset prompt, confirm.",
            "2006-2009: hold the trip button, ignition to position II, 'SERVICE' "
            "flashes, keep held ~5 s until it shows 'END'/resets.",
            "2010-2013: no dash procedure exists -- use a GAP IIDTool, iCarsoft LR "
            "or SDD.",
        ],
    },
    {
        "id": "jlr-oil-freelander2",
        "name": "Service reset (Freelander 2 / LR2) -- cluster button + start",
        "description": (
            "Resets the maintenance-interval reminder on the 2006-2014 Freelander 2 "
            "with the cluster-button hold. Very well documented. No dongle command "
            "exists."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "scegliauto 65052/54476/46795; resetservicelight; multiple videos agree",
        "models": "Freelander 2 / LR2 (2006-2014), key-start and push-button variants",
        "cautions": [
            "Reset AFTER the service, not before.",
        ],
        "procedure": [
            "Press and HOLD the reset button below the speedometer.",
            "With it held, insert the key / press START (ignition on).",
            "Keep holding until the cluster asks 'reset maintenance interval?'.",
            "Hold (or press) the button again to confirm.",
            "Ignition OFF then ON and check the reminder is gone.",
        ],
    },
    # ------------------------------------------------------------ EPB service mode
    {
        "id": "jlr-epb-modern",
        "name": "Rear brake pads -- EPB service mode (modern motor-on-caliper JLR)",
        "description": (
            "Modern JLR (Evoque, Disco Sport, RR Sport L494, L405, Velar, Jaguar "
            "XE/XF X260/F-Pace) has an electric park brake motor ON the rear caliper. "
            "To change rear pads the motors must wind back in SERVICE MODE -- and "
            "there is a no-tool switch dance for it. The critical warning: forcing "
            "the piston back WITHOUT service mode destroys the in-caliper actuator."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": ("iCarsoft LR EPB guide; rangerovers.net 342310; evoqueforums 6696; "
                   "xeforums 6363 (switch position disputed -- both recorded)"),
        "models": ("Evoque (late L538/L551), Discovery Sport L550, RR Sport L494, "
                   "RR L405, Velar, Jaguar XE / XF X260 / F-Pace -- REAR pads only"),
        "cautions": [
            "NEVER compress a rear piston on these cars without service mode -- it "
            "destroys the in-caliper EPB actuator (expensive).",
            "Sources disagree whether the switch is held in RELEASE (down) or APPLY "
            "(up) in step 3 -- if one doesn't take, try the other.",
            "If the dance won't take at all, the tool route (e.g. iCarsoft LR 'EPB -> "
            "Pad Replacement') is the fallback.",
            "Wind the screw-type piston back with an EPB wind-back tool, not a "
            "C-clamp.",
        ],
        "procedure": [
            "Make sure the park brake is RELEASED (ignition on; pull the EPB switch "
            "while pressing the brake if needed -- red PARK icon must clear).",
            "Ignition ON via START without the brake (no engine).",
            "Hold the EPB switch ~2 s (release position; try apply if it won't take).",
            "Hold the accelerator to FULL throttle ~2 s and KEEP IT HELD.",
            "Cycle the ignition off/on (two START presses) while still holding.",
            "Listen for the rear motors winding back; the cluster shows 'park brake "
            "in service mode'.",
            "Do the pad job (wind-back tool on the screw piston).",
            "To exit: hold EPB switch (apply) + accelerator ~2 s and cycle the "
            "ignition -- or it exits automatically on the first engine start.",
        ],
    },
    {
        "id": "jlr-epb-cable",
        "name": "EPB service mode + the famous gearset trap (Disco 3/4, RRS L320, L322)",
        "description": (
            "These cars use a cable-pulled drum-in-hat park brake: the CALIPER is "
            "conventional (C-clamp fine for pads), but disc/shoe work needs the cable "
            "slackened in service mode -- a no-tool dance. The famous trap: the EPB "
            "actuator gearset burns out because of WORN SHOES / STRETCHED CABLES; "
            "replacing the actuator alone re-fails."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "roverparts LR3 notes; landroverforums 61830; iscantech; roverparts.eu",
        "models": "Discovery 3/4, Range Rover Sport L320, Range Rover L322",
        "cautions": [
            "One of the most misdiagnosed faults on these cars: an EPB motor that "
            "'failed' usually burned out from worn shoes/stretched cables. Service "
            "shoes AND cables together or the new actuator dies the same death.",
            "Do NOT start the engine mid-procedure -- service mode cancels on the "
            "first engine start.",
            "Rear PADS need no service mode on these cars (conventional caliper).",
        ],
        "procedure": [
            "Ignition ON (no engine).",
            "Hold the EPB switch in APPLY, wait ~2 s.",
            "Hold the accelerator to full throttle, wait ~2 s (keep both held).",
            "Cycle the ignition off/on, then release -- an audible cable release "
            "confirms service mode.",
            "Do the drum/shoe/cable work.",
            "Exit: normal EPB apply after the job (or first engine start).",
        ],
    },
    # ------------------------------------------------------------ honest tool-only
    {
        "id": "jlr-battery-bms",
        "name": "Battery replacement / BMS registration -- what to know (tool job)",
        "description": (
            "JLR cars from ~2010 run a Battery Management System with a smart "
            "alternator. After a battery swap the BMS must be told -- and that write "
            "lives in the BCM, which no generic dongle reaches. There is NO no-tool "
            "route; this card is the honest brief so the swap is done right."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "iCarsoft BMS guide; premium-diagnostics; ANCEL/Nexas coverage docs",
        "models": "All JLR ~2010+ with BMS/smart charging (most of the range)",
        "cautions": [
            "Skipping registration: the alternator charges the new battery as if it "
            "were the old worn one -- overcharges and cooks it, shortens its life, "
            "and can disable stop-start.",
            "Fit a same-spec AGM battery of the correct Ah -- not a standard flooded "
            "battery.",
        ],
        "procedure": [
            "Fit a same-spec AGM battery of the correct capacity (check the label on "
            "the old one).",
            "Have the new battery REGISTERED with a JLR-capable tool (GAP IIDTool, "
            "iCarsoft LR, Autel, or SDD/Pathfinder) -- there is no dash or dongle "
            "route.",
            "Symptoms if it was skipped: battery/charge warning, short new-battery "
            "life, stop-start dead.",
        ],
    },
    {
        "id": "jlr-dpf-drive-cycle",
        "name": "DPF regeneration drive cycle (Ingenium / TDV6 diesels)",
        "description": (
            "If the DPF-full light shows on an otherwise healthy diesel, a passive "
            "on-road regeneration usually completes with a sustained run -- the "
            "legitimate first move before reaching for a tool. Forced/static regen "
            "is tool-only AND a workshop procedure (hot exhaust, stationary)."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "kmotors 2.0D DPF; rcv.co.uk; westminstermotors; discosportforums",
        "models": "Ingenium 2.0D (XE/XF/F-Pace/Evoque/Disco Sport), TDV6/SDV6 diesels",
        "cautions": [
            "If the car is in limp mode or the light is flashing/red, stop -- that "
            "stage needs a tool (forced regen) or a workshop, not a drive.",
            "Sources disagree on an rpm requirement (one says engine speed doesn't "
            "matter, one says keep above ~1500 rpm) -- the sustained speed at "
            "operating temperature is the part everyone agrees on.",
            "Short urban runs are what filled the DPF -- one drive cycle won't fix "
            "a car that only ever does the school run.",
        ],
        "procedure": [
            "Warm the engine to normal operating temperature.",
            "Drive 20-30 minutes at a steady 50-70 mph on an A-road or motorway.",
            "The light should clear during or shortly after the run.",
            "If it returns quickly or won't clear: differential-pressure sensor, "
            "frequent-regen faults or a genuinely full filter -- tool/workshop time.",
        ],
    },
    {
        "id": "jlr-tpms-relearn",
        "name": "TPMS relearn (existing sensors) -- auto-learn drive",
        "description": (
            "JLR predominantly auto-learns its tyre-pressure sensors: after a "
            "rotation or pressure correction, a steady drive re-learns the IDs with "
            "no tool. NEW sensors are different -- registering new IDs needs a TPMS "
            "activation tool."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "mytpms JLR; tirereview; ateq-tpms (speed/time variants recorded)",
        "models": "JLR ~2005+ with factory TPMS",
        "cautions": [
            "Replacing a SENSOR (new IDs) is tool territory -- this relearn only "
            "re-associates existing sensors.",
            "Sources vary on speed/time (30 mph/10 min, 12 mph/15 min, 50 mph/10 "
            "min) -- the conservative version below covers them all.",
        ],
        "procedure": [
            "Set all tyres (including the spare where it has a sensor) to the "
            "door-placard pressures.",
            "Drive at a steady 25-30 mph or above for at least 15 minutes.",
            "The warning clears as the IDs re-learn; if it persists, a sensor is "
            "dead or new IDs need a tool.",
        ],
    },
    {
        "id": "jlr-eas-transport-mode",
        "name": "Air suspension: calibration & transport mode -- what to know (tool job)",
        "description": (
            "Two honest warnings for air-sprung JLR. (1) Ride-height CALIBRATION "
            "after suspension work is tool-only -- getting it wrong means wrong "
            "heights, lockout or airspring damage. (2) A bought-in/auction car can "
            "arrive in TRANSPORT MODE: suspension works but wipers, locks, interior "
            "lights or speedo act dead and the fob won't lock -- that's not an "
            "electrical fault, and the exit needs JLR-approved diagnostics."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "iCarsoft EAS calibration guide; roverparts iLand notes; dealer PDI reports",
        "models": "Air-sprung Land Rovers (Disco 3/4/5, RR Sport, RR, Velar options)",
        "cautions": [
            "A car 'stuck low / won't raise' is almost always a genuine fault "
            "(sensor, compressor, valve block, leak) -- there is no secret button "
            "unlock, and battery-disconnect 'resets' just mask it.",
            "'Not in customer mode' on the dash is a technician calibration state -- "
            "never hand a car over showing it.",
        ],
        "procedure": [
            "Suspect transport mode on a fresh trade-in/auction car whose wipers, "
            "locks, lights or speedo act dead while the suspension works: don't "
            "chase wiring -- book the PDI/transport-mode exit on JLR-approved "
            "diagnostics (dealers report it can take ~2 hours).",
            "After ANY height-sensor, strut, compressor or valve-block work: "
            "calibrate with a proper EAS-capable tool (GAP IIDTool / SDD) -- there "
            "is no dash route and no dongle route.",
        ],
    },
]

_BY_ID = {r["id"]: r for r in RESETS}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_resets() -> dict:
    return {"brand": "jlr", "resets": [uds_reset.public_view(r) for r in RESETS]}


def perform(
    adapter: Optional[dict], mode: str, reset_id: str, confirm: bool = False
) -> dict:
    """Run one JLR reset. Every shipped entry is a dash/menu procedure, so this
    returns the steps rather than touching the car. The UDS run path below is
    ready for any JLR routine whose id gets captured (11-bit addressing)."""
    reset = _BY_ID.get(reset_id)
    if reset is None:
        return {"ok": False, "message": f"Unknown JLR reset '{reset_id}'.", "timestamp": _now()}
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
                    "11-bit/500k CAN JLR diagnostics use. Ignition on and retry."
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
