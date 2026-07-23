"""Fiat / Alfa Romeo (FCA / Stellantis) module scan over a generic OBD-II dongle.

Generic OBD-II (backend/obd/service.py) only sees the engine's emissions data.
This walks the manufacturer-level modules an FCA car exposes, in two tiers
scoped honestly to what a plain dongle reaches (docs/private/fiat-alfa-research.md
§1-3):

TIER 1 -- UDS-era FCA, the target (Giulia 952, Stelvio, 500X, Tipo, 124 Spider,
  Jeep Renegade, newer 500/Panda; ~2014+). FCA UDS uses 29-bit extended CAN at
  500k: request 18DAxxF1, response 18DAF1xx (research §2a, VERIFIED). scan()
  switches the ELM to ATSP7 and walks FCA_ECU_TABLE with Elm327Uds29Client. The
  2018+ Security Gateway does NOT block reads -- DTCs, VIN and live data all
  answer; only clearing/actuations are gated (research §1c), so a read-only scan
  is fully feasible even on an SGW car.

TIER 2 -- CAN-era fallback (~2005-2016: Grande Punto, 500, Panda, MiTo,
  Giulietta). If nothing answered on 29-bit, scan() drops to ATSP6 (11-bit) and
  runs the engine on the standard OBD block (0x7E0). Engine + ABS are reachable
  bare, but the body computer, cluster and airbag sit on a low-speed B-CAN not
  wired to OBD pins 6/14 and need a MultiECUScan-style B-CAN pin adapter
  (research §1b) -- the result message says so rather than imply a fault. If even
  the engine is silent the message names the K-line era (Alfa 147/156/GT, Punto
  mk2), which isn't reachable over a plain dongle at all (research §1a).

HARD GUARD (research §7): TPMS at 0xC7 (18DAC7F1) is NEVER addressed -- polling
it can engage the FCA immobilizer and stall the car. It is asserted out of the
table below, and scan()/clear() refuse address 0xC7 explicitly.

scan.py owns the ATSP switching: after the tier work it restores the protocol
ensure_protocol discovered (and the 11-bit functional header) so the cached
session hands back cleanly to the generic OBD stack.

Read-only apart from clear(), which the API layer guards behind a confirm.
"""

import re
from datetime import datetime, timezone
from typing import List, Optional

from backend.core import uds
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327Uds29Client, Elm327UdsClient

# TPMS -- the immobilizer trap. Never addressable (research §2a, §7): the Giulia
# PID author's explicit warning is that polling tyre PIDs on 18DAC7F1 "will engage
# the immobilizer and stop the car". Kept as a named constant so the guards and
# the selftest reference the same value.
TPMS_ADDR = 0xC7
_TPMS_REFUSAL = (
    "Refused: TPMS (0xC7 / 18DAC7F1) is never addressed on this brand -- polling it "
    "can engage the FCA immobilizer and stall the car (research §7)."
)

# Curated FCA UDS one-byte address table for the modules a dealer checks pre-sale.
# Each row is (addr, code, name, description); ``addr`` is the destination ECU
# byte (request 18DA<addr>F1, response 18DAF1<addr>). Only the addresses captured
# on a real car are listed -- confidence per row (research §2a). The unverified
# FCA modules (IPC/ORC/EPS/HVAC/radio) are deliberately NOT hard-coded: research
# §2 says probe-sweep them on a real car first.
FCA_ECU_TABLE = [
    # Engine / PCM. VERIFIED: danardi DA10F1 (all engine DIDs); P1kachu 0x10.
    (0x10, "10", "Engine control (ECM/PCM)", "Engine / injection (petrol or diesel)"),
    # Transmission. VERIFIED: danardi DA18F1 (ZF 8HP auto / TCT), trans oil temp.
    (0x18, "18", "Transmission (TCM)", "ZF 8HP automatic / TCT (if fitted)"),
    # ABS / ESC -- Continental MK C1 on Giorgio; also hosts the EPB on Giulia/
    # Stelvio (research §6). VERIFIED: P1kachu 0x28.
    (0x28, "28", "ABS / ESC (+ EPB)", "Anti-lock + stability; hosts the electric parking brake"),
    # Steering column (SCCM). VERIFIED: P1kachu 0x30.
    (0x30, "30", "Steering column (SCCM)", "Steering column control module"),
    # Body Computer (BCM). VERIFIED: P1kachu 0x40.
    (0x40, "40", "Body Computer (BCM)", "Body / central electrics"),
]

# HARD GUARD: the TPMS immobilizer address must never sit in the walkable table.
assert TPMS_ADDR not in {addr for addr, _, _, _ in FCA_ECU_TABLE}, \
    "TPMS 0xC7 must never be in the FCA scan table (immobilizer trap, research §7)"

# The engine on the standard OBD block for the 11-bit (CAN-era) tier-2 fallback.
ENGINE_OBD = 0x7E0

# Standard UDS identification DIDs (0x22), research §3a. Modules support different
# subsets; read best-effort and keep whatever answers as text.
IDENT_DIDS = [
    (0xF190, "VIN"),
    (0xF18C, "ECU serial"),
    (0xF191, "Hardware"),
    (0xF195, "Software"),
    (0xF1A0, "System name"),
]

# UDS NRCs that mean "I might answer in a different session" -- when a plain
# ReadDTC returns one of these we open the UDS extended session (10 03) and retry
# once (research §3a), identical to the VW/Mercedes ladder.
_SESSION_NRCS = (0x11, 0x22, 0x7E, 0x7F)

# reportDTCByStatusMask masks, laddered. There is no single public "FCA mask"
# (research §3a): 0xFF = all DTCs, 0x08 = confirmed only, 0xAF = the broad tool
# mask. A bad/unsupported mask returns NRC 0x31 (out of range) or 0x12 (sub-func
# not supported) -- move to the next mask on those, like the VW/Mercedes code.
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


def _restore_protocol(session, discovered) -> None:
    """scan.py owns ATSP: after the 29-bit / 11-bit tier work, hand the ELM back
    on the protocol ensure_protocol discovered, with the 11-bit functional header
    the generic OBD stack relies on. Sessions are cached and reused, so a stale
    ATSP7 left behind would break the next generic call -- this prevents that."""
    try:
        session.at(f"ATSP{discovered:X}" if discovered is not None else "ATSP0")
        session.at("ATSH7DF")
    except Exception:
        pass


def _probe(client, req_id: int, trace: List[str]) -> bool:
    """Presence ladder: 3E 00, then a VIN ident read (research §3a recipe) -- some
    ECUs never answer a naked TesterPresent but do answer 22 F190. Any reply,
    positive or negative, proves an ECU lives at the address."""
    for data in (bytes([0x3E, 0x00]), bytes([0x22, 0xF1, 0x90])):
        try:
            reply = client.request(req_id, data, timeout=1.5)
        except (ObdError, TransportError):
            continue
        if reply:
            return True
    trace.append("3E 00 / 22 F190 -> silence")
    return False


def _open_extended(client, req_id: int, trace: List[str]) -> bool:
    """Open a UDS extended session (10 03) so a module that refused a read in the
    default session will answer. Returns True on a positive 0x50 reply."""
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


def _read_ident(client, req_id: int, trace: List[str]) -> List[dict]:
    """Best-effort module identification via ReadDataByIdentifier (0x22)."""
    out: List[dict] = []
    for did, label in IDENT_DIDS:
        try:
            payload = client.request(req_id, bytes([0x22, did >> 8, did & 0xFF]), timeout=2.0)
        except (ObdError, TransportError):
            if did == 0xF190:
                trace.append("22 F190 -> no answer")
            continue
        # Positive RDBI reply is 0x62 <did_hi> <did_lo> <data...>; anything else
        # (incl. a 0x7F negative for an unsupported DID) we simply skip.
        if len(payload) < 4 or payload[0] != 0x62:
            if did == 0xF190 and payload and payload[0] == 0x7F and len(payload) >= 3:
                trace.append(f"22 F190 -> NRC {payload[2]:02X}")
            continue
        value = _printable(payload[3:])
        if value:
            out.append({"label": label, "value": value})
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


def _read_dtcs(client, req_id: int, trace: List[str]) -> List[dict]:
    """Fault memory, laddered. On a session-conditions NRC (0x11/0x22/0x7E/0x7F)
    open the UDS extended session (10 03) once and retry -- some FCA modules only
    answer 19 02 in an active session. Response-pending 7F..78 is handled by the
    core (uds._request_settled), so it never surfaces here as a failure."""
    try:
        return _read_dtcs_uds(client, req_id, trace)
    except uds.UdsNegativeResponse as exc:
        if exc.nrc in _SESSION_NRCS and _open_extended(client, req_id, trace):
            try:
                return _read_dtcs_uds(client, req_id, trace)
            except uds.UdsNegativeResponse as exc2:
                trace.append(f"19 02 after session -> {_nrc_text(exc2)}")
                raise
        raise


def _tier2_engine(session) -> dict:
    """CAN-era fallback: read the engine on the standard OBD block (0x7E0, 11-bit)
    the way the VW/Mercedes engine fallback does. 01 00 must answer on an
    emissions ECU with the ignition on; on that we try UDS ident/DTCs and, if the
    module doesn't speak UDS fault services, fall back to generic Mode 03."""
    trace: List[str] = []
    mod = {
        "address": f"0x{ENGINE_OBD:03X}", "response": f"0x{ENGINE_OBD + 8:03X}",
        "code": "10", "name": "Engine control (generic OBD)",
        "description": "Engine / injection on 11-bit HS-CAN (CAN-era Fiat fallback)",
        "present": False, "ident": [], "dtcs": [], "error": None, "trace": trace,
    }
    client = Elm327UdsClient(session)
    client.begin()
    try:
        try:
            reply = client.request(ENGINE_OBD, bytes([0x01, 0x00]), timeout=2.0)
            present = bool(reply)
        except (ObdError, TransportError):
            present = False
        if not present:
            trace.append("01 00 -> silence")
            return mod
        mod["present"] = True
        trace.append("answered on standard OBD 7E0 (01 00)")
        try:
            mod["ident"] = _read_ident(client, ENGINE_OBD, trace)
            mod["dtcs"] = _read_dtcs(client, ENGINE_OBD, trace)
        except uds.UdsNegativeResponse as exc:
            mod["error"] = str(exc)
        except (ObdError, TransportError) as exc:
            # Engine on the plain OBD channel may not speak UDS fault services --
            # generic Mode 03 codes still do.
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
    finally:
        client.restore()
    return mod


def scan(adapter: Optional[dict], mode: str = "real") -> dict:
    """Walk the FCA car's reachable modules and read each one's fault memory.

    ``adapter`` is a saved ELM327 adapter-instance dict (None for sim). Read-only.
    """
    if mode == "sim":
        return _simulated_scan()

    def run(session, entry):
        session.ensure_protocol()  # ObdError('link') here -> caught below as "ignition?"
        discovered = session.protocol_num
        if discovered not in (6, None):
            return {
                "ok": False, "simulated": False,
                "vehicle": {"vin": None, "protocol": session.protocol,
                            "adapter": (adapter or {}).get("name")},
                "modules": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k CAN Fiat / Alfa diagnostics use. If this is the FCA car, turn "
                    "the ignition on and retry. Older Alfa 147/156/GT and Punto mk2 are K-line "
                    "and aren't supported on this dongle."
                ),
                "timestamp": _now(),
            }

        # The car's identity first, while the receive filter is still open and
        # before any ATSP switch: Mode 09 VIN works on any ~2001+ FCA car.
        obd_vin = obd_service._read_vin(session)

        modules: List[dict] = []
        mfr_present: List[int] = []  # FCA addresses that answered 29-bit UDS

        # TIER 1: 29-bit UDS-era walk on ATSP7.
        session.at("ATSP7")
        session.at("ATCAF1")
        try:
            client = Elm327Uds29Client(session)
            client.begin()
            try:
                for addr, code, name, desc in FCA_ECU_TABLE:
                    trace: List[str] = []
                    mod = {
                        "address": f"0x{addr:02X}", "response": f"0x18DAF1{addr:02X}",
                        "code": code, "name": name, "description": desc,
                        "present": False, "ident": [], "dtcs": [], "error": None,
                        "trace": trace,
                    }
                    if not _probe(client, addr, trace):
                        modules.append(mod)
                        continue
                    mod["present"] = True
                    mfr_present.append(addr)
                    try:
                        mod["ident"] = _read_ident(client, addr, trace)
                        mod["dtcs"] = _read_dtcs(client, addr, trace)
                    except uds.UdsNegativeResponse as exc:
                        mod["error"] = str(exc)
                    except (ObdError, TransportError) as exc:
                        mod["error"] = str(exc)
                    modules.append(mod)
            finally:
                client.restore()

            # TIER 2: nothing on 29-bit -> the 11-bit engine (CAN-era Fiat).
            engine_present = False
            if not mfr_present:
                session.at("ATSP6")
                engine_mod = _tier2_engine(session)
                engine_present = engine_mod["present"]
                modules.append(engine_mod)
        finally:
            _restore_protocol(session, discovered)

        present = [m for m in modules if m["present"]]
        faults = sum(len(m["dtcs"]) for m in present)

        # Dealer check: every module that knows its VIN should agree. A module
        # reporting a different VIN was swapped in from another car (crash repair
        # or a cluster swap used to disguise mileage).
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
                " UDS-era FCA (Giulia/Stelvio/500X/Tipo/124/Renegade, ~2014+). Reads work even "
                "on a 2018+ Security-Gateway car -- the SGW blocks only clearing and actuations, "
                "not reads."
            )
        elif engine_present:
            era_note = (
                " The engine answered generic OBD but no module answered FCA 29-bit diagnostics: "
                "this looks like a CAN-era Fiat (~2005-2016: Grande Punto, 500, Panda, MiTo, "
                "Giulietta). Engine + ABS are reachable on the bare dongle, but the body computer, "
                "cluster and airbag sit on a low-speed B-CAN not wired to the OBD port -- they need "
                "a MultiECUScan-style B-CAN pin adapter (UK: Gendan)."
            )
        else:
            era_note = (
                " No FCA module and no engine answered. If this is the Fiat / Alfa, turn the "
                "ignition ON and retry. Older Alfa 147/156/GT and Punto mk2 are K-line and can't "
                "be diagnosed on this dongle."
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
    """Clear fault memory on one FCA module (address like "0x10") or all present.

    29-bit ClearDiagnosticInformation (14 FF FF FF). TPMS (0xC7) is refused. On a
    2018+ Security-Gateway car the clear is blocked over a bare dongle (research
    §1c) -- the message says so, and that reads still pass the SGW.
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
                "message": f"Bad module address '{address}' (expected hex like 0x10).",
                "timestamp": _now(),
            }
        if wanted == TPMS_ADDR:
            return {
                "ok": False, "simulated": False, "cleared": [],
                "message": _TPMS_REFUSAL, "timestamp": _now(),
            }

    def run(session, entry):
        session.ensure_protocol()
        discovered = session.protocol_num
        if discovered not in (6, None):
            return {
                "ok": False, "simulated": False, "cleared": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k CAN needed to clear Fiat / Alfa module faults."
                ),
                "timestamp": _now(),
            }

        session.at("ATSP7")
        session.at("ATCAF1")
        cleared, errors = [], []
        try:
            client = Elm327Uds29Client(session)
            client.begin()
            try:
                if wanted is not None:
                    targets = [wanted]
                else:
                    targets = [addr for addr, _, _, _ in FCA_ECU_TABLE
                               if _probe(client, addr, [])]
                for addr in targets:
                    if addr == TPMS_ADDR:  # defensive: never touch the immobilizer
                        continue
                    try:
                        uds.clear_dtcs(client, addr)
                        cleared.append(f"0x{addr:02X}")
                    except Exception as exc:
                        errors.append(f"0x{addr:02X}: {exc}")
            finally:
                client.restore()
        finally:
            _restore_protocol(session, discovered)

        ok = bool(cleared) and not errors
        message = f"Cleared fault memory on {', '.join(cleared) or 'no modules'}."
        if errors:
            message += (
                " Failed: " + "; ".join(errors)
                + " On a 2018+ FCA car the Security Gateway blocks clearing over a bare dongle -- "
                "reads still work, but clearing needs an SGW bypass plug or an AutoAuth-registered "
                "tool (research §1c)."
            )
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
        mod("0x10", "0x18DAF110", "10", "Engine control (ECM/PCM)",
            "Engine / injection (petrol or diesel)",
            True, ident=[{"label": "VIN", "value": "SIMULATEDGIULIA01"},
                         {"label": "Software", "value": "50560012"}],
            dtcs=[
                {"code": "P0401", "hex": "040100", "failureType": "00",
                 "status": "2F", "flags": ["test failed", "confirmed"]},
            ]),
        mod("0x18", "0x18DAF118", "18", "Transmission (TCM)",
            "ZF 8HP automatic / TCT (if fitted)", True,
            ident=[{"label": "VIN", "value": "SIMULATEDGIULIA01"}]),
        mod("0x28", "0x18DAF128", "28", "ABS / ESC (+ EPB)",
            "Anti-lock + stability; hosts the electric parking brake",
            True, dtcs=[{"code": "C1301", "hex": "130100", "failureType": "94",
                         "status": "08", "flags": ["confirmed"]}]),
        mod("0x30", "0x18DAF130", "30", "Steering column (SCCM)",
            "Steering column control module", True),
        mod("0x40", "0x18DAF140", "40", "Body Computer (BCM)",
            "Body / central electrics", False),
    ]
    return {
        "ok": True, "simulated": True,
        "vehicle": {"vin": "SIMULATEDGIULIA01", "protocol": "ISO 15765-4 (CAN 29/500)",
                    "adapter": "Simulated"},
        "modules": modules,
        "message": "SIMULATED -- VIN SIMULATEDGIULIA01. 4 modules answered, 2 faults stored. "
                   "UDS-era FCA -- reads work even on a 2018+ Security-Gateway car (SGW blocks "
                   "clearing/actuations only). No real car connected.",
        "timestamp": _now(),
    }
