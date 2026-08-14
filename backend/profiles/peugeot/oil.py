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

# FIELD FINDING (RCZ #1, 2026-08-14): this car ignores the PHYSICAL generic-OBD
# request id 0x7E0 completely -- 01 00 there returns NO DATA -- but answers the
# FUNCTIONAL broadcast 0x7DF from 0x7E8 (engine) and 0x7E9 (second ECU). So every
# generic Mode-01 read goes out on 0x7DF with the receive filter pinned to 0x7E8,
# which is why the id map below is extended rather than left to the +8 rule.
ENGINE_OBD_FUNC = 0x7DF
ENGINE_OBD_RESP = 0x7E8

_ADDRESSES = {"psa": ENGINE_PSA, "obd": ENGINE_OBD}

# The map handed to every client here: PSA's own pairs plus the functional
# broadcast pairing the generic reads depend on.
OIL_ID_MAP = dict(PEUGEOT_ID_MAP)
OIL_ID_MAP[ENGINE_OBD_FUNC] = ENGINE_OBD_RESP
OIL_ID_MAP[ENGINE_OBD] = ENGINE_OBD_RESP

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
_SESSION_LAPSE_S = 5.0     # how long the ECU's KWP session lingers after silence


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


def assess_coldstart(samples: List[dict], baseline: Optional[dict] = None) -> dict:
    """Judge a cold-start pressure curve against the two healthy RCZs.

    A block capture cannot carry rpm (the session locks generic OBD out), so
    this reads the SHAPE instead, which is what a cold start is diagnostic for:

        healthy   pressure is already high at the first reading, peaks while
                  the oil is cold and thick, then decays as it warms and
                  settles flat. RCZ #2: 3.96 bar at 6 s, 4.67 peak, 2.0 settled.
        low       pressure never gets up there at all -- the pump is not
                  delivering, or the sensor reads low.
        rail      pressure pinned at the top of the sensor's range: 5 V on the
                  signal wire, a circuit fault rather than real oil.
        stuck_high  high pressure that never decays as the engine warms, which
                  is the dead-solenoid signature (the pump stays in mechanical
                  full-delivery mode and nothing regulates it down).
        split     the two copies of the pressure value disagree by more than
                  the healthy cars ever did, which is what a car whose demanded
                  and measured pressure have parted company would show.
    """
    press = [(s["t"], s["oil_pressure_bar"]) for s in samples
             if s.get("oil_pressure_bar") is not None]
    deltas = [abs(s["oil_pressure_delta_bar"]) for s in samples
              if s.get("oil_pressure_delta_bar") is not None]
    if not press:
        return {"verdict": "no_pressure_data", "notes": [
            "Block C2 never decoded -- check it answered."]}

    values = [p for _, p in press]
    peak = max(values)
    early = [p for t, p in press if t <= 30]
    late = [p for t, p in press if t >= press[-1][0] - 30]
    settled = round(sum(late) / len(late), 2) if late else None
    early_peak = max(early) if early else None
    worst_delta = max(deltas) if deltas else None
    notes = []
    if baseline and baseline.get("coolant_c") is not None:
        notes.append(f"Engine started at {baseline['coolant_c']:.0f} C coolant.")
        if baseline["coolant_c"] > 50:
            notes.append("That is NOT a cold start -- the comparison against the "
                         "cold RCZ baseline is not like for like.")

    verdict = "in_spec"
    if peak >= 10.2:
        verdict = "sensor_rail"
        notes.append(f"Pressure reached {peak:.2f} bar -- that is the top of the "
                     "sensor's range, i.e. 5 V on the signal wire, not oil.")
    elif peak <= 1.0:
        verdict = "low_not_delivering"
        notes.append(f"Pressure never exceeded {peak:.2f} bar. The healthy cars "
                     "were at 3.96 bar six seconds after a cold start.")
    elif settled is not None and settled >= 3.5 and len(press) > 10:
        verdict = "stuck_high"
        notes.append(f"Pressure settled at {settled:.2f} bar instead of decaying "
                     "to about 2.0 as the healthy car did -- the regulation is "
                     "not bringing it down (dead or unpowered solenoid).")
    elif early_peak is not None and early_peak < 2.5:
        verdict = "low_cold"
        notes.append(f"Cold peak only {early_peak:.2f} bar; both healthy cars "
                     "exceeded 3.9 bar while cold.")
    else:
        notes.append(f"Cold peak {peak:.2f} bar, settled {settled:.2f} bar -- the "
                     "same shape as the healthy RCZ (4.67 peak, 2.0 settled).")

    if worst_delta is not None and worst_delta > 0.35:
        verdict = "split" if verdict == "in_spec" else verdict
        notes.append(f"The two copies of the pressure value disagreed by up to "
                     f"{worst_delta:.2f} bar; on both healthy cars they never "
                     "parted by more than 0.25.")

    return {
        "verdict": verdict,
        "cold_peak_bar": round(peak, 2),
        "settled_bar": settled,
        "worst_mirror_delta_bar": round(worst_delta, 2) if worst_delta is not None else None,
        "notes": notes,
        "reference": "backend/profiles/peugeot/data/ep6_oil.json (measured_baselines)",
    }


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
    """One generic Mode-01 read, sent functionally (see ENGINE_OBD_FUNC)."""
    try:
        payload = client.request(ENGINE_OBD_FUNC, bytes([0x01, pid]), timeout=timeout)
    except (ObdError, TransportError):
        return None
    if len(payload) < 3 or payload[0] != 0x41 or payload[1] != pid:
        return None
    return payload[2:]


# Generic Mode-01 fuel trims. Short term is the ECU's live correction, long term
# is what it has learned; a pair that sits well off zero on one car and near zero
# on another is a real difference in how the engine is breathing or fuelling.
TRIM_PIDS = [
    (0x06, "stft_b1_pct"),
    (0x07, "ltft_b1_pct"),
    (0x08, "stft_b2_pct"),
    (0x09, "ltft_b2_pct"),
]


def _trim_pct(body: bytes) -> Optional[float]:
    """Standard trim decode: (A - 128) * 100 / 128, i.e. -100%..+99.2%."""
    if not body:
        return None
    return round((body[0] - 128) * 100.0 / 128.0, 1)


def read_trims(client: PsaElm327UdsClient) -> dict:
    """Short- and long-term fuel trims, both banks. Absent banks are skipped."""
    out = {}
    for pid, key in TRIM_PIDS:
        body = _read_pid(client, pid, timeout=0.6)
        if body is None:
            continue
        value = _trim_pct(body)
        if value is not None:
            out[key] = value
    return out


# PSA KWP measurement blocks on the engine, request 21 <hi> 80 01. Which ids
# exist is calibration-specific; these are the ones that answered on RCZ #1
# (EP6 THP, engine ECU that refuses UDS service 0x22 entirely). The payload
# layout is NOT decoded here on purpose -- byte meanings are identified by
# capturing the blocks while rpm changes and diffing, not by guessing.
KWP_BLOCK_IDS = ["C0", "C1", "C2", "C3", "C4", "C5", "C6", "C7", "CA", "CB"]

# ---- Block C2, decoded on RCZ #1 (2026-08-14) ------------------------------
# OIL PRESSURE: u16 big-endian at offset 7, in hundredths of a bar. A second
# copy sits at offset 47 and tracks the first within +/-0.07 bar.
#
# Evidence, from four captures on one warm engine:
#   idle held 22 s          1.41-1.48 bar, dead steady
#   3000 rpm held           3.36-3.46 bar, falling back to 1.43 as revs dropped
#   idle -> 2500 -> idle    2.89-3.04 bar while revved, 1.43-1.53 at idle
# Pressure rising with rpm and returning to exactly the same idle value is what
# an oil-pressure signal does and what no temperature or counter does. The
# numbers also land on the published figures for this engine without being
# fitted to them: Autodata gives 1.7 +/- 0.3 bar hot idle (measured 1.43) and
# 1.7-2.3 bar at 2000 rpm, and the healthy-EP6 field curve gives 2.4 bar at
# 2900 rpm (measured 3.4 at ~3000 -- same shape, healthy side of it).
#
# The offset-47 twin is NOT yet proven to be the ECU's demanded pressure. On a
# healthy engine a measured and a demanded value sit on top of each other, so
# this car cannot separate them; a car with a regulation fault would. Both are
# therefore reported, and the difference between them is reported too, because
# on the 308 that difference is the whole question.
C2_OIL_PRESSURE = 7
C2_OIL_PRESSURE_MIRROR = 47
C2_PRESSURE_SCALE = 0.01


def decode_c2(body: bytes) -> dict:
    """Decode what block C2 is known to carry. Unknown bytes stay unread."""
    out = {}
    if body is None:
        return out
    for offset, key in ((C2_OIL_PRESSURE, "oil_pressure_bar"),
                        (C2_OIL_PRESSURE_MIRROR, "oil_pressure_mirror_bar")):
        raw = _u16_be(body[offset:offset + 2]) if len(body) > offset + 1 else None
        if raw is not None:
            out[key] = round(raw * C2_PRESSURE_SCALE, 2)
    if "oil_pressure_bar" in out and "oil_pressure_mirror_bar" in out:
        out["oil_pressure_delta_bar"] = round(
            out["oil_pressure_bar"] - out["oil_pressure_mirror_bar"], 2)
    return out


def _read_kwp_block(client: PsaElm327UdsClient, hi: int,
                    timeout: float = 0.6) -> Optional[bytes]:
    """One 21 <hi> 80 01 measurement-block read; payload after the echo."""
    try:
        reply = client.request(ENGINE_PSA, bytes([0x21, hi, 0x80, 0x01]), timeout=timeout)
    except (ObdError, TransportError):
        return None
    if not reply or reply[0] != 0x61 or len(reply) <= 4:
        return None
    return reply[4:]


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
# block_capture: the KWP measurement blocks as a timeline, for cars whose
# engine ECU refuses UDS service 0x22 (so D429 and friends are unreachable)
# --------------------------------------------------------------------------- #
def block_capture(adapter: Optional[dict], seconds: float = 90.0,
                  blocks: Optional[List[str]] = None, mode: str = "real") -> dict:
    """Log PSA KWP measurement blocks against rpm while the revs are varied.

    For an engine ECU that answers 21 <hi> 80 01 but refuses service 0x22 --
    the RCZ #1 case -- this is the only route to oil data, and the block byte
    layout is unpublished. So the method is the one that built the Astra K
    gearbox map: log every block RAW alongside engine rpm through a rev
    profile, then identify signals afterwards by which bytes move WITH rpm.

    Drive profile to run while this is capturing (about 90 s):
        idle 20 s -> hold ~2000 rpm 15 s -> idle 15 s -> hold ~3000 rpm 10 s
        -> idle to the end.
    Oil pressure is the byte that rises and falls with every rev and never
    settles back to a different value; a temperature drifts one way only; the
    pump's stage command steps between two levels rather than sweeping.

    Each tick also records fuel trims and coolant, so the same capture answers
    the car-to-car trim comparison without a second run. Read-only.
    """
    if mode == "sim":
        return {"ok": True, "simulated": True, "samples": [], "blocks": [],
                "message": "SIMULATED -- no real car connected.", "timestamp": _now()}

    wanted = [b.upper() for b in (blocks or KWP_BLOCK_IDS)]
    try:
        block_ids = [int(b, 16) & 0xFF for b in wanted]
    except (TypeError, ValueError):
        return {"ok": False, "simulated": False, "samples": [], "blocks": [],
                "message": "blocks must be hex bytes like ['C0','CB'].",
                "timestamp": _now()}

    def run(session, entry):
        session.ensure_protocol()
        bad = _bad_protocol(session, "the block capture")
        if bad:
            bad.update({"samples": [], "blocks": wanted})
            return bad

        client = PsaElm327UdsClient(session, OIL_ID_MAP)
        client.begin()
        samples: List[dict] = []
        started = monotonic()
        dead = 0
        tick = 0
        try:
            client.open_session(ENGINE_PSA)
            while monotonic() - started < seconds:
                sample = {"t": round(monotonic() - started, 3)}
                alive = False
                # 1.2 s, not 0.5: a FUNCTIONAL request is answered by every ECU
                # on the bus (0x7E8 and 0x7E9 here), and the ELM holds the line
                # open for the whole window collecting them. Half a second
                # returns empty-handed on this car -- which is exactly how the
                # first capture came back with no rpm at all.
                body = _read_pid(client, 0x0C, timeout=1.2)
                sample["engine_rpm"] = _decode_pid(0x0C, body) if body else None
                alive = alive or body is not None
                for hi in block_ids:
                    body = _read_kwp_block(client, hi)
                    sample[f"blk_{hi:02X}"] = body.hex().upper() if body else None
                    if hi == 0xC2 and body is not None:
                        sample.update(decode_c2(body))
                    alive = alive or body is not None
                if tick % 4 == 0:
                    body = _read_pid(client, 0x05, timeout=1.2)
                    sample["coolant_c"] = _decode_pid(0x05, body) if body else None
                    sample.update(read_trims(client))
                tick += 1
                samples.append(sample)
                if alive:
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
        rpms = [s["engine_rpm"] for s in samples if s.get("engine_rpm")]
        return {
            "ok": True, "simulated": False, "samples": samples,
            "blocks": wanted, "sampleRate": round(rate, 2),
            "rpmRange": [min(rpms), max(rpms)] if rpms else None,
            "message": (
                f"Captured {len(samples)} tick(s) over {elapsed:.0f}s (~{rate:.2f}/s) "
                f"across {len(wanted)} block(s)."
                + (f" Engine rpm spanned {min(rpms):.0f}-{max(rpms):.0f}."
                   if rpms else " No rpm was recorded.")
                + (" WARNING: rpm barely moved, so no byte can be separated from any "
                   "other -- rev the engine while this runs or the capture cannot "
                   "identify anything."
                   if rpms and max(rpms) - min(rpms) < 400 else "")
                + " Blocks stored raw. Read-only throughout."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "samples": [], "blocks": wanted,
                "message": str(exc), "timestamp": _now()}


def spot(adapter: Optional[dict], repeats: int = 3, mode: str = "real") -> dict:
    """One rpm reading paired with one oil-pressure reading, several times over.

    Needed because this ECU will not answer generic OBD while its diagnostic
    session is open, so a capture that logs pressure cannot log rpm alongside
    it. Each pass here reads rpm and coolant FIRST, then opens the session and
    reads block C2, then closes it -- roughly a second apart rather than
    simultaneous, which is close enough while the driver holds a steady throttle
    and is the only way to put a pressure figure against a real rpm figure.

    The driver does NOT have to hit a target rpm. Each pass records whatever
    rpm was actually there, so the useful instruction is "hold roughly steady,
    anywhere, for a few seconds at a time, at a few different throttle
    openings" -- the pairs then plot themselves into a pressure-against-rpm
    curve. Chasing an exact figure by ear adds error rather than removing it.
    Read-only.
    """
    if mode == "sim":
        return {"ok": True, "simulated": True, "readings": [],
                "message": "SIMULATED -- no real car connected.", "timestamp": _now()}

    def run(session, entry):
        session.ensure_protocol()
        bad = _bad_protocol(session, "the paired spot read")
        if bad:
            bad["readings"] = []
            return bad

        readings: List[dict] = []
        for index in range(max(1, min(repeats, 10))):
            # The KWP session does not close when we stop talking to it -- it
            # LAPSES, a few seconds later, and until it does the ECU keeps
            # ignoring generic OBD. Without this wait only the first pass ever
            # gets an rpm figure, which is exactly what the first field run did.
            if index:
                sleep(_SESSION_LAPSE_S)
            client = PsaElm327UdsClient(session, OIL_ID_MAP)
            client.begin()
            entry_reading: dict = {}
            try:
                body = _read_pid(client, 0x0C, timeout=1.2)
                entry_reading["engine_rpm"] = _decode_pid(0x0C, body) if body else None
                body = _read_pid(client, 0x05, timeout=1.2)
                entry_reading["coolant_c"] = _decode_pid(0x05, body) if body else None
                client.open_session(ENGINE_PSA)
                block = _read_kwp_block(client, 0xC2, timeout=1.0)
                entry_reading.update(decode_c2(block))
                entry_reading["blk_C2"] = block.hex().upper() if block else None
            finally:
                client.restore()
            readings.append(entry_reading)

        paired = sorted(
            (r for r in readings
             if r.get("engine_rpm") is not None
             and r.get("oil_pressure_bar") is not None),
            key=lambda r: r["engine_rpm"])
        return {
            "ok": True, "simulated": False, "readings": readings,
            "curve": [{"rpm": r["engine_rpm"], "bar": r["oil_pressure_bar"]}
                      for r in paired],
            "message": (
                f"{len(readings)} pass(es), {len(paired)} with both rpm and pressure. "
                + ("; ".join(f"{r['engine_rpm']:.0f} rpm -> {r['oil_pressure_bar']:.2f} bar"
                             for r in paired) or "no complete pair")
                + ". Each pair carries the rpm that was actually there, so there is "
                  "no need to hold an exact figure -- just hold roughly steady at a "
                  "few different openings. Read-only."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "readings": [],
                "message": str(exc), "timestamp": _now()}


# --------------------------------------------------------------------------- #
# coldstart: the whole cold start on one timeline, for a car whose complaint
# only appears in the first seconds after the key turns
# --------------------------------------------------------------------------- #
def coldstart(adapter: Optional[dict], seconds: float = 180.0,
              blocks: Optional[List[str]] = None, mode: str = "real") -> dict:
    """Capture a cold start end to end: pre-start baseline, then every block.

    Three things this does that block_capture does not, all of them because a
    cold start happens once and cannot be repeated for an hour:

    1. Reads generic OBD (rpm, coolant, voltage, fuel trims) BEFORE opening the
       diagnostic session. On this engine the two are mutually exclusive -- the
       moment the KWP session is open the ECU stops answering generic OBD -- so
       the pre-start baseline is the only clean chance to record coolant
       temperature and prove the engine really was cold.
    2. Logs EVERY measurement block raw for the whole run, not a chosen few.
       Slower per tick, but a cold start is not repeatable and a block dropped
       to save a second is a block that cannot be looked at later.
    3. Decodes oil pressure live from block C2 so the pressure curve is
       readable without post-processing, while still storing the raw bytes.

    Procedure: connect with ignition ON, engine OFF and stone cold. Start this.
    It takes the baseline, then tells the timeline where cranking began by the
    first tick where pressure appears. Crank when the run has started, then let
    it idle untouched for the rest -- no throttle. The oil-pressure warning
    lives in those first seconds, so the idle curve is the evidence.
    """
    if mode == "sim":
        return {"ok": True, "simulated": True, "baseline": {}, "samples": [],
                "message": "SIMULATED -- no real car connected.", "timestamp": _now()}

    wanted = [b.upper() for b in (blocks or KWP_BLOCK_IDS)]
    try:
        block_ids = [int(b, 16) & 0xFF for b in wanted]
    except (TypeError, ValueError):
        return {"ok": False, "simulated": False, "baseline": {}, "samples": [],
                "message": "blocks must be hex bytes like ['C0','C2'].",
                "timestamp": _now()}

    def run(session, entry):
        session.ensure_protocol()
        bad = _bad_protocol(session, "the cold-start capture")
        if bad:
            bad.update({"baseline": {}, "samples": [], "blocks": wanted})
            return bad

        client = PsaElm327UdsClient(session, OIL_ID_MAP)
        client.begin()
        baseline: dict = {}
        samples: List[dict] = []
        started = monotonic()
        dead = 0
        try:
            # Generic OBD first, while the diagnostic session is still shut.
            for pid, key in ((0x0C, "engine_rpm"), (0x05, "coolant_c"),
                             (0x42, "module_voltage"), (0x0F, "intake_air_c")):
                body = _read_pid(client, pid, timeout=1.2)
                if body is None:
                    continue
                value = (_decode_pid(pid, body) if pid != 0x0F
                         else float(body[0] - 40))
                baseline[key] = value
            baseline.update(read_trims(client))

            client.open_session(ENGINE_PSA)
            while monotonic() - started < seconds:
                sample = {"t": round(monotonic() - started, 3)}
                alive = False
                for hi in block_ids:
                    body = _read_kwp_block(client, hi)
                    sample[f"blk_{hi:02X}"] = body.hex().upper() if body else None
                    if hi == 0xC2 and body is not None:
                        sample.update(decode_c2(body))
                    alive = alive or body is not None
                samples.append(sample)
                if alive:
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
        press = [(s["t"], s["oil_pressure_bar"]) for s in samples
                 if s.get("oil_pressure_bar") is not None]
        running = [(t, p) for t, p in press if p > 0.3]
        first_t = running[0][0] if running else None
        peak = max((p for _, p in press), default=None)
        settled = round(sum(p for _, p in press[-5:]) / len(press[-5:]), 2) if press else None
        return {
            "ok": True, "simulated": False,
            "baseline": baseline, "samples": samples, "blocks": wanted,
            "sampleRate": round(rate, 2),
            "pressure": {"firstReading_s": first_t, "peak_bar": peak,
                         "settled_bar": settled},
            "assessment": assess_coldstart(samples, baseline),
            "message": (
                f"Cold start: {len(samples)} tick(s) over {elapsed:.0f}s "
                f"(~{rate:.2f}/s) across {len(wanted)} block(s). Baseline before "
                f"start: {baseline or 'nothing answered'}."
                + (f" Oil pressure first read {first_t:.0f}s in, peaked "
                   f"{peak:.2f} bar, settled about {settled:.2f} bar."
                   if first_t is not None and peak is not None else
                   " No oil pressure was decoded -- check block C2 answered.")
                + " Read-only throughout."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "baseline": {}, "samples": [],
                "blocks": wanted, "message": str(exc), "timestamp": _now()}


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
