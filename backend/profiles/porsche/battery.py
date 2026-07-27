"""Porsche Taycan (J1) high-voltage battery health -- the pre-purchase read.

What this is for: telling a dealer, before he buys a used Taycan, whether the
most expensive part of it is healthy. State of health, cell balance, thermal
spread and use history, read straight from the battery control unit.

**Transport-agnostic by design.** Everything here takes a ``client`` object with
``request(dst, uds, timeout)`` -- the same contract ``HsfzClient``,
``DoipClient`` and ``Elm327UdsClient`` all satisfy -- plus an address map, so
the identical logic serves the ENET/DoIP cable path and the cheap OBD-dongle
path. The BMS answers the same DIDs either way; only its address changes:

    CAN (ELM327):  bms 0x7E5  charger 0x744  vcu 0x7E0  gateway 0x710
    DoIP (ENET):   bms 0x407B charger 0x4044 vcu 0x4076 gateway 0x4010

Sources and provenance live in ``docs/private/taycan-battery-soh-research.md``.
Every scaling below was re-derived from a captured response frame in the
OBDb/Porsche-Taycan signal set (CC-BY-SA-4.0, MY2020-MY2025); the worked
examples are reproduced in the self-test so a wrong edit fails loudly.

HONESTY RULES ENFORCED HERE
  * A DID that does not answer is reported as unavailable **with the reason**.
    Nothing is estimated, defaulted or carried over from another car.
  * State of health is **refused outright on the MY2025+ J1.2 facelift**: the
    DID still answers but the scaling does not hold, and a confidently wrong
    SoH on a dealer's screen costs him money. A blank does not.
  * Raw 0x0000 from the SoH DID means "no value", not -1798%.
  * Capacity in Ah, charge-cycle count and isolation resistance are genuine
    gaps. They are absent, not guessed.

SAFETY
  This module is read-only and enforces it in code -- see ``assert_read_only``.
  On an 800 V car the dangerous surface is RoutineControl (contactor tests, HV
  shutdown, cell balancing, and Porsche's own multi-day discharge SoH test that
  leaves the car flat and immobilised for days). We never need a routine to
  read a value, so the whole service is refused by byte, not by name.
"""

from typing import Optional

from backend.core import uds

# --------------------------------------------------------------------------- #
# Safety: the only UDS services this feature may ever put on an HV bus.
# --------------------------------------------------------------------------- #
SAFE_SERVICES = {
    0x10: "DiagnosticSessionControl (default/extended only)",
    0x22: "ReadDataByIdentifier",
    0x19: "ReadDTCInformation",
    0x3E: "TesterPresent",
}

_REFUSED_SERVICES = {
    0x31: "RoutineControl -- contactor tests, HV shutdown, cell balancing and "
          "Porsche's multi-day discharge SoH test all live here",
    0x2F: "InputOutputControlByIdentifier -- actuator control",
    0x2E: "WriteDataByIdentifier -- coding/adaptation",
    0x27: "SecurityAccess -- only ever needed for writes",
    0x11: "ECUReset -- resetting the battery controller can drop the contactors",
    0x14: "ClearDiagnosticInformation -- destroys the fault evidence the "
          "purchase decision rests on",
    0x28: "CommunicationControl -- can silence a module on a live bus",
    0x85: "ControlDTCSetting -- suppresses fault storage, masks a bad pack",
    0x34: "RequestDownload -- flashing",
    0x35: "RequestUpload -- flashing",
    0x36: "TransferData -- flashing",
    0x37: "RequestTransferExit -- flashing",
    0x3D: "WriteMemoryByAddress",
}


class UnsafeRequest(Exception):
    """Raised when something tries to send a write/actuate service at the pack."""


def assert_read_only(request: bytes) -> None:
    """Gate every byte sequence this module sends at a high-voltage module.

    Refuses by service byte rather than by name, so a renamed or undocumented
    routine cannot slip through.
    """
    if not request:
        raise UnsafeRequest("Empty request")
    service = request[0]
    if service in _REFUSED_SERVICES:
        raise UnsafeRequest(
            f"Refused service 0x{service:02X}: {_REFUSED_SERVICES[service]}. "
            "The battery feature is read-only."
        )
    if service == 0x10 and len(request) >= 2 and request[1] not in (0x01, 0x03):
        raise UnsafeRequest(
            f"Refused diagnostic session 0x{request[1]:02X} -- only default "
            "(0x01) and extended (0x03) are allowed; 0x02 is the programming "
            "session."
        )
    if service not in SAFE_SERVICES:
        raise UnsafeRequest(
            f"Service 0x{service:02X} is not on the read-only allow-list "
            f"({', '.join(f'0x{s:02X}' for s in sorted(SAFE_SERVICES))})."
        )


# --------------------------------------------------------------------------- #
# Address maps -- the same battery, reached two ways.
# --------------------------------------------------------------------------- #
CAN_ADDRESSES = {"bms": 0x7E5, "charger": 0x744, "vcu": 0x7E0, "gateway": 0x710}
DOIP_ADDRESSES = {"bms": 0x407B, "charger": 0x4044, "vcu": 0x4076, "gateway": 0x4010}

# 33 module blocks -> 198 series positions (198s2p). A Performance Battery
# (28 modules) answers 0x1850-0x186B and negative-responds to the last five;
# that is "not fitted", never a fault.
CELL_BLOCK_FIRST = 0x1850
CELL_BLOCK_COUNT = 33
MODULE_TEMP_FIRST = 0x1821

# Net (usable) nameplate energy. Deliberately NOT the gross 79.2/93.4 figures --
# using gross would understate every car's health by ~11%.
NET_NAMEPLATE_KWH = {"performance": 71.0, "performance_plus": 83.7}

SOH_OFFSET = 14162
SOH_SLOPE = 0.127


# --------------------------------------------------------------------------- #
# Decoders. Each returns None when the payload is too short to trust.
# --------------------------------------------------------------------------- #
def _u16(data: bytes, at: int = 0) -> Optional[int]:
    if len(data) < at + 2:
        return None
    return (data[at] << 8) | data[at + 1]


def decode_soh(data: bytes) -> Optional[float]:
    """State of health %. ``(u16 - 14162) * 0.127``.

    Verified against captured frames: 0x3958 -> 65.786, 0x3A00 -> 87.122,
    0x3A27 -> 92.075. A raw 0x0000 is the BMS saying "no value" -- suppress it
    rather than printing -1798%.
    """
    raw = _u16(data)
    if raw is None or raw == 0:
        return None
    return round((raw - SOH_OFFSET) * SOH_SLOPE, 2)


def decode_cell_extreme(data: bytes) -> Optional[dict]:
    """Cell voltage max/min plus the index of the cell responsible."""
    raw = _u16(data)
    if raw is None:
        return None
    entry = {"volts": round(raw / 10000, 4), "cell": None}
    if len(data) >= 3:
        entry["cell"] = data[2]
    return entry


def decode_cell_soc_extreme(data: bytes) -> Optional[dict]:
    if not data:
        return None
    return {"percent": data[0], "cell": data[1] if len(data) >= 2 else None}


def decode_pack_voltage(data: bytes) -> Optional[float]:
    raw = _u16(data)
    return None if raw is None else round(raw / 10, 1)


def decode_pack_current(data: bytes) -> Optional[float]:
    """Amps, signed by offset. Negative = discharging."""
    raw = _u16(data)
    return None if raw is None else round(raw * 2.56 - 1500, 2)


def decode_temp_extreme(data: bytes) -> Optional[dict]:
    """Pack aggregate temperatures use a -100 offset (per-module ones use -50)."""
    if not data:
        return None
    return {"celsius": data[0] - 100, "cell": data[1] if len(data) >= 2 else None}


def decode_temp_average(data: bytes) -> Optional[int]:
    return None if not data else data[0] - 100


def decode_module_temps(data: bytes) -> Optional[list]:
    """Three sensors per module, -50 offset. NOT the -100 the pack totals use."""
    if len(data) < 3:
        return None
    return [b - 50 for b in data[:3]]


def decode_cell_block(data: bytes) -> Optional[dict]:
    """One module block: hardware id + 6 series positions of voltage and SoC.

    Layout (46-byte multi-frame response, after the echoed DID):
        byte 0          module hardware id
        byte 1+7k, 2+7k series-position k voltage, u16/10000 -> volts
        byte 3+7k       series-position k state of charge, %
        bytes 4..7 +7k  padding
    """
    if len(data) < 1 + 7:
        return None
    cells = []
    for k in range(6):
        base = 1 + 7 * k
        if len(data) < base + 4:
            break
        raw = (data[base] << 8) | data[base + 1]
        cells.append({"volts": round(raw / 10000, 4), "soc": data[base + 2]})
    if not cells:
        return None
    return {"moduleId": data[0], "cells": cells}


def decode_u32(data: bytes) -> Optional[int]:
    if len(data) < 4:
        return None
    return int.from_bytes(data[:4], "big")


def decode_present_energy(data: bytes) -> Optional[float]:
    """Energy currently in the pack, kWh. Only the first u16 is decoded --
    the remaining bytes of this 13-byte response are an open capture target."""
    raw = _u16(data)
    return None if raw is None else round(raw / 20, 2)


def decode_displayed_soc(data: bytes) -> Optional[float]:
    """Dash SoC, from the VCU. The value is repeated; take the first copy."""
    raw = _u16(data)
    return None if raw is None else round(raw / 100, 2)


# --------------------------------------------------------------------------- #
# Model-year gate. The J1.2 facelift answers the SoH DID with a value the
# J1.1 scaling turns into a plausible-looking lie (~56-68%), so we refuse it.
# --------------------------------------------------------------------------- #
_VIN_YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"


def model_year_from_vin(vin: Optional[str]) -> Optional[int]:
    """Model year from VIN position 10, for Porsche WP0/WP1 VINs only.

    Porsche uses the ISO/US position-10 year code consistently across markets,
    unlike (say) Mazda or Mercedes Europe -- see the honest-VIN-years work in
    the Android VinDecode. Returns None when we cannot be sure.
    """
    if not vin or len(vin) < 10:
        return None
    if vin[:3].upper() not in ("WP0", "WP1"):
        return None
    code = vin[9].upper()
    idx = _VIN_YEAR_CODES.find(code)
    if idx < 0:
        return None
    # The code cycles every 30 years; 2010 = 'A'. Taycans start in 2019.
    year = 2010 + idx
    return year


def soh_supported(vin: Optional[str], model_year: Optional[int] = None) -> tuple:
    """(supported, reason). Refuses MY2025+ / J1.2 rather than lying."""
    year = model_year if model_year is not None else model_year_from_vin(vin)
    if year is None:
        return True, None  # unknown year: read it, and label the caveat
    if year >= 2025:
        return False, (
            f"State of health is not decodable on the {year} (J1.2 facelift) "
            "battery: the control unit answers, but the published scaling was "
            "derived on the 2020-2024 pack and produces a wrong number on this "
            "one. The cell measurements below still read correctly."
        )
    return True, None


# --------------------------------------------------------------------------- #
# The read itself.
# --------------------------------------------------------------------------- #
def _read(client, dst: int, did: int, timeout: float = 3.0):
    """One guarded ReadDataByIdentifier. Returns (data, error)."""
    request = bytes([uds.SVC_READ_DATA_BY_ID, did >> 8, did & 0xFF])
    assert_read_only(request)
    try:
        return uds.read_did(client, dst, did, timeout=timeout), None
    except uds.UdsNegativeResponse as exc:
        return None, str(exc)
    except Exception as exc:  # transport-level: absent module, dropped link
        return None, str(exc)


def _value(target: dict, key: str, data, error, decoder, **extra) -> None:
    """Record a decoded value, or an honest reason it is missing."""
    if data is None:
        target[key] = {"value": None, "available": False, "reason": error}
        return
    decoded = decoder(data)
    if decoded is None:
        target[key] = {
            "value": None,
            "available": False,
            "reason": "the module answered but the value was empty or too short to decode",
            "raw": data.hex(),
        }
        return
    entry = {"value": decoded, "available": True, "raw": data.hex()}
    entry.update(extra)
    target[key] = entry


def read_health(
    client,
    addresses: Optional[dict] = None,
    vin: Optional[str] = None,
    model_year: Optional[int] = None,
    deep: bool = False,
    timeout: float = 3.0,
) -> dict:
    """Read the HV battery's health picture. Read-only throughout.

    ``deep=True`` additionally walks all 33 module blocks (198 cell voltages)
    and the per-module temperature sensors -- slower, and it needs the extended
    session, so it is opt-in.
    """
    addresses = addresses or CAN_ADDRESSES
    bms = addresses["bms"]
    result: dict = {
        "addresses": {k: f"0x{v:04X}" for k, v in addresses.items()},
        "soh": {},
        "cells": {},
        "thermal": {},
        "electrical": {},
        "charge": {},
        "history": {},
        "warnings": [],
        "notes": [],
    }

    # The deep DIDs live behind the extended session. Opening it is on the safe
    # list (0x10 03) and the ECU drops back to default on its own.
    session_open = False
    if deep:
        try:
            assert_read_only(bytes([uds.SVC_SESSION_CONTROL, 0x03]))
            uds.start_session(client, bms, 0x03, timeout=timeout)
            session_open = True
        except Exception as exc:
            result["notes"].append(
                f"Could not open the extended session on the battery controller "
                f"({exc}); reading what the default session allows."
            )

    # -- state of health ---------------------------------------------------- #
    supported, reason = soh_supported(vin, model_year)
    if not supported:
        result["soh"] = {
            "percent": {"value": None, "available": False, "reason": reason},
            "supported": False,
        }
    else:
        data, error = _read(client, bms, 0x51E0, timeout)
        _value(result["soh"], "percent", data, error, decode_soh)
        result["soh"]["supported"] = True
        if model_year_from_vin(vin) is None and model_year is None:
            result["notes"].append(
                "Model year could not be read from the VIN. State of health is "
                "decoded with the 2020-2024 (J1.1) scaling -- if this is a 2025 "
                "or later car, ignore the percentage."
            )
        pct = result["soh"]["percent"].get("value")
        if pct is not None:
            result["soh"]["band"] = soh_band(pct)
            if not 0 <= pct <= 105:
                result["warnings"].append(
                    f"State of health decoded to {pct}%, which is outside any "
                    "sane range -- treat it as a failed read, not a reading."
                )
                result["soh"]["percent"]["available"] = False
                result["soh"]["percent"]["reason"] = "decoded value out of range"

    # -- cell balance: the number that actually sells the car ---------------- #
    hi_data, hi_err = _read(client, bms, 0x1E33, timeout)
    lo_data, lo_err = _read(client, bms, 0x1E34, timeout)
    _value(result["cells"], "max", hi_data, hi_err, decode_cell_extreme)
    _value(result["cells"], "min", lo_data, lo_err, decode_cell_extreme)
    hi = result["cells"]["max"].get("value")
    lo = result["cells"]["min"].get("value")
    if hi and lo:
        delta_mv = round((hi["volts"] - lo["volts"]) * 1000, 1)
        result["cells"]["deltaMillivolts"] = {
            "value": delta_mv,
            "available": True,
            "verdict": cell_delta_verdict(delta_mv),
        }
    else:
        result["cells"]["deltaMillivolts"] = {
            "value": None,
            "available": False,
            "reason": "needs both the highest and lowest cell voltage",
        }

    soc_hi, soc_hi_err = _read(client, bms, 0x1E2C, timeout)
    soc_lo, soc_lo_err = _read(client, bms, 0x1E2D, timeout)
    _value(result["cells"], "socMax", soc_hi, soc_hi_err, decode_cell_soc_extreme)
    _value(result["cells"], "socMin", soc_lo, soc_lo_err, decode_cell_soc_extreme)

    # -- thermal ------------------------------------------------------------- #
    for key, did, decoder in (
        ("max", 0x1E0E, decode_temp_extreme),
        ("min", 0x1E0F, decode_temp_extreme),
        ("average", 0x1E10, decode_temp_average),
    ):
        data, error = _read(client, bms, did, timeout)
        _value(result["thermal"], key, data, error, decoder)
    for key, did in (("coolantIn", 0x181C), ("coolantOut", 0x181D), ("shunt", 0x180F)):
        data, error = _read(client, bms, did, timeout)
        _value(result["thermal"], key, data, error, lambda d: d[0] - 50 if d else None)

    # -- pack electrical ------------------------------------------------------ #
    pack_v, pack_v_err = _read(client, bms, 0x1801, timeout)
    sum_v, sum_v_err = _read(client, bms, 0x1E3B, timeout)
    _value(result["electrical"], "packVolts", pack_v, pack_v_err, decode_pack_voltage)
    _value(result["electrical"], "cellSumVolts", sum_v, sum_v_err, decode_pack_voltage)
    data, error = _read(client, bms, 0x1802, timeout)
    _value(result["electrical"], "packAmps", data, error, decode_pack_current)

    pv = result["electrical"]["packVolts"].get("value")
    sv = result["electrical"]["cellSumVolts"].get("value")
    if pv and sv and abs(pv - sv) > 5:
        result["warnings"].append(
            f"Pack voltage ({pv} V) and the sum of the cell voltages ({sv} V) "
            f"disagree by {abs(round(pv - sv, 1))} V. Those two should match -- "
            "a gap means a measurement fault in the pack."
        )

    # -- state of charge, both versions --------------------------------------- #
    data, error = _read(client, bms, 0x028C, timeout)
    _value(result["charge"], "trueSoc", data, error, lambda d: d[0] if d else None)
    data, error = _read(client, addresses["vcu"], 0x08D2, timeout)
    _value(result["charge"], "displayedSoc", data, error, decode_displayed_soc)
    data, error = _read(client, addresses["charger"], 0x4965, timeout)
    _value(result["charge"], "presentEnergyKwh", data, error, decode_present_energy)

    # -- independent SoH cross-check, free arithmetic ------------------------- #
    energy = result["charge"]["presentEnergyKwh"].get("value")
    true_soc = result["charge"]["trueSoc"].get("value")
    if energy and true_soc and true_soc > 5:
        implied = energy / true_soc * 100
        result["charge"]["impliedUsableKwh"] = {
            "value": round(implied, 1),
            "available": True,
            "note": (
                "Energy in the pack divided by true state of charge. Compare "
                "against the net nameplate for this car: "
                f"{NET_NAMEPLATE_KWH['performance']} kWh (Performance) or "
                f"{NET_NAMEPLATE_KWH['performance_plus']} kWh (Performance Plus). "
                "Most accurate above ~80% charge, after the car has rested."
            ),
        }
        estimates = {
            name: round(implied / kwh * 100, 1)
            for name, kwh in NET_NAMEPLATE_KWH.items()
        }
        result["charge"]["impliedSohPercent"] = {"value": estimates, "available": True}
        direct = result["soh"].get("percent", {}).get("value")
        if direct is not None:
            closest = min(estimates.values(), key=lambda e: abs(e - direct))
            if abs(closest - direct) > 8:
                result["warnings"].append(
                    f"The two health figures disagree: the battery controller "
                    f"reports {direct}%, while energy-over-charge implies about "
                    f"{closest}%. Both are shown -- neither is being picked as "
                    "the winner. Re-read above 80% charge, at rest."
                )

    # -- use history ---------------------------------------------------------- #
    data, error = _read(client, bms, 0x5200, timeout)
    _value(result["history"], "launchControlCount", data, error, decode_u32)
    data, error = _read(client, addresses["charger"], 0x15EE, timeout)
    _value(
        result["history"], "chargingMinutes", data, error, decode_u32,
        note="raw counter; the unit is confirmed as minutes",
    )
    data, error = _read(client, addresses["charger"], 0x15EF, timeout)
    _value(
        result["history"], "energyTurnoverRaw", data, error, decode_u32,
        note=(
            "raw lifetime energy counter. The scaling is PROVISIONAL -- 0.1 kWh "
            "per count is the only value that gives a believable average "
            "charging power, but it is not confirmed. Shown as a raw number "
            "deliberately."
        ),
    )

    # -- deep pass: every cell, every module sensor --------------------------- #
    if deep:
        result["modules"] = _read_modules(client, bms, timeout)
        result["cellMap"] = _summarise_cells(result["modules"])

    if session_open:
        result["notes"].append(
            "Read in the extended diagnostic session. The battery controller "
            "returns to its default session by itself; the cluster may show a "
            "'high voltage battery active' message until then."
        )
    result["notes"].append(
        "Read-only: state of health, cell and temperature values only. No "
        "routine, actuation, reset or clear was sent to the high-voltage system."
    )
    return result


def _absence_reason(error: Optional[str]) -> str:
    """Turn an ECU's refusal into the plain-English thing it actually means.

    "Request out of range" from a battery controller asked for module 31 does
    not mean something is broken -- it means the car has 28 modules. Saying
    that in English keeps a Performance Battery from looking like a fault.
    """
    if error and "0x31" in error:
        return "not fitted -- this pack has fewer modules (a Performance Battery has 28)"
    if error and "0x7F" not in error and "Negative response" not in error:
        return f"did not answer: {error}"
    return "not fitted -- the battery controller holds no data at this module address"


def _read_modules(client, bms: int, timeout: float) -> list:
    """Walk the 33 module blocks. A module that does not answer is 'not fitted'
    (a 28-module Performance Battery has five fewer), never a fault."""
    modules = []
    for n in range(CELL_BLOCK_COUNT):
        entry = {"index": n + 1, "present": False, "cells": [], "temps": None, "reason": None}
        data, error = _read(client, bms, CELL_BLOCK_FIRST + n, timeout)
        block = decode_cell_block(data) if data else None
        if block:
            entry["present"] = True
            entry["moduleId"] = block["moduleId"]
            entry["cells"] = block["cells"]
        else:
            entry["reason"] = _absence_reason(error)
            entry["detail"] = error
        temp_data, _temp_err = _read(client, bms, MODULE_TEMP_FIRST + n, timeout)
        if temp_data:
            entry["temps"] = decode_module_temps(temp_data)
        modules.append(entry)
    return modules


def _summarise_cells(modules: list) -> dict:
    """Min/max/mean/spread across every cell that answered, plus the worst ten."""
    cells = []
    for module in modules:
        for position, cell in enumerate(module.get("cells") or [], start=1):
            cells.append(
                {
                    "module": module["index"],
                    "position": position,
                    "volts": cell["volts"],
                    "soc": cell["soc"],
                }
            )
    if not cells:
        return {"count": 0, "available": False}
    volts = [c["volts"] for c in cells]
    mean = sum(volts) / len(volts)
    variance = sum((v - mean) ** 2 for v in volts) / len(volts)
    ordered = sorted(cells, key=lambda c: c["volts"])
    return {
        "available": True,
        "count": len(cells),
        "minVolts": round(min(volts), 4),
        "maxVolts": round(max(volts), 4),
        "meanVolts": round(mean, 4),
        "spreadMillivolts": round((max(volts) - min(volts)) * 1000, 1),
        "stdDevMillivolts": round(variance ** 0.5 * 1000, 2),
        "weakest": ordered[:10],
    }


# --------------------------------------------------------------------------- #
# Plain-English verdicts. Bands come from the research doc, not from taste.
# --------------------------------------------------------------------------- #
def soh_band(percent: float) -> dict:
    if percent >= 90:
        return {"label": "strong", "detail": "Healthy for any age of Taycan."}
    if percent >= 85:
        return {"label": "normal", "detail": "Normal degradation for age and mileage."}
    if percent >= 80:
        return {"label": "watch", "detail": "Below par -- worth checking the cell spread."}
    if percent >= 70:
        return {
            "label": "price it in",
            "detail": "Meaningful degradation. Reflect it in what you pay.",
        }
    return {
        "label": "below warranty floor",
        "detail": (
            "Under 70% is Porsche's own high-voltage battery warranty threshold "
            "-- check whether the car is still in its warranty period."
        ),
    }


def cell_delta_verdict(delta_mv: float) -> dict:
    if delta_mv <= 30:
        return {"label": "tight", "detail": "Cells are well balanced."}
    if delta_mv <= 50:
        return {"label": "acceptable", "detail": "Normal spread; nothing to chase."}
    if delta_mv <= 100:
        return {
            "label": "imbalance",
            "detail": (
                "A meaningful spread. Re-read at a different state of charge "
                "before drawing conclusions -- delta varies with SoC."
            ),
        }
    return {
        "label": "significant imbalance",
        "detail": (
            "A large spread, the classic single-weak-module signature. Check "
            "whether the worst cells are adjacent in one module."
        ),
    }
