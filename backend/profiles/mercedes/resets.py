"""Mercedes-Benz service resets.

Research (docs/private/mercedes-research.md §5): there is NO public ELM327 frame
that resets the Mercedes ASSYST / ASSYST PLUS service indicator -- on Mercedes
that reset lives inside the instrument cluster / EZS as a proprietary routine
reached only by STAR/XENTRY (or the dash procedure), exactly like BMW's CBS,
VAG's ESI and PSA's BSI. So every Mercedes reset here is a ``procedure``: the app
walks the driver through the exact dash/menu steps and touches nothing on the car.

The good news is that the ASSYST dash procedures are unusually well documented
per era, so most ship at "verified". The steering-wheel-menu family shared by the
W164/166/169/245/246/221/251 ships "experimental" (UNVERIFIED-plausible) until the
exact per-model wording is confirmed. The W210 cluster-"0" method is "community".

There is also one entry that is NOT a reset: an SBC brake SAFETY WARNING for the
W211/W219/R230 era. Its ``procedure`` field is a warning, not a set of steps to
follow -- the harness does not deactivate SBC (that is STAR-only) and does not
endorse the community "let the car sleep" ritual.

If a real ISTA/XENTRY ASSYST-reset transaction is ever captured, add it as a
``uds`` reset (target cluster / EZS, standard 11-bit) and perform() below runs it
unchanged.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.obd import service as obd_service
from backend.obd import uds_reset
from backend.obd.uds_elm327 import Elm327UdsClient

RESETS: List[dict] = [
    {
        "id": "mercedes-assyst-w204",
        "name": "ASSYST PLUS service reset (W204 C-Class, 4-button wheel)",
        "description": (
            "Resets the ASSYST PLUS service counter on the W204 C-Class (2007-2014) "
            "through the cluster workshop menu. No dongle command exists for the "
            "Mercedes service reset, so this is the built-in steering-wheel procedure."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "iFixit W204; JustAnswer W204 4-button; MBWorld W204 reset guide",
        "models": "W204 C-Class (2007-2014) with the 4-button multifunction wheel",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
            "If asked, pick the correct oil grade (e.g. 229.5 / 229.51).",
        ],
        "procedure": [
            "Ignition to position 1 (engine off).",
            "Hold the Phone / Accept-call button (right side of the wheel) and, within "
            "1 s, also hold OK (left side); keep both held ~5 s until the workshop menu "
            "appears.",
            "Scroll to ASSYST PLUS and press OK.",
            "Select Full Service and press OK.",
            "Confirm Full Service and press OK.",
            "Answer \"Service carried out?\" -> Yes -> OK.",
            "Ignition off to finish.",
        ],
    },
    {
        "id": "mercedes-assyst-w212",
        "name": "ASSYST PLUS service reset (W212 E-Class)",
        "description": (
            "Resets the ASSYST PLUS service counter on the W212 E-Class (2009-2016) "
            "via the multifunction-wheel menu. Manual dash procedure -- no dongle "
            "command exists for the Mercedes service reset."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "Willand Service Centre; MBWorld W212 how-to",
        "models": "W212 E-Class (2009-2016) with the multifunction wheel",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
            "Select the oil grade (e.g. 229.5 / 229.51) if the menu asks.",
        ],
        "procedure": [
            "Ignition to position 1 (engine off).",
            "Hold Accept-call (right) and, within 5 s, hold OK (left).",
            "Scroll to ASSYST PLUS -> OK.",
            "Select Full Service -> OK (pick oil grade if asked).",
            "Confirm Full Service -> OK.",
            "Ignition off to finish.",
        ],
    },
    {
        "id": "mercedes-assyst-w211",
        "name": "ASSYST service reset (W211 E-Class)",
        "description": (
            "Resets the ASSYST service counter on the W211 E-Class (2002-2009) via "
            "the multifunction-wheel menu. Manual dash procedure -- no dongle command "
            "exists for the Mercedes service reset."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "anusedcar W211; MB Medic service-exceeded guide",
        "models": "W211 E-Class (2002-2009) with the multifunction wheel",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
            "See the SBC safety warning below before touching this car's brakes -- "
            "early W211 (2003-2006) is an SBC car.",
        ],
        "procedure": [
            "Ignition to position 2 (dash lit, engine off).",
            "Press and hold the Call-answer (phone) button and OK together, ~5-10 s, "
            "until the service menu appears.",
            "Navigate to the ASSYST / Service item and confirm.",
            "Ignition off to finish.",
        ],
    },
    {
        "id": "mercedes-assyst-w203-menu",
        "name": "ASSYST PLUS service reset (W203 C / W209 CLK, 4-button menu)",
        "description": (
            "Resets the ASSYST PLUS counter on the W203 C-Class / W209 CLK (2000-2007) "
            "with the 4-button wheel, through the cluster workshop menu. Manual dash "
            "procedure -- no dongle command exists."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "MBWorld \"latest guide - resetting service indicator (most models)\"",
        "models": "W203 C-Class / W209 CLK (2000-2007) with the 4-button wheel",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
            "The button labels vary by wheel -- [b]=menu, [A]/R and [C]/+ are the "
            "left/right pair.",
        ],
        "procedure": [
            "Ignition to position 1.",
            "Press [b] (menu) repeatedly until the odometer is shown.",
            "Hold [A]/R then, within 1 s, also hold [C]/+ ~5 s until the voltage is "
            "shown, then release.",
            "Press [b] -> \"ASSYST PLUS\".",
            "Press [A] (Service data) -> press [b] (Full service) -> press [A].",
            "Press [b] to \"Confirm service?\" -> press [A] to complete.",
            "Ignition off to finish.",
        ],
    },
    {
        "id": "mercedes-assyst-w203-button",
        "name": "Service reset (W203 older, reset-button wheel)",
        "description": (
            "Resets the service reminder on an older W203 with a reset-button (not "
            "4-button) wheel. Manual dash procedure -- no dongle command exists."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": "MBWorld \"latest guide - resetting service indicator (most models)\"",
        "models": "W203 C-Class (older, reset-button wheel)",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
            "Some cars need the door closed before the key is inserted.",
        ],
        "procedure": [
            "Ignition to position 2.",
            "Press up/down until the service message is shown.",
            "Hold the RESET button ~3 s until the signal sounds.",
            "Ignition off to finish.",
        ],
    },
    {
        "id": "mercedes-assyst-w210",
        "name": "Service reset (W210 E-Class, cluster \"0\" button)",
        "description": (
            "Resets the service reminder on the W210 E-Class (1997-2002) using the "
            "cluster \"0\" / R button. Manual dash procedure -- no dongle command exists."
        ),
        "method": "procedure",
        "confidence": "community",
        "source": "MBWorld \"latest guide - resetting service indicator (most models)\"",
        "models": "W210 E-Class (1997-2002)",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
            "Community-sourced wording -- confirm against the car before relying on it.",
        ],
        "procedure": [
            "Ignition on.",
            "Hold the cluster \"0\" (R) button ~10 s until the signal sounds and the "
            "reset distance is shown.",
            "Ignition off to finish.",
        ],
    },
    {
        "id": "mercedes-assyst-plus-family",
        "name": "ASSYST PLUS service reset (W164/166 ML/GLE, W169 A, W245/246 B, W221 S, W251 R)",
        "description": (
            "The shared ASSYST PLUS steering-wheel-menu reset for the W164/166 ML/GLE, "
            "W169 A-Class, W245/246 B-Class, W221 S-Class and W251 R-Class. Same "
            "Accept-call + OK held through ignition-on -> ASSYST PLUS -> Full Service -> "
            "confirm flow as the W204/W212; higher-spec cars reach it via the "
            "cluster/COMAND service menu instead. Manual dash procedure."
        ),
        "method": "procedure",
        "confidence": "experimental",
        "source": "MB Medic; general ASSYST-PLUS family guides",
        "models": "W164/166 ML/GLE, W169 A, W245/246 B, W221 S, W251 R",
        "cautions": [
            "UNVERIFIED-plausible: this is the ASSYST-PLUS family pattern, but the exact "
            "wording and button labels vary per model -- confirm against the car's own "
            "menu before relying on it.",
            "Reset AFTER the oil + filter change, not before.",
        ],
        "procedure": [
            "Ignition to position 1 (engine off).",
            "Hold Accept-call and, within ~1 s, hold OK; keep both held until the "
            "workshop/service menu appears (on COMAND cars open the cluster or COMAND "
            "service menu instead).",
            "Scroll to ASSYST PLUS -> OK.",
            "Select Full Service -> OK.",
            "Confirm the service -> OK.",
            "Ignition off to finish.",
        ],
    },
    {
        "id": "mercedes-sbc-safety",
        "name": "SBC brakes -- do NOT change pads without deactivation (W211/W219/R230)",
        "description": (
            "SAFETY WARNING, not a reset: cars with Sensotronic Brake Control (SBC) -- "
            "W211 (2003-2006), W219 CLS, R230 SL -- must have SBC DEACTIVATED before the "
            "brakes are touched. SBC holds high hydraulic pressure and can clamp the "
            "calliper during a pad change. The harness does NOT do this."
        ),
        "method": "procedure",
        "confidence": "verified",
        "source": ("UOBDII / OBDII365 SBC deactivate (STAR/XENTRY); MBClub & benzworld "
                   "W211 pads-without-STAR threads"),
        "models": "W211 E-Class (2003-2006), W219 CLS, R230 SL",
        "cautions": [
            "SBC deactivation is STAR/XENTRY-only (a password-gated special function) -- "
            "it CANNOT be done over an OBD dongle, and this harness does not attempt it.",
            "The community \"let the car sleep\" ritual (lock the car, keep the key well "
            "away, doors closed so SBC bleeds down) is a workaround, NOT the official "
            "procedure -- the harness does not endorse it.",
            "Send the car to a specialist with STAR/XENTRY before any brake work on these "
            "models.",
        ],
        "procedure": [
            "WARNING: this is not a procedure to run -- it is a safety note.",
            "If this is a W211 (2003-2006) / W219 CLS / R230 SL, its brakes use SBC.",
            "SBC holds high hydraulic pressure; changing pads without deactivating it can "
            "clamp the calliper on your hand.",
            "Deactivation ('Deactivate SBC / SBC Hold-Stop') is a STAR/XENTRY special "
            "function only -- not doable over a dongle.",
            "Take the car to a specialist with STAR/XENTRY for any brake work.",
        ],
    },
]

_BY_ID = {r["id"]: r for r in RESETS}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_resets() -> dict:
    return {"brand": "mercedes", "resets": [uds_reset.public_view(r) for r in RESETS]}


def perform(
    adapter: Optional[dict], mode: str, reset_id: str, confirm: bool = False
) -> dict:
    """Run one Mercedes reset. Every shipped entry is a dash/menu procedure (and
    one is a pure safety warning), so this returns the steps rather than touching
    the car. The UDS run path below is ready for any Mercedes cluster/EZS routine
    whose id gets captured."""
    reset = _BY_ID.get(reset_id)
    if reset is None:
        return {"ok": False, "message": f"Unknown Mercedes reset '{reset_id}'.", "timestamp": _now()}
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
                    "11-bit/500k CAN Mercedes diagnostics use. Ignition on and retry."
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
