"""Mercedes-Benz engine + gearbox read over a generic OBD-II dongle.

Generic OBD-II (backend/obd/service.py) only sees the engine's emissions data.
This reads the two modules a plain dongle can reach at manufacturer depth on a
Mercedes and each one's fault memory + identification -- the same idea as the
Mazda/Peugeot/VW dongle scans, but scoped honestly to what Mercedes actually
exposes (docs/private/mercedes-research.md §1-2):

* Engine ECU on 0x7E0/0x7E8 and the automatic transmission on 0x7E1/0x7E9, both
  in the standard ISO 15765-4 block (response = request + 8). VERIFIED, §2 Table A.
* EVERYTHING else -- ABS/ESP, SRS, SAM front/rear, EZS/EIS, cluster, Airmatic,
  Parktronic, climate -- uses Mercedes' proprietary diagnostic scheme on
  model-specific *low* CAN ids (~0x300, NOT the 0x7E0..0x7E7 block) locked inside
  encrypted CBF files behind a filtering gateway. Those ids are not public and
  the modules need a model-specific wake packet: STAR/XENTRY territory, NOT
  reachable over any ELM327. So this is NOT a VCDS/ISTA-style all-modules walk;
  it is an honest engine+gearbox deep read plus generic OBD.

Mercedes splits into a KWP2000-over-CAN sub-era (~2004-2014) and a UDS-over-CAN
sub-era (~2014-2018); the dongle setup is identical, only the service bytes
differ, so each module ladders UDS first then falls back to the KWP dialect
(research §3).

FORBIDDEN (research §2 Table B): the model-specific wake broadcasts 0x01C /
0x692 / 0x4E4 (payload 02 10 92 ...) that bring body modules up DO exist, but
this harness never sends them -- they are bus-level writes aimed at waking
asleep body modules we can't address anyway (their per-module ids are STAR-only),
and blasting the bus is outside a read-only tool's remit. Engine + TCM are awake
with the ignition on and answer point-to-point without any wake packet (§3).

SCOPE (research §1): ~2004-2018 for the engine+gearbox read (petrol OBD ~2001+,
EU diesel ~2004+). ~2018+ cars are DoIP (diagnostics over Ethernet) which an
ELM327 cannot speak -- they give generic OBD-II only; pre-2004 cars predate OBD
engine diagnostics. In both cases the scan says so rather than imply a fault.

Read-only apart from clear(), which the API layer guards behind a confirm.
"""

import re
from datetime import datetime, timezone
from typing import List, Optional

from backend.core import uds
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

# The whole publicly-reliable Mercedes table over a plain dongle (research §2
# Table A). Each row is (request_id, response_id, code, name, description);
# response = request + 8, the standard ISO 15765-4 pairing. There is deliberately
# no body-module row -- those ids are proprietary/STAR-only (see the module
# docstring). Confidence per module is noted inline.
MERCEDES_ECU_TABLE = [
    # Engine ECU (ME petrol / CDI-CRD diesel). VERIFIED: petrol OBD ~2001+,
    # EU diesel ~2004+; a 2011 GLK connected "ISO 15765-4 CAN 500 kbps".
    (0x7E0, 0x7E8, "ME/CDI", "Engine control (ME / CDI)",
     "Engine / injection (petrol ME, diesel CDI)"),
    # Automatic transmission (722.6 5G / 722.9 7G-Tronic / 725.0 9G). VERIFIED on
    # 722.9 (its ATF-temp custom PID lives on 7E1); the 722.6 occupancy of 7E1 is
    # UNVERIFIED-plausible -- some answer here, some are proprietary (verify per car).
    (0x7E1, 0x7E9, "TCM", "Transmission (722.6 / 722.9 / 9G)",
     "Automatic gearbox control (if fitted)"),
]

# request_id -> response_id map handed to Elm327UdsClient. On Mercedes the
# reachable modules obey the standard +8 pairing, so this is trivial; it is kept
# for symmetry with the other profiles and to be explicit about the filter.
MERCEDES_ID_MAP = {tx: rx for tx, rx, _, _, _ in MERCEDES_ECU_TABLE}

# The engine also owns the standard OBD slot; 01 00 there is the one request an
# emissions ECU must answer with the ignition on. On Mercedes the engine IS 0x7E0.
ENGINE = 0x7E0

# Standard UDS identification DIDs (0x22), research §3 ("22 F190 VIN, or
# 22 F187 / F18C for ident"). Modules support different subsets; read best-effort
# and keep whatever answers as text.
IDENT_DIDS = [
    (0xF190, "VIN"),
    (0xF187, "Part number"),
    (0xF18C, "ECU serial"),
    (0xF189, "Software"),
]

# KWP2000 identification records read with service 0x1A when the UDS DIDs all
# refuse (KWP-era module, ~2004-2014). These are the standard KWP2000 ECU-ident
# records; UNVERIFIED-plausible for Mercedes (not independently captured on a
# Merc -- research §3), so we try them and keep whatever answers.
KWP_IDENT_RECORDS = [
    (0x87, "Part number (KWP 1A 87)"),
    (0x88, "Software (KWP 1A 88)"),
    (0x86, "ECU ident (KWP 1A 86)"),
    (0x9B, "Coding (KWP 1A 9B)"),
]

# UDS NRCs that mean "I might answer in a different session" -- when a plain
# ReadDTC returns one of these we open the UDS extended session (10 03) and retry
# once (research §3), identical to the VW ladder.
_SESSION_NRCS = (0x11, 0x22, 0x7E, 0x7F)

# reportDTCByStatusMask masks, laddered. There is no single public "Mercedes
# mask" (research §3): 0xFF = all DTCs, 0x08 = confirmed only, 0xAF = the broad
# tool mask. A bad/unsupported mask returns NRC 0x31 (out of range) or 0x12
# (sub-func not supported) -- move to the next mask on those, like the VW code.
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


def _probe(client: Elm327UdsClient, req_id: int, trace: List[str]) -> bool:
    """Presence ladder: 3E 00, then a VIN ident read (research §3 recipe) -- some
    ECUs never answer a naked TesterPresent but do answer 22 F190. Any reply,
    positive or negative, proves an ECU lives at the id pair. Engine and TCM are
    awake ignition-on, so no wake packet is needed."""
    for data in (bytes([0x3E, 0x00]), bytes([0x22, 0xF1, 0x90])):
        try:
            reply = client.request(req_id, data, timeout=1.2)
        except (ObdError, TransportError):
            continue
        if reply:
            return True
    trace.append("3E 00 / 22 F190 -> silence")
    return False


def _open_extended(client: Elm327UdsClient, req_id: int, trace: List[str]) -> bool:
    """Open a UDS extended session (10 03) so a module that refused a read in the
    default session will answer. Returns True on a positive 0x50 reply. Standard
    UDS 10 03 -- the UDS-era (2014+) escalation."""
    try:
        reply = client.request(req_id, bytes([0x10, 0x03]), timeout=2.0)
    except (ObdError, TransportError):
        trace.append("10 03 -> no answer")
        return False
    if reply and reply[0] == 0x50:
        trace.append("10 03 -> extended session")
        return True
    if reply and reply[0] == 0x7F and len(reply) >= 3:
        trace.append(f"10 03 -> NRC {reply[2]:02X}")
    return False


def _open_kwp_session(client: Elm327UdsClient, req_id: int, trace: List[str]) -> bool:
    """Bring a KWP2000-over-CAN module (~2004-2014) up: MB session 10 92 first,
    then plain 10 81 if refused (research §3). UNVERIFIED-plausible for Mercedes:
    0x92 is the manufacturer session the wake broadcasts in §2 Table B carry, but
    we send it point-to-point to an already-awake engine/TCM, NEVER as a bus wake
    broadcast. Read-safe -- the session lapses on its own after a few seconds."""
    for sub in (0x92, 0x81):
        try:
            reply = client.request(req_id, bytes([0x10, sub]), timeout=2.0)
        except (ObdError, TransportError):
            trace.append(f"10 {sub:02X} -> no answer")
            continue
        if reply and reply[0] == 0x50:
            trace.append(f"10 {sub:02X} -> KWP session")
            return True
        if reply and reply[0] == 0x7F and len(reply) >= 3:
            trace.append(f"10 {sub:02X} -> NRC {reply[2]:02X}")
    return False


def _read_ident(client: Elm327UdsClient, req_id: int, trace: List[str]) -> List[dict]:
    """Best-effort identification: UDS DIDs (0x22) first, then -- if every UDS DID
    refuses (a KWP-era module) -- the KWP2000 1A ident records."""
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
        # Positive RDBI reply is 0x62 <did_hi> <did_lo> <data...>; anything else
        # (incl. a 0x7F negative for an unsupported DID) we simply skip.
        if len(payload) < 4 or payload[0] != 0x62:
            refused += 1
            if did == 0xF190 and payload and payload[0] == 0x7F and len(payload) >= 3:
                trace.append(f"22 F190 -> NRC {payload[2]:02X}")
            continue
        value = _printable(payload[3:])
        if value:
            out.append({"label": label, "value": value})

    # Every UDS DID refused: a KWP-flavour module. Open its MB session and read
    # the classic KWP ident records instead (UNVERIFIED-plausible -- research §3).
    if not out and refused == len(IDENT_DIDS) and _open_kwp_session(client, req_id, trace):
        for rec, label in KWP_IDENT_RECORDS:
            try:
                payload = client.request(req_id, bytes([0x1A, rec]), timeout=2.0)
            except (ObdError, TransportError):
                continue
            # Positive KWP readEcuIdentification reply is 0x5A <record> <data...>.
            if len(payload) < 3 or payload[0] != 0x5A:
                continue
            value = _printable(payload[2:])
            if value:
                trace.append(f"1A {rec:02X} -> OK")
                out.append({"label": label, "value": value})
                found = _VIN_RE.search(value.upper())
                if found:
                    out.append({"label": "VIN", "value": found.group(0)})
    return out


def _read_dtcs_uds(client: Elm327UdsClient, req_id: int, trace: List[str]) -> List[dict]:
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


def _read_dtcs_kwp(client: Elm327UdsClient, req_id: int, trace: List[str]) -> List[dict]:
    """KWP2000 readDTCByStatus: 18 02 FF FF -> 58 <count> then (hi, lo, status)
    per DTC (2-byte DTC + status records). UNVERIFIED-plausible for Mercedes
    (standard KWP2000; not Merc-captured -- research §3). Adapted from the
    Peugeot 17 FF 00 KWP decode in backend/profiles/peugeot/scan.py."""
    _open_kwp_session(client, req_id, trace)
    try:
        payload = client.request(req_id, bytes([0x18, 0x02, 0xFF, 0xFF]), timeout=3.0)
    except (ObdError, TransportError) as exc:
        trace.append("18 02 FF FF -> no answer")
        raise exc
    if not payload or payload[0] != 0x58:
        if payload and payload[0] == 0x7F and len(payload) >= 3:
            exc = uds.UdsNegativeResponse(0x18, payload[2])
            trace.append(f"18 02 FF FF -> {_nrc_text(exc)}")
            raise exc
        raise ObdError("KWP fault read failed.", kind="no_data")
    trace.append("18 02 FF FF -> OK (KWP)")

    # 0x58 <count> <triples> is the book format; some ECUs skip the count.
    if len(payload) >= 2 and (len(payload) - 2) % 3 == 0:
        offset = 2
    elif (len(payload) - 1) % 3 == 0:
        offset = 1
    else:
        offset = 2
    dtcs = []
    for i in range(offset, len(payload) - 2, 3):
        hi, lo, status = payload[i], payload[i + 1], payload[i + 2]
        if hi == 0 and lo == 0:
            continue
        entry = uds.decode_dtc(hi, lo, 0x00)
        entry["hex"] = f"{hi:02X}{lo:02X}"
        entry["status"] = f"{status:02X}"
        entry["flags"] = ["stored (KWP)"]
        dtcs.append(entry)
    return dtcs


def _read_dtcs(client: Elm327UdsClient, req_id: int, trace: List[str]) -> List[dict]:
    """Fault memory, laddered: UDS 19 02 first; on a session-conditions NRC
    (0x11/0x22/0x7E/0x7F) open the UDS extended session (10 03) once and retry;
    if the module doesn't speak UDS DTCs at all, fall back to the KWP fault read
    (18 02 FF FF) a ~2004-2014 module answers. Mirrors the Peugeot ladder."""
    try:
        return _read_dtcs_uds(client, req_id, trace)
    except uds.UdsNegativeResponse as exc:
        if exc.nrc in _SESSION_NRCS and _open_extended(client, req_id, trace):
            try:
                return _read_dtcs_uds(client, req_id, trace)
            except uds.UdsNegativeResponse as exc2:
                trace.append(f"19 02 after session -> {_nrc_text(exc2)}")
        return _read_dtcs_kwp(client, req_id, trace)
    except (ObdError, TransportError):
        return _read_dtcs_kwp(client, req_id, trace)


def scan(adapter: Optional[dict], mode: str = "real") -> dict:
    """Read the Mercedes engine + gearbox fault memory over a dongle.

    ``adapter`` is a saved ELM327 adapter-instance dict (None for sim). Read-only.
    """
    if mode == "sim":
        return _simulated_scan()

    def run(session, entry):
        session.ensure_protocol()  # ObdError('link') here -> caught below as "ignition?"
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False,
                "vehicle": {"vin": None, "protocol": session.protocol,
                            "adapter": (adapter or {}).get("name")},
                "modules": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k CAN Mercedes engine/gearbox diagnostics use. If this is the "
                    "Mercedes, turn the ignition on and retry. A handful of 2001-2003 cars are "
                    "K-line for emissions engine data only."
                ),
                "timestamp": _now(),
            }

        # The car's identity first, while the receive filter is still open:
        # Mode 09 VIN works on any ~2001+ car no matter what UDS/KWP does next.
        obd_vin = obd_service._read_vin(session)

        client = Elm327UdsClient(session, MERCEDES_ID_MAP)
        client.begin()
        modules: List[dict] = []
        mfr_present: List[int] = []  # modules that answered a manufacturer probe
        try:
            for req_id, resp_id, code, name, desc in MERCEDES_ECU_TABLE:
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
                    # Mode 01 PID 00 on the standard OBD slot: the one request an
                    # emissions ECU must answer with the ignition on. On a
                    # DoIP-era car the manufacturer side is silent but this still
                    # answers, so the engine still shows up (via generic OBD).
                    try:
                        reply = client.request(ENGINE, bytes([0x01, 0x00]), timeout=2.0)
                        present = bool(reply)
                    except (ObdError, TransportError):
                        present = False
                    if present:
                        via_standard_obd = True
                        trace.append("answered on standard OBD 7E0 (01 00)")
                if not present:
                    modules.append(mod)
                    continue
                mod["present"] = True
                if not via_standard_obd:
                    mfr_present.append(req_id)
                try:
                    mod["ident"] = _read_ident(client, req_id, trace)
                    mod["dtcs"] = _read_dtcs(client, req_id, trace)
                except uds.UdsNegativeResponse as exc:
                    mod["error"] = str(exc)
                except (ObdError, TransportError) as exc:
                    if via_standard_obd:
                        # Engine on the plain OBD channel may not speak UDS/KWP
                        # fault services -- generic Mode 03 codes still do.
                        try:
                            from backend.obd import dtc as obd_dtc
                            codes = obd_dtc.read_codes(session, 0x03)
                            mod["dtcs"] = [
                                {"code": c, "hex": "", "failureType": "",
                                 "status": "", "flags": ["confirmed"]}
                                for c in codes
                            ]
                            trace.append("fell back to OBD Mode 03")
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

        # Standing honesty for Mercedes: the body/chassis/comfort modules aren't
        # missing -- they use proprietary ids behind the gateway and are
        # STAR/XENTRY-only over any dongle (research §2), so they are simply not
        # part of this engine+gearbox scan.
        body_note = (
            " Body modules (ABS/ESP, SRS, SAM, EZS, cluster, Airmatic, climate) "
            "use Mercedes' proprietary addressing behind the gateway and are "
            "STAR/XENTRY-only -- no OBD dongle reaches them, so they are outside "
            "this engine+gearbox scan, not faulty."
        )

        # Nothing answered Mercedes diagnostics, but generic OBD did: a DoIP-era
        # (~2018+) car whose manufacturer diag is on Ethernet, or a pre-2004 car
        # that predates OBD engine diagnostics. Not a fault (research §1, §6).
        generic_obd_ok = bool(obd_vin) or any(
            m["present"] and m["address"] == f"0x{ENGINE:03X}" for m in modules
        )
        doip_note = ""
        if not mfr_present and generic_obd_ok:
            doip_note = (
                " No engine or gearbox module answered Mercedes diagnostics, but "
                "generic OBD-II did. That points to a DoIP-era (~2018+) Mercedes -- "
                "its manufacturer diagnostics run over Ethernet (STAR/XENTRY), which "
                "an OBD dongle can't speak -- or a pre-2004 car that predates OBD "
                "engine diagnostics. Generic-OBD-only territory, not a fault."
            )

        return {
            "ok": True, "simulated": False,
            "vehicle": {"vin": vin, "protocol": session.protocol or "ISO 15765-4 (CAN 11/500)",
                        "adapter": (adapter or {}).get("name")},
            "modules": modules,
            "message": (
                (f"VIN {vin}. " if vin else "")
                + f"{len(present)} module(s) answered, {faults} fault"
                f"{'s' if faults != 1 else ''} stored.{vin_warning}{body_note}{doip_note} "
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
    """Clear fault memory on one module (address like "0x7E0") or all present."""
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
                    "11-bit/500k CAN needed to clear Mercedes engine/gearbox faults."
                ),
                "timestamp": _now(),
            }

        client = Elm327UdsClient(session, MERCEDES_ID_MAP)
        client.begin()
        cleared, errors = [], []
        try:
            if wanted is not None:
                targets = [wanted]
            else:
                targets = [rid for rid, _, _, _, _ in MERCEDES_ECU_TABLE
                           if _probe(client, rid, [])]
            for rid in targets:
                # Ladder matches the read side: UDS clear (14 FF FF FF), then the
                # KWP clear a ~2004-2014 module wants (14 FF 00) after the MB
                # session (UNVERIFIED-plausible -- research §3).
                try:
                    uds.clear_dtcs(client, rid)
                    cleared.append(f"0x{rid:03X}")
                except Exception as exc:
                    try:
                        _open_kwp_session(client, rid, [])
                        reply = client.request(rid, bytes([0x14, 0xFF, 0x00]), timeout=5.0)
                        if reply and reply[0] == 0x54:
                            cleared.append(f"0x{rid:03X}")
                        else:
                            errors.append(f"0x{rid:03X}: {exc}")
                    except Exception:
                        errors.append(f"0x{rid:03X}: {exc}")
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
        mod("0x7E0", "0x7E8", "ME/CDI", "Engine control (ME / CDI)",
            "Engine / injection (petrol ME, diesel CDI)",
            True, ident=[{"label": "VIN", "value": "SIMULATEDMB000001"},
                         {"label": "Part number", "value": "A6519005800"}],
            dtcs=[
                {"code": "P0170", "hex": "017000", "failureType": "00",
                 "status": "2F", "flags": ["test failed", "confirmed"]},
            ]),
        mod("0x7E1", "0x7E9", "TCM", "Transmission (722.6 / 722.9 / 9G)",
            "Automatic gearbox control (if fitted)",
            True, ident=[{"label": "Part number (KWP 1A 87)", "value": "A0325458832"}],
            dtcs=[{"code": "P0729", "hex": "072900", "failureType": "00",
                   "status": "08", "flags": ["confirmed"]}]),
    ]
    return {
        "ok": True, "simulated": True,
        "vehicle": {"vin": "SIMULATEDMB000001", "protocol": "ISO 15765-4 (CAN 11/500)",
                    "adapter": "Simulated"},
        "modules": modules,
        "message": "SIMULATED -- VIN SIMULATEDMB000001. 2 modules answered, 2 faults stored. "
                   "Body modules (ABS/ESP, SRS, SAM, cluster...) are STAR-only on Mercedes and "
                   "outside this engine+gearbox scan. No real car connected.",
        "timestamp": _now(),
    }
