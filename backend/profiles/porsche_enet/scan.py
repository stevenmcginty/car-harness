"""Porsche full-module scan over the ENET cable (DoIP, ISO 13400).

This is the Taycan's deep path. A generic OBD dongle sees the emissions data an
EV barely has; this walks every module the gateway will route to -- inverters,
on-board charger, DC-DC, thermal, chassis, body, driver assistance -- and reads
each one's own fault memory.

HOW IT FINDS MODULES. It does not trust a table. It asks the car:

  1. one functional broadcast (TesterPresent to 0xE400) -- every fitted module
     answers at once, which is the fastest honest inventory there is;
  2. an address sweep of 0x4000-0x40FF for anything the broadcast missed,
     which is cheap because the gateway NACKs an empty address immediately
     rather than making us wait for a timeout;
  3. per responder, DID 0x2A30 and 0xF19E so the module names itself.

The static table in ecus.py only supplies display names. A module we have no
name for is still reported, with whatever it says about itself.

WHY DoIP IS MORE HONEST THAN HSFZ HERE. The gateway distinguishes "there is no
such module on this car" (NACK 0x03) from "the module is fitted but did not
answer" (NACK 0x06). BMW's HSFZ gives silence for both. The scan reports that
difference rather than flattening it.

Read-only apart from clear(), which is confirm-gated at the API layer and
refuses high-voltage modules outright -- see clear().
"""

import time
from datetime import datetime, timezone
from typing import List, Optional

from backend.core import doip, doip_client, dtc_scheme, interfaces, uds
from . import ecus

# Modules where clearing fault memory destroys the evidence a purchase decision
# rests on -- and, on an 800 V car, where stored faults are the only record that
# something went wrong. Reads are always allowed; clears are not.
HV_MODULES = {
    0x407B,  # HV battery management
    0x4044,  # on-board charger
    0x40B7,  # DC-DC converter
    0x40C7,  # HV booster
    0x407C,  # front inverter
    0x40B8,  # rear inverter
}

# DTC status bits worth showing: test failed (0x01), pending (0x04),
# confirmed (0x08). Mask 0xFF is what Porsche's own tooling asks for, but it
# returns every self-test entry the module has ever defined -- thousands of
# them -- so filtering to real faults is mandatory, not cosmetic.
REAL_FAULT_BITS = 0x0D

SCAN_WARN_SECONDS = 300  # a session this long is worth warning about (12V drain)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def discover() -> dict:
    """Find a DoIP car on the ENET cable. Read-only UDP broadcast only."""
    candidate = interfaces.best_candidate()
    if candidate is None:
        return {
            "found": False,
            "protocol": None,
            "ip": None,
            "vin": None,
            "detail": (
                "No link-local (169.254.x.x) Ethernet adapter found. Plug the ENET "
                "cable into the car's OBD port and the laptop's USB-Ethernet "
                "adapter, switch the ignition fully ON (foot on the brake, start "
                "button -- accessory mode is not enough), then give Windows ~30s "
                "to settle on a link-local address."
            ),
        }

    probe = doip.probe(candidate)
    if probe["reached_doip"]:
        return {
            "found": True,
            "protocol": "DoIP",
            "ip": probe.get("raw_from_ip"),
            "vin": probe.get("vin"),
            "gatewayAddress": probe.get("logical_address"),
            "detail": probe["detail"].strip(),
        }

    return {
        "found": False,
        "protocol": None,
        "ip": None,
        "vin": None,
        "detail": (
            "The Ethernet adapter is up but no car announced itself over DoIP. "
            "Check the cable is fully seated, the ignition is fully ON, and that "
            "Python is allowed through Windows Firewall on this network. On a "
            "Porsche also check the cable's pin 8 activation line is wired -- "
            "some ENET cables ship with it disabled."
        ),
    }


def _read_ident(client, address: int) -> dict:
    """Ask a module to identify itself. Every field is optional and honest."""
    ident: dict = {}
    for did, key in ecus.IDENT_DIDS:
        try:
            data = uds.read_did(client, address, did, timeout=1.5)
        except Exception:
            continue
        if not data:
            continue
        if key == "selfIdentification":
            decoded = ecus.decode_self_id(data)
            if decoded:
                ident.update(decoded)
            continue
        text = "".join(chr(b) for b in data if 32 <= b < 127).strip()
        if text:
            ident[key] = text
    return ident


def _read_faults(client, address: int) -> tuple:
    """Fault memory for one module. Returns (dtcs, suppressed_count, error).

    Mask ladder 0xFF -> 0x0C -> 0x08: Porsche's own tooling uses 0xFF, but a
    picky module may only accept a narrower mask.
    """
    last_error = None
    payload = None
    for mask in (0xFF, 0x0C, 0x08):
        try:
            raw = client.request(
                address, bytes([uds.SVC_READ_DTC, 0x02, mask]), timeout=4.0
            )
            if raw and raw[0] == 0x7F:
                last_error = f"negative response 0x{raw[2]:02X}" if len(raw) >= 3 else "negative response"
                continue
            if raw and raw[0] == uds.SVC_READ_DTC + 0x40:
                payload = raw
                break
        except Exception as exc:
            last_error = str(exc)
            continue
    if payload is None:
        return [], 0, last_error

    availability = payload[2] if len(payload) >= 3 else 0xFF
    records = payload[3:]
    dtcs, suppressed = [], 0
    for i in range(0, len(records) - 3, 4):
        b0, b1, b2, status = records[i : i + 4]
        if b0 == b1 == b2 == 0:
            continue
        if not status & REAL_FAULT_BITS:
            suppressed += 1  # a defined-but-never-failed self-test entry
            continue
        entry = uds.decode_dtc(b0, b1, b2)
        # Porsche numbers its own faults; the J2012 conversion is structurally
        # right but its generic meaning does not apply here. See core/dtc_scheme.
        dtc_scheme.mark(entry, "porsche_enet")
        entry["status"] = f"{status:02X}"
        entry["statusAvailability"] = f"{availability:02X}"
        entry["flags"] = uds.status_flags(status, availability)
        entry["state"] = uds.fault_state(status, availability)
        dtcs.append(entry)
    return dtcs, suppressed, None


def _present_addresses(client) -> tuple:
    """(addresses, how) -- who is on this car, found the cheapest way first."""
    found: List[int] = []
    try:
        responders = client.functional_probe(
            ecus.FUNCTIONAL_ADDRESS, bytes([uds.SVC_TESTER_PRESENT, 0x00]), window=2.5
        )
        found = [r["address"] for r in responders]
    except Exception:
        found = []

    if found:
        return sorted(set(found)), "functional broadcast"

    # No broadcast answer: sweep. Absent addresses NACK immediately, so this
    # costs far less than it looks.
    swept: List[int] = []
    for address in range(ecus.SWEEP_START, ecus.SWEEP_END + 1):
        try:
            if client.request(address, bytes([uds.SVC_TESTER_PRESENT, 0x00]), timeout=0.5):
                swept.append(address)
        except doip_client.DoipNegativeAck:
            continue  # not fitted, or fitted and asleep -- either way, no data
        except Exception:
            continue
    # Addresses outside the sweep window that the table knows about.
    for _name, address, _vag, _asam in ecus.TAYCAN_ECUS:
        if address > ecus.SWEEP_END and address not in swept:
            try:
                if client.request(address, bytes([uds.SVC_TESTER_PRESENT, 0x00]), timeout=0.5):
                    swept.append(address)
            except Exception:
                continue
    return sorted(set(swept)), "address sweep"


def scan(mode: str = "real") -> dict:
    if mode == "sim":
        return _simulated_scan()

    started = time.monotonic()
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

    vin = found.get("vin")
    modules: List[dict] = []
    how = None
    try:
        with doip_client.DoipClient(
            found["ip"], source_address=ecus.TESTER_ADDRESS
        ) as client:
            power = client.power_mode()
            if power == "not ready":
                return {
                    "ok": False,
                    "simulated": False,
                    "vehicle": {"vin": vin, "ip": found["ip"], "protocol": "DoIP"},
                    "modules": [],
                    "message": (
                        "The car reports it is not electrically ready for "
                        "diagnostics. Switch the ignition fully ON -- foot on the "
                        "brake and press start, not just accessory mode."
                    ),
                    "timestamp": _now(),
                }

            if not vin:
                try:
                    vin = uds.read_vin(client, ecus.GATEWAY_ADDRESS)
                except Exception:
                    pass

            addresses, how = _present_addresses(client)
            for address in addresses:
                entry = {
                    "address": f"0x{address:04X}",
                    "code": None,
                    "name": ecus.name_for(address),
                    "present": True,
                    "dtcs": [],
                    "suppressed": 0,
                    "ident": {},
                    "error": None,
                    "known": address in ecus.NAMES,
                }
                entry["ident"] = _read_ident(client, address)
                if entry["ident"].get("vagCode"):
                    entry["code"] = entry["ident"]["vagCode"]
                dtcs, suppressed, error = _read_faults(client, address)
                entry["dtcs"] = dtcs
                entry["suppressed"] = suppressed
                entry["error"] = error
                modules.append(entry)
    except doip_client.DoipRoutingRejected as exc:
        return {
            "ok": False,
            "simulated": False,
            "vehicle": {"vin": vin, "ip": found["ip"], "protocol": "DoIP"},
            "modules": [],
            "message": (
                f"The car refused to open a diagnostic route. {exc} "
                "If another tester is already connected, disconnect it and retry."
            ),
            "timestamp": _now(),
        }
    except (doip_client.DoipError, OSError) as exc:
        return {
            "ok": False,
            "simulated": False,
            "vehicle": {"vin": vin, "ip": found["ip"], "protocol": "DoIP"},
            "modules": modules,
            "message": (
                f"Lost the diagnostic connection: {exc}. The Taycan's gateway "
                "sleeps after inactivity even with the ignition on, and takes a "
                "new address when it wakes -- cycle the ignition, wait 5s and "
                "scan again."
            ),
            "timestamp": _now(),
        }

    elapsed = time.monotonic() - started
    faults = sum(len(m["dtcs"]) for m in modules)
    suppressed = sum(m["suppressed"] for m in modules)
    unnamed = sum(1 for m in modules if not m["known"])

    message = (
        f"VIN {vin}. " if vin else ""
    ) + (
        f"{len(modules)} modules answered, {faults} fault"
        f"{'s' if faults != 1 else ''} stored. Read-only scan -- nothing was "
        "changed on the car."
    )
    if how:
        message += f" Modules found by {how}."
    if suppressed:
        message += (
            f" {suppressed} defined-but-never-failed self-test entries were "
            "filtered out."
        )
    if unnamed:
        message += (
            f" {unnamed} module{'s' if unnamed != 1 else ''} answered at an "
            "address not in our table -- shown with whatever it reports about "
            "itself."
        )

    notes = []
    if elapsed > SCAN_WARN_SECONDS:
        notes.append(
            f"The session ran {elapsed / 60:.1f} minutes. A diagnostic session "
            "keeps a Taycan awake, and these cars have a known 12V drain "
            "problem -- unplug when you are done."
        )
    return {
        "ok": True,
        "simulated": False,
        "vehicle": {
            "vin": vin,
            "ip": found["ip"],
            "protocol": "DoIP",
            "gatewayAddress": found.get("gatewayAddress"),
        },
        "modules": modules,
        "elapsedSeconds": round(elapsed, 1),
        "discovery": how,
        "notes": notes,
        "message": message,
        "timestamp": _now(),
    }


def clear(mode: str = "real", address: Optional[str] = None) -> dict:
    """Clear fault memory on one module, or on every non-HV module present.

    High-voltage modules are refused. On a car being bought or sold, their
    stored faults are the evidence the decision rests on, and a cleared HV
    memory also invalidates any battery assessment done afterwards.
    """
    if mode == "sim":
        return {
            "ok": True,
            "simulated": True,
            "cleared": [address or "all non-HV modules"],
            "refused": [],
            "message": "SIMULATED -- nothing was cleared on any real car.",
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
                "refused": [],
                "message": f"Bad module address '{address}' (expected hex like 0x4013).",
                "timestamp": _now(),
            }
        if wanted in HV_MODULES:
            return {
                "ok": False,
                "simulated": False,
                "cleared": [],
                "refused": [f"0x{wanted:04X}"],
                "message": (
                    f"Refused: {ecus.name_for(wanted)} is a high-voltage module. "
                    "Its stored faults are the evidence a purchase decision rests "
                    "on, and clearing them also invalidates a battery health "
                    "assessment. Read them, photograph them, and have the fault "
                    "fixed -- do not clear them."
                ),
                "timestamp": _now(),
            }

    found = discover()
    if not found["found"]:
        return {
            "ok": False,
            "simulated": False,
            "cleared": [],
            "refused": [],
            "message": found["detail"],
            "timestamp": _now(),
        }

    cleared, refused, errors = [], [], []
    try:
        with doip_client.DoipClient(
            found["ip"], source_address=ecus.TESTER_ADDRESS
        ) as client:
            if wanted is not None:
                targets = [wanted]
            else:
                present, _how = _present_addresses(client)
                targets = []
                for addr in present:
                    if addr in HV_MODULES:
                        refused.append(f"0x{addr:04X}")
                        continue
                    targets.append(addr)
            for addr in targets:
                try:
                    uds.clear_dtcs(client, addr)
                    cleared.append(f"0x{addr:04X}")
                except Exception as exc:
                    errors.append(f"0x{addr:04X}: {exc}")
    except (doip_client.DoipError, OSError) as exc:
        return {
            "ok": False,
            "simulated": False,
            "cleared": cleared,
            "refused": refused,
            "message": f"Lost the diagnostic connection: {exc}",
            "timestamp": _now(),
        }

    ok = bool(cleared) and not errors
    message = f"Cleared fault memory on {', '.join(cleared) or 'no modules'}."
    if refused:
        message += (
            f" Skipped {len(refused)} high-voltage module"
            f"{'s' if len(refused) != 1 else ''} on purpose -- their fault "
            "history is evidence, not clutter."
        )
    if errors:
        message += " Failed: " + "; ".join(errors)
    return {
        "ok": ok,
        "simulated": False,
        "cleared": cleared,
        "refused": refused,
        "message": message,
        "timestamp": _now(),
    }


def _simulated_scan() -> dict:
    def mod(addr, dtcs=None, ident=None, suppressed=0):
        return {
            "address": f"0x{addr:04X}",
            "code": f"0x{dict((a, v) for _n, a, v, _s in ecus.TAYCAN_ECUS).get(addr) or 0:02X}",
            "name": ecus.name_for(addr),
            "present": True,
            "dtcs": dtcs or [],
            "suppressed": suppressed,
            "ident": ident or {},
            "error": None,
            "known": addr in ecus.NAMES,
        }

    modules = [
        mod(0x4010, ident={"asamId": "EV_Gatew31xPO513", "partNumber": "9J1907468"}),
        mod(0x4076, ident={"asamId": "EV_VCU00XXX0209J1909101XX"}),
        mod(
            0x407B,
            ident={"asamId": "EV_BECM1982091", "partNumber": "9J1909107A"},
            suppressed=412,
        ),
        mod(0x407C, ident={"asamId": "EV_PWR1HIAMSPO513"}),
        mod(0x40B8, ident={"asamId": "EV_PWR2HIAMSPO513"}),
        mod(0x4044, ident={"asamId": "EV_OBC3Phase1KLOMLBev16B"}),
        mod(0x40B7, ident={"asamId": "EV_DCDC400VBasisPREHPO513"}),
        mod(0x4042, ident={"asamId": "EV_ThermContrVISAU49X"}),
        mod(
            0x4013,
            dtcs=[
                {
                    "code": "C10A00",
                    "hex": "C10A00",
                    "failureType": "00",
                    "status": "2F",
                    "flags": ["test failed", "confirmed"],
                }
            ],
            ident={"asamId": "EV_ESP9BOSCHPO513"},
            suppressed=38,
        ),
        mod(0x4014, ident={"asamId": "EV_DashBoardLGEPO513"}),
        mod(0x4073, ident={"asamId": "EV_MUTI"}),
        mod(0x400E, ident={"asamId": "EV_BCM1BOSCHAU651"}),
    ]
    return {
        "ok": True,
        "simulated": True,
        "vehicle": {
            "vin": "WP0ZZZY1ZNSA00001",
            "ip": "169.254.10.20",
            "protocol": "DoIP",
            "gatewayAddress": "0x4010",
        },
        "modules": modules,
        "elapsedSeconds": 11.4,
        "discovery": "functional broadcast",
        "notes": [],
        "message": (
            "SIMULATED -- VIN WP0ZZZY1ZNSA00001. 12 modules answered, 1 fault "
            "stored. No real car connected."
        ),
        "timestamp": _now(),
    }
