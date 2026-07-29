"""Porsche Taycan (J1) high-voltage battery health check over DoIP
(docs/private/porsche-research-taycan-battery.md).

The Taycan is the one Porsche in this harness that is NOT diagnosed over the
ELM327 dongle: its manufacturer diagnostics live behind a fully ISO 13400
(DoIP) compliant gateway, reachable over the same cheap BMW ENET (Ethernet-to-
OBD) cable the BMW profile already uses -- no PIWIS, no SFD unlock for reads.
The community battery tools (tCAN-Pro, MapEV Diag, Car Scanner) all take this
same path: ENET cable -> gateway -> UDS 0x22 reads on the battery controller.

WHAT IS HONEST HERE, AND WHAT IS A CANDIDATE
--------------------------------------------
* The TRANSPORT is a published open standard. Routing activation and the
  DiagnosticMessage frames are ISO 13400-2 verbatim (backend/core/doip.py).
  UDS 0x22 ReadDataByIdentifier is ISO 14229. Both are solid.

* The DIDs and their scalings are COMMUNITY CAPTURES from the VW-Group EV
  family (spot2000/Volkswagen-MEB-EV-CAN-parameters on GitHub -- an ID.3/MEB
  capture), NOT a Taycan-confirmed table. The Taycan is the J1 (MLB-derived)
  platform with a different BMS supplier, so every DID below is a CANDIDATE to
  verify against a real car, exactly like the VAG-block Cayenne addresses and
  the single-source Rennlist oil-temp value elsewhere in this profile. A caveat
  rides on the result so nobody trusts an absolute figure blind.

* The BMS logical address over DoIP is NOT publicly published for the Taycan.
  We activate routing to the gateway, learn its logical address from the
  announcement, and then try the UDS reads against a small documented candidate
  address set -- keeping whichever answers a valid positive response (0x62).

* DEALER "STATE OF HEALTH" HAS NO CONFIRMED PUBLIC DID. Porsche's own SoH is a
  multi-day calibrated calculation; the raw identifier PIWIS reads was never
  published. We therefore DO NOT read a guessed SoH DID and print a number --
  that would be a fabricated headline. Instead the check reports the pack
  vitals that DO have a known wire format (pack voltage, SOC, temperatures) and
  computes the one health signal a DIY read genuinely yields: the cell-voltage
  spread (delta between the highest and lowest cell), the classic pack-balance
  indicator. The caveat says plainly that true SoH needs a Porsche-confirmed
  identifier.

READ-ONLY. This path only ever sends routing activation + UDS 0x22 reads. It
never changes session, runs a routine, writes, clears, resets, or touches
security access -- a battery read must not be able to change the car.
"""

from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from backend.core import doip, interfaces

# Candidate DoIP logical addresses to try for the battery controller. The
# gateway (entity) address learned from the announcement is tried first because
# some gateways answer/route 0x22 on the shared channel; the rest are
# documented candidates to confirm on a real car. UNVERIFIED for the Taycan.
_CANDIDATE_BMS_TARGETS: List[Tuple[int, str]] = [
    (0x1706, "candidate BMS (0x1706)"),
    (0x1701, "candidate hybrid/HV controller (0x1701)"),
    (0x1010, "gateway-routed (0x1010)"),
]

# Read a bounded sample of per-cell voltages to derive the balance delta. The
# MEB capture exposes cells on 0x1E40.. ; we sample the first _CELL_SAMPLE and
# stop early after a run of non-answers so a car that lays them out differently
# just yields fewer cells instead of hanging.
_CELL_DID_BASE = 0x1E40
_CELL_SAMPLE = 34
_CELL_MISS_STOP = 4


def _u8(b: bytes, i: int = 0) -> Optional[int]:
    return b[i] if len(b) > i else None


def _u16(b: bytes, i: int = 0) -> Optional[int]:
    return (b[i] << 8) | b[i + 1] if len(b) > i + 1 else None


def _fmt(v: float, dp: int) -> str:
    return f"{v:.{dp}f}" if dp else f"{v:.0f}"


def _soc(b: bytes) -> Optional[str]:
    v = _u8(b)
    if v is None:
        return None
    soc = v / 2.5
    return _fmt(soc, 0) if 0 <= soc <= 100 else None


def _pack_voltage(b: bytes) -> Optional[str]:
    v = _u16(b)
    if v is None:
        return None
    volts = v / 4
    return _fmt(volts, 1) if 0 <= volts <= 1000 else None


def _pack_current(b: bytes) -> Optional[str]:
    if len(b) < 4:
        return None
    raw = (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]
    amps = (raw - 150000) / 100
    return _fmt(amps, 1) if -1500 <= amps <= 1500 else None


def _pack_temp(b: bytes) -> Optional[str]:
    v = _u8(b)
    if v is None:
        return None
    temp = v / 2 - 40
    return _fmt(temp, 0) if -40 <= temp <= 90 else None


def _cell_voltage(b: bytes) -> Optional[float]:
    """Per-cell volts as a float (for min/max maths), or None if implausible."""
    v = _u16(b)
    if v is None:
        return None
    volts = v / 1000 + 1
    return volts if 1.5 <= volts <= 5.0 else None


# (did, name, unit, decode) -- only values with a KNOWN (MEB-captured) scaling.
# SoH is deliberately absent: no confirmed public DID (see module docstring).
_PACK_DIDS: List[Tuple[int, str, str, Callable[[bytes], Optional[str]]]] = [
    (0x028C, "State of charge (BMS)", "%", _soc),
    (0x1E3B, "HV pack voltage", "V", _pack_voltage),
    (0x1E3D, "HV pack current", "A", _pack_current),
    (0x2A0B, "HV pack temperature", "°C", _pack_temp),
]

_CAVEAT = {
    "name": "Note",
    "value": (
        "Read over the ENET cable via the Taycan's ISO-13400 (DoIP) gateway. The DIDs "
        "and scalings are community captures from the VW-Group MEB EV family, NOT a "
        "Taycan-confirmed table — the J1 battery differs, so treat every figure as a "
        "candidate to verify against the car. Dealer State-of-Health has no confirmed "
        "public identifier and is not shown as a number; the cell-voltage spread is the "
        "honest DIY balance indicator. Read-only — nothing was changed on the car."
    ),
    "unit": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_did(client, target: int, did: int) -> Optional[bytes]:
    """One UDS 22 <did> read -> payload after ``62 <did-hi> <did-lo>``, or None."""
    req = bytes([0x22, (did >> 8) & 0xFF, did & 0xFF])
    try:
        reply = client.request(target, req)
    except doip.DoipError:
        return None
    if len(reply) < 3 or reply[0] != 0x62 or reply[1] != (did >> 8) & 0xFF or reply[2] != did & 0xFF:
        return None  # negative response (0x7F) or a mismatched echo
    return reply[3:]


def _find_bms_target(client, entity_address: Optional[int]) -> Optional[int]:
    """Return the first candidate address that answers the SOC read, or None."""
    targets: List[int] = []
    if entity_address is not None:
        targets.append(entity_address)
    targets.extend(addr for addr, _ in _CANDIDATE_BMS_TARGETS)
    seen = set()
    for target in targets:
        if target in seen:
            continue
        seen.add(target)
        if _read_did(client, target, 0x028C) is not None:
            return target
    return None


def _read_cells(client, target: int) -> List[float]:
    """Sample per-cell voltages from 0x1E40.. ; stop after a run of misses."""
    cells: List[float] = []
    misses = 0
    for n in range(_CELL_SAMPLE):
        body = _read_did(client, target, _CELL_DID_BASE + n)
        volts = _cell_voltage(body) if body is not None else None
        if volts is None:
            misses += 1
            if misses >= _CELL_MISS_STOP:
                break
            continue
        misses = 0
        cells.append(volts)
    return cells


def _simulated() -> dict:
    cells = [3.902, 3.905, 3.898, 3.901, 3.907, 3.900]
    delta_mv = round((max(cells) - min(cells)) * 1000)
    return {
        "ok": True, "simulated": True,
        "readings": [
            {"name": "State of charge (BMS)", "value": "72", "unit": "%"},
            {"name": "HV pack voltage", "value": "742.5", "unit": "V"},
            {"name": "HV pack current", "value": "-3.1", "unit": "A"},
            {"name": "HV pack temperature", "value": "24", "unit": "°C"},
            {"name": "Cells sampled", "value": str(len(cells)), "unit": ""},
            {"name": "Highest cell", "value": f"{max(cells):.3f}", "unit": "V"},
            {"name": "Lowest cell", "value": f"{min(cells):.3f}", "unit": "V"},
            {"name": "Cell spread (balance)", "value": str(delta_mv), "unit": "mV"},
            dict(_CAVEAT),
        ],
        "message": "SIMULATED — no real car connected. Shape matches a real DoIP read.",
        "timestamp": _now(),
    }


def battery_health(mode: str = "real") -> dict:
    """One read-only DoIP pass over the Taycan HV battery. Skips non-answers."""
    if mode == "sim":
        return _simulated()

    candidate = interfaces.best_candidate()
    ident = doip.probe(candidate)
    if not ident.get("reached_doip"):
        return {
            "ok": False, "simulated": False, "readings": [],
            "message": (
                "No DoIP gateway answered on the ENET cable. Plug the BMW ENET "
                "(Ethernet-to-OBD) cable into the Taycan and the laptop, ignition ON, wait "
                f"for a 169.254.x.x link-local address, then retry. ({ident.get('detail', '').strip()})"
            ),
            "timestamp": _now(),
        }

    car_ip = ident.get("raw_from_ip")
    vin = ident.get("vin")
    readings: List[dict] = []
    try:
        with doip.DoipDiagClient(car_ip) as client:
            entity = client.activate()  # routing activation to the gateway
            target = _find_bms_target(client, entity)
            if target is None:
                return {
                    "ok": True, "simulated": False, "readings": [],
                    "vin": vin,
                    "message": (
                        "DoIP gateway reached and routing activated, but no candidate battery "
                        "address answered the state-of-charge read. The Taycan's BMS logical "
                        "address is not publicly published — this is the expected 'candidate "
                        "address, verify on the car' wall, not a fault. VIN "
                        f"{vin or 'unread'}. Read-only; nothing was changed."
                    ),
                    "timestamp": _now(),
                }

            for did, name, unit, decode in _PACK_DIDS:
                body = _read_did(client, target, did)
                if body is None:
                    continue
                try:
                    value = decode(body)
                except Exception:
                    continue
                if value is not None:
                    readings.append({"name": name, "value": value, "unit": unit})

            cells = _read_cells(client, target)
            if len(cells) >= 2:
                hi, lo = max(cells), min(cells)
                readings.append({"name": "Cells sampled", "value": str(len(cells)), "unit": ""})
                readings.append({"name": "Highest cell", "value": f"{hi:.3f}", "unit": "V"})
                readings.append({"name": "Lowest cell", "value": f"{lo:.3f}", "unit": "V"})
                readings.append({"name": "Cell spread (balance)",
                                 "value": str(round((hi - lo) * 1000)), "unit": "mV"})
    except doip.DoipError as exc:
        return {
            "ok": False, "simulated": False, "readings": [], "vin": vin,
            "message": (
                f"Reached the DoIP gateway but the battery read failed: {exc}. Read-only; "
                "nothing was changed on the car."
            ),
            "timestamp": _now(),
        }

    if readings:
        readings.append(dict(_CAVEAT))

    return {
        "ok": True, "simulated": False, "readings": readings, "vin": vin,
        "message": (
            f"{len([r for r in readings if r['name'] != 'Note'])} Taycan battery value(s) "
            "answered over DoIP. Figures use community-captured VW-Group MEB scalings — "
            "candidates to verify against this car. Dealer State-of-Health has no confirmed "
            "public DID; the cell spread is the honest balance indicator. Read-only; nothing "
            "was changed on the car."
        ) if readings else (
            "Battery controller answered but returned no value with a known scaling. Read-only."
        ),
        "timestamp": _now(),
    }
