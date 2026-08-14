"""EP6 (Prince / 1.6 THP) oil-pressure diagnosis over a generic ELM327.

WHY THIS FILE EXISTS
--------------------
A 308 CC 1.6 THP (EP6CDT, "5FV") shows the oil-pressure warning at start-up
after the whole mechanical chain has already been renewed: timing chain, oil
pump, oil-pump solenoid, big-end bearings, oil-pressure sensor. The next step
is data, not parts: what does the engine ECU actually see and command around
the oil circuit, second by second, through a cold start -- and how does that
differ from a known-good car with the same engine (a 2013 RCZ THP)?

The EP6CDT runs a two-stage variable-displacement oil pump: the ECU switches
the pump between a low-pressure and a high-pressure stage through a solenoid
valve, watches the analogue oil-pressure sensor, and raises the warning when
measured pressure stays below what the current rpm/stage demands (the related
fault codes -- P0945, P15A0-family -- name the regulation solenoid directly).
So the interesting signals are: measured oil pressure, the ECU's
setpoint/stage decision, and the solenoid command.

THE HONEST PROBLEM (updated 2026-08-14): the diesel DPF sources (ludwig-v
zones, PSA-RE, OBDb) still carry nothing for the petrol oil circuit, but
the live DIDs are public after all. VLud (arduino-psa-diag) and the 208 GTi
Diagbox-telemetry dump both name the same three:

  0xD429  measured oil pressure     u16 * 0.00016 bar
  0xD47D  oil temperature           s16 * 0.25 °C
  0xD4D6  oil temperature (est.)    s16 * 0.25 °C
  0xD41D  sensor voltage            u16 * 0.2 mV   (not on every cal)

Confirmed on 208 I / 208 GTi THP (MED17, same generation as the RCZ / 308
5FV). Not yet read on a 5FV -- a silent D429 is possible; the sweep stays.
No public DID exists for OilPressInstr or OilPumpValve. Catalog + workshop
bands live in data/ep6_oil.json.

Discovery on the known-good car still matters for the two missing signals,
the same way the Astra K TCM map was built (backend/profiles/vauxhall/tcm.py):

  probe()    -- which address does this engine answer on (PSA 0x6A8 pair or
                standard OBD 0x7E0), which services work, what identifies the
                car/engine (VIN, calibration id), which generic PIDs exist.
  sweep()    -- walk a mode-22 DID range on the engine and record every id
                that answers, with its raw payload. Run once at idle and once
                held at fast idle: the DIDs whose payloads move with rpm are
                the live-data block; the oil-pressure DID is found by revving
                (pressure tracks rpm) and by its cold-start rise.
  capture()  -- timestamped time-series through a cold start: generic rpm /
                coolant / oil temp plus any raw DIDs the sweep surfaced.
                Tolerates the ECU dropping off the bus during cranking
                instead of aborting, because the seconds around cranking are
                exactly the window the warning lives in.

Baseline (RCZ) and patient (308) then get the same capture and the curves are
compared offline. Everything here is read-only: services 0x01, 0x09, 0x22,
0x3E and the read-safe PSA session ladder. Nothing is written to the car.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import List, Optional, Tuple

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.psa_uds_elm327 import PsaElm327UdsClient
from backend.profiles.peugeot.scan import PEUGEOT_ID_MAP

# The engine answers in one of two places. The PSA diagnostic pair is what
# Diagbox uses; the standard OBD slot always exists on a 2009+ petrol car.
ENGINE_PSA = 0x6A8   # -> 0x688 (PEUGEOT_ID_MAP)
ENGINE_OBD = 0x7E0   # -> 0x7E8 (request+8 fallback in the client)

_ADDRESSES = {"psa": ENGINE_PSA, "obd": ENGINE_OBD}

# Generic Mode-01 PIDs worth polling around an oil problem. Oil temperature
# (0x5C) is optional equipment in the standard -- the probe reports whether
# this calibration carries it before the capture relies on it.
_OBD_PIDS = [
    (0x0C, "engine_rpm", "rpm"),
    (0x05, "coolant_c", "°C"),
    (0x5C, "oil_temp_c", "°C"),
    (0x0B, "map_kpa", "kPa"),
    (0x42, "module_voltage", "V"),
    (0x33, "baro_kpa", "kPa"),
]

# Mode-22 mirrors of the OBD PIDs (0xF4xx) -- many MED17 calibrations expose
# them; if they answer, the same transport that reads them can read the rest
# of the calibration's F4/2xxx live blocks and the sweep knows where to look.
_MIRROR_DIDS = [0xF40C, 0xF405, 0xF45C, 0xF40B, 0xF442]

# Sweep ranges that have historically carried live data on PSA/Bosch petrol
# ECUs -- offered as defaults, not as truth; sweep() takes any range.
# D400-D4FF is now first: that is where D429/D47D live on MED17 THP.
DEFAULT_SWEEP_RANGES = "D400-D4FF, 0100-04FF, 1000-13FF, 2000-21FF, F400-F4FF"

# Imported catalog (see data/ep6_oil.json). Capture uses these when the
# caller does not pass dids. A 5FV that does not implement one simply
# returns None on that tick.
DEFAULT_CAPTURE_DIDS = ["D429", "D47D", "D41D"]

# u16 * 0.00016 bar. 0xFFFF = 10.4856 bar -- the FAP "10500 mbar" rail.
_OIL_PRESS_SCALE = 0.00016
_OIL_PRESS_RAIL = 0xFFFF

_DATA_PATH = Path(__file__).resolve().parent / "data" / "ep6_oil.json"
try:
    REFERENCE = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
except OSError:
    REFERENCE = {}

_MAX_SWEEP = 4096          # ids per call; keeps one call under ~45 min worst-case
_CAPTURE_DEAD_TICKS = 25   # consecutive all-silent ticks before giving up


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_pid(pid: int, body: bytes) -> Optional[float]:
    """Standard decode for the PIDs in _OBD_PIDS, None if body too short."""
    try:
        if pid == 0x0C:
            return round(((body[0] << 8) | body[1]) / 4.0, 1)
        if pid in (0x05, 0x5C):
            return float(body[0] - 40)
        if pid in (0x0B, 0x33):
            return float(body[0])
        if pid == 0x42:
            return round(((body[0] << 8) | body[1]) / 1000.0, 2)
    except IndexError:
        return None
    return None


def _u16_be(body: bytes) -> Optional[int]:
    if len(body) < 2:
        return None
    return (body[0] << 8) | body[1]


def _s16_be(body: bytes) -> Optional[int]:
    v = _u16_be(body)
    if v is None:
        return None
    return v - 0x10000 if v > 0x7FFF else v


def decode_oil_did(did: int, body: bytes) -> Optional[Tuple[str, float, str]]:
    """Known-catalog decode -> (name, value, unit) or None.

    D429 uses unsigned on purpose: 0xFFFF is the sensor-rail max (~10.49 bar),
    not a negative INT16. Temps are signed.
    """
    if body is None:
        return None
    if did == 0xD429:
        raw = _u16_be(body)
        if raw is None:
            return None
        return ("oil_pressure_bar", round(raw * _OIL_PRESS_SCALE, 3), "bar")
    if did == 0xD47D:
        raw = _s16_be(body)
        if raw is None:
            return None
        return ("oil_temp_c", round(raw * 0.25, 1), "°C")
    if did == 0xD4D6:
        raw = _s16_be(body)
        if raw is None:
            return None
        return ("oil_temp_estimated_c", round(raw * 0.25, 1), "°C")
    if did == 0xD41D:
        raw = _u16_be(body)
        if raw is None:
            return None
        return ("oil_pressure_sensor_mV", round(raw * 0.2, 0), "mV")
    return None


def assess(samples: List[dict]) -> dict:
    """Score a capture against the imported 5FV / THP field bands."""
    bands = (REFERENCE.get("bands_gauge_bar") or {
        "idle_hot_ok": [1.3, 2.4],
        "idle_hot_high_mechanical_mode": [2.8, 8.0],
        "rev_2000_ok": [1.5, 3.0],
        "rev_2000_high": [4.5, 10.0],
        "sensor_rail": [10.2, 10.6],
        "too_low_running": [0.0, 1.0],
    })
    idle: List[float] = []
    rev: List[float] = []
    rail = False
    saw_pressure = False
    for s in samples:
        raw = s.get("did_D429")
        if isinstance(raw, str) and raw.upper() == "FFFF":
            rail = True
        p = s.get("oil_pressure_bar")
        rpm = s.get("engine_rpm")
        if p is None:
            continue
        saw_pressure = True
        if p >= bands["sensor_rail"][0]:
            rail = True
        if rpm is None or rpm < 400:
            continue
        if 600 <= rpm <= 950:
            idle.append(p)
        elif 1800 <= rpm <= 2200:
            rev.append(p)

    def _mid(xs):
        return round(sorted(xs)[len(xs) // 2], 3) if xs else None

    idle_p, rev_p = _mid(idle), _mid(rev)
    notes = []
    if rail:
        verdict = "sensor_rail"
        notes.append("D429 at ~10.5 bar / 0xFFFF -- 5 V on the signal, not oil.")
    elif not saw_pressure:
        verdict = "no_pressure_data"
        notes.append("D429 never decoded. ECU silent on that id, or no ticks.")
    elif idle_p is not None and idle_p <= bands["too_low_running"][1]:
        verdict = "low_not_delivering"
        notes.append(f"Idle {idle_p} bar -- pump not delivering or sensor at 0.")
    elif idle_p is not None and idle_p >= bands["idle_hot_high_mechanical_mode"][0]:
        verdict = "high_mechanical_mode"
        notes.append(
            f"Idle {idle_p} bar -- solenoid likely unpowered (P0945 downgrade)."
        )
    elif rev_p is not None and rev_p >= bands["rev_2000_high"][0]:
        verdict = "high_mechanical_mode"
        notes.append(f"~2000 rpm {rev_p} bar -- well above 5FV spec 1.7-2.3.")
    elif idle_p is not None and bands["idle_hot_ok"][0] <= idle_p <= bands["idle_hot_ok"][1]:
        verdict = "in_spec"
        notes.append(f"Idle {idle_p} bar sits in Autodata 5FV hot-idle band.")
    else:
        verdict = "needs_curve"
        notes.append("Pressure moved but not enough idle/rev ticks to call it.")

    return {
        "verdict": verdict,
        "idle_bar": idle_p,
        "rev_2000_bar": rev_p,
        "sensor_rail": rail,
        "notes": notes,
        "reference": "backend/profiles/peugeot/data/ep6_oil.json",
    }


def _read_pid(client: PsaElm327UdsClient, pid: int, timeout: float = 1.0) -> Optional[bytes]:
    """One generic Mode-01 read on the standard engine slot."""
    try:
        payload = client.request(ENGINE_OBD, bytes([0x01, pid]), timeout=timeout)
    except (ObdError, TransportError):
        return None
    if len(payload) < 3 or payload[0] != 0x41 or payload[1] != pid:
        return None
    return payload[2:]


def _read_did_raw(client: PsaElm327UdsClient, addr: int, did: int,
                  timeout: float = 1.0) -> Optional[bytes]:
    """One service-0x22 read; payload after the echoed DID, or None."""
    try:
        payload = client.request(addr, bytes([0x22, did >> 8, did & 0xFF]),
                                 timeout=timeout)
    except (ObdError, TransportError):
        return None
    if len(payload) < 3 or payload[0] != 0x62:
        return None
    if len(payload) >= 3 and ((payload[1] << 8) | payload[2]) != did:
        return None
    return payload[3:]


def _bad_protocol(session, what: str) -> Optional[dict]:
    if session.protocol_num not in (6, None):
        return {
            "ok": False, "simulated": False,
            "message": (
                f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                f"11-bit/500k CAN {what} needs. Ignition on?"
            ),
            "timestamp": _now(),
        }
    return None


# --------------------------------------------------------------------------- #
# probe: which door is open on this engine, and who is the car
# --------------------------------------------------------------------------- #
def probe(adapter: Optional[dict], mode: str = "real") -> dict:
    """Map the engine ECU's diagnostic surface before any sweep or capture.

    Answers, per address (PSA 0x6A8/0x688 and standard 0x7E0/0x7E8): does it
    respond at all, does it speak service 0x22, do the PSA (10 C0) or UDS
    (10 03) sessions open. Plus the car's identity -- VIN and calibration id
    over Mode 09, ident DIDs over 0x22 -- so RCZ captures and 308 captures can
    never be mixed up, and the generic PID set (is oil temperature 0x5C even
    implemented on this calibration?). Read-only.
    """
    if mode == "sim":
        return {
            "ok": True, "simulated": True,
            "addresses": [], "identity": {}, "obd_pids": [], "mirror_dids": [],
            "oil_dids": [],
            "message": "SIMULATED -- no real car connected.", "timestamp": _now(),
        }

    def run(session, entry):
        session.ensure_protocol()
        bad = _bad_protocol(session, "PSA engine diagnostics")
        if bad:
            bad.update({"addresses": [], "identity": {}, "obd_pids": [],
                        "mirror_dids": [], "oil_dids": []})
            return bad

        client = PsaElm327UdsClient(session, PEUGEOT_ID_MAP)
        client.begin()
        addresses: List[dict] = []
        identity: dict = {}
        obd_pids: List[dict] = []
        mirrors: List[dict] = []
        oil_dids: List[dict] = []
        try:
            for label, addr in (("psa", ENGINE_PSA), ("obd", ENGINE_OBD)):
                report = {"address": f"0x{addr:03X}", "label": label,
                          "answers": False, "tester_present": False,
                          "service_22": False, "session_10C0": False,
                          "session_1003": False, "ident": []}
                for name, req in (("tester_present", bytes([0x3E, 0x00])),
                                  ("session_10C0", bytes([0x10, 0xC0])),
                                  ("session_1003", bytes([0x10, 0x03]))):
                    try:
                        reply = client.request(addr, req, timeout=1.2)
                    except (ObdError, TransportError):
                        continue
                    if reply:
                        report["answers"] = True
                        ok = reply[0] in (0x7E, 0x50)
                        report[name] = ok
                        if ok and name == "session_10C0":
                            client.mark_session_open(addr)
                # Ident DIDs prove service 0x22 and name the software.
                for did, label2 in ((0xF190, "VIN"), (0xF187, "Part number"),
                                    (0xF188, "Software"), (0xF18C, "ECU serial")):
                    body = _read_did_raw(client, addr, did, timeout=1.5)
                    if body is None:
                        continue
                    report["answers"] = True
                    report["service_22"] = True
                    text = "".join(chr(b) for b in body if 32 <= b < 127).strip()
                    report["ident"].append(
                        {"label": label2, "value": text or body.hex().upper()})
                addresses.append(report)

            # Mode 09: VIN + calibration id -- the calibration id is how a
            # 5FV (THP 156) is told apart from a 5FF (THP 200) RCZ.
            for pid, label in ((0x02, "vin"), (0x04, "calibration_id")):
                try:
                    reply = client.request(ENGINE_OBD, bytes([0x09, pid]), timeout=2.0)
                except (ObdError, TransportError):
                    continue
                if reply and reply[0] == 0x49 and len(reply) > 3:
                    text = "".join(chr(b) for b in reply[3:] if 32 < b < 127).strip("\x00 ")
                    if text:
                        identity[label] = text

            for pid, key, unit in _OBD_PIDS:
                body = _read_pid(client, pid)
                if body is None:
                    continue
                obd_pids.append({"pid": f"{pid:02X}", "name": key, "unit": unit,
                                 "value": _decode_pid(pid, body),
                                 "raw": body.hex().upper()})

            for did in _MIRROR_DIDS:
                for label, addr in (("psa", ENGINE_PSA), ("obd", ENGINE_OBD)):
                    body = _read_did_raw(client, addr, did)
                    if body is not None:
                        mirrors.append({"did": f"{did:04X}", "address": label,
                                        "raw": body.hex().upper()})

            # Imported THP oil catalog -- try both addresses, keep first hit.
            for did_hex in ("D429", "D47D", "D4D6", "D41D"):
                did = int(did_hex, 16)
                for label, addr in (("psa", ENGINE_PSA), ("obd", ENGINE_OBD)):
                    body = _read_did_raw(client, addr, did)
                    if body is None:
                        continue
                    decoded = decode_oil_did(did, body)
                    oil_dids.append({
                        "did": did_hex, "address": label,
                        "raw": body.hex().upper(),
                        "name": decoded[0] if decoded else None,
                        "value": decoded[1] if decoded else None,
                        "unit": decoded[2] if decoded else None,
                    })
                    break
        finally:
            client.restore()

        answering = [a for a in addresses if a["answers"]]
        has_5c = any(p["pid"] == "5C" for p in obd_pids)
        d429 = next((d for d in oil_dids if d["did"] == "D429"), None)
        return {
            "ok": True, "simulated": False,
            "addresses": addresses, "identity": identity,
            "obd_pids": obd_pids, "mirror_dids": mirrors,
            "oil_dids": oil_dids,
            "message": (
                f"Engine answered on {len(answering)} of 2 address(es): "
                + (", ".join(a["address"] for a in answering) or "none")
                + f". Generic PIDs alive: {len(obd_pids)}"
                + (" (incl. oil temp 0x5C)." if has_5c else
                   " (oil temp 0x5C NOT implemented -- captures rely on coolant"
                   " for warm-up context).")
                + (
                    f" Oil DID D429 answered: {d429['value']} {d429['unit']}."
                    if d429 and d429["value"] is not None
                    else " Oil DID D429 silent -- sweep D400-D4FF or try the other address."
                )
                + " Read-only probe; nothing was changed on the car."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "addresses": [], "identity": {},
                "obd_pids": [], "mirror_dids": [], "oil_dids": [],
                "message": str(exc), "timestamp": _now()}


# --------------------------------------------------------------------------- #
# sweep: find the live-data block the hard, honest way
# --------------------------------------------------------------------------- #
def sweep(adapter: Optional[dict], first: int, last: int, address: str = "psa",
          extended: bool = False, timeout: float = 0.6, mode: str = "real") -> dict:
    """Walk a mode-22 DID range on the engine and keep everything that answers.

    This is the discovery tool: run it on the known-good RCZ, once at idle and
    once with the engine held at ~2500 rpm, and diff the payloads. A DID whose
    value climbs with rpm and sits near zero before start is a pressure or
    pump signal; a DID that steps between two states as revs cross the pump's
    stage threshold is the solenoid/stage command. NRC 0x31 refusals are
    counted, not listed -- they prove the ECU is listening and the id simply
    isn't implemented. Read-only (service 0x22 only, plus the read-safe
    session ladder when extended=True).
    """
    if mode == "sim":
        return {"ok": True, "simulated": True, "answered": [], "refusedByNrc": {},
                "silent": 0, "message": "SIMULATED -- no real car connected.",
                "timestamp": _now()}

    if address not in _ADDRESSES:
        return {"ok": False, "simulated": False, "answered": [], "refusedByNrc": {},
                "silent": 0, "message": f"address must be one of {sorted(_ADDRESSES)}.",
                "timestamp": _now()}
    if not (0 <= first <= last <= 0xFFFF):
        return {"ok": False, "simulated": False, "answered": [], "refusedByNrc": {},
                "silent": 0, "message": "Need 0 <= first <= last <= 0xFFFF.",
                "timestamp": _now()}
    if last - first + 1 > _MAX_SWEEP:
        return {"ok": False, "simulated": False, "answered": [], "refusedByNrc": {},
                "silent": 0,
                "message": f"Range too wide ({last - first + 1} ids); max {_MAX_SWEEP} "
                           "per call. Split it.",
                "timestamp": _now()}

    addr = _ADDRESSES[address]

    def run(session, entry):
        session.ensure_protocol()
        bad = _bad_protocol(session, "the DID sweep")
        if bad:
            bad.update({"answered": [], "refusedByNrc": {}, "silent": 0})
            return bad

        client = PsaElm327UdsClient(session, PEUGEOT_ID_MAP)
        client.begin()
        answered: List[dict] = []
        refused: dict = {}
        silent = 0
        aborted = None
        try:
            if extended:
                client.open_session(addr)
            for did in range(first, last + 1):
                try:
                    payload = client.request(
                        addr, bytes([0x22, did >> 8, did & 0xFF]), timeout=timeout)
                except (ObdError, TransportError):
                    silent += 1
                    # A dead first stretch with zero signs of life = wrong
                    # address/bus, not an unimplemented range. Stop burning
                    # a timeout per id and say so.
                    if silent >= 60 and not answered and not refused:
                        aborted = (
                            "First 60 ids all silent with no refusals -- the engine "
                            f"is not listening at the '{address}' address. Run "
                            "peugeot_engine_probe and use the address that answers."
                        )
                        break
                    continue
                if not payload:
                    silent += 1
                elif payload[0] == 0x62:
                    answered.append({"did": f"{did:04X}",
                                     "payload": payload[3:].hex().upper()})
                elif payload[0] == 0x7F:
                    nrc = f"{payload[2]:02X}" if len(payload) >= 3 else "??"
                    refused[nrc] = refused.get(nrc, 0) + 1
                else:
                    silent += 1
        finally:
            client.restore()

        return {
            "ok": aborted is None, "simulated": False,
            "answered": answered, "refusedByNrc": refused, "silent": silent,
            "range": f"{first:04X}-{last:04X}", "address": address,
            "extendedSession": extended,
            "message": aborted or (
                f"Swept {last - first + 1} id(s) at 0x{addr:03X}: {len(answered)} "
                f"answered with data, refusals by NRC {refused or '{}'}, "
                f"{silent} silent. Payloads are raw hex on purpose -- identify a "
                "signal by how it MOVES between an idle sweep and a fast-idle "
                "sweep, then trust it. Read-only."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "answered": [], "refusedByNrc": {},
                "silent": 0, "message": str(exc), "timestamp": _now()}


# --------------------------------------------------------------------------- #
# capture: the cold start as a timeline
# --------------------------------------------------------------------------- #
def capture(adapter: Optional[dict], seconds: float = 120.0,
            dids: Optional[List[str]] = None, address: str = "psa",
            extended: bool = False, mode: str = "real") -> dict:
    """Timestamped series through a cold start: rpm + raw DIDs, crank-proof.

    The warning the 308 shows lives in the first seconds after the key turns,
    so the procedure is: connect with IGNITION ON, ENGINE OFF, start this
    capture, wait ~5 s, crank, let it idle -- and near the end give it a slow
    rev to ~3000 rpm so the pump's stage switch is on the timeline too.

    Every tick reads engine rpm (Mode 01 0x0C) plus each DID in ``dids`` raw;
    every fifth tick adds coolant, oil temperature (if implemented) and module
    voltage -- the voltage dip marks the exact cranking moment on the
    timeline. Cranking knocks ECUs off the bus for a moment, so unlike the
    Astra drive capture this one does NOT quit after three dead ticks: gaps
    are recorded as gaps and the capture only gives up after ~25 dead ticks
    in a row (adapter gone, not crank dip). Expect roughly 1-3 ticks/s over
    Bluetooth depending on how many DIDs ride along; the result reports the
    rate it actually achieved. Read-only.

    dids: hex strings. None uses the imported catalog (D429/D47D/D41D).
        An explicit empty list is still the rpm/coolant/voltage baseline.
    """
    if mode == "sim":
        # Fake 5FV idle at 1.70 bar (raw 0x2981) after a crank gap.
        samples = [
            {"t": 0.0, "engine_rpm": 0.0, "did_D429": "0000",
             "oil_pressure_bar": 0.0, "coolant_c": 18.0, "module_voltage": 12.1},
            {"t": 6.0, "engine_rpm": None, "did_D429": None,
             "oil_pressure_bar": None, "module_voltage": 9.8},
            {"t": 8.0, "engine_rpm": 850.0, "did_D429": "2981",
             "oil_pressure_bar": 1.7, "coolant_c": 19.0, "module_voltage": 14.1},
            {"t": 20.0, "engine_rpm": 2000.0, "did_D429": "30D4",
             "oil_pressure_bar": 2.0, "module_voltage": 14.1},
        ]
        return {
            "ok": True, "simulated": True, "samples": samples,
            "dids": list(DEFAULT_CAPTURE_DIDS),
            "assessment": assess(samples),
            "message": "SIMULATED -- no real car connected.", "timestamp": _now(),
        }

    if address not in _ADDRESSES:
        return {"ok": False, "simulated": False, "samples": [],
                "message": f"address must be one of {sorted(_ADDRESSES)}.",
                "timestamp": _now()}
    try:
        src = DEFAULT_CAPTURE_DIDS if dids is None else dids
        did_list = [int(d, 16) & 0xFFFF for d in src]
    except (TypeError, ValueError):
        return {"ok": False, "simulated": False, "samples": [],
                "message": "dids must be hex strings like ['1E20', 'F40C'].",
                "timestamp": _now()}
    addr = _ADDRESSES[address]

    def run(session, entry):
        session.ensure_protocol()
        bad = _bad_protocol(session, "the oil capture")
        if bad:
            bad["samples"] = []
            return bad

        client = PsaElm327UdsClient(session, PEUGEOT_ID_MAP)
        client.begin()
        samples: List[dict] = []
        started = monotonic()
        dead = 0
        gaps = 0
        tick = 0
        try:
            if extended and did_list:
                client.open_session(addr)
            while monotonic() - started < seconds:
                sample = {"t": round(monotonic() - started, 3)}
                alive = False
                body = _read_pid(client, 0x0C, timeout=0.5)
                sample["engine_rpm"] = _decode_pid(0x0C, body) if body else None
                alive = alive or body is not None
                for did in did_list:
                    body = _read_did_raw(client, addr, did, timeout=0.5)
                    sample[f"did_{did:04X}"] = body.hex().upper() if body else None
                    if body is not None:
                        decoded = decode_oil_did(did, body)
                        if decoded:
                            sample[decoded[0]] = decoded[1]
                    alive = alive or body is not None
                if tick % 5 == 0:
                    for pid, key in ((0x05, "coolant_c"), (0x5C, "oil_temp_c"),
                                     (0x42, "module_voltage")):
                        body = _read_pid(client, pid, timeout=0.5)
                        sample[key] = _decode_pid(pid, body) if body else None
                        alive = alive or body is not None
                tick += 1
                samples.append(sample)
                if alive:
                    if dead:
                        gaps += 1
                    dead = 0
                else:
                    dead += 1
                    if dead >= _CAPTURE_DEAD_TICKS:
                        break
                    sleep(0.2)
        finally:
            client.restore()

        elapsed = monotonic() - started
        rate = len(samples) / elapsed if elapsed else 0.0
        died = dead >= _CAPTURE_DEAD_TICKS
        return {
            "ok": True, "simulated": False, "samples": samples,
            "sampleRate": round(rate, 2), "busGaps": gaps,
            "address": address, "dids": [f"{d:04X}" for d in did_list],
            "assessment": assess(samples),
            "message": (
                f"Captured {len(samples)} tick(s) over {elapsed:.0f}s (~{rate:.2f}/s), "
                f"{gaps} bus gap(s) survived (cranking shows up as a gap plus a "
                "voltage dip -- that is the timeline's start-event marker). "
                + ("WARNING: the capture ended early -- the bus went quiet for "
                   f"{_CAPTURE_DEAD_TICKS} consecutive ticks (adapter or ignition "
                   "dropped, not a crank dip). " if died else "")
                + "Read-only throughout."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "samples": [],
                "message": str(exc), "timestamp": _now()}
