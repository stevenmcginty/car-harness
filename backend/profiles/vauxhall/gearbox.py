"""Astra K 1.4T automatic -- gearbox diagnostic over a Veepeak / ELM327.

This is the ScanMyOpelCAN / GDS2 "Automatic Transmission" surface that a
V-Peak can actually run: identify the TCM, read its calibration, read its
faults with GM (not SAE-generic) names, read the live block, and turn that
into a diagnosis. It does not flash, and it does not run Fast Learn.

Why it does not run Fast Learn, stated precisely
------------------------------------------------
This passage used to say Fast Learn "needs GDS2 + MDI/J2534" and that a V-Peak
cannot do 0xAE. That was true when the CPID and its control bytes were
unknown. It is now only half true, and the half that changed is worth being
exact about, because overstating it in either direction is a way to get this
wrong.

**What is now known.** The routine is GMW3110 DeviceControl (0xAE) on CPID
0x30, and the control packet has been recovered by static analysis of the GDS2
dealer data, with every field traceable to a file offset -- see procedures.py
and data/gds2_t76_tcm.json. The frame was rebuilt independently and reproduced
byte for byte in five separately built deliverables.

**What that means for transport.** The request and its release are seven bytes
each, which is exactly what fits an ISO-TP single frame, so no multi-frame
handling and no flow control are involved. The session framing is 0x10, 0x3E
and 0x20. Every one of those is inside what an ELM327 does unaided. So the
transport objection, as it was written here, does not hold: this is not a
J2534-only exchange.

**What is still unproven, and it is the part that decides it.** First, whether
the module demands SecurityAccess. GDS2 sends no 0x27 for this test, but that
proves the dealer tool does not offer a key, not that the TCM will not ask for
one -- a live attempt may still meet NRC 0x33, and the deliverable carries no
working seed/key algorithm to answer with. Second, whether a Bluetooth ELM327
can keep 0x3E TesterPresent inside the module's timeout for the length of a
routine that runs for minutes; that latency has never been measured against
this module.

So the honest position is: the bytes are known and the transport is in reach,
and the harness still does not send them, because two questions that can only
be answered on a car are still open. procedures.py holds the definition behind
three gates and explains each one. See also resets.py.

Where the numbers and names come from
-------------------------------------
Not invented, and not copied from the earlier OBDb-only attempt:

* **Address and live block.** Verified on VIN W0LPF2EC8HG072884 (2017 Astra K
  6-speed): TCM sits at 0x7E2 (silent at the textbook 0x7E1). Service 0x22
  PIDs 1940 / 1941 / 1942 / 194C / 194F / 1951 answer; 04FE / 5503 / 5504
  return NRC 31. Same 1940-on-7E2 mapping is Torque Pro's shipped
  "Pontiac / GM / Opel / Vauxhall" set (PID 221940, header 7E2, equation
  A-40) -- independent confirmation, not a one-car guess.
* **GM 6T30/35/40/45 clutch-solenoid map.** GM PI1131 (P0776/P0796/P0873/
  P0878/P2714/P2723), NHTSA MC-10200174 (P0796/P2714 sediment-stuck line
  solenoid), Transmission Digest / Sonnax 6T40 application chart. SAE J2012
  names for these codes ("Shift Solenoid E") are wrong on this family --
  GDS2 shows clutch + pressure-control solenoid. This file uses the GM
  names.
* **Failure-type byte.** ISO 14229-1 / SAE J2012-DA, which is what GDS2
  prints after the dash (P0716-71 = actuator stuck).
* **Protocol.** GMW3110 via Scapy's GMLAN contrib: faults 0xA9, ident 0x1A,
  live 0x22. Same dialect ScanMyOpelCAN uses over ELM327 on Astra K.
* **GDS2 dealer data.** data/gds2_t76_tcm.json, extracted from the 2023.3
  Opel/Vauxhall deliverable's module node for this exact TCM. It supplies GM's
  own names, scalings and state tables for 189 parameters and 17 device
  controls, each with the file offset it came from. Read through
  dealer_data.py. The read half of that decode is confirmed against this car:
  the file's fluid-temperature scaling is the one the harness already gets
  right on 0x7E2.
* **What a V-Peak cannot do.** SPS flash. That is the whole list now, and it is
  shorter than it used to be. This entry previously also named the clutch
  adaptive cells and commanded line pressure as out of reach; both were wrong,
  and wrong about the transport rather than the data. The adapt cells are a
  GMLAN 0x1A block at identifier 0x50 (tcm.py read_adaptive_cells), and
  calculated line pressure is an ordinary 0x22 read on 0x2862. Neither has yet
  answered on this car, so both are labelled dealer-data rather than verified.
  Device control 0xAE is a separate question, covered above.

Read-only. The only write in this profile is the confirm-gated fault clear.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional

from backend.obd import service as obd_service
from backend.obd.uds_elm327 import Elm327UdsClient

from . import tcm as tcm_mod
from .scan import (
    ENGINE,
    VAUXHALL_ID_MAP,
    _WRONG_BUS,
    _probe,
    _read_module,
)

# Prefer the address verified on the Astra K; keep the textbook 0x7E1 as
# fallback for other Global A cars.
TCM_CANDIDATES = (
    (tcm_mod.TCM, tcm_mod.TCM + 8, "7E2", "Transmission (TCM) -- Astra K address"),
    (0x7E1, 0x7E9, "7E1", "Transmission (TCM) -- textbook Global A address"),
)

# 6T30 / 6T35 / 6T40 / 6T45 clutch application (GM / Sonnax).
# An X means that clutch is applied in that gear.
CLUTCH_APPLY = {
    "C1234": {"name": "1-2-3-4 clutch", "gears": ("1st", "2nd", "3rd", "4th"),
              "solenoid": "PCS 5", "code": "P2723"},
    "C35R":  {"name": "3-5-Reverse clutch", "gears": ("3rd", "5th", "Reverse"),
              "solenoid": "PCS 2", "code": "P0776"},
    "C26":   {"name": "2-6 clutch", "gears": ("2nd", "6th"),
              "solenoid": "PCS 4", "code": "P2714"},
    "C456":  {"name": "4-5-6 clutch", "gears": ("4th", "5th", "6th"),
              "solenoid": "PCS 3", "code": "P0796"},
    "CBR1":  {"name": "Low / Reverse clutch", "gears": ("1st", "Reverse"),
              "solenoid": "on/off", "code": ""},
}

# GM 6T-family powertrain codes. `sae` is the generic J2012 string (what the
# offline dtc_db.json has) so a reader can see why we do not use it.
TCM_DTCS: Dict[str, dict] = {
    "P0705": {
        "name": "Transmission range sensor circuit",
        "sae": "Transmission Range Sensor Circuit Malfunction (PRNDL Input)",
        "part": "Internal mode switch / range sensor",
        "meaning": "TCM does not agree with the selector position.",
        "next": "Compare selector PID 194F against range-switch PID 1951. "
                "Mismatch is the switch, wiring, or a misadjusted lever -- "
                "not a clutch.",
    },
    "P0706": {
        "name": "Transmission range sensor performance",
        "sae": "Transmission Range Sensor Circuit Range/Performance",
        "part": "Internal mode switch / range sensor",
        "meaning": "Range pattern is legal but does not match commanded range.",
        "next": "Move P-R-N-D and watch 194F / 1951. A stuck bit pattern is "
                "the IMS, not the box.",
    },
    "P0711": {
        "name": "Transmission fluid temperature sensor performance",
        "sae": "Transmission Fluid Temperature Sensor Circuit Range/Performance",
        "part": "TFT sensor (in the TEHCM)",
        "meaning": "Fluid temperature implausible against coolant / time.",
        "next": "Read PID 1940. Stuck at ambient after a long drive, or stuck "
                "hot from cold, is the sensor in the control solenoid body.",
    },
    "P0712": {
        "name": "Transmission fluid temperature sensor low",
        "sae": "Transmission Fluid Temperature Sensor Circuit Low Input",
        "part": "TFT sensor",
        "meaning": "Sensor circuit shorted (reads unreasonably hot).",
        "next": "PID 1940 will sit at the top of its range. TEHCM / wiring.",
    },
    "P0713": {
        "name": "Transmission fluid temperature sensor high",
        "sae": "Transmission Fluid Temperature Sensor Circuit High Input",
        "part": "TFT sensor",
        "meaning": "Sensor circuit open (reads unreasonably cold).",
        "next": "PID 1940 will sit at -40 C. TEHCM / wiring.",
    },
    "P0716": {
        "name": "Input / turbine speed sensor performance",
        "sae": "Input/Turbine Speed Sensor Circuit Range/Performance",
        "part": "Input speed sensor (ISS)",
        "meaning": "Turbine speed does not track engine / output the way a "
                   "healthy converter and clutch set would.",
        "next": "Log PID 1941 against engine rpm through the flaring shift. "
                "A drop-out that is not a real stall is the sensor; a real "
                "ratio error with a solid speed signal is a clutch.",
    },
    "P0717": {
        "name": "Input / turbine speed sensor no signal",
        "sae": "Input/Turbine Speed Sensor Circuit No Signal",
        "part": "Input speed sensor (ISS)",
        "meaning": "ISS is silent while the engine is running.",
        "next": "PID 1941 should be non-zero at idle in P. Zero with the "
                "engine running is the sensor or TEHCM, not a clutch.",
    },
    "P0722": {
        "name": "Output speed sensor no signal",
        "sae": "Output Speed Sensor Circuit No Signal",
        "part": "Output speed sensor (OSS)",
        "meaning": "OSS is silent while the car is moving.",
        "next": "PID 1942 should climb with road speed. Capture a drive; "
                "zero while Mode-01 speed is moving is the sensor.",
    },
    "P0723": {
        "name": "Output speed sensor intermittent",
        "sae": "Output Speed Sensor Circuit Intermittent",
        "part": "Output speed sensor (OSS)",
        "meaning": "OSS drops out under load or heat.",
        "next": "Same as P0722 -- capture 1942 against road speed.",
    },
    "P0741": {
        "name": "Torque converter clutch stuck off (excessive slip)",
        "sae": "Torque Converter Clutch Circuit Performance or Stuck Off",
        "part": "TCC / converter",
        "meaning": "TCM commanded lock-up and the converter kept slipping.",
        "next": "A flare only in 5th/6th at light throttle with this code is "
                "TCC, not a gear clutch. Fluid and the converter come before "
                "a valve-body guess.",
    },
    "P0742": {
        "name": "Torque converter clutch stuck on",
        "sae": "Torque Converter Clutch Circuit Stuck On",
        "part": "TCC / converter",
        "meaning": "Converter is locked when it should be open -- stall / "
                   "shudder in D from rest is the classic feel.",
        "next": "Do not drive it far. TEHCM / TCC valve, then converter.",
    },
    "P0751": {
        "name": "Shift solenoid 1 performance / stuck off",
        "sae": "Shift Solenoid A Performance or Stuck Off",
        "part": "Shift solenoid (TEHCM)",
        "meaning": "On/off shift solenoid did not produce the commanded gear.",
        "next": "TEHCM first. A single shift-solenoid code plus a matching "
                "clutch code usually means the valve body, not the clutch pack.",
    },
    "P0752": {
        "name": "Shift solenoid 1 stuck on",
        "sae": "Shift Solenoid A Stuck On",
        "part": "Shift solenoid (TEHCM)",
        "meaning": "On/off shift solenoid stuck applied.",
        "next": "Same as P0751 -- TEHCM / valve body.",
    },
    "P0776": {
        "name": "PCS 2 performance -- 3-5-R clutch stuck off",
        "sae": "Pressure Control Solenoid B Performance/Stuck Off",
        "part": "3-5-Reverse clutch / PCS 2",
        "meaning": "3rd, 5th or Reverse did not hold. GM PI1131: often the "
                   "pressure-control solenoid in the TEHCM, not the clutch "
                   "itself.",
        "next": "If 3rd and 5th both flare and Reverse is weak, this is the "
                "code. Fluid + TEHCM before a clutch pack. GDS2 Fast Learn "
                "after any TEHCM or fluid work -- not possible on a V-Peak.",
    },
    "P0777": {
        "name": "PCS 2 stuck on -- 3-5-R clutch stuck on",
        "sae": "Pressure Control Solenoid B Stuck On",
        "part": "3-5-Reverse clutch / PCS 2",
        "meaning": "3-5-R clutch will not release.",
        "next": "Bind-up in 1st/2nd/4th/6th. TEHCM / valve body.",
    },
    "P0796": {
        "name": "PCS 3 performance -- 4-5-6 clutch stuck off",
        "sae": "Pressure Control Solenoid C Performance/Stuck Off",
        "part": "4-5-6 clutch / PCS 3",
        "meaning": "4th, 5th or 6th did not hold. GM EI (NHTSA MC-10200174) "
                   "also ties P0796/P2714 to a line solenoid stuck by sediment "
                   "-- sometimes freed by cycling + a fluid change.",
        "next": "If the car dies in 4-5-6 but 1-2-3 are fine, this is the "
                "code. Try fresh Dexron-VI and a TEHCM cycle on GDS2 before "
                "condemning the clutch.",
    },
    "P0797": {
        "name": "PCS 3 stuck on -- 4-5-6 clutch stuck on",
        "sae": "Pressure Control Solenoid C Stuck On",
        "part": "4-5-6 clutch / PCS 3",
        "meaning": "4-5-6 clutch will not release.",
        "next": "Bind-up in 1st/2nd/3rd. TEHCM / valve body.",
    },
    "P0871": {
        "name": "Transmission fluid pressure sensor / switch performance",
        "sae": "Transmission Fluid Pressure Sensor/Switch C Circuit Range/Performance",
        "part": "Line pressure sensor (TEHCM)",
        "meaning": "Measured line pressure does not match commanded.",
        "next": "This is the closest a dongle gets to 'line pressure' -- the "
                "code itself. The live pressure number is GDS2-only. Fluid "
                "level/condition and the TEHCM pump feed first.",
    },
    "P0872": {
        "name": "Transmission fluid pressure sensor low",
        "sae": "Transmission Fluid Pressure Sensor/Switch C Circuit Low",
        "part": "Line pressure sensor",
        "meaning": "Pressure sensor circuit low.",
        "next": "TEHCM / wiring. Do not chase a clutch on this code alone.",
    },
    "P0873": {
        "name": "Transmission fluid pressure sensor high",
        "sae": "Transmission Fluid Pressure Sensor/Switch C Circuit High",
        "part": "Line pressure sensor",
        "meaning": "Pressure sensor circuit high. Listed with P0776/P0796/"
                   "P2714/P2723 on GM PI1131 as a TEHCM solenoid-body fault.",
        "next": "TEHCM. Same bulletin family as the clutch-solenoid codes.",
    },
    "P1870": {
        "name": "Transmission component slipping",
        "sae": "Transmission Component Slipping",
        "part": "TCC or a gear clutch (TCM cannot tell which from this code)",
        "meaning": "Calculated ratio is wrong. Older GM code; on 6T still "
                   "means slip somewhere.",
        "next": "Need the companion code (P0741 vs P0776/P0796/P2714/P2723) "
                "to name the clutch. Capture 1941/1942 plus rpm through the "
                "shift that slips.",
    },
    "P2714": {
        "name": "PCS 4 performance -- 2-6 clutch stuck off",
        "sae": "Pressure Control Solenoid D Performance/Stuck Off",
        "part": "2-6 clutch / PCS 4",
        "meaning": "2nd or 6th did not hold. Same sediment-stuck line-solenoid "
                   "family as P0796 (NHTSA MC-10200174).",
        "next": "2nd and 6th both gone, other gears fine: this clutch / PCS 4. "
                "Fluid + TEHCM before a hard-parts call.",
    },
    "P2715": {
        "name": "PCS 4 stuck on -- 2-6 clutch stuck on",
        "sae": "Pressure Control Solenoid D Stuck On",
        "part": "2-6 clutch / PCS 4",
        "meaning": "2-6 clutch will not release.",
        "next": "Bind-up off 2nd/6th. TEHCM / valve body.",
    },
    "P2723": {
        "name": "PCS 5 performance -- 1-2-3-4 clutch stuck off",
        "sae": "Pressure Control Solenoid E Performance/Stuck Off",
        "part": "1-2-3-4 clutch / PCS 5",
        "meaning": "1st through 4th did not hold. A 3-4 flare with this code "
                   "is the input clutch, not a 'software' problem.",
        "next": "If 1-2-3-4 all suffer and 5th/6th are cleaner, this is the "
                "code. TEHCM / C1234. Fast Learn after the repair -- GDS2.",
    },
    "P2724": {
        "name": "PCS 5 stuck on -- 1-2-3-4 clutch stuck on",
        "sae": "Pressure Control Solenoid E Stuck On",
        "part": "1-2-3-4 clutch / PCS 5",
        "meaning": "1-2-3-4 clutch will not release.",
        "next": "Bind-up in 5th/6th. TEHCM / valve body.",
    },
    "P2762": {
        "name": "TCC pressure control solenoid performance",
        "sae": "Torque Converter Clutch Pressure Control Solenoid Control Circuit Range/Performance",
        "part": "TCC solenoid (TEHCM)",
        "meaning": "TCC solenoid is electrically OK but the converter does "
                   "not follow.",
        "next": "Pair with P0741/P0742. Fluid and TEHCM before a converter.",
    },
    "P2763": {
        "name": "TCC pressure control solenoid high",
        "sae": "Torque Converter Clutch Pressure Control Solenoid Control Circuit High",
        "part": "TCC solenoid",
        "meaning": "TCC solenoid circuit high.",
        "next": "Electrical / TEHCM. Not a clutch pack.",
    },
    "P2764": {
        "name": "TCC pressure control solenoid low",
        "sae": "Torque Converter Clutch Pressure Control Solenoid Control Circuit Low",
        "part": "TCC solenoid",
        "meaning": "TCC solenoid circuit low / open.",
        "next": "Electrical / TEHCM. Not a clutch pack.",
    },
}

# ISO 14229-1 / SAE J2012-DA failure-type byte -- the number after the dash
# on a GDS2 printout. Only the ones a 6T TCM actually uses are named; unknown
# bytes stay as hex so we never invent a meaning.
FAILURE_TYPES: Dict[int, str] = {
    0x00: "no additional failure type",
    0x01: "general electrical failure",
    0x02: "general signal failure",
    0x07: "mechanical failure",
    0x11: "circuit short to ground",
    0x12: "circuit short to battery",
    0x13: "circuit open",
    0x16: "circuit voltage below threshold",
    0x17: "circuit voltage above threshold",
    0x1D: "circuit current out of range",
    0x1F: "circuit intermittent",
    0x21: "signal amplitude below minimum",
    0x22: "signal amplitude above maximum",
    0x23: "signal stuck low",
    0x24: "signal stuck high",
    0x27: "signal rate of change above threshold",
    0x29: "signal invalid",
    0x2A: "signal stuck in range",
    0x2F: "signal erratic",
    0x31: "no signal",
    0x62: "signal compare failure",
    0x64: "signal plausibility failure",
    0x71: "actuator stuck",
    0x72: "actuator stuck open",
    0x73: "actuator stuck closed",
    0x74: "actuator slipping",
    0x7A: "fluid leak or seal failure",
    0x7B: "low fluid level",
    0x92: "performance / incorrect operation",
    0x93: "no operation",
    0x96: "component internal failure",
    0x98: "component or system over temperature",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def enrich_dtc(entry: dict) -> dict:
    """Attach the GM 6T meaning to a decoded fault, in place. Unknown codes
    are left alone -- we do not invent a clutch for a code we do not know."""
    code = (entry.get("code") or "").upper()
    info = TCM_DTCS.get(code)
    if info:
        entry["description"] = info["name"]
        entry["saeDescription"] = info["sae"]
        entry["part"] = info["part"]
        entry["meaning"] = info["meaning"]
        entry["next"] = info["next"]
    ftype_hex = entry.get("failureType")
    try:
        ftype = int(ftype_hex, 16) if ftype_hex is not None else None
    except (TypeError, ValueError):
        ftype = None
    if ftype is not None and ftype in FAILURE_TYPES:
        entry["failureTypeName"] = FAILURE_TYPES[ftype]
    return entry


def _clutch_story(codes: List[str]) -> Optional[str]:
    """One line that names the clutch(es) the stored codes accuse."""
    hits = []
    for key, row in CLUTCH_APPLY.items():
        if row["code"] and row["code"] in codes:
            gears = ", ".join(row["gears"])
            hits.append(f"{row['name']} ({row['solenoid']}) -- used in {gears}")
    return "; ".join(hits) if hits else None


def _diagnose(dtcs: List[dict], live: List[dict], ident: List[dict]) -> List[str]:
    """Plain-language findings. Empty list means 'nothing to say yet'."""
    findings: List[str] = []
    codes = [d.get("code", "").upper() for d in dtcs]
    known = [d for d in dtcs if d.get("code", "").upper() in TCM_DTCS]

    if not dtcs:
        findings.append(
            "TCM fault memory is empty. A flare with no code is still a real "
            "complaint -- capture the 19xx block through the shift "
            "(vauxhall_tcm_capture) before clearing anything."
        )
    else:
        findings.append(
            f"TCM reports {len(dtcs)} fault(s)"
            + (f", {len(known)} with a 6T35/6T40 name." if known
               else ". None of them are in the 6T clutch/sensor table -- "
                    "do not guess a clutch from an unnamed code.")
        )

    story = _clutch_story(codes)
    if story:
        findings.append("Clutch map: " + story + ".")

    for d in known:
        bit = d["display"] if d.get("display") else d.get("code")
        extra = f" ({d['failureTypeName']})" if d.get("failureTypeName") else ""
        findings.append(f"{bit}{extra}: {d.get('meaning')} Next: {d.get('next')}")

    live_by_name = {r["name"]: r for r in live}
    temp_row = live_by_name.get("Transmission fluid temperature")
    if temp_row and temp_row.get("value") not in (None, ""):
        try:
            temp = int(str(temp_row["value"]).split()[0])
        except ValueError:
            temp = None
        if temp is not None:
            if temp < 64:
                findings.append(
                    f"Fluid is {temp} C -- below every published Fast Learn "
                    "window. Warm it before any adapt session on GDS2."
                )
            elif temp > 85:
                findings.append(
                    f"Fluid is {temp} C -- above every published Fast Learn "
                    "window. Let it cool before any adapt session on GDS2."
                )
            else:
                findings.append(
                    f"Fluid is {temp} C -- inside the published Fast Learn "
                    "union (64-85 C). The exact window still lives in this "
                    "TCM's calibration; GDS2 will show it."
                )

    selector = live_by_name.get("Selector position")
    rang = live_by_name.get("Range switch pattern")
    if selector and rang and selector.get("value") and rang.get("value"):
        sel = str(selector["value"])
        raw = str(rang["value"])
        if sel in raw or raw.endswith(f"({sel})"):
            findings.append(
                f"Selector {sel} matches the range switch. Not a P0705/P0706 "
                "kind of problem at this instant."
            )
        else:
            findings.append(
                f"Selector {sel} does not match range switch {raw}. That is "
                "an IMS / lever / wiring problem, not a clutch."
            )

    end_model = next((i.get("value") for i in ident
                      if i.get("label") == "End model part number"), None)
    if end_model:
        findings.append(
            f"TCM end-model part {end_model}. Take this + the VIN to TIS2Web "
            "/ SPS before changing parts -- GM has issued 6T35 shift-quality "
            "calibrations that make Fast Learn unnecessary."
        )

    findings.append(
        "V-Peak cannot run Transmission Adaptive Values Learn (GDS2 "
        "DeviceControl 0xAE, security-gated) and cannot flash a calibration. "
        "It can read everything above. Next tool for adapts / mapping is "
        "GDS2+MDI or an equivalent J2534 pass-through, not another ELM327."
    )
    return findings


def _readings_from(dtcs, live, ident, findings, address, dialect) -> List[dict]:
    readings: List[dict] = [
        {"name": "TCM address", "value": f"0x{address:03X}", "unit": "",
         "description": f"Dialect {dialect or 'unproven'}."},
    ]
    for item in ident:
        readings.append({
            "name": item.get("label") or "ident",
            "value": item.get("value") or "",
            "unit": "",
            "description": f"Read over {item.get('dialect') or dialect}.",
        })
    if not dtcs:
        readings.append({
            "name": "TCM faults", "value": "none stored", "unit": "",
            "description": "Fault memory empty -- not the same as a healthy shift.",
        })
    for d in dtcs:
        readings.append({
            "name": d.get("display") or d.get("code") or "fault",
            "value": d.get("description") or "no 6T name for this code",
            "unit": "",
            "description": " ".join(
                p for p in (
                    d.get("failureTypeName") and f"Failure type: {d['failureTypeName']}.",
                    d.get("part") and f"Part: {d['part']}.",
                    d.get("meaning"),
                    d.get("next"),
                ) if p
            ),
        })
    readings.extend(live)
    for i, line in enumerate(findings, 1):
        readings.append({
            "name": f"Finding {i}", "value": line, "unit": "",
            "description": "From the 6T35/6T40 clutch map + what this TCM just said.",
        })
    return readings


def _worksheet(vin, address, dialect, ident, dtcs, live, findings) -> str:
    lines = [
        "Vauxhall Astra K / GM 6T35 gearbox report (read-only, V-Peak / ELM327)",
        f"VIN: {vin or 'not reported'}",
        f"TCM: 0x{address:03X}  dialect {dialect or 'unproven'}",
        f"Read: {_now()}",
        "",
        "Calibration / identification",
    ]
    if ident:
        for item in ident:
            lines.append(f"    {item.get('label')}: {item.get('value')}")
    else:
        lines.append("    (module answered, no identifiers)")
    lines.append("")
    lines.append("Faults (GM name, not SAE-generic)")
    if not dtcs:
        lines.append("    none stored")
    for d in dtcs:
        lines.append(f"    {d.get('display') or d.get('code')}")
        if d.get("description"):
            lines.append(f"        {d['description']}")
        if d.get("failureTypeName"):
            lines.append(f"        failure type: {d['failureTypeName']}")
        if d.get("saeDescription"):
            lines.append(f"        SAE (wrong on this box): {d['saeDescription']}")
        if d.get("meaning"):
            lines.append(f"        {d['meaning']}")
        if d.get("next"):
            lines.append(f"        next: {d['next']}")
    lines.append("")
    lines.append("Live")
    if not live:
        lines.append("    (no 19xx / OBDb PID answered)")
    for r in live:
        unit = f" {r['unit']}" if r.get("unit") else ""
        lines.append(f"    {r['name']}: {r['value']}{unit}")
    lines.append("")
    lines.append("Diagnosis")
    for line in findings:
        lines.append(f"    - {line}")
    lines.append("")
    lines.append(
        "Sources: Torque GM/Opel PID 221940 @ 7E2; on-car sweep of "
        "W0LPF2EC8HG072884; GM PI1131; NHTSA MC-10200174; Sonnax 6T40 "
        "clutch chart; GMW3110 via Scapy GMLAN; ISO 14229-1 failure types. "
        "Nothing was written to the car."
    )
    return "\n".join(lines)


def analyze(adapter: Optional[dict], mode: str = "real") -> dict:
    """One read-only pass: find the TCM, read calibration + faults + live,
    and say what that combination means on a 6T35."""
    if mode == "sim":
        return _simulated()

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "readings": [], "worksheet": "",
                "dtcs": [], "ident": [], "findings": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not "
                    + _WRONG_BUS
                ),
                "timestamp": _now(),
            }

        vin = obd_service._read_vin(session)
        id_map = dict(VAUXHALL_ID_MAP)
        for req, resp, _, _ in TCM_CANDIDATES:
            id_map[req] = resp
        id_map[ENGINE] = ENGINE + 8

        client = Elm327UdsClient(session, id_map)
        client.begin()
        try:
            chosen = None
            ident: List[dict] = []
            dtcs: List[dict] = []
            dialect = None
            error = None
            trace: List[str] = []
            for req, resp, code, name in TCM_CANDIDATES:
                hit = _probe(client, req, trace)
                if hit is None:
                    continue
                ident, dtcs, dialect, error = _read_module(client, req, hit[1], trace)
                chosen = (req, resp, code, name)
                break

            live: List[dict] = []
            if chosen:
                live = tcm_mod.read_transmission(client)
                if not live:
                    from . import pids as vx_pids
                    live = vx_pids._read_obdb_transmission(client)
        finally:
            client.restore()

        if chosen is None:
            return {
                "ok": True, "simulated": False, "readings": [], "worksheet": "",
                "dtcs": [], "ident": [], "findings": [],
                "message": (
                    "No transmission controller answered at 0x7E2 or 0x7E1. "
                    "Manual car, ignition off, or a TCM on an address neither "
                    "map covers -- run Find modules. Nothing was changed."
                ),
                "timestamp": _now(),
            }

        req, resp, code, name = chosen
        findings = _diagnose(dtcs, live, ident)
        readings = _readings_from(dtcs, live, ident, findings, req, dialect)
        worksheet = _worksheet(vin, req, dialect, ident, dtcs, live, findings)
        unread = " FAULT MEMORY UNREAD -- " + error if error and not dtcs else ""
        return {
            "ok": True, "simulated": False,
            "vehicle": {"vin": vin,
                        "protocol": session.protocol or "ISO 15765-4 (CAN 11/500)",
                        "adapter": (adapter or {}).get("name")},
            "tcm": {"address": f"0x{req:03X}", "response": f"0x{resp:03X}",
                    "code": code, "name": name, "dialect": dialect or "unproven",
                    "error": error, "trace": trace},
            "ident": ident, "dtcs": dtcs, "live": live, "findings": findings,
            "readings": readings, "worksheet": worksheet,
            "message": (
                (f"VIN {vin}. " if vin else "")
                + f"TCM at 0x{req:03X} ({dialect or 'unproven'}). "
                + f"{len(dtcs)} fault(s), {len(live)} live value(s). "
                + f"{len(findings)} finding(s).{unread} "
                "Read-only -- nothing was changed on the car."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "readings": [], "worksheet": "",
                "dtcs": [], "ident": [], "findings": [],
                "message": str(exc), "timestamp": _now()}


def _simulated() -> dict:
    """A 6T35 with the 3-4 flare this car was brought in for."""
    dtcs = [
        enrich_dtc({
            "code": "P2723", "hex": "272300", "failureType": "74",
            "display": "P2723-74", "status": "08", "flags": ["confirmed"],
            "dialect": "gmlan",
        }),
        enrich_dtc({
            "code": "P0716", "hex": "071671", "failureType": "71",
            "display": "P0716-71", "status": "08", "flags": ["confirmed"],
            "dialect": "gmlan",
        }),
    ]
    ident = [
        {"label": "VIN", "value": "W0LPF2EC8HG072884", "dialect": "gmlan"},
        {"label": "End model part number", "value": "24275318", "dialect": "gmlan"},
        {"label": "Base model part number", "value": "24261679", "dialect": "gmlan"},
        {"label": "Software module 01", "value": "55487001 AE", "dialect": "gmlan"},
    ]
    live = [
        {"name": "Transmission fluid temperature", "value": "71", "unit": "°C",
         "description": "SIMULATED. TCM 0x7E2 DID 1940."},
        {"name": "Selector position", "value": "D", "unit": "",
         "description": "SIMULATED. TCM 0x7E2 DID 194F."},
        {"name": "Range switch pattern", "value": "0x30 (D)", "unit": "",
         "description": "SIMULATED. TCM 0x7E2 DID 1951."},
        {"name": "Fast Learn temperature window",
         "value": "In range of the published windows (64-85 °C).", "unit": ""},
    ]
    findings = _diagnose(dtcs, live, ident)
    readings = _readings_from(dtcs, live, ident, findings, 0x7E2, "gmlan")
    worksheet = _worksheet("W0LPF2EC8HG072884", 0x7E2, "gmlan",
                           ident, dtcs, live, findings)
    return {
        "ok": True, "simulated": True,
        "vehicle": {"vin": "W0LPF2EC8HG072884",
                    "protocol": "ISO 15765-4 (CAN 11/500)",
                    "adapter": "Simulated"},
        "tcm": {"address": "0x7E2", "response": "0x7EA", "code": "7E2",
                "name": "Transmission (TCM) -- Astra K address",
                "dialect": "gmlan", "error": None, "trace": []},
        "ident": ident, "dtcs": dtcs, "live": live, "findings": findings,
        "readings": readings, "worksheet": worksheet,
        "message": (
            "SIMULATED -- VIN W0LPF2EC8HG072884. TCM at 0x7E2. "
            "2 faults (P2723-74 1-2-3-4 clutch slipping, P0716-71 ISS). "
            "No real car connected."
        ),
        "timestamp": _now(),
    }
