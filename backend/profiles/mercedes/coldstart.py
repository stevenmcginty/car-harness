"""OM651 cold-start / extended-crank capture (CLA W117 220 CDI and family).

WHY THIS FILE EXISTS
--------------------
After a cylinder-head rebuild the car STRUGGLES to start on the FIRST start of
the day -- about 1.5 to 2 seconds of cranking before it catches, and it labours
as it catches -- then fires instantly every time after. A video can only show
that the starter spins the engine fast and evenly; it cannot show WHY the ECU
withheld injection for those seconds. Three candidates explain that shape and
they are told apart by one thing only -- what the engine ECU was doing during
them:

    fuel drain-back / air ingress -> rail pressure crawls up to the release
                                     threshold, and the engine fires the moment
                                     it gets there
    cold injector leak-off        -> same crawl, but the rail also sags again
                                     once it is running cold
    glow / pre-heat deficit       -> rail is AT pressure early, injection is
                                     already commanded, and the engine still
                                     will not light

So this module records the crank itself. The dealer "actual values" for the
CRD3MFA1 engine ECU were mined from XENTRY 26.6.4's Caesar layer into
data/cla_dids.json (41 DIDs, every one graded `candidate` -- none has answered
on a car yet).

    probe()     -- key-on preflight. Reads all 41 mined DIDs once, both byte
                   orders, and says which answered. Run this the evening
                   before: a cold start happens once a day and must not be
                   spent finding out that half the table NRC-refuses.
    coldstart() -- the capture. Three phases on one timeline: key-on baseline,
                   the crank (a deliberately tiny DID list so the sample rate
                   survives), then the run-up. Every value is stored WITH its
                   raw bytes, so a wrong mined scaling can be re-read later
                   without needing another cold night.
    assess()    -- the reading: how long the crank was, when the rail crossed
                   the injection-release threshold, whether firing followed
                   that crossing, when cam/crank sync arrived, and what the
                   glow controller was doing.

Read-only throughout: services 0x22 and 0x3E only, no session opened, nothing
written to the car.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Callable, Dict, List, Optional, Tuple

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient
from backend.profiles.mercedes.scan import MERCEDES_ID_MAP

ENGINE = 0x7E0
DATA_PATH = Path(__file__).resolve().parent / "data" / "cla_dids.json"
ECU_KEY = "crd3mfa1_om651"

# Injection release. The Delphi CRD3 will not command a main injection until
# the rail is above roughly this pressure; the exact figure is not published,
# so it is a band and the assessment reports the measured crossing time rather
# than passing or failing against a single number.
RAIL_RELEASE_BAR = 250.0
RAIL_RELEASE_LOW_BAR = 180.0

FIRE_RPM = 500.0        # above this the engine is running, not cranking
CRANK_RPM = 50.0        # above this the starter is turning it
# The complaint measured from the car (Steve, 2026-09-04) is a 1.5-2 s struggle
# to start, not the ~5 s dry crank a first look at the video suggested. A
# healthy OM651 lights within about a second, so the bar sits below the
# complaint and the assessment reports the measured number either way.
EXTENDED_CRANK_S = 1.5
LATE_SYNC_S = 1.0       # cam/crank sync should land inside the first revs
# After it catches, a struggling start hunts before it settles. Idle is ~800
# rpm; a dip below this in the first seconds is the engine nearly dying again.
STUMBLE_RPM = 600.0
SETTLE_RPM = 700.0      # sustained above this = it has taken
STUMBLE_WINDOW_S = 8.0  # how long after firing to watch for the struggle

_SLOW_EVERY = 5         # run-phase slow group every Nth tick
_DEAD_SECONDS = 90.0    # nothing answering this long = the link is gone
_MIN_GAP_S = 2.0

# The crank list. Kept brutally short on purpose, and this is the tightest
# constraint in the module: each 0x22 read is a full request/response round
# trip, and the crank being measured is only about TWO seconds long. Six DIDs
# is already most of what that window will fit at a useful rate -- adding a
# seventh buys a channel and costs the shape of the ramp. Order matters: rail
# feedback leads because the whole question is when it crosses release.
CRANK_DIDS = ["D058", "5017", "600A", "60F9", "6000", "D017"]

# Read every tick once the engine is running.
RUN_FAST_DIDS = ["5017", "D058", "600A", "601A", "6000", "6076", "6078", "200B"]

# Everything else, every _SLOW_EVERY ticks.
_SLOW_SKIP = set(CRANK_DIDS) | set(RUN_FAST_DIDS)


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


CLA = _load(DATA_PATH)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entries() -> List[dict]:
    return list(CLA.get(ECU_KEY, {}).get("dids", []))


def _by_did() -> Dict[str, dict]:
    return {e["did"].upper(): e for e in _entries()}


def _key(entry: dict) -> str:
    return f"did_{entry['did'].lower()}"


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #
# Byte order is the one thing the mined table cannot tell us. The SLK's
# SIM271DE20 answered little-endian on the car (2026-08-23), but the CRD3MFA1
# is a different supplier's ECU and inherits nothing from that result. So both
# orders are decoded at preflight and the plausible one is chosen from values
# whose real range is known (battery volts, coolant temperature).
def _raw_int(reply: bytes, entry: dict, endian: str) -> Optional[int]:
    off, size = entry["byte_offset"], entry["byte_size"]
    if len(reply) < off + size:
        return None
    return int.from_bytes(reply[off:off + size], endian)


def _signed(entry: dict) -> bool:
    """Only channels that can legitimately go negative are read signed. The
    mined table marks those by carrying a large negative offset (the rail
    pressure error's -5000, the ICP TDC correction's -180); everything else is
    a count, a pressure or a quantity and cannot sit below zero."""
    conv = entry.get("conversion", {})
    return conv.get("type") == "linear" and conv.get("offset", 0.0) <= -100.0


def enum_name(entry: dict, value: Optional[float]) -> Optional[str]:
    """Enum state name for a decoded numeric value, None if it is not an enum
    or the car reported a value the mined table does not list."""
    conv = entry.get("conversion", {})
    if conv.get("type") != "enum" or value is None:
        return None
    states = conv.get("states", [])
    for i, state in enumerate(states):
        if isinstance(state, str):
            if i == int(value):
                return state
        elif float(state.get("value", -1)) == float(value):
            return state.get("name")
    return f"unlisted({int(value)})"


def decode_entry(entry: dict, reply: Optional[bytes],
                 endian: str = "little") -> Optional[float]:
    """Mined scaling -> engineering value; None if short, refused or silent.

    Every mined entry sits at byte offset 3 of the positive reply -- the first
    data byte after `62 <DID hi> <DID lo>`.
    """
    if not reply or len(reply) < 3 or reply[0] != 0x62:
        return None
    raw = _raw_int(reply, entry, endian)
    if raw is None:
        return None
    size = entry["byte_size"]
    if _signed(entry) and raw >= 1 << (8 * size - 1):
        raw -= 1 << (8 * size)
    conv = entry.get("conversion", {})
    if conv.get("type") == "enum":
        return float(raw)
    if conv.get("type") != "linear":
        return float(raw)
    return round(raw * conv["multiplier"] + conv.get("offset", 0.0), 4)


def _read(client: Elm327UdsClient, entry: dict, endian: str = "little",
          timeout: float = 1.0) -> Tuple[Optional[str], Optional[float]]:
    """One service-0x22 read -> (raw hex of the whole reply, decoded value)."""
    try:
        reply = client.request(ENGINE, bytes.fromhex(entry["request"]),
                               timeout=timeout)
    except (ObdError, TransportError):
        return None, None
    if not reply or reply[0] != 0x62:
        return None, None
    return reply.hex().upper(), decode_entry(entry, reply, endian)


# --------------------------------------------------------------------------- #
# Endian choice
# --------------------------------------------------------------------------- #
# Plausible ranges for the channels whose real value is known at key-on with a
# cold engine. Deliberately wide -- this only has to separate a sane reading
# from a byte-swapped one, which is normally out by a factor of 256.
_PLAUSIBLE = {
    "6702": (6.0, 16.0),    # battery voltage at start, V
    "2018": (-40.0, 60.0),  # coolant temperature at a cold soak, degC
    "2011": (-40.0, 60.0),  # oil temperature at a cold soak, degC
    "2017": (-40.0, 80.0),  # cylinder head temperature, degC
}


def pick_endian(raws: Dict[str, str]) -> dict:
    """Choose the byte order from raw preflight replies.

    raws maps DID -> raw reply hex. Each plausibility channel votes for the
    order that puts it inside its known range; ties and no-votes fall back to
    little-endian (what the one other Mercedes ECU on file answered) and say
    so, because a guess that announces itself can be corrected from the stored
    raw bytes without another cold start.
    """
    table = _by_did()
    votes = {"little": 0, "big": 0}
    detail = []
    for did, (lo, hi) in _PLAUSIBLE.items():
        entry, raw_hex = table.get(did), raws.get(did)
        if entry is None or not raw_hex:
            continue
        reply = bytes.fromhex(raw_hex)
        row = {"did": did, "name": entry["name"], "range": [lo, hi]}
        for order in ("little", "big"):
            value = decode_entry(entry, reply, order)
            row[order] = value
            if value is not None and lo <= value <= hi:
                votes[order] += 1
                row.setdefault("votes_for", []).append(order)
        detail.append(row)
    if votes["big"] > votes["little"]:
        chosen, confident = "big", True
    elif votes["little"] > votes["big"]:
        chosen, confident = "little", True
    else:
        chosen, confident = "little", False
    return {"endian": chosen, "confident": confident, "votes": votes,
            "evidence": detail,
            "note": ("Chosen from the plausibility channels above."
                     if confident else
                     "NO channel separated the two orders -- defaulted to "
                     "little-endian (what the SLK's SIM271DE20 answered). "
                     "Every sample keeps its raw bytes, so this can be "
                     "re-decoded later without another cold start.")}


# --------------------------------------------------------------------------- #
# probe: the key-on preflight
# --------------------------------------------------------------------------- #
def _probe_client(client: Elm327UdsClient) -> dict:
    ident: List[dict] = []
    vin = None
    for did, label in ((0xF190, "VIN"), (0xF187, "Part number"),
                       (0xF18C, "ECU serial"), (0xF189, "Software")):
        try:
            reply = client.request(ENGINE, bytes([0x22, did >> 8, did & 0xFF]),
                                   timeout=2.0)
        except (ObdError, TransportError):
            continue
        if len(reply) >= 4 and reply[0] == 0x62:
            text = "".join(chr(b) for b in reply[3:] if 32 <= b < 127).strip()
            if text:
                ident.append({"label": label, "value": text})
                if label == "VIN" and len(text) >= 17:
                    vin = text[:17]

    table = _by_did()
    rows: List[dict] = []
    raws: Dict[str, str] = {}
    for entry in _entries():
        raw, _ = _read(client, entry, timeout=2.0)
        row = {"did": entry["did"], "name": entry["name"],
               "family": entry["family"], "unit": entry.get("unit") or None,
               "answered": raw is not None, "raw": raw}
        if raw is not None:
            raws[entry["did"].upper()] = raw
            reply = bytes.fromhex(raw)
            row["value_little"] = decode_entry(entry, reply, "little")
            row["value_big"] = decode_entry(entry, reply, "big")
        rows.append(row)

    endian = pick_endian(raws)
    for row in rows:
        if row["answered"]:
            row["value"] = row[f"value_{endian['endian']}"]
            row["state"] = enum_name(table[row["did"].upper()], row["value"])

    answered_dids = {r["did"].upper() for r in rows if r["answered"]}
    crank_ready = [d for d in CRANK_DIDS if d in answered_dids]
    return {"vin": vin, "ident": ident, "dids": rows,
            "answeredCount": len(answered_dids), "totalCount": len(rows),
            "endian": endian,
            "crankPlan": crank_ready,
            "crankPlanDropped": [d for d in CRANK_DIDS if d not in crank_ready]}


def probe(adapter: Optional[dict], mode: str = "real") -> dict:
    """Key-on preflight: which of the 41 mined OM651 DIDs this car answers.

    Run it the EVENING BEFORE the cold start, ignition on, engine off. It costs
    nothing and it is the only way to find out that a DID NRC-refuses without
    burning the one cold start of the day discovering it mid-crank.
    """
    if mode == "sim":
        return {"ok": True, "simulated": True, "vin": "SIMULATEDMB000002",
                "ident": [], "dids": [], "answeredCount": 0,
                "totalCount": len(_entries()),
                "endian": {"endian": "little", "confident": False,
                           "votes": {}, "evidence": [], "note": "simulated"},
                "crankPlan": list(CRANK_DIDS), "crankPlanDropped": [],
                "message": "SIMULATED -- no real car connected.",
                "timestamp": _now()}

    def run(session, entry):
        session.ensure_protocol()
        client = Elm327UdsClient(session, MERCEDES_ID_MAP)
        client.begin()
        try:
            result = _probe_client(client)
        finally:
            client.restore()
        n, total = result["answeredCount"], result["totalCount"]
        dropped = result["crankPlanDropped"]
        result.update({"ok": n > 0, "simulated": False, "timestamp": _now()})
        result["message"] = (
            f"{n}/{total} mined DIDs answered. Byte order chosen: "
            f"{result['endian']['endian']}"
            f"{'' if result['endian']['confident'] else ' (UNCONFIRMED)'}. "
            f"Crank list ready: {', '.join(result['crankPlan']) or 'NONE'}."
            + (f" Dropped (silent): {', '.join(dropped)}." if dropped else "")
            + ("" if n else " Nothing answered -- check the ignition is on and "
                            "that mercedes_scan_modules sees the engine ECU."))
        return result

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "vin": None, "ident": [],
                "dids": [], "answeredCount": 0, "totalCount": len(_entries()),
                "endian": None, "crankPlan": [], "crankPlanDropped": [],
                "message": str(exc), "timestamp": _now()}


# --------------------------------------------------------------------------- #
# coldstart: the capture
# --------------------------------------------------------------------------- #
def _capture_client(client: Elm327UdsClient, seconds: float, endian: str,
                    crank_plan: List[str],
                    on_sample: Optional[Callable[[dict], None]] = None,
                    should_stop: Optional[Callable[[], bool]] = None) -> dict:
    """The timeline. Read-only.

    Phases move one way only: keyon -> crank -> run. The crank phase reads the
    short list and nothing else, because sample rate IS the measurement there:
    the whole question is the SHAPE of the rail pressure ramp across roughly
    five seconds, and a slow group in the middle of it would put a two-second
    hole in exactly the window that matters.

    Silences are recorded, not fatal: a stretch where nothing answered becomes
    a gap {from, to, seconds}. on_sample() sees each tick as it lands so a
    relayed run can be watched live, and should_stop() ends it cleanly with
    everything so far kept.
    """
    table = _by_did()
    crank_entries = [table[d] for d in crank_plan if d in table]
    run_fast = [table[d] for d in RUN_FAST_DIDS if d in table]
    run_slow = [e for e in _entries() if e["did"].upper() not in _SLOW_SKIP]

    samples: List[dict] = []
    gaps: List[dict] = []
    started = monotonic()
    last_alive = started
    gap_from = None
    tick = 0
    phase = "keyon"
    crank_start_t = None
    fire_t = None
    stopped = gave_up = False

    rpm_entry = table.get("5017")
    rpm_key = _key(rpm_entry) if rpm_entry else None

    def close_gap(to_t: float) -> None:
        if gap_from is None:
            return
        length = round(to_t - gap_from, 2)
        if length >= _MIN_GAP_S:
            gaps.append({"from": gap_from, "to": round(to_t, 2),
                         "seconds": length})

    while True:
        if should_stop is not None and should_stop():
            stopped = True
            break
        sample = {"t": round(monotonic() - started, 2), "phase": phase}
        alive = False

        if phase == "run":
            group = list(run_fast)
            if tick % _SLOW_EVERY == 0:
                group += run_slow
        else:
            group = list(crank_entries)

        for entry in group:
            key = _key(entry)
            raw, value = _read(client, entry, endian, timeout=1.0)
            sample[key] = value
            sample[f"{key}_raw"] = raw
            state = enum_name(entry, value)
            if state is not None:
                sample[f"{key}_s"] = state
            alive = alive or value is not None

        rpm = sample.get(rpm_key) if rpm_key else None
        if rpm is not None:
            if crank_start_t is None and rpm >= CRANK_RPM:
                crank_start_t = sample["t"]
                phase = sample["phase"] = "crank"
            if fire_t is None and rpm >= FIRE_RPM and crank_start_t is not None:
                fire_t = sample["t"]
                phase = "run"

        samples.append(sample)
        if on_sample is not None:
            try:
                on_sample(sample)
            except Exception:
                pass    # the capture outranks the viewer
        tick += 1

        now = monotonic()
        if alive:
            close_gap(sample["t"])
            gap_from = None
            last_alive = now
        elif gap_from is None:
            gap_from = sample["t"]
        if now - started >= seconds:
            break
        if not alive:
            if now - last_alive >= _DEAD_SECONDS:
                gave_up = True
                break
            sleep(0.2)      # a dead bus must not spin the CPU or flood a relay

    if gap_from is not None and samples:
        close_gap(samples[-1]["t"])
    return {"samples": samples, "gaps": gaps,
            "deadSeconds": round(float(sum(g["seconds"] for g in gaps)), 2),
            "crank_start_t": crank_start_t, "fire_t": fire_t,
            "stopped": stopped, "gave_up": gave_up,
            "crankPlan": [e["did"] for e in crank_entries]}


def _series(samples: List[dict], did: str) -> List[Tuple[float, float]]:
    entry = _by_did().get(did.upper())
    if entry is None:
        return []
    key = _key(entry)
    return [(s["t"], s[key]) for s in samples if s.get(key) is not None]


def assess(samples: List[dict], crank_start_t: Optional[float] = None,
           fire_t: Optional[float] = None) -> dict:
    """The reading: how long the crank was and what the ECU was withholding.

    The decision this makes is deliberately narrow. It measures WHEN the rail
    crossed the injection-release band and WHEN the engine fired, and reports
    the gap between them. A crank that ends the moment the rail arrives points
    at fuel delivery; a crank that runs on with the rail already up points away
    from it, at pre-heat or air. Anything else is reported as measured and left
    for a person, because every scaling here is still graded `candidate`.
    """
    notes: List[str] = []
    rail = _series(samples, "D058")
    rpm = _series(samples, "5017")
    demand = _series(samples, "600A")

    if crank_start_t is None:
        crank_start_t = next((t for t, v in rpm if v >= CRANK_RPM), None)
    if fire_t is None:
        fire_t = next((t for t, v in rpm if v >= FIRE_RPM), None)

    crank_s = None
    if crank_start_t is not None and fire_t is not None and fire_t >= crank_start_t:
        crank_s = round(fire_t - crank_start_t, 2)

    def window(series):
        if crank_start_t is None:
            return []
        end = fire_t if fire_t is not None else float("inf")
        return [(t, v) for t, v in series if crank_start_t <= t <= end]

    crank_rail = window(rail)
    crank_rpm = [v for _, v in window(rpm) if v >= CRANK_RPM]

    rail_at_crank = crank_rail[0][1] if crank_rail else None
    rail_at_fire = crank_rail[-1][1] if crank_rail else None
    rail_max_crank = max((v for _, v in crank_rail), default=None)
    t_release = next((t for t, v in crank_rail if v >= RAIL_RELEASE_BAR), None)
    t_release_low = next((t for t, v in crank_rail
                          if v >= RAIL_RELEASE_LOW_BAR), None)
    release_to_fire = (round(fire_t - t_release, 2)
                       if t_release is not None and fire_t is not None else None)
    rail_climb_s = (round(t_release - crank_start_t, 2)
                    if t_release is not None and crank_start_t is not None else None)

    sync_t = None
    for t, v in _series(samples, "60F9"):
        if v and (crank_start_t is None or t >= crank_start_t):
            sync_t = round(t - (crank_start_t or 0.0), 2)
            break

    glow_states = []
    for s in samples:
        state = s.get("did_d017_s")
        if state and state not in glow_states:
            glow_states.append(state)

    volts = [v for _, v in window(_series(samples, "6702"))]
    inj = [v for _, v in window(_series(samples, "6000"))]

    # The catch itself. The complaint is as much "it struggles" as "it cranks
    # long": the engine lights, nearly dies, and only then settles. That shows
    # as an rpm dip in the first seconds of the run phase, and it points at a
    # rail that arrived only just high enough rather than at pre-heat.
    stumble_min = None
    settle_s = None
    if fire_t is not None:
        after = [(t, v) for t, v in rpm
                 if fire_t <= t <= fire_t + STUMBLE_WINDOW_S]
        if after:
            stumble_min = round(min(v for _, v in after), 1)
        stable_from = None
        for t, v in [(t, v) for t, v in rpm if t >= fire_t]:
            if v >= SETTLE_RPM:
                stable_from = stable_from if stable_from is not None else t
            else:
                stable_from = None
            if stable_from is not None and t - stable_from >= 2.0:
                settle_s = round(stable_from - fire_t, 2)
                break

    # Sanity on the mined scalings before anything is concluded from them.
    suspect = []
    if rail and max(v for _, v in rail) > 3000:
        suspect.append("rail pressure feedback (D058) exceeded 3000 bar")
    if rail and all(v == 0 for _, v in rail):
        suspect.append("rail pressure feedback (D058) never left zero")
    if crank_rpm and max(crank_rpm) > 6000:
        suspect.append("engine speed (5017) exceeded 6000 rpm while cranking")
    if suspect:
        notes.append("Scaling looks wrong for: " + "; ".join(suspect)
                     + ". Every sample kept its raw bytes -- re-decode with the "
                       "other byte order before trusting the verdict.")

    verdict = "insufficient_data"
    if crank_s is None:
        notes.append("The crank was not captured -- engine speed never crossed "
                     f"{CRANK_RPM:.0f} then {FIRE_RPM:.0f} rpm inside the run. "
                     "Start the capture BEFORE turning the key.")
    elif crank_s < EXTENDED_CRANK_S:
        verdict = "no_extended_crank"
        notes.append(f"The engine fired after {crank_s:.1f}s -- shorter than the "
                     f"{EXTENDED_CRANK_S:.1f}s complaint. This was probably not "
                     "a true cold soak, or a start earlier the same day already "
                     "primed the fuel circuit. Check the stumble figures below "
                     "before calling it healthy: a short crank that then hunts "
                     "is still the fault.")
    elif t_release is not None and release_to_fire is not None \
            and release_to_fire <= 1.5:
        verdict = "rail_pressure_delay"
        notes.append(
            f"The rail took {rail_climb_s:.1f}s of the {crank_s:.1f}s crank to "
            f"reach {RAIL_RELEASE_BAR:.0f} bar, and the engine fired "
            f"{release_to_fire:.1f}s after it got there. The ECU was waiting "
            "for fuel pressure, not for heat -- fuel drain-back / air ingress "
            "in the low-pressure side is the first thing to prove, and the "
            "ignition-prime test proves it without any tools.")
    elif rail_at_crank is not None and rail_at_crank >= RAIL_RELEASE_BAR:
        verdict = "rail_ready_early"
        notes.append(
            f"The rail was already at {rail_at_crank:.0f} bar on the first "
            f"crank sample and the engine still turned for {crank_s:.1f}s. "
            "Fuel delivery is NOT the bottleneck -- look at pre-heat (glow "
            "states below), cam/crank sync time and compression.")
    elif t_release is None and rail_max_crank is not None:
        verdict = "rail_never_reached_release"
        notes.append(
            f"The rail never passed {RAIL_RELEASE_BAR:.0f} bar during the "
            f"crank (peak {rail_max_crank:.0f} bar) yet the engine started. "
            "Either the release threshold on this ECU is lower than assumed, "
            "or the D058 scaling is wrong. Check the raw bytes.")
    else:
        verdict = "crank_long_cause_unclear"
        notes.append("The crank was long but the rail trace does not explain "
                     "it on its own. Compare against a warm start captured the "
                     "same day -- the difference is the fault.")

    if stumble_min is not None and stumble_min < STUMBLE_RPM:
        notes.append(
            f"After it caught, engine speed fell back to {stumble_min:.0f} rpm "
            "before it settled -- it nearly died again. That is the 'struggle' "
            "half of the complaint, and it fits a rail that only just reached "
            "the release pressure rather than a pre-heat fault.")
    if settle_s is not None and settle_s > 2.0:
        notes.append(f"It took {settle_s:.1f}s after firing to hold a steady "
                     "idle.")

    if sync_t is not None and sync_t > LATE_SYNC_S:
        notes.append(f"Cam/crank sync (60F9) took {sync_t:.1f}s to go TRUE. "
                     "That is late; a sensor or a reluctor problem delays "
                     "injection on its own.")
    elif sync_t is not None:
        notes.append(f"Cam/crank sync (60F9) went TRUE {sync_t:.1f}s into the "
                     "crank -- normal, so the crank/cam sensors are cleared.")

    if glow_states:
        notes.append("Glow plug controller states seen: "
                     + ", ".join(glow_states)
                     + ". 'Pre Glow' before cranking and 'Starter Glow' during "
                       "it are what a healthy pre-heat looks like; jumping "
                       "straight to 'No Pre Glow' on a cold engine is not.")

    if volts and min(volts) < 9.5:
        notes.append(f"Battery voltage fell to {min(volts):.1f} V during the "
                     "crank even on a booster pack -- fix that before reading "
                     "anything else here.")

    return {
        "verdict": verdict,
        "crank_seconds": crank_s,
        "crank_start_t": crank_start_t,
        "fire_t": fire_t,
        "crank_rpm_min": round(min(crank_rpm), 1) if crank_rpm else None,
        "crank_rpm_max": round(max(crank_rpm), 1) if crank_rpm else None,
        "rail_at_crank_bar": rail_at_crank,
        "rail_at_fire_bar": rail_at_fire,
        "rail_max_crank_bar": rail_max_crank,
        "rail_release_bar": RAIL_RELEASE_BAR,
        "t_rail_release": t_release,
        "t_rail_release_low": t_release_low,
        "rail_climb_seconds": rail_climb_s,
        "release_to_fire_seconds": release_to_fire,
        "rail_demand_at_crank_bar": (window(demand)[0][1]
                                     if window(demand) else None),
        "stumble_rpm_min": stumble_min,
        "settle_seconds": settle_s,
        "sync_seconds": sync_t,
        "glow_states": glow_states,
        "crank_voltage_min": round(min(volts), 2) if volts else None,
        "injection_demand_max": round(max(inj), 3) if inj else None,
        "scaling_suspect": suspect,
        "notes": notes,
        "reference": "backend/profiles/mercedes/data/cla_dids.json",
    }


def coldstart(adapter: Optional[dict], seconds: float = 180.0,
              endian: Optional[str] = None,
              crank_dids: Optional[List[str]] = None,
              preflight: bool = True, mode: str = "real",
              on_sample: Optional[Callable[[dict], None]] = None,
              should_stop: Optional[Callable[[], bool]] = None) -> dict:
    """Capture an OM651 cold start from key-on through the crank to warm idle.

    Plug in and switch the ignition ON, then START THIS FIRST and only turn the
    key to crank once it reports that it is running. The capture needs the
    key-on seconds before the starter engages: they are the baseline the crank
    is measured against.

    endian: force "little" or "big"; omit to decide it from the preflight.
    crank_dids: override the short crank list; omit to use the preflight's.
    preflight: read every DID once at key-on first (a few seconds) to drop
        silent ones from the crank list and settle the byte order. Turn it off
        only when a probe() from the same car has already answered.
    """
    if mode == "sim":
        return {"ok": True, "simulated": True, "samples": [], "gaps": [],
                "deadSeconds": 0.0, "preflight": None,
                "endian": endian or "little", "crankPlan": list(CRANK_DIDS),
                "assessment": assess([]),
                "message": "SIMULATED -- no real car connected.",
                "timestamp": _now()}
    if endian not in (None, "little", "big"):
        return {"ok": False, "simulated": False, "samples": [],
                "message": "endian must be 'little', 'big' or omitted.",
                "timestamp": _now()}

    def run(session, entry):
        session.ensure_protocol()
        client = Elm327UdsClient(session, MERCEDES_ID_MAP)
        client.begin()
        pre = None
        try:
            chosen_endian = endian
            plan = list(crank_dids) if crank_dids else list(CRANK_DIDS)
            if preflight:
                pre = _probe_client(client)
                if chosen_endian is None:
                    chosen_endian = pre["endian"]["endian"]
                if not crank_dids:
                    plan = pre["crankPlan"] or list(CRANK_DIDS)
            chosen_endian = chosen_endian or "little"
            result = _capture_client(client, min(seconds, 3600.0),
                                     chosen_endian, plan,
                                     on_sample=on_sample,
                                     should_stop=should_stop)
        finally:
            client.restore()

        samples = result["samples"]
        elapsed = samples[-1]["t"] if samples else 0.0
        assessment = assess(samples, result["crank_start_t"], result["fire_t"])
        crank_ticks = sum(1 for s in samples if s["phase"] == "crank")
        return {
            "ok": bool(samples), "simulated": False,
            "vin": (pre or {}).get("vin"),
            "preflight": pre,
            "endian": chosen_endian,
            "crankPlan": result["crankPlan"],
            "sampleCount": len(samples),
            "crankSampleCount": crank_ticks,
            "elapsed_s": elapsed,
            "rate_per_s": round(len(samples) / elapsed, 2) if elapsed else None,
            "samples": samples,
            "gaps": result["gaps"],
            "deadSeconds": result["deadSeconds"],
            "stoppedEarly": result["stopped"],
            "assessment": assessment,
            "message": (
                f"Captured {len(samples)} tick(s) over {elapsed:.0f}s "
                f"({crank_ticks} during the crank), byte order "
                f"{chosen_endian}. "
                + (f"Crank lasted {assessment['crank_seconds']:.1f}s. "
                   if assessment["crank_seconds"] is not None else "")
                + (f"{len(result['gaps'])} link gap(s) totalling "
                   f"{result['deadSeconds']:.0f}s. " if result["gaps"] else "")
                + ("Stopped early on request. " if result["stopped"] else "")
                + (f"WARNING: gave up -- nothing answered for "
                   f"{_DEAD_SECONDS:.0f}s. " if result["gave_up"] else "")
                + f"Verdict: {assessment['verdict']}."),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "samples": [],
                "preflight": None, "endian": endian, "crankPlan": [],
                "assessment": assess([]),
                "message": str(exc), "timestamp": _now()}
