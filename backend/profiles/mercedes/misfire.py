"""SLK R172 petrol rough-running misfire capture (captures/mercedes/slk.md step 4).

WHY THIS FILE EXISTS
--------------------
Steve's 2011 SLK (R172) runs rough. The engine is either the M271 EVO CGI turbo
4-cyl (SLK200/250, SIM271DE20 ECU) or the M276 3.5 V6 (SLK350, MED177) --
step 0 of the plan identifies which. The dealer "actual values" for both ECUs
were mined from XENTRY 26.6.4's Caesar layer into data/xentry_dids.json (85
DIDs, all graded `candidate` -- none has answered on a car yet). This module
turns that mined table into a working instrument:

    identify() -- which engine is fitted (probe one SIM-only and one MED-only
                  DID), plus VIN and part numbers. Step 0.
    capture()  -- the timestamped drive-log of step 4: every tick reads rpm,
                  the per-cylinder misfire counters and the upstream lambda;
                  every 5th tick adds coolant, oil temp, voltage, load, MAF,
                  fuel pressures and mixture adaptations. Every decoded value
                  is stored WITH its raw bytes so a wrong scaling can be
                  re-read later without driving the car again. Samples can be
                  handed out tick by tick (on_sample) and the drive stopped
                  early (should_stop), because a 10-minute capture run down
                  a phone relay wants watching while it happens rather than
                  read once it is over.
    assess()   -- the step-5 reading: WHICH counter climbed, WHEN (cold vs
                  warm), and whether trims or rail pressure moved with it.

The signatures (slk.md step 5):
    one cylinder climbing          -> coil/injector on that hole
    all climbing evenly            -> fuelling/air
    counters climb + rail sags     -> HP pump

Every entry stays graded exactly as its source: candidate until it has
answered on this car, after which the live_<vin> promotion recorded in the
data file's notes applies. Read-only throughout: services 0x22 and 3E only,
no sessions opened, nothing written to the car.
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
DATA_PATH = Path(__file__).resolve().parent / "data" / "xentry_dids.json"
KNOWN_PATH = Path(__file__).resolve().parent / "data" / "slk_rough_running.json"

_BURST_SECONDS = 20.0   # cam positions every tick this long after first fire
_SLOW_EVERY = 5        # slow-group read every Nth tick

# Give up on a wall clock, not on a tick count. A tick is however long the
# reads took, so it is not a unit of time at all once the dongle is in the car
# on the end of a phone relay: a cell-tower handover can swallow several
# seconds, and counting ticks would end a customer's drive capture at minute 4
# without telling anyone until they stopped driving. 90s of nothing whatsoever
# is a dropped adapter or a switched-off ignition; anything shorter is the link
# breathing and the capture waits it out.
_DEAD_SECONDS = 90.0
_MIN_GAP_S = 2.0       # shorter silences are ordinary jitter, not a hole


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


XENTRY = _load(DATA_PATH)
KNOWN = _load(KNOWN_PATH)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entry_key(entry: dict) -> str:
    """Stable sample key. Misfire counters become cyl<n> in engine-DID order --
    the SIM's own names carry the true cylinder numbers and they deliberately
    disagree with DID order (6012 is cyl 4), so the plan stamps the mapping."""
    return entry.get("_key") or f"did_{entry['did'].lower()}"


def _build_plan(ecu_key: str) -> dict:
    """Per-ECU capture plan from the mined table.

    fast  -- read EVERY tick: rpm + the misfire family + upstream lambda
             (+ short-term mixture correction where the ECU exposes one)
    slow  -- every 5th tick: coolant, oil temp, voltage, load, MAF, fuel
             pressures, mixture long-term adaptations, camshaft positions
    cyls  -- [(cylinder number, entry)] in engine order, from the misfire family
    """
    ecu = XENTRY[ecu_key]
    by_family: Dict[str, List[dict]] = {}
    for raw_entry in ecu["dids"]:
        entry = dict(raw_entry)
        by_family.setdefault(entry["family"], []).append(entry)

    # Cylinder mapping: only DIDs 6011..6018 are true per-cylinder counters --
    # the MED177 family also carries aggregates (65A0/65B0 homogeneous/
    # stratified) and the 606D rough-run test value, which stay out of the
    # cylinder map. The SIM's names carry real hole numbers (6012 = cyl 4);
    # where the name is silent, DID order stands in, flagged so.
    misfire_fam = by_family.get("misfire", [])
    cyl_entries = [e for e in misfire_fam
                   if 0x6011 <= int(e["did"], 16) <= 0x6018]
    cyls = []
    for i, entry in enumerate(sorted(cyl_entries,
                                     key=lambda e: int(e["did"], 16)), start=1):
        name_digit = next((w[-1] for w in (entry.get("name") or "").split()
                           if w[-1].isdigit()), None)
        entry["_key"] = f"cyl{name_digit}" if name_digit else f"cyl{i}"
        entry["_key_source"] = "name" if name_digit else "did-order-assumption"
        cyl_no = int(name_digit) if name_digit else i
        cyls.append((cyl_no, entry))
    for entry in misfire_fam:
        if entry not in cyl_entries:
            entry.setdefault("_key", f"did_{entry['did'].lower()}")
            entry["_key_source"] = "aggregate"
            by_family.setdefault("mixture_aggregate", []).append(entry)

    # rpm must lead every tick; lambda is the fast mixture channel.
    rpm = dict(next(e for e in ecu["dids"] if e["did"] == "2000"))
    rpm["_key"] = "rpm"
    lam = next((dict(e) for e in ecu["dids"] if e["did"] == "2046"), None)
    stft = next((dict(e) for e in ecu["dids"] if e["did"] == "6132"), None)
    smooth = [dict(e) for e in by_family.get("smooth_running", [])]
    for e in smooth:
        e["_key"] = f"did_{e['did'].lower()}"
    fast = [rpm] + [dict(e) for _, e in cyls] + smooth \
        + ([lam] if lam else []) + ([stft] if stft else [])

    slow = []
    for fam in ("baseline", "fuel_pressure", "mixture", "camshaft",
                "charge_pressure", "injector", "ignition", "mixture_aggregate"):
        for entry in by_family.get(fam, []):
            if entry["did"] in ("2000", "2046", "6132"):
                continue
            slow.append(entry)
    for entry in slow:
        entry.setdefault("_key", f"did_{entry['did'].lower()}")

    rail = [e for e in slow if e["family"] == "fuel_pressure"
            and e["did"] in ("206F", "2071")]
    # burst -- camshaft actual positions, read EVERY tick for the first
    # _BURST_SECONDS after the engine fires. The SLK's cold-start roughness and
    # chain rattle (2026-08-23) live in those seconds, where a bled-down
    # tensioner shows as cam error that snaps to target as oil pressure builds;
    # the 30 s slow cadence never sees it.
    burst = [e for e in slow if e["family"] == "camshaft"]
    return {"ecu": ecu_key, "fast": fast, "slow": slow, "cyls": cyls,
            "rail": rail, "burst": burst, "title": ecu["title"]}


def decode_entry(entry: dict, reply: Optional[bytes]) -> Optional[float]:
    """Mined scaling -> engineering value, None if short/negative/silent.

    Every mined entry sits at byte offset 3 of the positive reply -- the first
    data byte after `62 <DID hi> <DID lo>` -- little-endian (confirmed on-car), linear conversion.
    """
    if not reply or len(reply) < 3 or reply[0] != 0x62:
        return None
    off, size = entry["byte_offset"], entry["byte_size"]
    if len(reply) < off + size:
        return None
    # Confirmed on the car 2026-08-23 (SLK R172, SIM271DE20): multi-byte
    # values arrive LITTLE-endian (rpm FD 04 = 1277, MAP BC 2E = 992 mbar at
    # key-on) and the mixture family is signed two's complement (trims FE FF =
    # -2 counts = -0.3 %). Camshaft deviations are signed single bytes.
    raw = int.from_bytes(reply[off:off + size], "little")
    signed = (entry.get("family") == "mixture"
              or (entry.get("family") == "camshaft" and size == 1))  # PWM duty is unsigned
    if signed and raw >= 1 << (8 * size - 1):
        raw -= 1 << (8 * size)
    conv = entry["conversion"]
    if conv.get("type") == "float32" and size == 4:
        import struct
        return round(struct.unpack("<f", reply[off:off + 4])[0], 6)
    if conv.get("type") != "linear":
        return float(raw)   # 'unknown' conversions pass the raw count through
    return round(raw * conv["multiplier"] + conv["offset"], 4)


def _read_entry(client: Elm327UdsClient, entry: dict,
                timeout: float = 1.0) -> Tuple[Optional[str], Optional[float]]:
    """One service-0x22 read -> (raw hex of the whole reply, decoded value)."""
    try:
        reply = client.request(ENGINE, bytes.fromhex(entry["request"]), timeout=timeout)
    except (ObdError, TransportError):
        return None, None
    if not reply or reply[0] != 0x62:
        return None, None
    return reply.hex().upper(), decode_entry(entry, reply)


# Marker DIDs used to tell the two candidate ECUs apart: each answers only on
# its own engine (SIM271DE20 is 4-cyl so has no cylinder-5+ counter; MED177 has
# true per-cylinder injector adaptations the SIM lacks).
_SIM_ONLY_DID = "2100"   # knock sensor signal voltage cyl 1 (SIM ignition family)
_MED_ONLY_DIDS = ("6015", "6016", "6017", "6018", "6570")


def _identify_client(client: Elm327UdsClient) -> dict:
    """Step 0 against an open client: identity + which of the two ECUs this is."""
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

    sim_entry = next((e for e in XENTRY["sim271de20_m271_turbo"]["dids"]
                      if e["did"] == _SIM_ONLY_DID), None)
    med_hits, sim_hits = [], []
    med_raw = {}
    if sim_entry is not None:
        _, value = _read_entry(client, sim_entry, timeout=1.5)
        if value is not None:
            sim_hits.append(_SIM_ONLY_DID)
    for did in _MED_ONLY_DIDS:
        entry = next((e for e in XENTRY["med177_m276_v6"]["dids"]
                      if e["did"] == did), None)
        if entry is None:
            continue
        raw, value = _read_entry(client, entry, timeout=1.5)
        if value is not None:
            med_hits.append(did)
            med_raw[did] = raw

    if sim_hits and not med_hits:
        ecu_key = "sim271de20_m271_turbo"
    elif med_hits and not sim_hits:
        ecu_key = "med177_m276_v6"
    elif sim_hits and med_hits:
        ecu_key = "ambiguous"
    else:
        ecu_key = None

    return {
        "vin": vin, "ident": ident, "ecu": ecu_key,
        "sim_markers": sim_hits, "med_markers": med_hits,
        "marker_raw": med_raw,
        "engine": KNOWN.get("engines", {}).get(ecu_key, {}).get("name")
        if ecu_key else None,
        "message": "",
    }


def identify(adapter: Optional[dict], mode: str = "real") -> dict:
    """slk.md step 0: which variant is this SLK, answered by the car itself."""
    if mode == "sim":
        return {
            "ok": True, "simulated": True, "vin": "SIMULATEDMB000001",
            "ident": [{"label": "VIN", "value": "SIMULATEDMB000001"},
                      {"label": "Part number", "value": "A2712000350"}],
            "ecu": "sim271de20_m271_turbo", "sim_markers": ["2100"],
            "med_markers": [], "marker_raw": {},
            "engine": "M271 EVO DE18 LA turbo CGI 4-cyl (SLK200/250, 271.8)",
            "message": "SIMULATED -- no real car connected.", "timestamp": _now(),
        }

    def run(session, entry):
        session.ensure_protocol()
        client = Elm327UdsClient(session, MERCEDES_ID_MAP)
        client.begin()
        try:
            result = _identify_client(client)
        finally:
            client.restore()
        result.update({"ok": result["ecu"] is not None, "simulated": False,
                       "timestamp": _now()})
        result["message"] = (
            f"Engine ECU identified as {result['ecu']}" if result["ecu"]
            else "Neither SIM-only nor MED-only markers answered -- the engine "
                 "ECU may not speak UDS service 0x22 (run mercedes_scan_modules "
                 "first; a KWP-era answer changes this plan's step 3 data).")
        return result

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "vin": None, "ident": [],
                "ecu": None, "sim_markers": [], "med_markers": [],
                "marker_raw": {}, "engine": None,
                "message": str(exc), "timestamp": _now()}


def _capture_client(client: Elm327UdsClient, seconds: float, ecu_key: str,
                    on_sample: Optional[Callable[[dict], None]] = None,
                    should_stop: Optional[Callable[[], bool]] = None) -> dict:
    """The step-4 timeline against an open client. Read-only.

    on_sample(sample), if given, is handed each tick the moment it lands, so a
    background job can show the drive live and keep everything already read
    even if the run never finishes. It is deliberately fenced off: a progress
    consumer that throws is its own problem, and must never cost Steve the
    drive he just did. should_stop(), if given, ends the loop cleanly and
    returns everything collected so far.

    Silences are recorded rather than fatal. A stretch of ticks where nothing
    at all answered becomes a gap {from, to, seconds} -- `from` is the first
    silent tick and `to` the tick that answered again (or the last tick, if
    the capture ended still silent). Only stretches of _MIN_GAP_S or longer
    are worth telling the operator about.
    """
    plan = None
    samples: List[dict] = []
    gaps: List[dict] = []
    started = monotonic()
    last_alive = started      # monotonic stamp of the last tick that answered
    gap_from = None           # sample-time the current silence started at
    tick = 0
    stopped = False
    gave_up = False
    first_fire_t = None       # sample-time rpm first read non-zero

    def close_gap(to_t: float) -> None:
        if gap_from is None:
            return
        length = round(to_t - gap_from, 2)
        if length >= _MIN_GAP_S:
            gaps.append({"from": gap_from, "to": round(to_t, 2),
                         "seconds": length})

    while True:
        if plan is None:
            plan = _build_plan(ecu_key)
        if should_stop is not None and should_stop():
            stopped = True
            break
        sample = {"t": round(monotonic() - started, 2)}
        alive = False
        for entry in plan["fast"]:
            key = _entry_key(entry)
            raw, value = _read_entry(client, entry, timeout=1.0)
            sample[key] = value
            sample[f"{key}_raw"] = raw
            alive = alive or value is not None
        if sample.get("rpm") and first_fire_t is None:
            first_fire_t = sample["t"]
        in_burst = (first_fire_t is not None
                    and sample["t"] - first_fire_t <= _BURST_SECONDS)
        if in_burst:
            for entry in plan.get("burst", []):
                key = _entry_key(entry)
                raw, value = _read_entry(client, entry, timeout=1.0)
                sample[key] = value
                sample[f"{key}_raw"] = raw
                alive = alive or value is not None
        if tick % _SLOW_EVERY == 0:
            for entry in plan["slow"]:
                if in_burst and entry in plan.get("burst", []):
                    continue    # already read this tick
                key = _entry_key(entry)
                raw, value = _read_entry(client, entry, timeout=1.0)
                sample[key] = value
                sample[f"{key}_raw"] = raw
                alive = alive or value is not None
        samples.append(sample)
        if on_sample is not None:
            try:
                on_sample(sample)
            except Exception:
                pass    # see the docstring: the drive data outranks the viewer
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
    return {"plan": plan, "samples": samples, "gaps": gaps,
            "deadSeconds": round(float(sum(g["seconds"] for g in gaps)), 2),
            "stopped": stopped, "gave_up": gave_up}


def _accumulated(series) -> float:
    """Total misfires logged over a (t, count) series. The SIM271DE20 clears
    its per-cylinder counters once warm (seen on the SLK: cyl 4 read 7 for
    40 s after a cold start, then 0), so last-minus-first would hide a cold
    spike -- sum only the positive steps and ignore resets."""
    total = 0.0
    for (_, a), (_, b) in zip(series, series[1:]):
        if b > a:
            total += b - a
    return total + max(0.0, series[0][1])


def assess(samples: List[dict], ecu_key: Optional[str]) -> dict:
    """slk.md step 5: which counter climbed, when, and what moved with it."""
    bands = KNOWN.get("bands", {})
    sig_level = bands.get("misfire_delta_significant", 15)
    margin = bands.get("misfire_leader_margin", 10)
    trim_note = bands.get("trim_pct_note_threshold", 8.0)
    cold_c = bands.get("cold_coolant_c", 60.0)

    if ecu_key is None:
        return {"verdict": "no_ecu_identified", "notes": [
            "Run identify first -- the cylinder map differs between the "
            "4-cyl and the V6."]}

    plan = _build_plan(ecu_key)
    notes: List[str] = []
    deltas = {}
    first_last = {}
    for cyl, entry in plan["cyls"]:
        key = _entry_key(entry)
        series = [(s["t"], s[key]) for s in samples
                  if s.get(key) is not None]
        if len(series) >= 2:
            deltas[cyl] = _accumulated(series)
            first_last[cyl] = (series[0][1], series[-1][1])

    verdict = "quiet_or_insufficient_data"
    leaders: List[int] = []
    if len(deltas) >= 2:
        ranked = sorted(deltas.items(), key=lambda kv: -kv[1])
        top, rest = ranked[0], ranked[1:]
        others_max = max((v for _, v in rest), default=0)
        if top[1] >= sig_level and top[1] - others_max >= margin:
            co_leaders = [c for c, v in deltas.items()
                          if v >= sig_level and top[1] - v < margin]
            leaders = sorted(set([top[0]] + co_leaders))
            verdict = ("single_cylinder" if len(leaders) == 1
                       else "multiple_cylinders")
            hole = ", ".join(str(c) for c in leaders)
            detail = "; ".join(f"cyl{c} {first_last[c][0]:.0f}->{first_last[c][1]:.0f}"
                               for c in leaders)
            notes.append(f"Cylinder {hole} climbed while the rest sat still "
                         f"({detail}) -- coil/injector on that hole, or (V6) "
                         "oil-fouled well from a leaking cam magnet.")
        elif all(v >= sig_level for _, v in ranked) and \
                (max(deltas.values()) - min(deltas.values())) < margin:
            verdict = "all_cylinders_evenly"
            notes.append(
                "All counters climbed together within "
                f"{margin} counts ({', '.join(f'c{c}:{v:.0f}' for c, v in sorted(deltas.items()))}) "
                "-- fuelling/air, not one hole's hardware.")

    # Cold/warm split: when did the counters move?
    cold_gain = warm_gain = None
    coolant_series = [(s["t"], s["did_2013"]) for s in samples
                      if s.get("did_2013") is not None]
    if deltas and coolant_series:
        cold_t = next((t for t, c in coolant_series if c >= cold_c), None)
        if cold_t is None:
            cold_t = coolant_series[-1][0]
        def gain_before(t_cut):
            out = {}
            for cyl, entry in plan["cyls"]:
                key = _entry_key(entry)
                pts = [(s["t"], s[key]) for s in samples
                       if s.get(key) is not None and s["t"] <= t_cut]
                if len(pts) >= 2:
                    out[cyl] = _accumulated(pts)
            return out
        cold_gain = sum(max(0, v) for v in gain_before(cold_t).values())
        warm_gain = sum(max(0, v) for v in deltas.values()) - cold_gain
        if warm_gain < 0:
            warm_gain = 0.0
        if cold_gain and not warm_gain:
            notes.append(
                f"All misfire gain happened below {cold_c:.0f} C coolant "
                "(the DI-carbon cold-idle pattern; also the PCV pattern).")
        elif warm_gain and cold_gain and max(warm_gain, cold_gain) > sig_level:
            notes.append("Misfires continued when warm too -- less consistent "
                         "with pure cold-start carbon buildup.")

    # Trims/adaptations direction (fuelling evidence). Only channels scaled in
    # % are thresholded -- the V6's multiplicative adaptation factors are unit-
    # less and are reported raw by the capture instead.
    trim_entries = [e for e in plan["fast"] + plan["slow"]
                    if e["family"] == "mixture" and e.get("unit") == "%"]
    trims = {}
    for entry in trim_entries:
        key = _entry_key(entry)
        vals = [s[key] for s in samples if s.get(key) is not None]
        if vals:
            trims[f"{key} ({entry['name']})"] = round(sum(vals) / len(vals), 2)
            avg = trims[f"{key} ({entry['name']})"]
            if abs(avg) >= trim_note:
                direction = "LEAN" if avg > 0 else "rich"
                notes.append(f"{entry['name']} averaged {avg:+.1f}% ({direction}) "
                             "-- fuelling/air side worth checking (PCV, boost "
                             "hose, injectors, carbon).")

    # Rail pressure sagging while counters climb -> HP pump (MED177).
    pump_note = None
    if plan["rail"]:
        rail_key = _entry_key(plan["rail"][0])
        rail_pts = [(s["t"], s[rail_key]) for s in samples
                    if s.get(rail_key) is not None]
        # The running total only counts the cylinders this car actually
        # answered on, and only ticks where every one of them answered. A
        # relay gap leaves Nones, and folding those to zero would read as the
        # counters having RESET -- which drags the midpoint this compares
        # against and can invent a pressure sag out of a lost connection.
        live_keys = [k for k in (_entry_key(e) for _, e in plan["cyls"])
                     if any(s.get(k) is not None for s in samples)]
        misf_total = []
        for s in samples:
            if live_keys and all(s.get(k) is not None for k in live_keys):
                misf_total.append((s["t"], sum(s[k] for k in live_keys)))
        if len(rail_pts) > 4 and misf_total:
            mid_rail = sorted(v for _, v in rail_pts)[len(rail_pts) // 2]
            low_half = [t for t, m in misf_total
                        if m >= (misf_total[-1][1] + misf_total[0][1]) / 2 + sig_level]
            rails_in_low = [v for t, v in rail_pts
                            if any(abs(t - lt) < 2.5 for lt in low_half)]
            if rails_in_low and sorted(rails_in_low)[len(rails_in_low) // 2] < mid_rail * 0.9:
                pump_note = ("Rail pressure ran below its median while the "
                             "counters climbed -- HP fuel pump territory.")
                notes.append(pump_note)
                if verdict in ("single_cylinder", "all_cylinders_evenly"):
                    verdict = "hp_pressure_sag"

    matched = match_known_issues(verdict, ecu_key, leaders)
    return {
        "verdict": verdict,
        "leaders": leaders,
        "deltas": {str(c): round(v, 1) for c, v in deltas.items()},
        "cold_gain": round(cold_gain, 1) if cold_gain is not None else None,
        "warm_gain": round(warm_gain, 1) if warm_gain is not None else None,
        "trims_avg": trims,
        "pump_sag": pump_note is not None,
        "notes": notes,
        "known_issues_matched": matched,
        "reference": "backend/profiles/mercedes/data/slk_rough_running.json",
    }


def match_known_issues(verdict: str, ecu_key: Optional[str],
                       leaders: List[int]) -> List[dict]:
    """Known-issue rows for this engine, ranked by how well the capture's
    verdict matches their documented signature. Honest about being weak:
    this is a reading aid, not a diagnosis."""
    out = []
    for issue in KNOWN.get("issues", []):
        if ecu_key and ecu_key not in issue.get("engines", []):
            continue
        hint = (issue.get("signature_hint") or "").lower()
        score = 0
        if verdict == "single_cylinder" and (
                "one cylinder" in hint or "one-two" in hint
                or "climbing cylinders" in hint or "hole" in hint):
            score += 2
        if verdict == "multiple_cylinders" and (
                "adjacent" in hint or "climbing cylinders" in hint):
            score += 2
        if verdict == "all_cylinders_evenly" and (
                "both banks" in hint or "spread across cylinders" in hint
                or "cold idle" in hint or "trims" in hint):
            score += 2
        if verdict == "hp_pressure_sag" and "rail pressure" in hint:
            score += 3
        out.append({
            "id": issue["id"], "name": issue["name"],
            "symptoms": issue["symptoms"], "fix": issue["fix"],
            "dtc_hints": issue.get("dtc_hints", []),
            "signature_hint": issue.get("signature_hint"),
            "sources": issue.get("sources", []),
            "match_score": score,
        })
    out.sort(key=lambda i: -i["match_score"])
    return out


def known_issues(ecu_key: Optional[str] = None) -> dict:
    """The research reference, optionally filtered to one engine."""
    issues = KNOWN.get("issues", [])
    if ecu_key:
        issues = [i for i in issues if ecu_key in i.get("engines", [])]
    return {"engines": KNOWN.get("engines", {}), "bands": KNOWN.get("bands", {}),
            "issues": issues}


def capture(adapter: Optional[dict], seconds: float = 300.0,
            ecu: Optional[str] = None, mode: str = "real",
            on_sample: Optional[Callable[[dict], None]] = None,
            should_stop: Optional[Callable[[], bool]] = None) -> dict:
    """slk.md step 4: the rough-running drive log.

    Plug in with the ignition ON and start this BEFORE the drive profile:
        idle ~3 min (the CGI cold-start window) -> warm idle 2 min
        -> hold ~2000 rpm -> hold ~3000 rpm -> (if load-dependent) a road loop.
    Every tick logs rpm + per-cylinder misfire counters + lambda; every 5th
    tick adds the slow channels (coolant, oil temp, voltages, pressures,
    adaptations). Values stay graded `candidate` until they answer here.
    Read-only throughout.

    on_sample(sample) sees each tick as it lands and should_stop() ends the
    drive early -- both optional, and neither changes what is read from the
    car. The result carries "gaps" and "deadSeconds" so a reader can tell a
    clean run from one that spent half the drive off the air.
    """
    if mode == "sim":
        return {
            "ok": True, "simulated": True, "samples": [], "gaps": [],
            "deadSeconds": 0.0,
            "assessment": assess([], ecu), "ecu": ecu,
            "message": "SIMULATED -- no real car connected.", "timestamp": _now(),
        }
    if ecu not in (None,) and ecu not in XENTRY:
        return {"ok": False, "simulated": False, "samples": [], "ecu": ecu,
                "message": f"Unknown ecu '{ecu}' -- use one of {sorted(XENTRY)}.",
                "timestamp": _now()}

    def run(session, entry):
        session.ensure_protocol()
        client = Elm327UdsClient(session, MERCEDES_ID_MAP)
        client.begin()
        try:
            chosen = ecu
            ident = None
            if chosen is None:
                ident = _identify_client(client)
                chosen = ident.get("ecu")
                if chosen in (None, "ambiguous"):
                    return {
                        "ok": False, "simulated": False, "samples": [],
                        "ecu": chosen, "identify": ident,
                        "message": (
                            "Could not pin the engine ECU down (markers "
                            f"SIM={ident.get('sim_markers')} "
                            f"MED={ident.get('med_markers')}). Run identify "
                            "and pass --ecu explicitly."),
                        "timestamp": _now(),
                    }
            result = _capture_client(client, min(seconds, 3600.0), chosen,
                                     on_sample=on_sample,
                                     should_stop=should_stop)
        finally:
            client.restore()

        plan = result["plan"]
        samples = result["samples"]
        gaps = result["gaps"]
        dead_s = result["deadSeconds"]
        rpms = [s[_entry_key(plan["fast"][0])] for s in samples
                if s.get(_entry_key(plan["fast"][0])) is not None]
        elapsed = samples[-1]["t"] if samples else 0.0
        assessment = assess(samples, chosen)
        return {
            "ok": bool(samples), "simulated": False,
            "ecu": chosen, "engine": plan["title"],
            "identify": ident,
            "sampleCount": len(samples),
            "elapsed_s": elapsed,
            "rate_per_s": round(len(samples) / elapsed, 2) if elapsed else None,
            "rpm_range": [min(rpms), max(rpms)] if rpms else None,
            "samples": samples,
            "gaps": gaps,
            "deadSeconds": dead_s,
            "stoppedEarly": result["stopped"],
            "assessment": assessment,
            "message": (
                f"Captured {len(samples)} tick(s) over {elapsed:.0f}s on {chosen}. "
                + (f"rpm spanned {min(rpms):.0f}-{max(rpms):.0f}. " if rpms else "")
                + (f"{len(gaps)} link gap(s) totalling {dead_s:.0f}s -- the car "
                   "answered nothing through those, so the timeline has holes; "
                   "the samples either side are still good. " if gaps else "")
                + ("Stopped early on request -- everything read up to that "
                   "point is here. " if result["stopped"] else "")
                + (f"WARNING: the capture gave up -- nothing answered for "
                   f"{_DEAD_SECONDS:.0f}s (adapter, ignition or relay dropped "
                   "for good, not a handover). " if result["gave_up"] else "")
                + f"Verdict: {assessment['verdict']}."
                + (" Rev the engine / include a cold window -- nothing moved "
                   "enough to judge." if assessment["verdict"] == "quiet_or_insufficient_data" else "")),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "samples": [], "ecu": ecu,
                "message": str(exc), "timestamp": _now()}
