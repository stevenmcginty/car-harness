"""BMW full-module scan over ENET (HSFZ for F-series, DoIP probe for G-series).

Torque can only see the engine's generic OBD-II data; this walks every module
on the car through the gateway and reads each one's manufacturer-level fault
memory. Read-only apart from clear(), which requires explicit confirmation at
the API layer.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.core import doip, hsfz, interfaces, uds

# F-series diagnostic addresses, incl. UKL-platform MINIs (F54/F55/F56/F60)
# where the BDC took over the 0x40 body slot. Probing an address that isn't
# fitted is harmless -- the gateway just never answers and we mark it
# "not fitted".
ECU_TABLE = [
    (0x10, "ZGW", "Central gateway"),
    (0x12, "DME/DDE", "Engine"),
    (0x18, "EGS", "Automatic transmission"),
    (0x01, "ACSM", "Airbag / crash safety"),
    (0x29, "DSC", "ABS / stability control"),
    (0x30, "EPS", "Power steering"),
    (0x40, "FEM/BDC", "Body electronics"),
    (0x72, "REM", "Rear body electronics"),
    (0x60, "KOMBI", "Instrument cluster"),
    (0x63, "HU", "Head unit (nav / radio)"),
    (0x62, "HU-L", "Head unit (basic radio, if fitted)"),
    (0x78, "IHKA", "Climate control"),
    (0x5D, "KAFAS", "Camera / driver assistance (if fitted)"),
    (0x5E, "GWS", "Gear selector (if fitted)"),
    (0x56, "FZD", "Roof function centre (if fitted)"),
    (0x67, "ZBE", "iDrive controller (if fitted)"),
    (0x3D, "HUD", "Head-up display (if fitted)"),
    (0x71, "AHM", "Trailer module (if fitted)"),
    (0x36, "TCB", "Telematics (if fitted)"),
    (0x21, "VDM", "Adaptive suspension (if fitted)"),
    (0x64, "PMA", "Parking assistant (if fitted)"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def discover() -> dict:
    """Find the car on the ENET cable: HSFZ first (F-series), then DoIP.

    Returns {found, protocol, ip, vin, detail} -- read-only either way.
    """
    candidate = interfaces.best_candidate()
    if candidate is None:
        return {
            "found": False,
            "protocol": None,
            "ip": None,
            "vin": None,
            "detail": (
                "No link-local (169.254.x.x) Ethernet adapter found. Plug the ENET "
                "cable into the car's OBD port and the laptop, then wait ~30s for "
                "the adapter to settle."
            ),
        }

    ident = hsfz.identify(candidate)
    if ident["reached"]:
        return {
            "found": True,
            "protocol": "HSFZ",
            "ip": ident["ip"],
            "vin": ident["vin"],
            "detail": ident["detail"].strip(),
        }

    probe = doip.probe(candidate)
    if probe["reached_doip"]:
        return {
            "found": True,
            "protocol": "DoIP",
            "ip": probe.get("raw_from_ip"),
            "vin": probe.get("vin"),
            "detail": probe["detail"].strip(),
        }

    return {
        "found": False,
        "protocol": None,
        "ip": None,
        "vin": None,
        "detail": (
            "ENET adapter is up but the car answered neither HSFZ (F-series) nor "
            "DoIP (G-series). Check the cable is fully seated, turn the ignition "
            "on, and allow Python through Windows Firewall."
        ),
    }


def scan(mode: str = "real") -> dict:
    if mode == "sim":
        return _simulated_scan()

    found = discover()
    if not found["found"]:
        return {
            "ok": False,
            "simulated": False,
            "vehicle": None,
            "modules": [],
            "message": found["detail"],
            "timestamp": _now(),
        }
    if found["protocol"] != "HSFZ":
        return {
            "ok": False,
            "simulated": False,
            "vehicle": {"vin": found["vin"], "ip": found["ip"], "protocol": found["protocol"]},
            "modules": [],
            "message": (
                "Car answered DoIP (G-series). The full-module scan currently "
                "supports HSFZ (F-series) -- DoIP module scanning is the next job."
            ),
            "timestamp": _now(),
        }

    modules: List[dict] = []
    vin = found["vin"]
    try:
        with hsfz.HsfzClient(found["ip"]) as client:
            if not vin:
                try:
                    vin = uds.read_vin(client)
                except Exception:
                    pass
            for addr, code, name in ECU_TABLE:
                entry = {
                    "address": f"0x{addr:02X}",
                    "code": code,
                    "name": name,
                    "present": False,
                    "dtcs": [],
                    "error": None,
                }
                if not uds.answers(client, addr):
                    modules.append(entry)
                    continue
                entry["present"] = True
                try:
                    entry["dtcs"] = uds.read_dtcs(client, addr)
                except uds.UdsNegativeResponse as exc:
                    entry["error"] = str(exc)
                except hsfz.HsfzError as exc:
                    entry["error"] = str(exc)
                modules.append(entry)
    except (hsfz.HsfzError, OSError) as exc:
        return {
            "ok": False,
            "simulated": False,
            "vehicle": {"vin": vin, "ip": found["ip"], "protocol": "HSFZ"},
            "modules": modules,
            "message": f"Lost the diagnostic connection: {exc}",
            "timestamp": _now(),
        }

    present = [m for m in modules if m["present"]]
    faults = sum(len(m["dtcs"]) for m in present)
    return {
        "ok": True,
        "simulated": False,
        "vehicle": {"vin": vin, "ip": found["ip"], "protocol": "HSFZ"},
        "modules": modules,
        "message": (
            f"{len(present)} modules answered, {faults} fault"
            f"{'s' if faults != 1 else ''} stored. Read-only scan -- nothing was "
            "changed on the car."
        ),
        "timestamp": _now(),
    }


def clear(mode: str = "real", address: Optional[str] = None) -> dict:
    """Clear fault memory on one module (address like \"0x63\") or all present."""
    if mode == "sim":
        return {
            "ok": True,
            "simulated": True,
            "cleared": [address or "all"],
            "message": "SIMULATED -- nothing was cleared on any real car.",
            "timestamp": _now(),
        }

    found = discover()
    if not found["found"] or found["protocol"] != "HSFZ":
        return {
            "ok": False,
            "simulated": False,
            "cleared": [],
            "message": found["detail"] if not found["found"] else (
                "Clear over DoIP is not supported yet."
            ),
            "timestamp": _now(),
        }

    wanted: Optional[int] = None
    if address:
        try:
            wanted = int(address, 16)
        except ValueError:
            return {
                "ok": False,
                "simulated": False,
                "cleared": [],
                "message": f"Bad module address '{address}' (expected hex like 0x63).",
                "timestamp": _now(),
            }

    cleared, errors = [], []
    try:
        with hsfz.HsfzClient(found["ip"]) as client:
            targets = [wanted] if wanted is not None else [
                addr for addr, _, _ in ECU_TABLE if uds.answers(client, addr)
            ]
            for addr in targets:
                try:
                    uds.clear_dtcs(client, addr)
                    cleared.append(f"0x{addr:02X}")
                except Exception as exc:
                    errors.append(f"0x{addr:02X}: {exc}")
    except (hsfz.HsfzError, OSError) as exc:
        return {
            "ok": False,
            "simulated": False,
            "cleared": cleared,
            "message": f"Lost the diagnostic connection: {exc}",
            "timestamp": _now(),
        }

    ok = bool(cleared) and not errors
    message = f"Cleared fault memory on {', '.join(cleared) or 'no modules'}."
    if errors:
        message += " Failed: " + "; ".join(errors)
    return {
        "ok": ok,
        "simulated": False,
        "cleared": cleared,
        "message": message,
        "timestamp": _now(),
    }


def _simulated_scan() -> dict:
    def mod(addr, code, name, present, dtcs=None):
        return {
            "address": addr,
            "code": code,
            "name": name,
            "present": present,
            "dtcs": dtcs or [],
            "error": None,
        }

    modules = [
        mod("0x10", "ZGW", "Central gateway", True),
        mod("0x12", "DME/DDE", "Engine", True),
        mod("0x29", "DSC", "ABS / stability control", True),
        mod("0x60", "KOMBI", "Instrument cluster", True),
        mod(
            "0x63",
            "HU",
            "Head unit (nav / radio)",
            True,
            [
                {
                    "code": "B7F203",
                    "hex": "B7F203",
                    "failureType": "03",
                    "status": "2F",
                    "flags": ["test failed", "confirmed"],
                },
                {
                    "code": "B7F821",
                    "hex": "B7F821",
                    "failureType": "21",
                    "status": "08",
                    "flags": ["confirmed"],
                },
            ],
        ),
        mod("0x78", "IHKA", "Climate control", True),
        mod("0x21", "VDM", "Adaptive suspension (if fitted)", False),
        mod("0x64", "PMA", "Parking assistant (if fitted)", False),
    ]
    return {
        "ok": True,
        "simulated": True,
        "vehicle": {"vin": "SIMULATED00000BMW", "ip": "169.254.10.20", "protocol": "HSFZ"},
        "modules": modules,
        "message": "SIMULATED -- 5 modules answered, 2 faults stored. No real car connected.",
        "timestamp": _now(),
    }
