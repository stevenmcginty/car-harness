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

THE GAP THAT REMAINS
--------------------
No gear register has been identified yet. Every candidate read zero at a
standstill, which is what a gear register does when the car is not moving, so
the standstill tests could not separate "this is the gear" from "this is
something else that happens to be zero". That is what capture() is for: log
the whole answering block through a drive and the gear register is the one
that steps 1-2-3-4-5-6 in time with road speed. Nothing here guesses it.

Clutch adaptive cells, line pressure and clutch slip remain genuinely
unavailable at this port -- that part of the original research holds. Those
live in the GDS2 parameter list and no amount of sweeping this module exposes
them.
"""

from datetime import datetime, timezone
from time import monotonic, sleep
from typing import Callable, Dict, List, Optional, Tuple

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

# The transmission controller's address on this car. Response follows the
# ISO 15765-4 +8 rule like the rest of the powertrain block.
TCM = 0x7E2
ENGINE = 0x7E0

# Selector positions, read off DID 194F by moving the lever and re-reading.
# N was not captured -- the test was cut short at the driver's discretion -- so
# it is absent rather than interpolated. 3, 4 and 5 are unobserved.
SELECTOR_MAP: Dict[int, str] = {
    1: "P",
    2: "R",
    6: "D",
}

# Internal mode switch / range switch raw patterns from DID 1951. GM range
# switches encode position as a multi-bit pattern rather than an ordinal, and
# the three observed values are distinct 4-bit patterns in the high nibble+1
# region, which is consistent with that. Kept as raw hex with the position it
# was observed in; no attempt is made to decode the individual switch contacts.
RANGE_SWITCH_PATTERNS: Dict[int, str] = {
    0x48: "P",
    0x18: "R",
    0x30: "D",
}


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
    return SELECTOR_MAP.get(v)


def _range_switch(b: bytes) -> Optional[str]:
    v = _u8(b)
    if v is None:
        return None
    known = RANGE_SWITCH_PATTERNS.get(v)
    return f"0x{v:02X} ({known})" if known else f"0x{v:02X} (pattern not yet observed)"


def _shaft_speed(b: bytes) -> Optional[str]:
    """Raw 16-bit value, shown raw with a divided-by-8 reading beside it.

    Evidence, all at a standstill: 0 with the engine stopped, 5736 idling in P
    (engine 785 rpm), 5079 in D (engine drooping under converter load), 0 in R.
    Zero-when-stopped and dropping when the converter loads proves it is a
    driveline speed. raw/8 gives 717 rpm in P against 785 engine rpm, which is
    the right order of magnitude for converter slip at idle.

    It is NOT called turbine speed, because turbine speed should read zero in D
    at a standstill with the brake held and this reads 5079. That contradiction
    is unresolved, so the raw value is always shown and the scaling is marked
    inferred. A drive capture against road speed will settle both.
    """
    v = _u16(b)
    if v is None:
        return None
    return f"{v} raw (~{v / 8:.0f} rpm if /8)"


def _raw_u8(b: bytes) -> Optional[str]:
    v = _u8(b)
    return None if v is None else f"0x{v:02X} ({v})"


def _raw_u16(b: bytes) -> Optional[str]:
    v = _u16(b)
    return None if v is None else str(v)


# The answering parameter block, as swept on the car. Each row is
# (did, name, unit, confidence, decode, evidence).
#
# 1943-194B, 194D, 194E, 1950, 1952-1955 and 1957 all answered NRC 31 and are
# genuinely absent from this calibration -- they are not listed as "silent",
# they were asked and refused.
SIGNALS: List[Tuple[int, str, str, str, Callable[[bytes], Optional[str]], str]] = [
    (0x1940, "Transmission fluid temperature", "°C", "verified", _fluid_temp_c,
     "Started at ambient (23 C, matching coolant on a cold car), rose to 30 C "
     "over several minutes of idling while coolant reached 51 C."),
    (0x194F, "Selector position", "", "verified", _selector,
     "Moved 1 -> 6 -> 2 as the lever went P -> D -> R."),
    (0x1951, "Range switch pattern", "", "verified", _range_switch,
     "Moved 0x48 -> 0x30 -> 0x18 with the lever. Distinct bit patterns per "
     "position, consistent with a GM internal mode switch."),
    (0x1941, "Driveline shaft speed", "", "inferred", _shaft_speed,
     "0 stopped, 5736 idling in P, 5079 in D, 0 in R. A driveline speed for "
     "certain; which shaft is unresolved."),
    (0x1942, "Second shaft speed (output candidate)", "", "inferred", _raw_u16,
     "Zero in every standstill test including with the engine running. "
     "Consistent with output shaft speed but untested -- the car never moved."),
    (0x194C, "Unidentified parameter 194C", "", "unknown", _raw_u8,
     "Answered 0 in every standstill state tested. Prime gear-register "
     "candidate precisely because a gear register reads zero standing still."),
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


def read_transmission(client: Elm327UdsClient) -> List[dict]:
    """The verified TCM block as display readings, each carrying its grade."""
    readings: List[dict] = []
    for did, name, unit, confidence, decode, evidence in SIGNALS:
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
            "description": f"TCM 0x{TCM:03X} DID {did:04X}, confidence: {confidence}. "
                           f"Verified on this car: {evidence}",
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
                    answered.append({"did": f"{did:04X}", "payload": payload[3:].hex().upper()})
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
                    for did, name, _unit, _conf, _decode, _ev in SIGNALS:
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
