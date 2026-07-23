"""Jaguar / Land Rover module scan over a generic OBD-II dongle.

Generic OBD-II (backend/obd/service.py) only sees the engine's emissions data.
This walks the manufacturer-level modules a JLR car exposes over a plain dongle,
scoped honestly to the research (docs/private/jlr-research-architecture.md +
jlr-research-modules.md):

TARGET ERA -- the 2006-2016 Ford/PLA CAN cars (Jaguar XF X250, XK X150, XJ X351,
  F-Type; Land Rover Disco 3/4, RR Sport L320, Freelander 2, Ford-era L322,
  pre-17MY L405/L494). JLR modules answer on 11-bit CAN at 500k, response =
  request + 8 (VERIFIED on-car: the X351/XK Torque PID pack drives 7E0/7E1/795
  with ATSP6). The publicly-verified module map is the DRIVELINE: engine 0x7E0,
  ZF automatic 0x7E1, rear e-diff 0x795. ABS, air suspension, EPB and the rest
  exist on HS-CAN but their request ids were never published (research: UNKNOWN,
  probe-sweep on a real car) -- they are NOT invented here.

THE TWO WALLS (never presented as faults):
  * MS-CAN -- body/comfort modules (BCM, cluster, HVAC, doors) sit on a ~125k
    MS-CAN that is NOT wired to OBD pins 6/14; physically unreachable bare
    (the Peugeot pin lesson, JLR edition).
  * DoIP -- MY17+ new-architecture cars (Disco 5 L462, Velar L560, 2018+
    XE/XF/F-Pace, Defender L663, I-Pace, L460/L461, 17MY+ L405/L494) moved
    manufacturer diagnostics to DoIP over Ethernet, which no ELM327 can speak.
    Evoque L538 / Disco Sport L550 gateway-filter the OBD port to a similar
    effect. All of those cars still answer generic OBD -- the scan detects the
    pattern (VIN/0100 answers, no module depth) and says which wall it hit.

DIALECT -- ~2010+ modules speak UDS (22/19 02/14 FF FF FF); the older Ford-era
  cars may speak KWP2000-over-CAN (1A ident, 18 02 FF FF read, 14 FF 00 clear).
  Each module ladders UDS first then falls back to the KWP dialect, exactly like
  the Mercedes profile (KWP rungs are standard KWP2000, UNVERIFIED-plausible for
  JLR -- not captured on a JLR car).

Nothing security-related is ever addressed: RFA/KVM/immobiliser modules are
HARD-REFUSE in the research (name-only, no ids, no commands) and have no entry
here at all. Read-only apart from clear(), which the API layer confirm-gates.
"""

import re
from datetime import datetime, timezone
from typing import List, Optional

from backend.core import uds
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

# The publicly-verified JLR module table over a plain dongle (research §10a).
# Response = request + 8 for every row (795 -> 79D), the Elm327UdsClient default.
# No body-module row -- those sit on the unreachable MS-CAN; no ABS/EAS row --
# their ids were never published and are not guessed.
JLR_ECU_TABLE = [
    (0x7E0, "PCM", "Engine control (ECM/PCM)",
     "Engine / injection (petrol or diesel)"),
    (0x7E1, "TCM", "Transmission (ZF 6HP/8HP)",
     "ZF automatic gearbox (if fitted)"),
    (0x795, "RDCM", "Rear differential (e-diff)",
     "Active rear differential (X351/XK/F-Type and others, if fitted)"),
]

ENGINE = 0x7E0

# Standard UDS identification DIDs (research §4). Modules support different
# subsets; read best-effort and keep whatever answers as text.
IDENT_DIDS = [
    (0xF190, "VIN"),
    (0xF187, "Part number"),
    (0xF18C, "ECU serial"),
    (0xF188, "Software"),
    (0xF191, "Hardware"),
]

# KWP2000 identification records (service 0x1A) read when every UDS DID refuses
# (an older Ford-era module). UNVERIFIED-plausible for JLR, same as Mercedes.
KWP_IDENT_RECORDS = [
    (0x87, "Part number (KWP 1A 87)"),
    (0x88, "Software (KWP 1A 88)"),
    (0x86, "ECU ident (KWP 1A 86)"),
]

# UDS NRCs that mean "I might answer in a different session" -- open the UDS
# extended session (10 03) and retry once, identical to the VW/Mercedes ladder.
_SESSION_NRCS = (0x11, 0x22, 0x7E, 0x7F)

# reportDTCByStatusMask masks, laddered. No public "JLR mask" (research §5):
# 0xFF = all, 0x08 = confirmed, 0xAF = broad tool mask. A bad mask returns NRC
# 0x31/0x12 -- move to the next mask on those.
_DTC_MASKS = (0xFF, 0x08, 0xAF)
_MASK_RETRY_NRCS = (0x12, 0x31)

_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nrc_text(exc: uds.UdsNegativeResponse) -> str:
    return f"NRC {exc.nrc:02X} ({uds.NRC_NAMES.get(exc.nrc, 'unknown')})"


def _printable(body: bytes) -> str:
    text = "".join(chr(b) for b in body if 32 <= b < 127).strip()
    if text and len(text) >= (len(body) + 1) // 2:
        return text
    return body.hex().upper()


def _probe(client, req_id: int, trace: List[str]) -> bool:
    """Presence ladder: 3E 00, then a VIN ident read -- some ECUs never answer a
    naked TesterPresent but do answer 22 F190. Any reply, positive or negative,
    proves an ECU lives at the address."""
    for data in (bytes([0x3E, 0x00]), bytes([0x22, 0xF1, 0x90])):
        try:
            reply = client.request(req_id, data, timeout=1.5)
        except (ObdError, TransportError):
            continue
        if reply:
            return True
    trace.append("3E 00 / 22 F190 -> silence")
    return False


def _open_extended(client, req_id: int, opened: set, trace: List[str]) -> bool:
    """Open a UDS extended session (10 03). Returns True on a positive 0x50."""
    if req_id in opened:
        return True
    try:
        reply = client.request(req_id, bytes([0x10, 0x03]), timeout=2.0)
    except (ObdError, TransportError):
        trace.append("10 03 -> no answer")
        return False
    if reply and reply[0] == 0x50:
        trace.append("10 03 -> extended session")
        opened.add(req_id)
        return True
    if reply and reply[0] == 0x7F and len(reply) >= 3:
        trace.append(f"10 03 -> NRC {reply[2]:02X}")
    return False


def _open_kwp(client, req_id: int, opened: set, trace: List[str]) -> bool:
    """Bring an older KWP2000-over-CAN module up: plain KWP start 10 81
    (UNVERIFIED-plausible for JLR -- standard KWP2000, not JLR-captured)."""
    if req_id in opened:
        return True
    try:
        reply = client.request(req_id, bytes([0x10, 0x81]), timeout=2.0)
    except (ObdError, TransportError):
        trace.append("10 81 -> no answer")
        return False
    if reply and reply[0] == 0x50:
        trace.append("10 81 -> KWP session")
        opened.add(req_id)
        return True
    if reply and reply[0] == 0x7F and len(reply) >= 3:
        trace.append(f"10 81 -> NRC {reply[2]:02X}")
    return False


def _read_ident(client, req_id: int, kwp_opened: set, trace: List[str]) -> List[dict]:
    """Best-effort module identification: UDS RDBI (0x22) first; if every DID
    refuses, the classic KWP 1A records an older Ford-era module answers."""
    out: List[dict] = []
    refused = 0
    for did, label in IDENT_DIDS:
        try:
            payload = client.request(req_id, bytes([0x22, did >> 8, did & 0xFF]), timeout=2.0)
        except (ObdError, TransportError):
            refused += 1
            if did == 0xF190:
                trace.append("22 F190 -> no answer")
            continue
        if len(payload) < 4 or payload[0] != 0x62:
            refused += 1
            if did == 0xF190 and payload and payload[0] == 0x7F and len(payload) >= 3:
                trace.append(f"22 F190 -> NRC {payload[2]:02X}")
            continue
        value = _printable(payload[3:])
        if value:
            out.append({"label": label, "value": value})
    if not out and refused == len(IDENT_DIDS) and _open_kwp(client, req_id, kwp_opened, trace):
        for rec, label in KWP_IDENT_RECORDS:
            try:
                payload = client.request(req_id, bytes([0x1A, rec]), timeout=2.0)
            except (ObdError, TransportError):
                continue
            if len(payload) < 3 or payload[0] != 0x5A:
                continue
            value = _printable(payload[2:])
            if value:
                out.append({"label": label, "value": value})
                trace.append(f"1A {rec:02X} -> OK")
                found = _VIN_RE.search(value.upper())
                if found:
                    out.append({"label": "VIN", "value": found.group(0)})
    return out


def _read_dtcs_uds(client, req_id: int, trace: List[str]) -> List[dict]:
    """One pass of the 19 02 mask ladder (0xFF -> 0x08 -> 0xAF). Raises on a
    session-family NRC so the caller can escalate to 10 03 and retry."""
    last_error = None
    payload = None
    for mask in _DTC_MASKS:
        try:
            reply = client.request(req_id, bytes([0x19, 0x02, mask]), timeout=3.0)
        except (ObdError, TransportError) as exc:
            trace.append(f"19 02 {mask:02X} -> no answer")
            raise exc
        if reply and reply[0] == 0x59:
            trace.append(f"19 02 {mask:02X} -> OK")
            payload = reply
            break
        if reply and reply[0] == 0x7F and len(reply) >= 3:
            last_error = uds.UdsNegativeResponse(0x19, reply[2])
            if reply[2] in _MASK_RETRY_NRCS:
                continue  # bad/unsupported mask -- try the next one
            trace.append(f"19 02 {mask:02X} -> {_nrc_text(last_error)}")
            raise last_error
    if payload is None:
        if last_error is not None:
            trace.append(f"19 02 -> {_nrc_text(last_error)}")
            raise last_error
        raise ObdError("Fault read failed.", kind="no_data")

    # Reply: 0x59 0x02 <availabilityMask> then 4-byte records [dtc x3][status].
    records = payload[3:]
    dtcs = []
    for i in range(0, len(records) - 3, 4):
        b0, b1, b2, status = records[i : i + 4]
        if b0 == b1 == b2 == 0:
            continue
        entry = uds.decode_dtc(b0, b1, b2)
        entry["status"] = f"{status:02X}"
        entry["flags"] = uds.status_flags(status)
        dtcs.append(entry)
    return dtcs


def _read_dtcs_kwp(client, req_id: int, kwp_opened: set, trace: List[str]) -> List[dict]:
    """KWP2000 readDTCByStatus: 18 02 FF FF -> 58 <count> then (hi, lo, status)
    triples. The fallback an older Ford-era JLR module answers (UNVERIFIED-
    plausible, standard KWP2000 -- adapted from the Mercedes/Peugeot decode)."""
    _open_kwp(client, req_id, kwp_opened, trace)
    try:
        payload = client.request(req_id, bytes([0x18, 0x02, 0xFF, 0xFF]), timeout=3.0)
    except (ObdError, TransportError) as exc:
        trace.append("18 02 FF FF -> no answer")
        raise exc
    if not payload or payload[0] != 0x58:
        if payload and payload[0] == 0x7F and len(payload) >= 3:
            err = uds.UdsNegativeResponse(0x18, payload[2])
            trace.append(f"18 02 FF FF -> {_nrc_text(err)}")
            raise err
        raise ObdError("KWP fault read failed.", kind="no_data")
    trace.append("18 02 FF FF -> OK (KWP)")
    # 0x58 <count> <triples> is the book format; some ECUs skip the count.
    data = payload
    offset = 2 if len(data) >= 2 and (len(data) - 2) % 3 == 0 else 1
    dtcs = []
    for i in range(offset, len(data) - 2, 3):
        hi, lo, status = data[i], data[i + 1], data[i + 2]
        if hi == 0 and lo == 0:
            continue
        entry = uds.decode_dtc(hi, lo, 0x00)
        entry["hex"] = f"{hi:02X}{lo:02X}"
        entry["status"] = f"{status:02X}"
        entry["flags"] = ["stored (KWP)"]
        dtcs.append(entry)
    return dtcs


def _read_dtcs(client, req_id: int, uds_opened: set, kwp_opened: set,
               trace: List[str]) -> List[dict]:
    """Fault memory, laddered: UDS 19 02 first; on a session-conditions NRC open
    the UDS extended session (10 03) once and retry; if the module doesn't speak
    UDS DTCs at all, fall back to the KWP fault read (18 02 FF FF)."""
    try:
        return _read_dtcs_uds(client, req_id, trace)
    except uds.UdsNegativeResponse as exc:
        if exc.nrc in _SESSION_NRCS and _open_extended(client, req_id, uds_opened, trace):
            try:
                return _read_dtcs_uds(client, req_id, trace)
            except uds.UdsNegativeResponse as exc2:
                trace.append(f"19 02 after session -> {_nrc_text(exc2)}")
        return _read_dtcs_kwp(client, req_id, kwp_opened, trace)
    except (ObdError, TransportError):
        return _read_dtcs_kwp(client, req_id, kwp_opened, trace)


def _engine_answers_standard(client, trace: List[str]) -> bool:
    """Mode 01 PID 00 -- the one request an emissions ECU must answer with the
    ignition on, even on a DoIP-era car whose manufacturer side stays quiet."""
    try:
        reply = client.request(ENGINE, bytes([0x01, 0x00]), timeout=2.0)
    except (ObdError, TransportError):
        return False
    if reply:
        trace.append("answered on standard OBD 7E0 (01 00)")
        return True
    return False


def _obd_mode03(session, trace: List[str]) -> List[dict]:
    """Generic OBD Mode 03 stored codes as module DTC rows (engine fallback)."""
    from backend.obd import dtc as obd_dtc
    codes = obd_dtc.read_codes(session, 0x03)
    trace.append("fell back to OBD Mode 03")
    return [
        {"code": c, "hex": "", "failureType": "", "status": "", "flags": ["confirmed"]}
        for c in codes
    ]


def scan(adapter: Optional[dict], mode: str = "real") -> dict:
    """Walk the JLR car's reachable driveline modules and read each one's fault
    memory. ``adapter`` is a saved ELM327 adapter-instance dict (None for sim).
    Read-only."""
    if mode == "sim":
        return _simulated_scan()

    def run(session, entry):
        session.ensure_protocol()  # ObdError('link') here -> caught below
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False,
                "vehicle": {"vin": None, "protocol": session.protocol,
                            "adapter": (adapter or {}).get("name")},
                "modules": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k CAN JLR module diagnostics use. If this is the Jaguar / "
                    "Land Rover, turn the ignition ON and retry. Early S-Type/X-Type are "
                    "J1850 and the 2002-05 BMW-era L322 often won't talk to a CAN dongle."
                ),
                "timestamp": _now(),
            }

        # The car's identity first, while the receive filter is still open:
        # Mode 09 VIN works on any ~2001+ car regardless of what UDS/KWP does next.
        obd_vin = obd_service._read_vin(session)

        modules: List[dict] = []
        mfr_present = 0     # modules that answered manufacturer-level services
        engine_on_obd = False
        uds_opened: set = set()
        kwp_opened: set = set()

        client = Elm327UdsClient(session)
        client.begin()
        try:
            for addr, code, name, desc in JLR_ECU_TABLE:
                trace: List[str] = []
                mod = {
                    "address": f"0x{addr:03X}", "response": f"0x{addr + 8:03X}",
                    "code": code, "name": name, "description": desc,
                    "present": False, "ident": [], "dtcs": [], "error": None,
                    "trace": trace,
                }
                via_standard_obd = False
                present = _probe(client, addr, trace)
                if not present and addr == ENGINE:
                    present = _engine_answers_standard(client, trace)
                    via_standard_obd = present
                    engine_on_obd = engine_on_obd or present
                if not present:
                    modules.append(mod)
                    continue
                mod["present"] = True
                if not via_standard_obd:
                    mfr_present += 1
                try:
                    mod["ident"] = _read_ident(client, addr, kwp_opened, trace)
                    mod["dtcs"] = _read_dtcs(client, addr, uds_opened, kwp_opened, trace)
                except (uds.UdsNegativeResponse, ObdError, TransportError) as exc:
                    if addr == ENGINE:
                        # Engine on the plain OBD channel may not speak UDS/KWP
                        # fault services -- generic Mode 03 codes still do.
                        try:
                            mod["dtcs"] = _obd_mode03(session, trace)
                        except Exception:
                            mod["error"] = str(exc)
                    else:
                        mod["error"] = str(exc)
                modules.append(mod)
        finally:
            client.restore()

        present = [m for m in modules if m["present"]]
        faults = sum(len(m["dtcs"]) for m in present)

        # Dealer check: every module that knows its VIN should agree. A module
        # reporting a different VIN was swapped in from another car.
        vins = [(m["code"], item["value"][:17]) for m in modules
                for item in m["ident"]
                if item["label"] == "VIN" and len(item["value"]) >= 11]
        vin_warning = ""
        if len({v for _, v in vins}) > 1:
            vin_warning = (
                " WARNING - VIN MISMATCH: "
                + ", ".join(f"{code}={v}" for code, v in vins)
                + " -- a control unit from another car is fitted (crash repair or "
                "clocking risk)."
            )
        vin = (vins[0][1] if vins else None) or obd_vin

        if mfr_present:
            era_note = (
                " CAN-era JLR (2006-2016 Ford/PLA family). This driveline read is the honest "
                "dongle ceiling: ABS, air suspension and EPB exist on the same bus but their "
                "module addresses were never published, and body modules (BCM, cluster, HVAC, "
                "doors) sit on an MS-CAN that isn't wired to the OBD pins the dongle uses -- "
                "not faults, just out of reach. Full-depth work needs a GAP IIDTool or SDD."
            )
        elif engine_on_obd or vin:
            era_note = (
                " The car answers generic OBD but no module answered JLR diagnostics. That is "
                "the signature of a MY17+ DoIP car (Disco 5, Velar, 2018+ XE/XF/F-Pace, "
                "Defender L663, I-Pace, late L405/L494 -- manufacturer diagnostics moved to "
                "Ethernet, which no OBD dongle can speak) or of the gateway-filtered Evoque "
                "L538 / Discovery Sport L550. Generic-OBD-only territory, not a fault. The "
                "Service tab's dash-procedure resets still work on these cars."
            )
        else:
            era_note = (
                " No JLR module and no engine answered. If this is the Jaguar / Land Rover, "
                "turn the ignition ON and retry. Early S-Type/X-Type (J1850) and the 2002-05 "
                "BMW-era L322 can't be diagnosed on this dongle."
            )

        return {
            "ok": True, "simulated": False,
            "vehicle": {"vin": vin, "protocol": session.protocol or "ISO 15765-4 (CAN 11/500)",
                        "adapter": (adapter or {}).get("name")},
            "modules": modules,
            "message": (
                (f"VIN {vin}. " if vin else "")
                + f"{len(present)} module(s) answered, {faults} fault"
                f"{'s' if faults != 1 else ''} stored.{vin_warning}{era_note} "
                "Read-only scan -- nothing was changed on the car."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {
            "ok": False, "simulated": False, "vehicle": None, "modules": [],
            "message": str(exc), "timestamp": _now(),
        }


def clear(adapter: Optional[dict], mode: str = "real", address: Optional[str] = None) -> dict:
    """Clear fault memory on one JLR module (address like "0x7E0") or all present.

    Ladder matches the read side: UDS ClearDiagnosticInformation (14 FF FF FF),
    then the KWP clear (14 FF 00) an older Ford-era module wants.
    """
    if mode == "sim":
        return {
            "ok": True, "simulated": True, "cleared": [address or "all present"],
            "message": "SIMULATED -- nothing was cleared on any real car.",
            "timestamp": _now(),
        }

    wanted: Optional[int] = None
    if address:
        try:
            wanted = int(address, 16)
        except ValueError:
            return {
                "ok": False, "simulated": False, "cleared": [],
                "message": f"Bad module address '{address}' (expected hex like 0x7E0).",
                "timestamp": _now(),
            }

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "cleared": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k CAN needed to clear JLR module faults."
                ),
                "timestamp": _now(),
            }

        cleared, errors = [], []
        kwp_opened: set = set()
        client = Elm327UdsClient(session)
        client.begin()
        try:
            if wanted is not None:
                targets = [wanted]
            else:
                targets = [addr for addr, _, _, _ in JLR_ECU_TABLE
                           if _probe(client, addr, [])]
            for addr in targets:
                try:
                    uds.clear_dtcs(client, addr)
                    cleared.append(f"0x{addr:03X}")
                except Exception:
                    try:
                        _open_kwp(client, addr, kwp_opened, [])
                        reply = client.request(addr, bytes([0x14, 0xFF, 0x00]), timeout=4.0)
                        if reply and reply[0] == 0x54:
                            cleared.append(f"0x{addr:03X} (KWP)")
                        else:
                            errors.append(f"0x{addr:03X}: no positive clear reply")
                    except Exception as exc:
                        errors.append(f"0x{addr:03X}: {exc}")
        finally:
            client.restore()

        ok = bool(cleared) and not errors
        message = f"Cleared fault memory on {', '.join(cleared) or 'no modules'}."
        if errors:
            message += " Failed: " + "; ".join(errors)
        return {
            "ok": ok, "simulated": False, "cleared": cleared,
            "message": message, "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {
            "ok": False, "simulated": False, "cleared": [],
            "message": str(exc), "timestamp": _now(),
        }


def _simulated_scan() -> dict:
    def mod(addr, resp, code, name, desc, present, ident=None, dtcs=None):
        return {
            "address": addr, "response": resp, "code": code, "name": name,
            "description": desc, "present": present,
            "ident": ident or [], "dtcs": dtcs or [], "error": None, "trace": [],
        }

    modules = [
        mod("0x7E0", "0x7E8", "PCM", "Engine control (ECM/PCM)",
            "Engine / injection (petrol or diesel)",
            True, ident=[{"label": "VIN", "value": "SIMULATEDDISCO401"},
                         {"label": "Part number", "value": "AH22-12A650-BC"}],
            dtcs=[
                {"code": "P0234", "hex": "023400", "failureType": "00",
                 "status": "2F", "flags": ["test failed", "confirmed"]},
            ]),
        mod("0x7E1", "0x7E9", "TCM", "Transmission (ZF 6HP/8HP)",
            "ZF automatic gearbox (if fitted)", True,
            ident=[{"label": "VIN", "value": "SIMULATEDDISCO401"}]),
        mod("0x795", "0x79D", "RDCM", "Rear differential (e-diff)",
            "Active rear differential (X351/XK/F-Type and others, if fitted)", False),
    ]
    return {
        "ok": True, "simulated": True,
        "vehicle": {"vin": "SIMULATEDDISCO401", "protocol": "ISO 15765-4 (CAN 11/500)",
                    "adapter": "Simulated"},
        "modules": modules,
        "message": "SIMULATED -- VIN SIMULATEDDISCO401. 2 modules answered, 1 fault stored. "
                   "CAN-era JLR driveline read (engine + ZF gearbox; no e-diff fitted). "
                   "No real car connected.",
        "timestamp": _now(),
    }
