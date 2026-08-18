"""Astra K transmission controller -- the map as verified on a real car.

WHY THIS FILE EXISTS
--------------------
pids.py reads the transmission over the OBDb VauxhallOpel signalset: TCM at
0x7E1, DIDs 04FE / 5503 / 5504. On VIN W0LPF2EC8HG072884 (Astra K, 2017,
6-speed automatic) every one of those assumptions is wrong:

    0x7E1   silent to 3E 00, 1A 90, 22 F190 and A9 82, ignition on AND with
            the engine running, in the default session and after 10 03. It is
            not a sleeping module. Nothing is at that address.

    0x7E2   answers 1A 90 with the car's VIN, identifies as GM part 24275318
            (base 24261679, boot 24262989, software modules 55487001/2/3 AE),
            broadcast code 5318 -- and carries a live parameter block in the
            0x19xx range. This is the transmission controller on this car.

    04FE / 5503 / 5504 return NRC 31 (request out of range) at BOTH addresses.
            The module speaks service 0x22 perfectly well; those particular
            parameter ids simply do not exist in this calibration.

So the signal map below was not taken from a database. It was derived on the
car by changing something physical, re-reading, and keeping only what moved the
way it had to. The evidence for each signal is recorded next to it, because a
map built this way is exactly as trustworthy as the evidence behind it and no
more -- and the next person (or the next Astra) deserves to see the working.

CONFIDENCE GRADES
-----------------
    verified   The value tracked a physical change that could only produce
               that response. Safe to display with a label.
    inferred   Behaviour is consistent with the named signal but an
               alternative explanation survives. Displayed WITH the raw value
               and marked, never as a bare decoded number.
    unknown    Answers, but nothing observed has pinned it down. Logged raw so
               a drive capture can settle it. Never given a friendly name.

These grades describe what THIS CAR has shown, and they still do. A second
field, `source`, was added when the dealer data arrived, and records something
different: where the name came from.

    on-car       Named by what the car did, as above.
    dealer-data  Named by the GDS2 extract in data/gds2_t76_tcm.json.
    both         Named by the car and by the dealer file, in agreement.

A signal the dealer file names but this car has never been made to move stays
`inferred` no matter how official the source. Being in GM's own parameter list
is documentary evidence about the calibration, not evidence about this
gearbox. Where the two sources disagree, the disagreement is written down and
neither is quietly dropped.

THE GAP THAT REMAINS -- REVISITED
---------------------------------
This section used to say that no gear register had been identified, and that
clutch adaptive cells, line pressure and clutch slip were genuinely
unavailable at this port. The dealer data changes both statements, and it is
worth being precise about how much.

**The gear register.** The T76 parameter list names two: `Gear Command` and
`Shift Transmission Gear Command`, both on parameter id 0x199A, with a state
table running 1-8 plus 9 for Reverse. It also names `Gear Ratio` on 0x195B,
scaled by 0.000122. So the register the standstill tests could not find does
exist, and it is a *commanded* gear rather than an achieved one -- which, for
diagnosing a flare, is the more useful of the two, because a flare is exactly
the gap between the commanded gear and the delivered ratio. Neither has yet
answered on this car, because neither had been asked for: 0x199A and 0x195B
were outside the 0x1940-0x1957 range the original sweep covered. They are in
SIGNALS now, graded `inferred` with source `dealer-data`, and a drive capture
settles them.

**What 0x194C actually is.** It was logged here as an unidentified parameter
and called the prime gear-register candidate, on the reasoning that a gear
register reads zero standing still. The dealer file names it `4-5 Shift Time`,
scaled 0.025 s. That fits the observation better than the gear hypothesis did
-- a shift-time register also reads zero until the shift it measures happens --
and it retires the candidacy. It is one of a family: 0x1992 last shift, 0x1993
1-2, 0x1994 2-3, 0x1995 3-4, 0x280A 5-6.

**Clutch adaptive cells and line pressure.** The claim that these are
unreachable over this port was wrong, and specifically wrong about the
transport. Thirteen `Steady State TAP` cells -- one per gear, with and without
the torque converter clutch applied, plus reverse -- are packed into a single
block read at GMLAN identifier 0x50, each cell one byte at its own offset,
scaled by 2 kPa. That is service 0x1A, which gmlan.py already speaks, not
service 0x22, which is why sweeping the 0x22 parameter space could never have
found them. `Calculated Line Pressure` is on 0x2862 and the five solenoid
pressure commands on 0x2817-0x281C, all ordinary 0x22 reads. `TCC Slip Speed`
is 0x1991.

None of those have been read from this car yet. read_adaptive_cells() asks for
them; until it has answered on the real gearbox they are dealer-data claims
about a calibration, and they are labelled as such.
"""

from datetime import datetime, timezone
from time import monotonic, sleep
from typing import Callable, Dict, List, Optional, Tuple

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

from . import dealer_data

# The transmission controller's address on this car. Response follows the
# ISO 15765-4 +8 rule like the rest of the powertrain block, and the dealer
# file's connection record for this module node agrees: request 0x7E2,
# response 0x7EA, GMLAN_HS_PRIMARY_500k_Pin6&14.
TCM = 0x7E2
ENGINE = 0x7E0

# Selector positions on DID 194F. The three marked "on-car" were read by moving
# the lever and re-reading; N was not captured because the test was cut short at
# the driver's discretion. The dealer file's own state table for this parameter
# (ref 7975_51909, mask (X>>0)&0x7F) names all six, agrees with all three
# observations, and supplies the ones the lever test missed.
SELECTOR_MAP: Dict[int, str] = {
    0: "Invalid",
    1: "P",
    2: "R",
    4: "N",
    6: "D",
    8: "Manual",
}
SELECTOR_OBSERVED = (1, 2, 6)

# Internal mode switch / range switch raw patterns from DID 1951, as observed on
# this car by moving the lever.
RANGE_SWITCH_PATTERNS: Dict[int, str] = {
    0x48: "P",
    0x18: "R",
    0x30: "D",
}

# The dealer file names this parameter "Internal Mode Switch A/B/C/P" and reads
# it as (X>>3)&0xF, naming seven of the sixteen possible contact patterns as
# High/Low combinations of the four switch contacts.
#
# Those seven do not include any of the three read off this car: 0x48, 0x18 and
# 0x30 reduce to 9, 3 and 6 under that mask, and 9, 3 and 6 are all absent from
# the table. This is a real disagreement between an authoritative source and a
# reproducible observation, and it is left standing rather than resolved by
# guessing. Either the mask differs on this calibration, or the table is
# partial. Until that is settled the on-car patterns are what gets displayed,
# because they were measured against a lever that was physically moved.
RANGE_SWITCH_DEALER_REF = "7982_51909"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _u8(b: bytes) -> Optional[int]:
    return b[0] if len(b) >= 1 else None


def _u16(b: bytes) -> Optional[int]:
    return (b[0] << 8) | b[1] if len(b) >= 2 else None


def _fluid_temp_c(b: bytes) -> Optional[int]:
    """One byte, C = A - 40.

    Evidence: read 63 with the engine cold and stopped, when generic OBD
    coolant read 23 C -- 63 - 40 = 23, an exact match on a car that had been
    standing. Over the following minutes of idling it moved 63 -> 66 -> 68 ->
    70 (23 -> 30 C) while coolant climbed 23 -> 51 C. Transmission fluid
    warming far slower than coolant is exactly the expected behaviour, and a
    byte that both starts at ambient and rises slowly under idle is a fluid
    temperature. Same -40 offset as every other single-byte temperature in the
    standard.
    """
    v = _u8(b)
    if v is None:
        return None
    c = v - 40
    return c if -40 <= c <= 200 else None


def _selector(b: bytes) -> Optional[str]:
    v = _u8(b)
    if v is None:
        return None
    # The dealer file masks this parameter with 0x7F before looking it up.
    name = SELECTOR_MAP.get(v & 0x7F)
    if name is None:
        return f"0x{v:02X} (position not in the dealer state table)"
    if (v & 0x7F) in SELECTOR_OBSERVED:
        return name
    return f"{name} [dealer-data, not yet seen on this car]"


def _range_switch(b: bytes) -> Optional[str]:
    v = _u8(b)
    if v is None:
        return None
    known = RANGE_SWITCH_PATTERNS.get(v)
    return f"0x{v:02X} ({known})" if known else f"0x{v:02X} (pattern not yet observed)"


def _shaft_speed(b: bytes) -> Optional[str]:
    """Raw 16-bit value, shown raw with the /8 rpm reading beside it.

    Evidence, all at a standstill: 0 with the engine stopped, 5736 idling in P
    (engine 785 rpm), 5079 in D (engine drooping under converter load), 0 in R.
    Zero-when-stopped and dropping when the converter loads proves it is a
    driveline speed. raw/8 gives 717 rpm in P against 785 engine rpm, which is
    the right order of magnitude for converter slip at idle. The 2026-08-13
    drive captures then settled the scaling: in every 5th-gear sample with the
    converter locked, raw/8 matched engine rpm within about 70 rpm.

    The dealer file agrees exactly on the number and names the parameter:
    0x1941 is `Transmission ISS`, scaled by 0.125, which is the same divide by
    eight arrived at from the car.

    The old contradiction survives the agreement and is left standing. Turbine
    speed should read zero in Drive at a standstill with the brake held, and
    this reads 5079. Two sources now call it the input shaft, and the car's
    behaviour still does not look like an input shaft's at rest. The raw value
    therefore stays on display and the row stays `inferred`. A drive capture
    against road speed is what settles it.
    """
    v = _u16(b)
    if v is None:
        return None
    return f"{v} raw (~{v / 8:.0f} rpm)"


def _shift_time_s(b: bytes) -> Optional[str]:
    """One byte scaled 0.025 s, per the dealer file's shift-time family."""
    v = _u8(b)
    if v is None:
        return None
    return f"{v * 0.025:.3f} s ({v} raw)"


def _gear_command(b: bytes) -> Optional[str]:
    """Commanded gear on 0x199A, using the dealer file's own state table."""
    v = _u8(b)
    if v is None:
        return None
    states = dealer_data.states_for("7736_51909")
    name = states.get(v)
    return f"{name} ({v} raw)" if name else f"{v} raw (not in the state table)"


def _gear_ratio(b: bytes) -> Optional[str]:
    v = _u16(b)
    if v is None:
        return None
    return f"{v * 0.000122:.3f} ({v} raw)"


def _tcc_slip(b: bytes) -> Optional[str]:
    v = _u16(b)
    if v is None:
        return None
    return f"{v * 0.125:.0f} rpm ({v} raw)"


def _learn_status(b: bytes) -> Optional[str]:
    """The fast-learn status parameter, 0x2823.

    This is a plain read and it is the single most useful thing the dealer data
    gave up, because it can be polled on any car right now, with nothing
    written, and it says whether the module considers a fast learn possible and
    what it is waiting for if not.
    """
    v = _u8(b)
    if v is None:
        return None
    states = dealer_data.states_for("8682_51909")
    name = states.get(v)
    return f"{name} ({v})" if name else f"state {v} (not in the state table)"


def _raw_u8(b: bytes) -> Optional[str]:
    v = _u8(b)
    return None if v is None else f"0x{v:02X} ({v})"


def _raw_u16(b: bytes) -> Optional[str]:
    v = _u16(b)
    return None if v is None else str(v)


# The answering parameter block. Each row is
# (did, name, unit, confidence, source, decode, evidence).
#
# `confidence` grades what this car has shown; `source` records where the name
# came from. See the header for why those are two different questions.
#
# 1943-194B, 194D, 194E, 1950, 1952-1955 and 1957 all answered NRC 31 and are
# genuinely absent from this calibration -- they are not listed as "silent",
# they were asked and refused. The four rows added from the dealer file sit
# outside the range that sweep covered, so their silence has never been tested
# either; if they refuse, they refuse, and that is a finding worth having.
SIGNALS: List[Tuple[int, str, str, str, str,
                    Callable[[bytes], Optional[str]], str]] = [
    (0x1940, "Transmission fluid temperature", "°C", "verified", "both",
     _fluid_temp_c,
     "Started at ambient (23 C, matching coolant on a cold car), rose to 30 C "
     "over several minutes of idling while coolant reached 51 C. The dealer "
     "file names the same id with the same (X*1)-40 scaling, which is the "
     "ground-truth check on the whole extract."),
    (0x194F, "Selector position", "", "verified", "both", _selector,
     "Moved 1 -> 6 -> 2 as the lever went P -> D -> R. The dealer state table "
     "(ref 7975_51909) agrees on all three and adds 0 Invalid, 4 Neutral and "
     "8 Manual, which the lever test did not reach."),
    (0x1951, "Range switch pattern", "", "verified", "on-car", _range_switch,
     "Moved 0x48 -> 0x30 -> 0x18 with the lever. Distinct bit patterns per "
     "position, consistent with a GM internal mode switch. The dealer file "
     "names this parameter and gives seven contact patterns under a (X>>3)&0xF "
     "mask, none of which matches any of the three measured here -- so the "
     "measured values are what is shown and the conflict is left open."),
    (0x1941, "Transmission ISS (input shaft speed)", "", "inferred", "both",
     _shaft_speed,
     "0 stopped, 5736 idling in P, 5079 in D, 0 in R, and raw/8 tracked engine "
     "rpm within ~70 rpm in locked 5th on the 2026-08-13 drives. The dealer "
     "file names it Transmission ISS at exactly that 0.125 scaling. Still "
     "inferred because 5079 in Drive at a standstill is not what an input "
     "shaft should read."),
    (0x1942, "Transmission OSS (output shaft speed)", "", "inferred",
     "dealer-data", _raw_u16,
     "Zero in every standstill test including with the engine running, which "
     "is what an output shaft does when the car is not moving and therefore "
     "proves nothing. The dealer file names it Transmission OSS at 0.125 "
     "rpm/count. A drive capture is what settles it."),
    (0x194C, "4-5 shift time", "s", "inferred", "dealer-data", _shift_time_s,
     "Answered 0 in every standstill state tested. This row used to be the "
     "prime gear-register candidate; the dealer file names it 4-5 Shift Time "
     "at 0.025 s/count, which fits the observation better -- a shift-time "
     "register also reads zero until the shift it measures happens."),
    (0x199A, "Commanded gear", "", "inferred", "dealer-data", _gear_command,
     "Never read on this car: 0x199A is outside the 0x1940-0x1957 range the "
     "original sweep covered. The dealer file names it Gear Command with a "
     "state table of 1-8 plus 9 for Reverse. This is the gear register the "
     "standstill tests could not find."),
    (0x195B, "Gear ratio", "", "inferred", "dealer-data", _gear_ratio,
     "Never read on this car. The dealer file names it Gear Ratio at "
     "0.000122/count. Against commanded gear this is the pair that shows a "
     "flare directly: the ratio failing to arrive where the command went."),
    (0x1991, "TCC slip speed", "rpm", "inferred", "dealer-data", _tcc_slip,
     "Never read on this car. The dealer file names it TCC Slip Speed at "
     "0.125 rpm/count -- the converter-clutch measurement this profile "
     "previously had to derive from ISS against engine rpm."),
    (0x2823, "Fast learn status", "", "inferred", "dealer-data", _learn_status,
     "Never read on this car. The dealer file's 17-state table is the status "
     "screen GDS2 shows during Transmission Adaptive Values Learn. It is a "
     "plain read and it reports what the module is waiting for -- brake, "
     "Park, Drive, fluid temperature -- without anything being written."),
]

# Engine values worth having on the same timeline as the gearbox ones. A flare
# is only visible as gear behaviour AGAINST rpm, so a capture that logs the
# gearbox alone cannot show one.
CAPTURE_OBD_PIDS: List[Tuple[int, str, str, Callable[[bytes], Optional[float]]]] = [
    (0x0C, "engine_rpm", "rpm", lambda b: ((b[0] << 8) | b[1]) / 4 if len(b) >= 2 else None),
    (0x0D, "vehicle_speed", "km/h", lambda b: float(b[0]) if len(b) >= 1 else None),
    (0x11, "throttle", "%", lambda b: b[0] * 100 / 255 if len(b) >= 1 else None),
]


def _read_did(client: Elm327UdsClient, header: int, did: int,
              timeout: float = 1.0) -> Optional[bytes]:
    """One service 0x22 read. Payload after the echoed parameter id, or None."""
    try:
        payload = client.request(header, bytes([0x22, did >> 8, did & 0xFF]), timeout=timeout)
    except (ObdError, TransportError):
        return None
    if len(payload) < 4 or payload[0] != 0x62:
        return None
    if (payload[1] << 8 | payload[2]) != did:
        return None
    return payload[3:]


def _read_pid(client: Elm327UdsClient, pid: int, timeout: float = 1.0) -> Optional[bytes]:
    """One generic Mode-01 read on the engine."""
    try:
        payload = client.request(ENGINE, bytes([0x01, pid]), timeout=timeout)
    except (ObdError, TransportError):
        return None
    if len(payload) < 3 or payload[0] != 0x41 or payload[1] != pid:
        return None
    return payload[2:]


_SOURCE_WORDING = {
    "on-car": "Verified on this car",
    "both": "Verified on this car and named by the GDS2 dealer data",
    "dealer-data": "Named by the GDS2 dealer data, not yet seen on this car",
}


def _name_answer(did: int, body: bytes) -> List[dict]:
    """Every dealer-named signal a raw 0x22 answer carries, decoded.

    A sweep used to hand back nothing but hex, which meant a parameter that
    answered was still an unknown until somebody went looking. The dealer file
    turns most of that hex into names on the spot -- and one request often
    carries several signals, so this returns a list. 0x281D packs four
    transmission fluid pressure switches into one byte.

    Nothing here is proof about this car. It is the GDS2 reading of the same
    bytes, offered alongside the raw payload rather than instead of it, so the
    two can be compared rather than confused.
    """
    out = []
    for entry in dealer_data.by_did(did):
        decoded = dealer_data.decode(entry, body)
        if decoded is not None:
            out.append(decoded)
    return out


# The clutch adaptive pressure cells: thirteen one-byte values packed into the
# single GMLAN identifier 0x50 block, scaled 2 kPa each. Kept as a list of the
# dealer reference names in the order the file gives their byte offsets, so the
# reader can see that this is one request and not thirteen.
ADAPTIVE_CELL_IDENTIFIER = 0x50


def read_adaptive_cells(client: Elm327UdsClient) -> Optional[dict]:
    """Read the Steady State TAP cells -- the clutch adapt values.

    This is the reading the profile said for a long time was unavailable over
    a dongle, and the reason it looked unavailable was a wrong assumption about
    the transport rather than about the data. The cells are not service 0x22
    parameters at all; they are a block behind GMLAN ReadDataByIdentifier
    (0x1A) at identifier 0x50, which is why sweeping the 0x22 space could never
    have turned them up.

    Read-only: 0x1A is the identification service the profile already uses for
    part numbers.

    Returns None if the module does not answer, which is itself worth knowing
    -- these values have never been read from this car, and the dealer file is
    a statement about the T76 calibration, not about this gearbox.
    """
    try:
        payload = client.request(
            TCM, bytes([0x1A, ADAPTIVE_CELL_IDENTIFIER]), timeout=1.5
        )
    except (ObdError, TransportError):
        return None
    if len(payload) < 3 or payload[0] != 0x5A or payload[1] != ADAPTIVE_CELL_IDENTIFIER:
        return None
    body = payload[2:]
    cells = []
    for entry in dealer_data.by_request("1A 50"):
        decoded = dealer_data.decode(entry, body)
        if decoded is not None:
            cells.append(decoded)
    cells.sort(key=lambda c: c["name"])
    return {
        "request": "1A 50",
        "payload": body.hex().upper(),
        "cells": cells,
        "confidence": "inferred",
        "source": "dealer-data",
        "note": (
            "Cell names, byte offsets and the 2 kPa scaling come from the GDS2 "
            "T76 data set, not from this car. Nothing has yet confirmed that "
            "this module lays the block out the way the file says. Compare the "
            "raw payload against the decoded cells before trusting a number."
        ),
    }


def read_transmission(client: Elm327UdsClient) -> List[dict]:
    """The TCM block as display readings, each carrying its grade and source."""
    readings: List[dict] = []
    for did, name, unit, confidence, source, decode, evidence in SIGNALS:
        body = _read_did(client, TCM, did)
        if body is None:
            continue
        try:
            value = decode(body)
        except Exception:
            continue
        if value is None:
            continue
        label = name if confidence == "verified" else f"{name} [{confidence}]"
        readings.append({
            "name": label,
            "value": str(value),
            "unit": unit,
            "confidence": confidence,
            "source": source,
            "description": f"TCM 0x{TCM:03X} DID {did:04X}, confidence: "
                           f"{confidence}, source: {source}. "
                           f"{_SOURCE_WORDING.get(source, 'Source unrecorded')}: "
                           f"{evidence}",
        })
    return readings


def sweep(adapter: Optional[dict], first: int = 0x1900, last: int = 0x19FF,
          mode: str = "real") -> dict:
    """Walk a parameter-id range on the TCM and report which ids answer.

    This is how the block above was found, kept as a tool because one sweep of
    0x1940-0x1957 is not the whole calibration. An id that answers NRC 31 is
    reported as absent-by-refusal, which is a different and more useful fact
    than silence: it proves the module is listening and the id is simply not
    implemented.
    """
    if mode == "sim":
        return {
            "ok": True, "simulated": True, "answered": [], "refused": [], "silent": [],
            "message": "SIMULATED -- no real car connected.", "timestamp": _now(),
        }

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "answered": [], "refused": [], "silent": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k High Speed GMLAN this module lives on."
                ),
                "timestamp": _now(),
            }

        client = Elm327UdsClient(session, {TCM: TCM + 8, ENGINE: ENGINE + 8})
        client.begin()
        answered, refused, silent = [], [], []
        try:
            for did in range(first, last + 1):
                try:
                    payload = client.request(
                        TCM, bytes([0x22, did >> 8, did & 0xFF]), timeout=0.6
                    )
                except (ObdError, TransportError):
                    silent.append(f"{did:04X}")
                    continue
                if not payload:
                    silent.append(f"{did:04X}")
                elif payload[0] == 0x62:
                    row = {"did": f"{did:04X}",
                           "payload": payload[3:].hex().upper()}
                    named = _name_answer(did, payload[3:])
                    if named:
                        row["dealerData"] = named
                    answered.append(row)
                elif payload[0] == 0x7F:
                    refused.append({"did": f"{did:04X}",
                                    "nrc": f"{payload[2]:02X}" if len(payload) >= 3 else "??"})
                else:
                    silent.append(f"{did:04X}")
        finally:
            client.restore()

        return {
            "ok": True, "simulated": False,
            "answered": answered, "refused": refused, "silent": silent,
            "message": (
                f"Swept {last - first + 1} parameter id(s) on the TCM at 0x{TCM:03X}: "
                f"{len(answered)} answered, {len(refused)} refused as not implemented, "
                f"{len(silent)} silent. Read-only -- only service 0x22 was sent."
            ),
            "timestamp": _now(),
        }

    return obd_service._with_session(adapter, run)


def capture(adapter: Optional[dict], seconds: float = 120.0,
            mode: str = "real", fast: bool = False) -> dict:
    """Log the TCM block against engine rpm and road speed through a drive.

    THIS is the flare diagnostic. A flare cannot be seen in a single reading:
    it is a shape over time -- the gearbox failing to deliver a ratio while
    engine rpm climbs into the gap. So this polls the answering parameters and
    the engine as fast as the dongle allows, timestamps every sample, and hands
    back the series for analysis afterwards.

    It also does the discovery job the standstill tests could not. Log this
    through a drive and the gear register identifies itself: it is the
    parameter that steps up and down in time with road speed. The unknown and
    inferred rows are logged raw for exactly that reason.

    Over a Bluetooth ELM327 expect roughly one sample per 4-5 seconds with the
    default set: each of the nine reads costs a full BLE round trip. That is
    enough to see a flare that lasts several seconds; it is not enough to
    resolve the internals of a clean 200 ms shift, and the result says so
    rather than letting the sample rate be mistaken for precision.

    fast=True trims the loop to the three reads a flare actually needs --
    input shaft speed (1941), output shaft speed (1942) and engine rpm --
    which roughly triples the rate. Fluid temperature and throttle are still
    read every fifth tick so cold-start behaviour and pedal position stay on
    the timeline without paying their round-trip cost every sample. Selector,
    range switch and 194C are dropped entirely: selector position is known
    from the lever and 194C has never decoded to anything.

    The 2026-08-13 drive captures settled the ISS scaling: in every 5th-gear
    sample (ratio 1.00, converter locked) 1941/8 matched engine rpm within
    ~70 rpm. So fast samples also carry the derived values that matter live:
    gear ratio (1941/1942) and converter slip (engine rpm minus 1941/8). A
    flare is the ratio leaving one gear's value without arriving at the
    next while slip stays modest; converter slip is that same rpm rise with
    the ratio pinned.
    """
    if mode == "sim":
        return {
            "ok": True, "simulated": True, "samples": [],
            "message": "SIMULATED -- no real car connected.", "timestamp": _now(),
        }

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "samples": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k High Speed GMLAN this capture needs."
                ),
                "timestamp": _now(),
            }

        client = Elm327UdsClient(session, {TCM: TCM + 8, ENGINE: ENGINE + 8})
        client.begin()
        samples: List[dict] = []
        started = monotonic()
        misses = 0
        tick = 0
        try:
            while monotonic() - started < seconds:
                sample = {"t": round(monotonic() - started, 3)}
                if fast:
                    iss = oss = None
                    body = _read_did(client, TCM, 0x1941, timeout=0.6)
                    sample["did_1941"] = body.hex().upper() if body else None
                    iss = _u16(body) if body else None
                    body = _read_did(client, TCM, 0x1942, timeout=0.6)
                    sample["did_1942"] = body.hex().upper() if body else None
                    oss = _u16(body) if body else None
                    body = _read_pid(client, 0x0C, timeout=0.6)
                    rpm = ((body[0] << 8) | body[1]) / 4 if body and len(body) >= 2 else None
                    sample["engine_rpm"] = round(rpm, 1) if rpm is not None else None
                    if tick % 5 == 0:
                        body = _read_did(client, TCM, 0x1940, timeout=0.6)
                        sample["did_1940"] = body.hex().upper() if body else None
                        body = _read_pid(client, 0x11, timeout=0.6)
                        sample["throttle"] = (
                            round(body[0] * 100 / 255, 1) if body and len(body) >= 1 else None
                        )
                    # Derived on the spot so a live reader can watch the shape
                    # without redoing the hex: ratio for gear, turbine rpm and
                    # converter slip from the /8 scaling proven on this car.
                    if iss and oss:
                        sample["ratio"] = round(iss / oss, 3)
                    if iss is not None:
                        sample["turbine_rpm"] = round(iss / 8, 1)
                        if rpm is not None:
                            sample["converter_slip_rpm"] = round(rpm - iss / 8, 1)
                    dead = sample.get("engine_rpm") is None and sample.get("did_1941") is None
                else:
                    for did, name, _unit, _conf, _src, _decode, _ev in SIGNALS:
                        body = _read_did(client, TCM, did, timeout=0.6)
                        sample[f"did_{did:04X}"] = body.hex().upper() if body else None
                    for pid, key, _unit, decode in CAPTURE_OBD_PIDS:
                        body = _read_pid(client, pid, timeout=0.6)
                        try:
                            sample[key] = round(decode(body), 1) if body else None
                        except Exception:
                            sample[key] = None
                    dead = sample.get("engine_rpm") is None and sample.get("did_1940") is None
                tick += 1
                if dead:
                    misses += 1
                    if misses >= 3:
                        break
                    sleep(0.3)
                    continue
                misses = 0
                samples.append(sample)
        finally:
            client.restore()

        rate = len(samples) / seconds if seconds else 0
        if fast:
            message = (
                f"Fast capture: {len(samples)} sample(s) over {seconds:.0f}s "
                f"(~{rate:.2f}/s). ISS/OSS/rpm every tick with derived ratio, "
                "turbine rpm and converter slip; fluid temp and throttle every "
                "fifth tick. Selector, range switch and 194C not read. "
                "Read-only throughout."
            )
        else:
            message = (
                f"Captured {len(samples)} sample(s) over {seconds:.0f}s "
                f"(~{rate:.1f}/s). Every TCM parameter is stored raw so the gear "
                "register can be identified by which one steps with road speed. "
                "At this rate a flare lasting a second or more is visible; the "
                "internals of a clean fast shift are not. Read-only throughout."
            )
        return {
            "ok": True, "simulated": False, "samples": samples,
            "sampleRate": round(rate, 2),
            "message": message,
            "timestamp": _now(),
        }

    return obd_service._with_session(adapter, run)
