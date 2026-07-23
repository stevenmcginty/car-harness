"""Porsche service resets.

Research (docs/private/porsche-research-modules.md §8): Porsche reset *actuations*
live inside proprietary tools (PIWIS, Durametric) and no raw dongle 2E/31 frame is
public -- the same situation as BMW's CBS, PSA's BSI, VAG's ESI and JLR's SDD. So
every entry here is a ``procedure``: the app walks the driver through the exact
dash/menu/stalk steps and touches nothing on the car. That is the house rule --
verified DASH-BUTTON procedures beat speculative dongle frames -- and dash
procedures don't care about the K-line, TP2.0 or DoIP walls, so they work on every
era that has a button method, dongle or not.

Two verified dash routes run through the range: the 997/991 analog-cluster
trip-button hold, and the Macan/Cayenne (2018+ MK3) steering-wheel Maintenance
menu. The honest tool-only verdicts (996/986, Cayenne 955/957, Macan MK2 2022+ /
Taycan) ship as WARN-style cards -- the steps explain the situation instead of
pretending a dongle can do it. Porsche is more involved than most brands here:
unlike a mainstream car's quick button combo, several eras genuinely need a scan
tool, and the cards say so.

If a real PIWIS/Durametric transaction is ever captured, add it as a ``uds`` reset
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
        "id": "porsche-oil-997-991",
        "name": "Oil / service reset (911 997 / 991, analog cluster) -- trip button",
        "description": (
            "Resets the oil-service reminder on the 997 and 991 (analog instrument "
            "cluster) with the trip-odometer button through ignition. No dongle "
            "command exists for any Porsche service reset, so this is the built-in "
            "method."
        ),
        "method": "procedure",
        "confidence": "community",
        "source": "rennlist 997-forum 1029307; multiple 997/991 oil-service-reset threads",
        "models": "911 997.1/997.2 (2005-2012), 991.1/991.2 (2012-2019) with the analog cluster",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
            "Cars with the full digital cluster use the steering-wheel Maintenance "
            "menu instead (see the Macan/Cayenne entry).",
        ],
        "procedure": [
            "Ignition ON to accessory (dash lit), engine OFF.",
            "Press and HOLD the trip-odometer reset button on the instrument cluster.",
            "Keep it held until the display shows an 'Oil Service' / 'Service Reset' "
            "prompt.",
            "Follow the on-screen prompt to confirm the reset (press/hold as it asks).",
            "Ignition OFF then ON and check the reminder is gone.",
        ],
    },
    {
        "id": "porsche-oil-macan-cayenne-menu",
        "name": "Oil / service reset (Macan, Cayenne 2018+ MK3) -- steering-wheel menu",
        "description": (
            "Resets the maintenance reminder on the newer steering-wheel-menu cars "
            "through the on-board computer's Maintenance / Service Intervals screen. "
            "No dongle command exists."
        ),
        "method": "procedure",
        "confidence": "community",
        "source": "macanforum 168049; 6speedonline cayenne-958 330525; Macan/Cayenne forums",
        "models": "Macan 95B, Cayenne (2018+ MK3) and later cars with the steering-wheel menu",
        "cautions": [
            "Reset AFTER the oil + filter change, not before.",
            "The Macan MK2 (2022+) and Taycan do NOT have this dash route -- they need "
            "a scan tool (see that entry).",
        ],
        "procedure": [
            "Ignition ON (engine OFF).",
            "Use the steering-wheel buttons to open the on-board computer menu.",
            "Go to Maintenance / Service Intervals.",
            "Select the item to reset (Oil Service / Inspection / Brake Fluid).",
            "HOLD the confirm button until the cluster confirms the reset.",
            "Ignition OFF then ON and check the reminder is gone.",
        ],
    },
    # ------------------------------------------------------------ honest tool-only
    {
        "id": "porsche-oil-996-986",
        "name": "Service reset (911 996 / Boxster 986) -- tool required",
        "description": (
            "The 996 and 986 have NO reliable button dance for the service reset -- "
            "historically it needs PST2 / Durametric or an address-aware handheld. "
            "Their service-interval data lives on the K-line buses (K1/pin 7, K2/pin "
            "3), not on generic OBD, so a plain dongle can't read or reset it."
        ),
        "method": "procedure",
        "confidence": "community",
        "source": "996/986 forums -- 'no button sequence, needs a tool'; Durametric coverage",
        "models": "911 996 (1998-2005), Boxster 986 (1997-2004)",
        "cautions": [
            "There is no dash or dongle route -- don't keep trying button combos.",
            "The reset needs PST2, Durametric, or an address-aware handheld (Foxwell/"
            "iCarsoft POR).",
        ],
        "procedure": [
            "Change the oil + filter first.",
            "Reset the service interval with PST2, Durametric, or an address-aware "
            "handheld (Foxwell NT530 / iCarsoft POR) -- there is no dash or dongle "
            "route on these cars.",
        ],
    },
    {
        "id": "porsche-oil-cayenne-955-957",
        "name": "Service reset (Cayenne 955 / 957) -- usually tool required",
        "description": (
            "The first-gen Cayenne (955/957) is a VW Touareg underneath. Reports are "
            "mixed: some owners describe a menu/stalk method, but most report the "
            "reset needs a tool (Durametric or a VAG-capable scanner). The enhanced "
            "diagnostics are on VW TP2.0, which a plain BLE dongle can't hold."
        ),
        "method": "procedure",
        "confidence": "experimental",
        "source": "6speedonline cayenne-955/957 threads -- mixed reports (menu vs tool)",
        "models": "Cayenne 955 / 9PA (2003-2006), Cayenne 957 (2007-2010)",
        "cautions": [
            "Reports genuinely disagree -- try the on-board-computer Service menu "
            "first, but expect to need a tool (Durametric / VAG-capable scanner).",
            "Reset AFTER the service, not before.",
        ],
        "procedure": [
            "Change the oil + filter first.",
            "Try the on-board computer -> Service / Maintenance menu and hold the "
            "reset/confirm if the screen offers it.",
            "If the menu route refuses (most cars): reset with Durametric or a "
            "VAG-capable scanner -- the TP2.0 enhanced side is out of reach for a "
            "plain dongle.",
        ],
    },
    {
        "id": "porsche-oil-macan-mk2-taycan",
        "name": "Service reset (Macan MK2 2022+, Taycan, newest) -- scanner required",
        "description": (
            "The newest cars -- Macan MK2 (2022+), Taycan, and the current-generation "
            "models -- cannot have the service reminder reset without a scan tool per "
            "multiple owner reports. These are DoIP-era cars; deep functions are "
            "PIWIS-III / address-aware-tool territory, not a dash procedure."
        ),
        "method": "procedure",
        "confidence": "community",
        "source": "Macan/Taycan forums -- 'cannot be reset without a scanner'",
        "models": "Macan MK2 (2022+), Taycan (2019+), current-generation Porsche",
        "cautions": [
            "There is no dash button combo on these cars -- don't keep trying.",
            "The reset needs an address-aware scan tool (iCarsoft POR / Foxwell / "
            "PIWIS III).",
        ],
        "procedure": [
            "Change the oil + filter first.",
            "Reset the service reminder with an address-aware scan tool (iCarsoft POR, "
            "Foxwell, or PIWIS III) -- these DoIP-era cars have no dash or dongle "
            "route.",
        ],
    },
]

_BY_ID = {r["id"]: r for r in RESETS}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_resets() -> dict:
    return {"brand": "porsche", "resets": [uds_reset.public_view(r) for r in RESETS]}


def perform(
    adapter: Optional[dict], mode: str, reset_id: str, confirm: bool = False
) -> dict:
    """Run one Porsche reset. Every shipped entry is a dash/menu procedure, so this
    returns the steps rather than touching the car. The UDS run path below is ready
    for any Porsche routine whose id gets captured (11-bit addressing)."""
    reset = _BY_ID.get(reset_id)
    if reset is None:
        return {"ok": False, "message": f"Unknown Porsche reset '{reset_id}'.", "timestamp": _now()}
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
                    "11-bit/500k CAN Porsche diagnostics use. Ignition on and retry."
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
