"""Porsche module scan over a generic OBD-II dongle.

Generic OBD-II (backend/obd/service.py) only sees the engine's emissions data.
This walks the manufacturer-level modules a Porsche exposes over a plain dongle,
scoped honestly to the research (docs/private/porsche-research-architecture.md §7
+ porsche-research-modules.md §2/§3/§10a):

TARGET ERA -- the 2005-2016 Porsche-own CAN cars (911 997.1/997.2, Boxster/Cayman
  987, 911 991.1, 981, Panamera 970). Porsche brings its diagnostic bus to the
  OBD CAN pins and answers on 11-bit CAN at 500k. The ONE publicly-verified module
  is the engine DME on the standard ISO 15765-4 block (7E0/7E8) -- KWP service
  0x21 on 997/987, UDS 22/19 on 981+. Transmission 7E1/7E9 is a standard-block
  GUESS (probe). Every other Porsche module (PSM/ABS, airbag, cluster, PASM/PDCC,
  body) answers on request/response IDs Porsche NEVER published (research: UNKNOWN,
  probe-sweep on a real car) -- they are NOT invented here.

VAG-MLB REUSE -- the Cayenne 958 (92A) and Macan 95B are VW-Group MLB cars whose
  modules answer the VAG address block (gateway 710/77A, cluster 714/77E, ABS
  713/77D, airbag 715/77F, EPS 712/77C, AWD 70F/779, EPB 752/7BC -- response =
  request + 0x6A). We add that block best-effort ONLY when the VIN WMI marks the
  car a Porsche SUV (Cayenne/Macan). Porsche runs different control-unit software,
  so every VAG row is a CANDIDATE to verify -- a non-answer may mean Porsche uses a
  different address, not that the module is absent (research §3c).

THE WALLS (never presented as faults):
  * K-line + pin 3 -- 911 996 / Boxster 986 (~1997-2005) are KWP2000 over K-line;
    the dongle reaches the DME/Tiptronic/alarm on K1 (pin 7) for generic EOBD only
    (handled by the generic OBD scan, not this CAN walk), and the ABS/airbag/
    cluster/HVAC body modules are on a second K-line on pin 3 no ELM327 can reach
    (the Peugeot pins-3/8 lesson, Porsche edition). This scan needs CAN, so it
    reports the K-line honestly and stops.
  * TP2.0 -- the Cayenne 955/957 is a VW Touareg 7L on VW TP2.0, whose channel
    setup can't be held over BLE (repo vag-research.md §1). Generic OBD only.
  * DoIP -- MY17+ cars (Panamera 971, Cayenne 9YA, 911 992, Macan facelift, 718
    later, Taycan, plus the 991.2/982 grey zone) moved deep diagnostics to DoIP
    over Ethernet, which no ELM327 can speak. All still answer generic OBD -- the
    scan detects the pattern (VIN/0100 answers, no module depth) and says so.

DIALECT -- 981/991/970 and the Cayenne 958/Macan speak UDS (22 F190 / 19 02 mask);
  the older 997/987 speak KWP2000-over-CAN (1A ident, 18 02 FF FF read, 14 FF 00
  clear). Each module ladders UDS first then falls back to the KWP dialect, exactly
  like the JLR/Mercedes profiles (KWP rungs are standard KWP2000, UNVERIFIED-
  plausible for Porsche apart from the 997.2 0x21 capture -- see pids.py).

Nothing security-related is ever addressed: immobiliser/Kessy/component-protection
are HARD-REFUSE in the research (§7) -- no ids, no commands, no entry here at all;
the alarm is a passive fault-read only. Read-only apart from clear(), which the API
layer confirm-gates.
"""

import re
from datetime import datetime, timezone
from typing import List, Optional

from backend.core import uds
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

# The Porsche-own module block that answers on the standard ISO 15765-4 pairs
# (response = request + 8), walked on every CAN-era Porsche (research §2b/§10a).
# Only the engine is publicly VERIFIED; the transmission is a standard-block guess
# (probe). No PSM/ABS/airbag/cluster row -- those IDs were never published and are
# NOT guessed. Row shape mirrors the VW scan so the id_map (below) can carry the
# VAG +0x6A rows alongside these +8 rows.
PORSCHE_ECU_TABLE = [
    (0x7E0, 0x7E8, "DME", "Engine control (DME/ECM)",
     "Engine / injection -- the one guaranteed module (KWP 0x21 on 997/987, UDS on 981+)"),
    (0x7E1, 0x7E9, "TCU", "Transmission (Tiptronic / PDK)",
     "Automatic / PDK gearbox -- standard-block guess (7E1/7E9), probe; if fitted"),
]

# The VAG address block shared by the Cayenne 958 (92A) and Macan 95B (MLB),
# reused best-effort from the VW profile (research §3c/§10a). Response =
# request + 0x6A (a VAG body module does NOT answer request + 8), so these rows
# carry an explicit response id and are fed to Elm327UdsClient via the id_map.
# ``code`` is the VCDS 2-digit address, like the VW scan. Every row is a CANDIDATE
# -- Porsche STGs differ, so a non-answer is "Porsche-specific address", not absent.
VAG_ECU_TABLE = [
    (0x710, 0x77A, "19", "Gateway (J533)",
     "CAN gateway -- VAG address (Cayenne 958 / Macan); verify, Porsche STG may differ"),
    (0x714, 0x77E, "17", "Instrument cluster",
     "Dashboard cluster (holds service data) -- VAG address; verify, Porsche STG may differ"),
    (0x713, 0x77D, "03", "PSM / ABS (ESP)",
     "ABS / PSM stability -- VAG address; verify, Porsche STG may differ"),
    (0x715, 0x77F, "15", "Airbag / SRS",
     "Restraint / airbag (read-only faults) -- VAG address; verify, Porsche STG may differ"),
    (0x712, 0x77C, "44", "Steering (EPS / Servotronic)",
     "Electric power steering -- VAG address; verify, Porsche STG may differ"),
    (0x70F, 0x779, "22", "AWD coupling (transfer case)",
     "All-wheel-drive coupling (VAG addr 22) -- verify, Porsche STG may differ"),
    (0x752, 0x7BC, "53", "Parking brake (EPB)",
     "Electromechanical parking brake -- VAG address; verify (service-position = actuation, "
     "out of scope)"),
]

ENGINE = 0x7E0

# Standard UDS identification DIDs (research §4). Modules support different
# subsets; read best-effort and keep whatever answers as text.
IDENT_DIDS = [
    (0xF190, "VIN"),
    (0xF187, "Part number (Porsche)"),
    (0xF18C, "ECU serial"),
    (0xF189, "Software version"),
    (0xF194, "Supplier software number"),
    (0xF191, "Hardware"),
]

# KWP2000 identification records (service 0x1A) read when every UDS DID refuses
# (an older 997/987 KWP-over-CAN module). 1A 90 returns the VIN/ECU identification
# on Porsche KWP (research §4); UNVERIFIED-plausible for the rest.
KWP_IDENT_RECORDS = [
    (0x90, "ECU identification (KWP 1A 90)"),
    (0x80, "ECU data (KWP 1A 80)"),
    (0x86, "ECU ident (KWP 1A 86)"),
]

# UDS NRCs that mean "I might answer in a different session" -- open the UDS
# extended session (10 03) and retry once, identical to the VW/JLR ladder.
_SESSION_NRCS = (0x11, 0x22, 0x7E, 0x7F)

# reportDTCByStatusMask masks, laddered. No public "Porsche mask" (research §6):
# 0xFF = all, 0x08 = confirmed, 0xAF = broad tool mask. A bad mask returns NRC
# 0x31/0x12 -- move to the next mask on those.
_DTC_MASKS = (0xFF, 0x08, 0xAF)
_MASK_RETRY_NRCS = (0x12, 0x31)

_VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_vag_mlb_suspect(vin: Optional[str]) -> bool:
    """Heuristic from the VIN WMI: Porsche's SUVs (Cayenne, Macan) carry world-
    manufacturer identifier 'WP1'; the sports cars and Panamera carry 'WP0'. Only
    the SUVs are the VW-Group MLB cars whose modules answer the VAG address block
    (research §3c). Best-effort ONLY -- a misread just means the VAG rows time out
    quietly, and a Cayenne 955/957 (TP2.0) or MY17+ (DoIP) SUV can't answer 11-bit
    VAG UDS anyway, so a false positive costs nothing. Cars we can't identify as an
    SUV are scanned Porsche-own only. (The WMI->family mapping is standard VIN
    knowledge, not published in the research doc -- hence best-effort.)"""
    if not vin or len(vin) < 3:
        return False
    return vin[:3].upper() == "WP1"


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
    (UNVERIFIED-plausible for Porsche -- standard KWP2000, not Porsche-captured)."""
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
    refuses, the classic KWP 1A records an older 997/987 module answers."""
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
    triples. The fallback an older 997/987 KWP-over-CAN module answers (research
    §6; standard KWP2000 -- adapted from the JLR/Mercedes decode)."""
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
    ignition on, even on a TP2.0/DoIP-era car whose manufacturer side stays quiet."""
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
    """Walk the Porsche's reachable driveline modules and read each one's fault
    memory. ``adapter`` is a saved ELM327 adapter-instance dict (None for sim).
    Read-only."""
    if mode == "sim":
        return _simulated_scan()

    def run(session, entry):
        session.ensure_protocol()  # ObdError('link') here -> caught below
        if session.protocol_num not in (6, None):
            kline = session.protocol_num in (3, 4, 5)
            return {
                "ok": False, "simulated": False,
                "vehicle": {"vin": None, "protocol": session.protocol,
                            "adapter": (adapter or {}).get("name")},
                "modules": [],
                "message": (
                    (
                        f"The car answered {session.protocol or 'K-line'} - that is a 996/986 "
                        "(911/Boxster ~1997-2005) on KWP2000 K-line. The dongle reaches the DME "
                        "on K1 (pin 7) for generic EOBD only; the CAN module walk can't run over "
                        "K-line, and the ABS/airbag/cluster/HVAC body modules are on a second "
                        "K-line on pin 3 no ELM327 can reach. Pre-2001 cars may pre-date EOBD."
                    ) if kline else (
                        f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                        "11-bit/500k CAN Porsche module diagnostics use. If this is the Porsche, "
                        "turn the ignition ON and retry. A Cayenne 955/957 is a VW Touareg on "
                        "TP2.0 that can't be module-scanned through a plain dongle."
                    )
                ),
                "timestamp": _now(),
            }

        # The car's identity first, while the receive filter is still open:
        # Mode 09 VIN works on any ~2001+ car regardless of what UDS/KWP does next.
        obd_vin = obd_service._read_vin(session)

        # Branch on platform (research §2/§9): every CAN-era Porsche gets the
        # Porsche-own block; a Cayenne 958 / Macan (VIN WMI 'WP1') additionally
        # gets the VAG address block best-effort.
        vag_suspect = _is_vag_mlb_suspect(obd_vin)
        ecu_table = list(PORSCHE_ECU_TABLE)
        if vag_suspect:
            ecu_table += VAG_ECU_TABLE
        # Elm327UdsClient needs the explicit response id for the VAG +0x6A rows;
        # the +8 rows equal the default, so a full map is harmless.
        id_map = {rid: resp for rid, resp, *_ in ecu_table}

        modules: List[dict] = []
        mfr_present = 0     # modules that answered manufacturer-level services
        engine_on_obd = False
        uds_opened: set = set()
        kwp_opened: set = set()

        client = Elm327UdsClient(session, id_map)
        client.begin()
        try:
            for req_id, resp_id, code, name, desc in ecu_table:
                trace: List[str] = []
                mod = {
                    "address": f"0x{req_id:03X}", "response": f"0x{resp_id:03X}",
                    "code": code, "name": name, "description": desc,
                    "present": False, "ident": [], "dtcs": [], "error": None,
                    "trace": trace,
                }
                via_standard_obd = False
                present = _probe(client, req_id, trace)
                if not present and req_id == ENGINE:
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
                    mod["ident"] = _read_ident(client, req_id, kwp_opened, trace)
                    mod["dtcs"] = _read_dtcs(client, req_id, uds_opened, kwp_opened, trace)
                except (uds.UdsNegativeResponse, ObdError, TransportError) as exc:
                    if req_id == ENGINE:
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
                " Porsche-own CAN era (2005-2016). This engine + best-effort driveline read is "
                "the honest dongle ceiling: Porsche never published its per-module CAN IDs, so "
                "PSM/ABS, airbag, cluster, PASM/PDCC and the body modules answer on addresses "
                "that aren't in any public table, and the crown-jewel measuring values (camshaft "
                "deviation, over-rev history, electronic oil level, PDK temps) have no public DID "
                "-- not faults, just out of reach. Full-depth work needs an address-aware tool "
                "(Foxwell/iCarsoft) or PIWIS/Durametric."
            )
            if vag_suspect:
                era_note += (
                    " VIN reads as a Cayenne/Macan (VW-Group MLB), so the VAG address block was "
                    "walked too -- but Porsche control-unit software differs, so a VAG row that "
                    "didn't answer may be a Porsche-specific address rather than an absent module."
                )
        elif engine_on_obd or vin:
            era_note = (
                " The car answers generic OBD but no module answered Porsche diagnostics. That is "
                "the signature of a Cayenne 955/957 (a VW Touareg on TP2.0, unreachable over BLE) "
                "or of a MY17+ DoIP car (Panamera 971, Cayenne 9YA, 911 992, Macan facelift, 718 "
                "later, Taycan, and the 991.2/982 grey zone -- deep diagnostics moved to Ethernet, "
                "which no OBD dongle can speak). Generic-OBD-only territory, not a fault. The "
                "Service tab's dash-procedure resets still work where the era has a button method."
            )
        else:
            era_note = (
                " No Porsche module and no engine answered. If this is the Porsche, turn the "
                "ignition ON and retry. A 996/986 on K-line, a Cayenne 955/957 on TP2.0 or a "
                "MY17+ DoIP car can't be module-scanned on this dongle."
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
    """Clear fault memory on one Porsche module (address like "0x7E0") or all present.

    Ladder matches the read side: UDS ClearDiagnosticInformation (14 FF FF FF),
    then the KWP clear (14 FF 00) an older 997/987 module wants. The id_map covers
    both the Porsche-own +8 rows and the VAG +0x6A rows so any address the UI
    offers resolves to the right receive filter.
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
                    "11-bit/500k CAN needed to clear Porsche module faults."
                ),
                "timestamp": _now(),
            }

        # Cover every possible address (Porsche-own + VAG) so an explicit address
        # resolves to its receive filter; the "all" path only clears modules that
        # actually probe present, so VAG rows on a non-MLB car are simply skipped.
        all_rows = PORSCHE_ECU_TABLE + VAG_ECU_TABLE
        id_map = {rid: resp for rid, resp, *_ in all_rows}

        cleared, errors = [], []
        kwp_opened: set = set()
        client = Elm327UdsClient(session, id_map)
        client.begin()
        try:
            if wanted is not None:
                targets = [wanted]
            else:
                targets = [rid for rid, _, _, _, _ in all_rows if _probe(client, rid, [])]
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
        mod("0x7E0", "0x7E8", "DME", "Engine control (DME/ECM)",
            "Engine / injection -- the one guaranteed module (KWP 0x21 on 997/987, UDS on 981+)",
            True, ident=[{"label": "VIN", "value": "SIMULATEDBOXSTER1"},
                         {"label": "Part number (Porsche)", "value": "9A190701600"}],
            dtcs=[
                {"code": "P0300", "hex": "030000", "failureType": "00",
                 "status": "2F", "flags": ["test failed", "confirmed"]},
            ]),
        mod("0x7E1", "0x7E9", "TCU", "Transmission (Tiptronic / PDK)",
            "Automatic / PDK gearbox -- standard-block guess (7E1/7E9), probe; if fitted", True,
            ident=[{"label": "VIN", "value": "SIMULATEDBOXSTER1"}]),
    ]
    return {
        "ok": True, "simulated": True,
        "vehicle": {"vin": "SIMULATEDBOXSTER1", "protocol": "ISO 15765-4 (CAN 11/500)",
                    "adapter": "Simulated"},
        "modules": modules,
        "message": "SIMULATED -- VIN SIMULATEDBOXSTER1. 2 modules answered, 1 fault stored. "
                   "Porsche-own CAN read (engine DME + PDK gearbox). Porsche's other module IDs "
                   "aren't public, so this is the honest engine + driveline ceiling. No real car "
                   "connected.",
        "timestamp": _now(),
    }
