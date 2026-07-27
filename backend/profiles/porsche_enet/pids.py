"""Porsche Taycan HV battery health over the ENET cable.

This is the feature the cable actually earns its keep for: a real state-of-health
number, cell balance and thermal spread from the battery control unit, before
the dealer commits money to a used EV.

The decoding lives in ``backend/profiles/porsche/battery.py`` and is shared with
the OBD-dongle path -- the battery answers the same DIDs whichever way you reach
it, only its address changes. Everything here is the DoIP-side plumbing.
"""

from datetime import datetime, timezone

from backend.core import doip_client, uds
from backend.profiles.porsche import battery
from . import ecus, scan as scan_mod


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_client():
    """Find the car and open an activated DoIP session.

    Returns (client, vehicle_dict). Raises RuntimeError with a message meant to
    be shown to a human when the car cannot be reached.
    """
    found = scan_mod.discover()
    if not found["found"]:
        raise RuntimeError(found["detail"])
    client = doip_client.DoipClient(found["ip"], source_address=ecus.TESTER_ADDRESS)
    vin = found.get("vin")
    if not vin:
        try:
            vin = uds.read_vin(client, ecus.GATEWAY_ADDRESS)
        except Exception:
            vin = None
    return client, {"vin": vin, "ip": found["ip"], "protocol": "DoIP"}


def _battery_report(mode: str, deep: bool) -> dict:
    if mode == "sim":
        return _simulated(deep)

    try:
        client, vehicle = open_client()
    except RuntimeError as exc:
        return {
            "ok": False,
            "simulated": False,
            "vehicle": None,
            "battery": None,
            "message": str(exc),
            "timestamp": _now(),
        }

    try:
        with client:
            power = client.power_mode()
            if power == "not ready":
                return {
                    "ok": False,
                    "simulated": False,
                    "vehicle": vehicle,
                    "battery": None,
                    "message": (
                        "The car is not electrically ready for diagnostics. "
                        "Switch the ignition fully ON (foot on the brake, press "
                        "start) and try again."
                    ),
                    "timestamp": _now(),
                }
            health = battery.read_health(
                client,
                addresses=battery.DOIP_ADDRESSES,
                vin=vehicle.get("vin"),
                deep=deep,
            )
    except battery.UnsafeRequest as exc:
        # Should be unreachable -- the guard exists so a future edit that tries
        # to send a routine at the pack fails loudly instead of quietly.
        return {
            "ok": False,
            "simulated": False,
            "vehicle": vehicle,
            "battery": None,
            "message": f"Blocked by the read-only guard: {exc}",
            "timestamp": _now(),
        }
    except (doip_client.DoipError, OSError) as exc:
        return {
            "ok": False,
            "simulated": False,
            "vehicle": vehicle,
            "battery": None,
            "message": (
                f"Lost the diagnostic connection: {exc}. The gateway sleeps after "
                "inactivity even with the ignition on -- cycle the ignition, wait "
                "5s and try again."
            ),
            "timestamp": _now(),
        }

    message = _headline(health)
    for warning in health.get("warnings") or []:
        message += " " + warning
    return {
        "ok": True,
        "simulated": False,
        "vehicle": vehicle,
        "battery": health,
        "readings": _readings(health),
        "message": message,
        "timestamp": _now(),
    }


def _readings(health: dict) -> list:
    """Project the battery report onto the {name, value, unit} chip contract the
    generic brand panel already renders. Only values that actually answered
    appear -- a missing reading is left out rather than shown as a blank or a
    zero, which would read as a measurement."""
    out = []

    def add(name, value, unit=""):
        if value is not None:
            out.append({"name": name, "value": value, "unit": unit})

    soh = health.get("soh", {})
    if soh.get("percent", {}).get("available"):
        add("State of health", soh["percent"]["value"], "%")
        if soh.get("band"):
            add("Health verdict", soh["band"]["label"])
    elif soh.get("supported") is False:
        add("State of health", "not decodable on this model year")

    cells = health.get("cells", {})
    if cells.get("deltaMillivolts", {}).get("available"):
        add("Cell spread", cells["deltaMillivolts"]["value"], "mV")
        add("Balance verdict", cells["deltaMillivolts"]["verdict"]["label"])
    for key, label in (("max", "Highest cell"), ("min", "Lowest cell")):
        entry = cells.get(key, {})
        if entry.get("available"):
            cell = entry["value"]
            suffix = f" (cell {cell['cell']})" if cell.get("cell") is not None else ""
            add(label, f"{cell['volts']} V{suffix}")

    thermal = health.get("thermal", {})
    if thermal.get("average", {}).get("available"):
        add("Pack temperature", thermal["average"]["value"], "°C")
    for key, label in (("max", "Hottest cell"), ("min", "Coldest cell")):
        entry = thermal.get(key, {})
        if entry.get("available"):
            add(label, entry["value"]["celsius"], "°C")

    electrical = health.get("electrical", {})
    for key, label, unit in (
        ("packVolts", "Pack voltage", "V"),
        ("packAmps", "Pack current", "A"),
    ):
        entry = electrical.get(key, {})
        if entry.get("available"):
            add(label, entry["value"], unit)

    charge = health.get("charge", {})
    for key, label, unit in (
        ("trueSoc", "Charge (battery's own)", "%"),
        ("displayedSoc", "Charge (dash)", "%"),
        ("presentEnergyKwh", "Energy in pack", "kWh"),
        ("impliedUsableKwh", "Implied usable capacity", "kWh"),
    ):
        entry = charge.get(key, {})
        if entry.get("available"):
            add(label, entry["value"], unit)
    implied = charge.get("impliedSohPercent", {})
    if implied.get("available"):
        estimates = implied["value"]
        add(
            "Health, estimated independently",
            " / ".join(f"{v}% ({k.replace('_', ' ')})" for k, v in estimates.items()),
        )

    history = health.get("history", {})
    if history.get("launchControlCount", {}).get("available"):
        add("Launch-control starts", history["launchControlCount"]["value"])
    if history.get("chargingMinutes", {}).get("available"):
        add("Lifetime charging", round(history["chargingMinutes"]["value"] / 60), "hours")

    cell_map = health.get("cellMap")
    if cell_map and cell_map.get("available"):
        add("Cells read", cell_map["count"])
        add("Spread across pack", cell_map["spreadMillivolts"], "mV")
        add("Cell voltage range", f"{cell_map['minVolts']} - {cell_map['maxVolts']} V")
        add("Standard deviation", cell_map["stdDevMillivolts"], "mV")
        weakest = (cell_map.get("weakest") or [None])[0]
        if weakest:
            add(
                "Weakest cell",
                f"{weakest['volts']} V (module {weakest['module']}, position {weakest['position']})",
            )
    if "modules" in health:
        present = sum(1 for m in health["modules"] if m.get("present"))
        add("Battery modules answering", f"{present} of {len(health['modules'])}")
    return out


def _headline(health: dict) -> str:
    """One sentence a dealer can act on, honest when the number is missing."""
    parts = []
    soh = health.get("soh", {}).get("percent", {})
    if soh.get("available"):
        band = health["soh"].get("band", {})
        parts.append(f"State of health {soh['value']}% ({band.get('label', 'read')})")
    elif health.get("soh", {}).get("supported") is False:
        parts.append("State of health not decodable on this model year")
    else:
        parts.append("State of health did not read")

    delta = health.get("cells", {}).get("deltaMillivolts", {})
    if delta.get("available"):
        parts.append(
            f"cell spread {delta['value']} mV ({delta['verdict']['label']})"
        )

    soc = health.get("charge", {}).get("trueSoc", {})
    temp = health.get("thermal", {}).get("average", {})
    context = []
    if soc.get("available"):
        context.append(f"{soc['value']}% charge")
    if temp.get("available"):
        context.append(f"{temp['value']} C")
    tail = f" Measured at {', '.join(context)}." if context else ""
    return ". ".join(parts) + "." + tail


def report(
    mode: str = "real",
    dealer: str = "",
    customer: str = "",
    mileage: str = "",
) -> tuple:
    """Build the customer-facing HV battery PDF.

    Returns ``(filename, pdf_bytes)``, or raises RuntimeError with a message
    meant for a human if the car could not be read.

    The read is taken FRESH rather than reusing whatever is on screen: a
    document handed to a customer carries its own timestamp, and it should be
    a measurement that actually happened at that moment. ``deep=True`` so the
    full 198-cell map backs the cell-balance section.
    """
    from . import report as report_mod

    data = _battery_report(mode, deep=True)
    if not data.get("ok"):
        raise RuntimeError(data.get("message") or "Could not read the battery.")

    pdf_bytes = report_mod.build(
        data.get("battery") or {},
        data.get("vehicle") or {},
        dealer=dealer,
        customer=customer,
        mileage=mileage,
    )
    vin = (data.get("vehicle") or {}).get("vin") or "unknown-vin"
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    prefix = "SIMULATED-" if data.get("simulated") else ""
    return f"{prefix}battery-report-{vin}-{stamp}.pdf", pdf_bytes


def live(mode: str = "real") -> dict:
    """Battery health: state of health, cell balance, temperatures, history."""
    return _battery_report(mode, deep=False)


def _simulated(deep: bool) -> dict:
    health = {
        "addresses": {k: f"0x{v:04X}" for k, v in battery.DOIP_ADDRESSES.items()},
        "soh": {
            "percent": {"value": 87.12, "available": True, "raw": "3a00"},
            "supported": True,
            "band": battery.soh_band(87.12),
        },
        "cells": {
            "max": {"value": {"volts": 3.579, "cell": 94}, "available": True},
            "min": {"value": {"volts": 3.543, "cell": 62}, "available": True},
            "deltaMillivolts": {
                "value": 36.0,
                "available": True,
                "verdict": battery.cell_delta_verdict(36.0),
            },
            "socMax": {"value": {"percent": 65, "cell": 94}, "available": True},
            "socMin": {"value": {"percent": 64, "cell": 62}, "available": True},
        },
        "thermal": {
            "max": {"value": {"celsius": 21, "cell": 9}, "available": True},
            "min": {"value": {"celsius": 19, "cell": 21}, "available": True},
            "average": {"value": 20, "available": True},
        },
        "electrical": {
            "packVolts": {"value": 722.5, "available": True},
            "cellSumVolts": {"value": 722.1, "available": True},
            "packAmps": {"value": -0.4, "available": True},
        },
        "charge": {
            "trueSoc": {"value": 62, "available": True},
            "displayedSoc": {"value": 58.4, "available": True},
            "presentEnergyKwh": {"value": 51.9, "available": True},
        },
        "history": {
            "launchControlCount": {"value": 2, "available": True},
            "chargingMinutes": {"value": 86651, "available": True},
        },
        "warnings": [],
        "notes": [
            "SIMULATED -- these are not readings from any real car.",
            "Read-only: state of health, cell and temperature values only.",
        ],
    }
    if deep:
        health["modules"] = [
            {
                "index": n + 1,
                "present": n < 28,
                "moduleId": 131 + n if n < 28 else None,
                "cells": (
                    [{"volts": round(3.55 + ((n * 7 + k) % 9) * 0.004, 4), "soc": 64}
                     for k in range(6)]
                    if n < 28 else []
                ),
                "temps": [20, 20, 21] if n < 28 else None,
                "reason": None if n < 28 else "not fitted -- this pack has fewer modules",
            }
            for n in range(33)
        ]
        health["cellMap"] = battery._summarise_cells(health["modules"])
    return {
        "ok": True,
        "simulated": True,
        "vehicle": {"vin": "WP0ZZZY1ZNSA00001", "ip": "169.254.10.20", "protocol": "DoIP"},
        "battery": health,
        "readings": _readings(health),
        "message": "SIMULATED -- " + _headline(health),
        "timestamp": _now(),
    }
