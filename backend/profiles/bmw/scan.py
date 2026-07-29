"""BMW full-module scan over ENET (HSFZ for F-series, DoIP probe for G-series).

Torque can only see the engine's generic OBD-II data; this walks every module
on the car through the gateway and reads each one's manufacturer-level fault
memory. Read-only apart from clear(), which requires explicit confirmation at
the API layer.
"""

import os
from datetime import datetime, timezone
from typing import List, Optional

from backend.core import doip, doip_client, hsfz, interfaces, uds

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


# --------------------------------------------------------------------------
# G-series: the same car, over DoIP instead of HSFZ
# --------------------------------------------------------------------------
# G-series BMWs and 2018-on MINIs dropped HSFZ for standard DoIP (ISO 13400),
# which core/doip_client.py already speaks -- it was proved on a real Taycan.
# What differs per manufacturer is ADDRESSING: DoIP routes to a 16-bit logical
# address, while every BMW tool, document and table in this repo works in the
# 8-bit diagnostic address (0x12 engine, 0x63 head unit).
#
# BMW's mapping is not published, so it is not guessed at either. Instead the
# candidates below are tried and THE CAR DECIDES: a DoIP gateway answers a
# message aimed at an address it does not route with NACK 0x03, "unknown target
# address" (doip_client.DoipNegativeAck.not_fitted). So a mapping that is wrong
# produces 0x03 for every module -- which is a definite answer, not silence --
# and the scan moves to the next candidate rather than reporting a car full of
# missing modules. Whichever mapping the car accepts is reported in the result,
# so the first real G-series scan settles this for good.
TESTER_ADDRESSES = [
    0x0EF4,  # BMW's own tester byte (0xF4, as HSFZ uses) in a DoIP high nibble
    0x00F4,  # the same byte, plain
    0x0E80,  # the ISO-13400 example tester address
]
ADDRESS_MAPPINGS = [
    ("low-byte", lambda addr: addr),           # 0x12 -> 0x0012
    ("0x0Exx", lambda addr: 0x0E00 | addr),    # 0x12 -> 0x0E12
    ("0x17xx", lambda addr: 0x1700 | addr),    # 0x12 -> 0x1712
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
        return _scan_doip(found)

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


def _doip_port() -> int:
    """The car's DoIP port. Overridable so the self-test can stand a fake
    gateway on loopback and drive this exact code path."""
    try:
        return int(os.environ.get("CARHARNESS_DOIP_PORT", doip_client.TCP_DATA_PORT))
    except ValueError:
        return doip_client.TCP_DATA_PORT


def _doip_connect(ip: str):
    """An activated DoIP session, trying BMW's tester addresses in turn.

    Returns (client, tester_address). Routing activation is where a wrong
    tester address shows up -- the entity rejects it outright -- so this is
    settled before a single module is probed.
    """
    last: Optional[Exception] = None
    for tester in TESTER_ADDRESSES:
        try:
            return doip_client.DoipClient(ip, port=_doip_port(), source_address=tester), tester
        except doip_client.DoipError as exc:
            last = exc
    raise last or doip_client.DoipError("No tester address was accepted.")


def _probe_mapping(client, mapping) -> tuple[list[int], int]:
    """Probe every module under one address mapping.

    Returns (addresses that answered, count the gateway called unknown). A
    mapping where the gateway calls every address unknown is the wrong
    mapping, not an empty car.
    """
    answered, unknown = [], 0
    for addr, _code, _name in ECU_TABLE:
        logical = mapping(addr)
        try:
            reply = client.request(logical, bytes([uds.SVC_TESTER_PRESENT, 0x00]), timeout=0.8)
        except doip_client.DoipNegativeAck as exc:
            if exc.not_fitted:
                unknown += 1
            continue
        except (doip_client.DoipError, OSError):
            continue
        if reply:
            answered.append(addr)
    return answered, unknown


def _scan_doip(found: dict) -> dict:
    """Walk a G-series car's modules over DoIP."""
    vehicle = {"vin": found["vin"], "ip": found["ip"], "protocol": "DoIP"}
    ip = found["ip"]
    if not ip:
        return {
            "ok": False, "simulated": False, "vehicle": vehicle, "modules": [],
            "message": (
                "The car announced itself over DoIP but without an address to "
                "connect back to. Re-seat the ENET cable and try again."
            ),
            "timestamp": _now(),
        }

    try:
        client, tester = _doip_connect(ip)
    except (doip_client.DoipError, OSError) as exc:
        return {
            "ok": False, "simulated": False, "vehicle": vehicle, "modules": [],
            "message": (
                f"The car answered DoIP but would not activate a diagnostic route: {exc}. "
                "Ignition on, and only one diagnostic tool connected at a time."
            ),
            "timestamp": _now(),
        }

    modules: List[dict] = []
    try:
        with client:
            chosen_name, chosen = None, None
            for name, mapping in ADDRESS_MAPPINGS:
                answered, unknown = _probe_mapping(client, mapping)
                if answered:
                    chosen_name, chosen = name, mapping
                    break
            if chosen is None:
                return {
                    "ok": False, "simulated": False, "vehicle": vehicle, "modules": [],
                    "message": (
                        "Connected to the car over DoIP and activated a route, but no "
                        "module answered under any of the address mappings tried "
                        f"({', '.join(n for n, _ in ADDRESS_MAPPINGS)}). BMW's DoIP "
                        "logical addresses are not published; this scan needs one "
                        "capture from a working G-series tool to pin them down. "
                        "Nothing is wrong with the car."
                    ),
                    "timestamp": _now(),
                }

            vin = found["vin"]
            for addr, code, name in ECU_TABLE:
                logical = chosen(addr)
                entry = {
                    "address": f"0x{addr:02X}",
                    "logicalAddress": f"0x{logical:04X}",
                    "code": code,
                    "name": name,
                    "present": False,
                    "dtcs": [],
                    "error": None,
                }
                if not uds.answers(client, logical):
                    modules.append(entry)
                    continue
                entry["present"] = True
                if not vin:
                    try:
                        vin = uds.read_vin(client, logical)
                    except Exception:
                        pass
                try:
                    entry["dtcs"] = uds.read_dtcs(client, logical)
                except uds.UdsNegativeResponse as exc:
                    entry["error"] = str(exc)
                except (doip_client.DoipError, OSError) as exc:
                    entry["error"] = str(exc)
                modules.append(entry)
    except (doip_client.DoipError, OSError) as exc:
        return {
            "ok": False, "simulated": False, "vehicle": vehicle, "modules": modules,
            "message": f"Lost the DoIP connection: {exc}",
            "timestamp": _now(),
        }

    vehicle["vin"] = vin
    vehicle["tester"] = f"0x{tester:04X}"
    vehicle["addressMapping"] = chosen_name
    present = [m for m in modules if m["present"]]
    faults = sum(len(m["dtcs"]) for m in present)
    return {
        "ok": True,
        "simulated": False,
        "vehicle": vehicle,
        "modules": modules,
        "message": (
            f"{len(present)} modules answered over DoIP, {faults} fault"
            f"{'s' if faults != 1 else ''} stored (tester 0x{tester:04X}, "
            f"'{chosen_name}' addressing). Read-only scan -- nothing was changed."
        ),
        "timestamp": _now(),
    }


def _clear_doip(found: dict, wanted: Optional[int]) -> dict:
    """Clear fault memory on a G-series car. Same address problem as the scan,
    solved the same way -- the mapping is settled against the car first, and a
    clear is only ever sent to an address that has answered."""
    cleared, errors = [], []
    try:
        client, _tester = _doip_connect(found["ip"])
    except (doip_client.DoipError, OSError) as exc:
        return {
            "ok": False, "simulated": False, "cleared": [],
            "message": f"Could not open a DoIP diagnostic route: {exc}",
            "timestamp": _now(),
        }
    try:
        with client:
            chosen = None
            for _name, mapping in ADDRESS_MAPPINGS:
                answered, _unknown = _probe_mapping(client, mapping)
                if answered:
                    chosen, present = mapping, answered
                    break
            if chosen is None:
                return {
                    "ok": False, "simulated": False, "cleared": [],
                    "message": (
                        "No module answered over DoIP, so nothing was cleared. The "
                        "logical-address mapping for this car is still unconfirmed."
                    ),
                    "timestamp": _now(),
                }
            targets = [wanted] if wanted is not None else present
            for addr in targets:
                try:
                    uds.clear_dtcs(client, chosen(addr))
                    cleared.append(f"0x{addr:02X}")
                except Exception as exc:
                    errors.append(f"0x{addr:02X}: {exc}")
    except (doip_client.DoipError, OSError) as exc:
        return {
            "ok": False, "simulated": False, "cleared": cleared,
            "message": f"Lost the DoIP connection: {exc}",
            "timestamp": _now(),
        }

    ok = bool(cleared) and not errors
    message = f"Cleared fault memory over DoIP on {', '.join(cleared) or 'no modules'}."
    if errors:
        message += " Failed: " + "; ".join(errors)
    return {
        "ok": ok, "simulated": False, "cleared": cleared,
        "message": message, "timestamp": _now(),
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
    if not found["found"]:
        return {
            "ok": False,
            "simulated": False,
            "cleared": [],
            "message": found["detail"],
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

    if found["protocol"] != "HSFZ":
        return _clear_doip(found, wanted)

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
