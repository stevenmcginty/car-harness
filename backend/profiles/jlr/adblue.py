"""Jaguar X260 XF 3.0d AdBlue/SCR + DPF diagnostics over a generic OBD-II dongle.

Target: the 2016 XF (X260) 3.0d V6 diesel (306DT), EU6 with SCR -- SDD-era CAN
diagnostics, so a plain dongle genuinely reaches the PT-CAN (research:
docs/private/jlr-x260-adblue-research.md). The job is read-only depth on the
aftertreatment: what fault memory says, what live values can be read, and a
sourced assessment of the AdBlue fault space -- NOT any invented decoding.

THE HONEST GAP: no public JLR DID list exists for DPF soot mass, regen status,
DEF level/quality, NOx ppm or the EGTs (research §2 Q3). Every SCR/DPF value
is therefore CAPTURE-FIRST: until jlr_engine_sweep has mapped the DIDs on the
real car, this module reads raw payloads for what IS known (the Old Jaguar
Group petrol-capture engine DIDs, provenance-marked) and flags the
aftertreatment set as capture pending. Nothing here turns a raw byte into a
gram of soot or a litre of AdBlue.

health()    one read-only pass: generic Mode-01 baseline FIRST (before any
            session games), the known-table modules' fault codes through the
            existing scan.py ladder, the known engine DIDs raw, and a sourced
            assessment block from research §4 (ranked AdBlue failure modes +
            expected codes) and §5 (regen thresholds: auto ~26 g soot, forced
            permissible below ~46 g, above that a physical clean).
drive_log() a timed log pairing rpm with the candidate/known DIDs, raw
            payloads with timestamps, through warm-up or a regen -- the
            timeline that identifies a signal after the sweep has surfaced
            its DID. Every-fifth-tick generic baseline, achieved sample rate
            reported.

Both stay inside the read-only envelope: 01 xx, 3E 00, 22 xx, 10 03 and the
scan.py fault ladder (19 02). Nothing is written, ever.
"""

from datetime import datetime, timezone
from time import monotonic, sleep
from typing import List, Optional

from backend.core import uds
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient
from backend.profiles.jlr.pids import (
    AFTERTREATMENT_PIDS,
    EXTERNAL_ENGINE_DIDS,
    JLR_DIDS,
)
from backend.profiles.jlr.scan import (
    JLR_ECU_TABLE,
    _probe,
    _read_dtcs,
    _read_ident,
)

ENGINE = 0x7E0

# The engine rows of the Old Jaguar Group capture (pids.JLR_DIDS) -- the only
# JLR DIDs with any provenance at all. Provenance rides on every value: they
# were captured on X351/XK PETROLS, so on the 3.0d diesel they are unverified
# until re-read. The SCR/DPF set has NO known DIDs -- see _CAPTURE_PENDING.
_OJG_ENGINE_DIDS = [(did, name) for addr, did, name, _, _ in JLR_DIDS
                    if addr == ENGINE]

# The aftertreatment candidate set: research §7 seeds RANGES (0300-05FF,
# 2100-21FF, 2200-22FF, F4xx mirrors on the PCM) but no specific ids -- those
# arrive only when jlr_engine_sweep has captured them on the real car. On top
# of that, the external-research file carries unverified candidates (most
# importantly 042C DPF soot, two JLR-diesel provenances) which the drive log
# polls RAW from day one; verified stays false until the car confirms.
_CAPTURE_PENDING = {
    "dids": [],
    "external": [
        {"did": f"{did:04X}", "name": name, "provenance": prov, "verified": False}
        for did, name, prov in EXTERNAL_ENGINE_DIDS
    ],
    "status": ("capture pending -- no public JLR DID exists for DPF soot mass, "
               "regen status, DEF level/quality, NOx ppm or EGTs. External "
               "research adds unverified candidates (042C DPF soot is the one "
               "to try first). Run jlr_engine_sweep on 7E0 over the research "
               "§7 ranges (0300-05FF, 2100-21FF, 2200-22FF, F400-F4FF), "
               "identify a signal by how it moves, then pass the found ids to "
               "jlr_adblue_drive_log."),
}

# Research §4: the AdBlue/SCR fault space on the XF 3.0d, ranked by community
# frequency. Sourced text, shown as the guide when the dash complains.
_FAULT_SPACE = [
    {"rank": 1,
     "cause": "Crystallised DEF injector -- urea crystals block the injector; the "
              "system flags fluid quality.",
     "source": "r/Jaguar 'Incorrect Diesel Exhaust Fluid Quality Detected'; "
               "3RG Stop Crystal usage videos"},
    {"rank": 2,
     "cause": "NOx sensor (upstream or downstream of the SCR) misreads -- triggers "
              "'incorrect DEF quality' with good fluid and can over-dose the SCR "
              "cat. Swap front/rear sensors to identify the bad one.",
     "source": "jaguarfuelconversions.co.uk; xeforums DEF/NOx thread"},
    {"rank": 3,
     "cause": "Reagent tank level sensor fault -- reads 0/empty with a full tank; "
              "distinct dash message.",
     "source": "jaguarforum 'AdBlue Level Reset' (2018 X260)"},
    {"rank": 4,
     "cause": "Contaminated or degraded AdBlue (watered down, screenwash, expired).",
     "source": "community consensus, research §4"},
]

# Research §4: the codes to expect in the PCM around this system. Used to
# match what the fault read actually returned -- never claimed unread.
_EXPECTED_CODES = {
    "P204F": "SCR system efficiency",
    "P249D": "reductant injection (persistent case documented after pump/NOx/"
             "temp sensor replacement)",
    "P2463": "DPF restriction -- soot accumulation",
    "P2201": "NOx sensor circuit (P2201-family)",
}

# Research §5: regen knowledge. Thresholds are community-sourced figures, NOT
# DID-derived -- the assessment says so explicitly because no soot-mass DID
# has been captured yet.
_REGEN_THRESHOLDS = {
    "autoRegenSootG": "~26",
    "forcedRegenPermissibleBelowG": "~46",
    "aboveThat": "the filter needs physical cleaning, not a forced regen",
    "regenRun": "~20 min at 500 °C+, stationary, fuel level adequate, outdoors",
    "source": "fullfatrr DPF guide; autoexplain SDD walkthrough; iCarsoft "
              "forced-regen guide; Snap-on DPF guide",
}

# Consecutive all-silent ticks before the drive log gives up (adapter or
# ignition gone -- a working car never goes quiet this long).
_DEAD_TICKS = 10

# Generic Mode-01 decoders (ISO 15031 -- public formulas, unlike the JLR DIDs).
def _decode_pid(pid: int, body: Optional[bytes]) -> Optional[float]:
    if not body or len(body) < 1:
        return None
    if pid == 0x0C and len(body) >= 2:
        return round(((body[0] << 8) | body[1]) / 4.0, 0)
    if pid == 0x05:
        return body[0] - 40
    if pid == 0x42 and len(body) >= 2:
        return round(((body[0] << 8) | body[1]) / 1000.0, 2)
    return None


# The legislated Mode-01 aftertreatment decoders -- SAE J1979DA formulas (as
# published by DashLogic), a public standard rather than anything JLR-specific,
# so decoding them is not invention. Per-car support on this ECU is unknown
# until asked: an unsupported PID answers silence and its keys simply stay out
# of that tick's sample. Byte letters are the payload after 41 <pid>.
def _s16(hi: int, lo: int) -> int:
    v = (hi << 8) | lo
    return v - 0x10000 if v >= 0x8000 else v


def _u16b(body: bytes, i: int = 0) -> Optional[int]:
    return (body[i] << 8) | body[i + 1] if len(body) > i + 1 else None


def _scaled(value: Optional[int], divisor: float) -> Optional[float]:
    return round(value / divisor, 2) if value is not None else None


def _temp10(value: Optional[int]) -> Optional[float]:
    """J1979DA word/10 - 40 (the standard temperature encoding)."""
    return round(value / 10.0 - 40, 1) if value is not None else None


def _byte_at(body: bytes, i: int, divisor: float) -> Optional[float]:
    return round(body[i] / divisor, 1) if len(body) > i else None


_AFTERTREATMENT_DECODES = {
    # pid -> [(sample key, decoder)] -- see docs/private/jlr-306dt-external-pid-research.md §a
    0x7A: [("dpf_delta_pressure_kpa", lambda b: _scaled(_s16(b[0], b[1]), 100.0))],
    0x7C: [("dpf_temp_inlet_c", lambda b: _temp10(_u16b(b, 0))),
           ("dpf_temp_outlet_c", lambda b: _temp10(_u16b(b, 2)))],
    0x83: [("nox_b1s1_ppm", lambda b: _u16b(b, 0)),
           ("nox_b1s2_ppm", lambda b: _u16b(b, 2))],
    0x85: [("reagent_tank_level_pct", lambda b: _byte_at(b, 5, 2.55))],
    0x8B: [("dpf_regen_trigger_pct", lambda b: _byte_at(b, 2, 2.55))],
    0x78: [("egt_b1s1_c", lambda b: _temp10(_u16b(b, 0)))],
    0x77: [("egr_commanded_pct", lambda b: _byte_at(b, 0, 2.55))],
}


def _read_aftertreatment(client) -> dict:
    """One pass over the legislated aftertreatment PIDs -> the decoded values
    that answered. Called on the every-fifth-tick baseline (six extra reads a
    tick would halve the BLE sample rate for numbers that move slowly)."""
    out: dict = {}
    for pid, _name, _prov in AFTERTREATMENT_PIDS:
        body = _read_pid(client, pid)
        if body is None:
            continue
        for key, decode in _AFTERTREATMENT_DECODES.get(pid, []):
            try:
                value = decode(body)
            except (IndexError, ValueError):
                value = None
            if value is not None:
                out[key] = value
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_pid(client, pid: int, timeout: float = 1.0) -> Optional[bytes]:
    """One generic Mode-01 read on the engine -> payload after 41 <pid>."""
    try:
        payload = client.request(ENGINE, bytes([0x01, pid]), timeout=timeout)
    except (ObdError, TransportError):
        return None
    if len(payload) < 3 or payload[0] != 0x41 or payload[1] != pid:
        return None
    return payload[2:]


def _read_did_raw(client, addr: int, did: int) -> Optional[bytes]:
    """One raw RDBI read -> the bytes after 62 <did>, or None."""
    try:
        payload = client.request(
            addr, bytes([0x22, did >> 8, did & 0xFF]), timeout=1.5)
    except (ObdError, TransportError):
        return None
    if len(payload) < 4 or payload[0] != 0x62:
        return None
    return payload[3:]


def _assessment(modules: List[dict]) -> dict:
    """Sourced, conditional text from research §4 + §5 -- driven by whatever
    the fault read actually returned. Never claims a DID-derived soot number
    (none is readable until the DIDs are captured)."""
    codes = [d["code"] for m in modules for d in m.get("dtcs", [])]
    matched = {c: _EXPECTED_CODES[c] for c in codes if c in _EXPECTED_CODES}
    p2201 = sorted(c for c in codes if c.startswith("P220"))

    notes = []
    if matched or p2201:
        named = ", ".join(sorted(set(matched) | set(p2201)))
        notes.append(
            f"The fault read returned aftertreatment-relevant codes ({named}). "
            "Against research §4: P204F/P249D point at the SCR/dosing side "
            "(crystallised injector or NOx sensor misread -- the ranked list below "
            "orders them), P2463 at DPF soot restriction, P2201-family at the NOx "
            "sensor circuits."
        )
    else:
        notes.append(
            "No aftertreatment code (P204F / P249D / P2463 / P2201-family) is "
            "stored in the modules a dongle reaches. If the dash still shows an "
            "AdBlue/DPF message, the fault space below is the ranked guide -- and "
            "a drive log (jlr_adblue_drive_log) through warm-up is the next read."
        )
    notes.append(
        "DPF regen thresholds (research §5, community-sourced -- NOT read from the "
        f"car): auto regen from roughly {_REGEN_THRESHOLDS['autoRegenSootG']} g "
        "soot; a forced regen is only permissible below roughly "
        f"{_REGEN_THRESHOLDS['forcedRegenPermissibleBelowG']} g; above that "
        f"{_REGEN_THRESHOLDS['aboveThat']}. The harness cannot yet measure soot "
        "mass on this car -- no DID captured -- so permissibility is judged by the "
        "tool that reads it (SDD/IIDTool), not invented here. Forced regen stays "
        "tool-only: there is no public JLR routine-control id, and this harness "
        "would not send one."
    )
    return {
        "matchedCodes": matched,
        "faultSpace": _FAULT_SPACE,
        "expectedCodes": dict(_EXPECTED_CODES),
        "regenThresholds": dict(_REGEN_THRESHOLDS),
        "notes": notes,
    }


def _default_drive_dids() -> List[str]:
    """The OJG engine set plus the external research candidates (042C DPF soot
    first among them); sweep-captured candidates join by passing ``dids``
    explicitly."""
    dids = [f"{did:04X}" for did, _ in _OJG_ENGINE_DIDS]
    dids += [f"{did:04X}" for did, _, _ in EXTERNAL_ENGINE_DIDS]
    return dids


def _panel(baseline: Optional[dict], modules: List[dict],
           values: List[dict]) -> dict:
    """The health pass as generic panel data: `readings` chips + a `worksheet`
    copy-out block -- the shapes the desktop extras panel renders for any
    brand action (same pattern as discover.py / sweep.py)."""
    readings = []
    if baseline:
        for key, label, unit in (
            ("engine_rpm", "Engine rpm (Mode 01)", "rpm"),
            ("coolant_c", "Coolant", "°C"),
            ("module_voltage", "Module voltage", "V"),
        ):
            if baseline.get(key) is not None:
                readings.append({"name": label, "value": str(baseline[key]),
                                 "unit": unit, "description": ""})
    for mod in modules:
        faults = len(mod.get("dtcs", []))
        state = ("present" if mod.get("present") else "no answer") + (
            f" · {faults} fault{'s' if faults != 1 else ''} stored"
            if mod.get("present") else "")
        readings.append({"name": f"{mod['code']} {mod['address']}", "value": state,
                         "unit": "", "description": mod.get("name", "")})
    for value in values:
        readings.append({
            "name": f"DID {value['did']} · {value['name']}",
            "value": value["payload"], "unit": "raw",
            "description": value.get("provenance", ""),
        })

    lines = ["JLR AdBlue/DPF health pass"]
    for mod in modules:
        lines.append(f"{mod['code']} {mod['address']}: "
                     + ("present" if mod.get("present") else "no answer"))
        for dtc in mod.get("dtcs", []):
            lines.append(f"  {dtc.get('code')} {dtc.get('status', '')}".rstrip())
    for value in values:
        lines.append(f"DID {value['did']} {value['name']}: {value['payload']} "
                     f"[{value.get('provenance', '')}]")
    return {"readings": readings, "worksheet": "\n".join(lines)}


def health(adapter: Optional[dict], mode: str = "real") -> dict:
    """One read-only AdBlue/SCR + DPF health pass on the X260 diesel.

    Order matters: the generic Mode-01 baseline (rpm, coolant, module voltage)
    is taken FIRST, before any session games; then the known-table modules get
    the existing scan.py fault ladder; then the known engine DIDs are read RAW
    with their provenance marked, and the SCR/DPF set is flagged capture
    pending. The assessment block is sourced text from the research doc,
    conditional on the codes actually read.
    """
    if mode == "sim":
        modules = [
            {"address": "0x7E0", "response": "0x7E8", "code": "PCM",
             "name": "Engine control (ECM/PCM)", "present": True,
             "ident": [{"label": "VIN", "value": "SIMULATEDX260DIE"}],
             "dtcs": [
                 {"code": "P249D", "hex": "249D00", "failureType": "00",
                  "status": "2F", "flags": ["test failed", "confirmed"]},
                 {"code": "P2463", "hex": "246300", "failureType": "00",
                  "status": "28", "flags": ["confirmed"]},
             ],
             "error": None, "trace": []},
            {"address": "0x7E1", "response": "0x7E9", "code": "TCM",
             "name": "Transmission (ZF 6HP/8HP)", "present": True,
             "ident": [], "dtcs": [], "error": None, "trace": []},
            {"address": "0x795", "response": "0x79D", "code": "RDCM",
             "name": "Rear differential (e-diff)", "present": False,
             "ident": [], "dtcs": [], "error": None, "trace": []},
        ]
        values = [
            {"did": "033E", "name": "Boost / manifold pressure", "payload": "0064",
             "provenance": "old-jaguar-group (petrol capture -- unverified on diesel)"},
            {"did": "03F3", "name": "Engine oil temperature", "payload": "58",
             "provenance": "old-jaguar-group (petrol capture -- unverified on diesel)"},
            {"did": "0520", "name": "Charge air temp (post-intercooler)",
             "payload": "2A",
             "provenance": "old-jaguar-group (petrol capture -- unverified on diesel)"},
        ]
        return {
            "ok": True, "simulated": True,
            "vehicle": {"vin": "SIMULATEDX260DIE",
                        "protocol": "ISO 15765-4 (CAN 11/500)",
                        "adapter": "Simulated"},
            "baseline": {"engine_rpm": 820.0, "coolant_c": 74.0,
                         "module_voltage": 14.1},
            "modules": modules, "values": values,
            "candidates": dict(_CAPTURE_PENDING),
            "assessment": _assessment(modules),
            **_panel({"engine_rpm": 820.0, "coolant_c": 74.0, "module_voltage": 14.1},
                     modules, values),
            "message": "SIMULATED -- no real car connected. Shape of a real pass: "
                       "Mode-01 baseline, known-table faults via the scan ladder, "
                       "known engine DIDs raw with provenance, SCR/DPF set flagged "
                       "capture pending, sourced assessment below.",
            "timestamp": _now(),
        }

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "vehicle": None,
                "baseline": None, "modules": [], "values": [],
                "candidates": dict(_CAPTURE_PENDING), "assessment": None,
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, "
                    "not the 11-bit/500k CAN JLR diagnostics use."
                ),
                "timestamp": _now(),
            }

        obd_vin = obd_service._read_vin(session)

        client = Elm327UdsClient(session)
        client.begin()
        baseline: Optional[dict] = None
        modules: List[dict] = []
        values: List[dict] = []
        uds_opened: set = set()
        kwp_opened: set = set()
        try:
            # 1. Generic Mode-01 baseline FIRST -- before any session games.
            baseline = {}
            for pid, key in ((0x0C, "engine_rpm"), (0x05, "coolant_c"),
                             (0x42, "module_voltage")):
                body = _read_pid(client, pid)
                baseline[key] = _decode_pid(pid, body)

            # 2. Known-table modules through the existing scan.py ladder.
            for addr, code, name, desc in JLR_ECU_TABLE:
                trace: List[str] = []
                mod = {
                    "address": f"0x{addr:03X}", "response": f"0x{addr + 8:03X}",
                    "code": code, "name": name, "description": desc,
                    "present": False, "ident": [], "dtcs": [], "error": None,
                    "trace": trace,
                }
                if not _probe(client, addr, trace):
                    modules.append(mod)
                    continue
                mod["present"] = True
                try:
                    mod["ident"] = _read_ident(client, addr, kwp_opened, trace)
                    mod["dtcs"] = _read_dtcs(client, addr, uds_opened, kwp_opened,
                                             trace)
                except (uds.UdsNegativeResponse, ObdError, TransportError) as exc:
                    mod["error"] = str(exc)
                modules.append(mod)

            # 3. Known engine DIDs, RAW, provenance marked -- then the external
            #    research candidates the same way, so the first health pass on
            #    the car already answers "does 042C speak on a 3.0 V6?".
            for did, name in _OJG_ENGINE_DIDS:
                body = _read_did_raw(client, ENGINE, did)
                if body is None:
                    continue
                values.append({
                    "did": f"{did:04X}", "name": name,
                    "payload": body.hex().upper(),
                    "provenance": "old-jaguar-group (petrol capture -- unverified "
                                  "on diesel)",
                })
            for did, name, prov in EXTERNAL_ENGINE_DIDS:
                body = _read_did_raw(client, ENGINE, did)
                if body is None:
                    continue
                values.append({
                    "did": f"{did:04X}", "name": name,
                    "payload": body.hex().upper(),
                    "provenance": f"external research, unverified -- {prov}",
                })
        finally:
            client.restore()

        vins = [(m["code"], item["value"][:17]) for m in modules
                for item in m["ident"]
                if item["label"] == "VIN" and len(item["value"]) >= 11]
        vin = (vins[0][1] if vins else None) or obd_vin
        faults = sum(len(m["dtcs"]) for m in modules if m["present"])
        present = sum(1 for m in modules if m["present"])

        return {
            "ok": True, "simulated": False,
            "vehicle": {"vin": vin,
                        "protocol": session.protocol or "ISO 15765-4 (CAN 11/500)",
                        "adapter": (adapter or {}).get("name")},
            "baseline": baseline,
            "modules": modules, "values": values,
            "candidates": dict(_CAPTURE_PENDING),
            "assessment": _assessment(modules),
            **_panel(baseline, modules, values),
            "message": (
                (f"VIN {vin}. " if vin else "")
                + f"{present} known-table module(s) answered, {faults} fault"
                f"{'s' if faults != 1 else ''} stored, {len(values)} known engine "
                "DID(s) answered raw. SCR/DPF values: capture pending (no public "
                "DID -- run jlr_engine_sweep). Read-only pass; nothing was changed "
                "on the car."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {
            "ok": False, "simulated": False, "vehicle": None, "baseline": None,
            "modules": [], "values": [], "candidates": dict(_CAPTURE_PENDING),
            "assessment": None, "message": str(exc), "timestamp": _now(),
        }


def drive_log(adapter: Optional[dict], mode: str = "real", seconds: float = 300.0,
              dids: Optional[List[str]] = None) -> dict:
    """Timed log pairing engine rpm with the candidate/known DIDs, raw.

    Run it through warm-up or a regen (a regen is ~20 min at 500 °C+ per
    research §5, so a 5-minute default only spans warm-up -- pass more
    seconds to cover one). Every tick reads rpm plus each DID's RAW payload
    with a timestamp; every fifth tick adds coolant and module voltage.
    Payloads are raw on purpose: a signal is identified by how it moves
    against rpm, then decoded against the car -- never guessed here.

    dids: hex strings. None uses the known OJG engine set plus the external
        research candidates (042C soot first); sweep-captured candidates
        replace the set when passed explicitly.
    """
    if mode == "sim":
        # Fake warm-up: rpm with raw DID payloads beside it, coolant + voltage
        # on the every-fifth-tick baseline, a non-answer recorded as a gap.
        samples = [
            {"t": 0.0, "engine_rpm": 810.0, "did_033E": "0064", "did_03E6": "34",
             "did_03F2": "44", "did_03F3": "3C", "did_0520": "1E", "did_042C": "1F40",
             "coolant_c": 22.0, "module_voltage": 12.2,
             "dpf_delta_pressure_kpa": 0.15, "nox_b1s1_ppm": 85,
             "reagent_tank_level_pct": 62.4, "egt_b1s1_c": 118.0},
            {"t": 4.2, "engine_rpm": 1750.0, "did_033E": "00C4", "did_03E6": "35",
             "did_03F2": "44", "did_03F3": "42", "did_0520": "28", "did_042C": "2AF8"},
            {"t": 8.1, "engine_rpm": 1980.0, "did_033E": "00E1", "did_03E6": "35",
             "did_03F2": "45", "did_03F3": "4B", "did_0520": "2C", "did_042C": "30D4"},
            {"t": 12.4, "engine_rpm": 690.0, "did_033E": "005A", "did_03E6": None,
             "did_03F2": "45", "did_03F3": "50", "did_0520": "30", "did_042C": "2134"},
        ]
        return {
            "ok": True, "simulated": True, "samples": samples,
            "dids": dids if dids else _default_drive_dids(), "sampleRate": 0.3,
            "message": "SIMULATED -- no real car connected. Fake warm-up shape: "
                       "rpm with raw DID payloads beside it (042C soot candidate "
                       "included), coolant + voltage plus the decoded legislated "
                       "aftertreatment PIDs on the every-fifth-tick baseline.",
            "timestamp": _now(),
        }

    if dids is None:
        did_list = [int(d, 16) for d in _default_drive_dids()]
        did_src = _default_drive_dids()
    else:
        try:
            did_list = [int(d, 16) & 0xFFFF for d in dids]
            did_src = [f"{d:04X}" for d in did_list]
        except (TypeError, ValueError):
            return {"ok": False, "simulated": False, "samples": [], "dids": [],
                    "sampleRate": 0.0,
                    "message": "dids must be hex strings like ['2104', 'F40C'].",
                    "timestamp": _now()}

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "samples": [], "dids": did_src,
                "sampleRate": 0.0,
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, "
                    "not the 11-bit/500k CAN the JLR drive log needs."
                ),
                "timestamp": _now(),
            }

        client = Elm327UdsClient(session)
        client.begin()
        samples: List[dict] = []
        started = monotonic()
        dead = 0
        tick = 0
        try:
            while monotonic() - started < seconds:
                sample = {"t": round(monotonic() - started, 3)}
                alive = False
                body = _read_pid(client, 0x0C, timeout=0.8)
                sample["engine_rpm"] = _decode_pid(0x0C, body)
                alive = alive or body is not None
                for did in did_list:
                    body = _read_did_raw(client, ENGINE, did)
                    sample[f"did_{did:04X}"] = body.hex().upper() if body else None
                    alive = alive or body is not None
                if tick % 5 == 0:
                    for pid, key in ((0x05, "coolant_c"), (0x42, "module_voltage")):
                        body = _read_pid(client, pid, timeout=0.8)
                        sample[key] = _decode_pid(pid, body)
                        alive = alive or body is not None
                    # The legislated aftertreatment PIDs ride the same
                    # every-fifth-tick baseline: decoded (public J1979DA
                    # formulas) rather than raw, but only the ones that answer.
                    sample.update(_read_aftertreatment(client))
                tick += 1
                samples.append(sample)
                if alive:
                    dead = 0
                else:
                    dead += 1
                    if dead >= _DEAD_TICKS:
                        break
                    sleep(0.2)
        finally:
            client.restore()

        elapsed = monotonic() - started
        rate = len(samples) / elapsed if elapsed else 0.0
        rpms = [s["engine_rpm"] for s in samples if s.get("engine_rpm")]
        died = dead >= _DEAD_TICKS
        return {
            "ok": True, "simulated": False, "samples": samples, "dids": did_src,
            "sampleRate": round(rate, 2),
            "message": (
                f"Drive log: {len(samples)} tick(s) over {elapsed:.0f}s (~{rate:.2f}/s) "
                f"across {len(did_list)} DID(s)."
                + (f" Rpm spanned {min(rpms):.0f}-{max(rpms):.0f}." if rpms
                   else " No rpm was recorded -- check the dongle stayed awake.")
                + (" WARNING: the log ended early -- the bus went quiet for "
                   f"{_DEAD_TICKS} consecutive ticks (adapter or ignition dropped)."
                   if died else "")
                + " Payloads are raw; a regen needs ~20 min at 500 °C+ (research "
                  "§5) -- pass seconds=1500+ to span one. Read-only throughout."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "samples": [], "dids": [],
                "sampleRate": 0.0, "message": str(exc), "timestamp": _now()}
