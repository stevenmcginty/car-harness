"""Peugeot / PSA module scan over a generic OBD-II dongle (UDS + PSA KWP).

Generic OBD-II (backend/obd/service.py) only sees the engine's emissions data.
This walks the manufacturer-level modules a PSA car exposes to the dongle and
reads each responding module's fault memory + identification -- the same idea
as the BMW ENET scan and the Mazda dongle scan, but using PSA's own per-module
CAN id pairs instead of the standard 0x7E0+8 block.

FIELD-TEST REALITY (see backend/obd/psa_uds_elm327.py for the full story):

* The VIN is read FIRST over plain OBD Mode 09 PID 02 -- mandatory on ~2001+
  cars -- so the report always identifies the car even if PSA diagnostics
  refuse everything else.
* A standard dongle reaches only the powertrain bus (OBD pins 6/14): engine,
  gearbox, ABS/ESP, steering, SCR. The body modules (BSI, airbag, cluster...)
  sit on the diagnostic CAN at OBD pins 3/8 -- reachable only with a PSA
  pin-swap adapter. Their silence is wiring, not car trouble, and the report
  says so instead of listing them as mysteriously absent.
* Modules ladder UDS first, then PSA's KWP dialect: session 10 C0, fault read
  17 FF 00, ident zone 21 80. Each module carries a request->reply trace so a
  failed field scan shows exactly which rung broke.

Read-only apart from clear(), which the API layer guards behind a confirm.
"""

import re
from datetime import datetime, timezone
from typing import List, Optional

from backend.core import uds
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.psa_uds_elm327 import PsaElm327UdsClient
from backend.obd.psa_j2534 import PsaJ2534UdsClient


def _make_j2534_link(config: Optional[dict]):
    """Build (not open) a J2534 link from a saved j2534 adapter's config. Split
    out so the self-test can substitute a simulated device."""
    from backend.obd.j2534_link import J2534Link

    return J2534Link(str((config or {}).get("dll") or "").strip())

# Curated PSA diagnostic address table for the modules a dealer actually cares
# about on a pre-sale check. Each row is (request_id, response_id, code, name,
# description). These are 11-bit CAN ids taken from the community PSA diagnostic
# databases (ludwig-v/arduino-psa-diag ECU_LIST.md, prototux/PSA-RE
# nodes/AEE2004.full.yml, Barracuda09/PyPSADiag) -- the T7-era ids (308 / 308 CC
# / RCZ, 2007-2013) follow PSA's node arithmetic: request 0x6A0+node /
# response 0x680+node on the 500k IS bus wired to OBD pins 6/14, and
# request 0x740+node / response 0x640+node on the 125k body buses behind the
# BSI (OBD pins 3/8). "(if fitted)" modules simply time out and are marked not
# present on cars without them -- harmless.
PEUGEOT_ECU_TABLE = [
    (0x752, 0x652, "BSI", "Body Systems Interface", "Central body computer / gateway"),
    (0x6A8, 0x688, "CMM", "Engine control", "Engine / injection (petrol or diesel)"),
    (0x6A9, 0x689, "BVA", "Automatic gearbox", "Automatic transmission (if fitted)"),
    (0x6AD, 0x68D, "ABS/ESP", "Braking / stability", "ABS anti-lock + ESP stability"),
    (0x744, 0x644, "AIRBAG", "Airbags / SRS", "Restraint / airbag control"),
    (0x75F, 0x65F, "COMBINE", "Instrument cluster", "Dashboard cluster"),
    (0x767, 0x667, "BTE", "Folding roof (308 CC)", "Convertible roof controller -- CC-only, also runs the windows"),
    (0x75D, 0x65D, "AAS", "Parking aid", "Park distance sensors (if fitted)"),
    (0x6B5, 0x695, "DIRECTION", "Power steering", "Electric power steering"),
    (0x6A5, 0x685, "CAV", "Steering angle sensor", "ESP steering-angle sensor (id derived from the PSA-RE node map)"),
    (0x6B7, 0x697, "CORPRO", "Headlamp correction", "Headlamp levelling / directional beams (if fitted)"),
    (0x6B3, 0x693, "FSE", "Electric parking brake", "Electric parking brake (if fitted)"),
    (0x76D, 0x66D, "CLIM", "Climate control", "Air conditioning / heating"),
    (0x778, 0x678, "TDCAR", "Rear climate", "Rear climate control (if fitted)"),
    (0x6AF, 0x68F, "DSG", "Tyre pressure (TPMS)", "Tyre pressure monitoring (if fitted)"),
    (0x760, 0x660, "AUTORADIO", "Radio / audio", "Audio head unit"),
    (0x771, 0x671, "CDCHG", "CD changer", "CD changer (if fitted)"),
    (0x765, 0x665, "AFFICHEUR", "Central display", "Central screen (if fitted)"),
    (0x764, 0x664, "TELEMAT", "Telematics", "Telematics / emergency call (if fitted)"),
    (0x773, 0x673, "KML", "Hands-free kit", "Bluetooth hands-free unit (if fitted)"),
    (0x6A1, 0x681, "SCR", "AdBlue / SCR", "Diesel selective catalytic reduction"),
    (0x6C1, 0x601, "ADDITIF", "DPF additive", "Diesel particulate additive pump (newer cars)"),
    (0x775, 0x675, "ADDGO3", "DPF additive (older)", "Diesel additive dosing, pre-PADDGO cars (if fitted)"),
    (0x730, 0x710, "CPL", "Rain / light sensor", "Rain & light sensor, newer Hella UDS variant (if fitted)"),
    (0x74A, 0x64A, "CDPL", "Rain / light sensor (T7)", "Rain & light sensor, T7-era id"),
    (0x742, 0x642, "COM2000", "Steering column", "COM2000 steering-wheel controls unit"),
    (0x746, 0x646, "RHF", "RF receiver", "Radio-frequency receiver for the remote key"),
    (0x741, 0x641, "ADML", "Keyless start", "Keyless entry / start (if fitted)"),
    (0x75C, 0x65C, "ALARME", "Alarm", "Factory alarm (if fitted)"),
    (0x74B, 0x64B, "PDPC", "Driver door", "Driver door electronics / window module"),
    (0x76E, 0x66E, "BDMC", "Driver seat memory", "Driver seat memory (if fitted)"),
    (0x76F, 0x66F, "BDMP", "Passenger seat memory", "Passenger seat memory (if fitted)"),
    (0x768, 0x668, "TOUVRANT", "Sunroof", "Opening sunroof, fixed-glass cars (if fitted)"),
    (0x748, 0x648, "BSC", "Boot servo", "Boot lid servo control (if fitted)"),
    (0x76C, 0x66C, "BSR", "Trailer module", "Towbar / trailer module (if fitted)"),
    (0x731, 0x711, "BECH", "Aux heater", "Additional heater, diesel (if fitted)"),
    (0x6B4, 0x694, "BMS", "HV battery", "High-voltage battery management (EV/PHEV, if fitted)"),
]

# request_id -> response_id map handed to the PSA UDS client so it can set the
# right receive filter per module (PSA response != request+8).
PEUGEOT_ID_MAP = {tx: rx for tx, rx, _, _, _ in PEUGEOT_ECU_TABLE}

# Powertrain-bus modules: the set a standard dongle CAN reach on pins 6/14
# (the 500k IS bus). Everything else lives on the body buses behind the BSI.
ENGINE_BUS = {0x6A8, 0x6A9, 0x6AD, 0x6B5, 0x6A1, 0x6A5, 0x6B7}

# The engine also owns the standard OBD slot; 01 00 there is the one request an
# emissions ECU must answer with the ignition on.
ENGINE = 0x6A8
OBD_ENGINE = 0x7E0

# Standard UDS identification DIDs (0x22). Modules support different subsets;
# we read best-effort and keep whatever answers as printable text.
IDENT_DIDS = [
    (0xF190, "VIN"),
    (0xF187, "Part number"),
    (0xF188, "Software"),
    (0xF18C, "ECU serial"),
]

# KWP ident zones read with service 0x21 when the 0x22 DIDs all refuse.
KWP_ZONES = [(0x80, "Ident (zone 80)"), (0x90, "Ident (zone 90)")]

# KWP zones that can carry the VIN, tried in order by read_vin() below. Zone
# 0x80 is the classic PSA identification block; 0x90 answers on some MED17 /
# DiagOnCAN-era ECUs. A zone this ECU doesn't implement simply stays silent.
KWP_VIN_ZONES = (0x80, 0x90)

# UDS NRCs that mean "I might answer in a different session" -- when a plain
# ReadDTC returns one of these we open the PSA session ladder and retry once.
_SESSION_NRCS = (0x11, 0x22, 0x7E, 0x7F)

# reportDTCByStatusMask masks, most-PSA-likely first: 0x09 is what PSA's own
# tooling asks for (arduino-psa-diag), then the OEM-style masks.
_DTC_MASKS = (0x09, 0xAF, 0x0C, 0xFF)

# A VIN is 17 chars WITHOUT I/O/Q -- and never all digits. A KWP ident zone's
# text is "part number + serial" (the RCZ field car: 9663711580 003966 = 17
# digits), which the bare pattern happily matched, producing a phantom VIN and
# a false cross-module mismatch warning. A real WMI always carries letters
# (S-Z for Europe), so require at least two.
_VIN_RE = re.compile(r"(?=[A-HJ-NPR-Z0-9]{17})"
                     r"(?=.{0,16}[A-HJ-NPR-Z].{0,15}[A-HJ-NPR-Z])[A-HJ-NPR-Z0-9]{17}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _vin_in(payload: bytes, skip: int) -> Optional[str]:
    """A 17-char VIN hiding in a positive reply after ``skip`` echo bytes.

    ASCII filter only, never the hex fallback of _printable(): a hex dump of
    a binary payload is itself 0-9A-F characters and would false-match the
    VIN pattern.
    """
    if not payload or len(payload) <= skip:
        return None
    text = "".join(chr(b) for b in payload[skip:] if 32 <= b < 127)
    found = _VIN_RE.search(text.upper())
    return found.group(0) if found else None


def read_vin(session, trace: Optional[List[str]] = None) -> Optional[str]:
    """The car's VIN, laddered across every PSA generation a dongle can meet.

    Why a ladder: the generic Mode 09 PID 02 read is answered by ~2010+ UDS
    cars, but the KWP-era engines (MED17-class petrol, DCM diesels, the
    2009-2013 308 CC included) ignore it -- field-verified: that car answers
    no Mode 09 at all, refuses UDS 22 F1 90 with NRC 11, and only wakes for
    the KWP StartCommunication 81. So, in order, best-effort:

    1. Mode 09 PID 02 broadcast (0x7DF) -- any ~2010+ car.
    2. Mode 09 PID 2 physical (0x7E0) -- some ECUs ignore functional addressing.
    3. UDS 22 F1 90 on the engine's PSA pair (0x6A8 -> 0x688).
    4. PSA KWP session (10 03 / 10 C0 / 81) then the ident zones 0x21 reads.

    Every rung is read-only and failure-tolerant: the first VIN found wins and
    a silent car just returns None. Run BEFORE any per-module aiming narrows
    the ELM's receive filter.
    """
    trace = trace if trace is not None else []

    try:
        vin = obd_service._read_vin(session)
    except Exception:
        vin = None  # a session without the generic query() stack (self-test)
    if vin:
        return vin

    client = PsaElm327UdsClient(session, PEUGEOT_ID_MAP)
    client.begin()
    try:
        # Rung 2: Mode 09 on the engine's physical OBD slot.
        try:
            reply = client.request(OBD_ENGINE, bytes([0x09, 0x02]), timeout=3.0)
            if reply and reply[0] == 0x49:
                vin = _vin_in(reply, 3)      # 49 02 <count> then the VIN
                if vin:
                    trace.append("0902 @7E0 -> VIN")
                    return vin
        except (ObdError, TransportError):
            pass

        # Rung 3: UDS VIN on the engine's own PSA id pair.
        try:
            reply = client.request(ENGINE, bytes([0x22, 0xF1, 0x90]), timeout=2.5)
            if reply and reply[0] == 0x62:
                vin = _vin_in(reply, 3)      # 62 F1 90 then the VIN
                if vin:
                    trace.append("22 F190 @6A8 -> VIN")
                    return vin
        except (ObdError, TransportError):
            pass

        # Rung 4: KWP ident zones after the PSA session ladder.
        if client.open_session(ENGINE, trace):
            for zone in KWP_VIN_ZONES:
                try:
                    reply = client.request(
                        ENGINE, bytes([0x21, zone]), timeout=2.5
                    )
                except (ObdError, TransportError):
                    continue
                if not reply or reply[0] != 0x61:
                    continue
                vin = _vin_in(reply, 2)      # 61 <zone> then the ident text
                if vin:
                    trace.append(f"21 {zone:02X} -> VIN")
                    return vin
        return None
    finally:
        client.restore()


def _nrc_text(exc: uds.UdsNegativeResponse) -> str:
    return f"NRC {exc.nrc:02X} ({uds.NRC_NAMES.get(exc.nrc, 'unknown')})"


def _printable(body: bytes) -> str:
    text = "".join(chr(b) for b in body if 32 <= b < 127).strip()
    if text and len(text) >= (len(body) + 1) // 2:
        return text
    return body.hex().upper()


def _probe(client: PsaElm327UdsClient, req_id: int, trace: List[str]) -> bool:
    """Presence ladder: 3E 00, then an ident read, then the PSA session open --
    some ECUs never answer a naked TesterPresent, some only wake for 10 C0.
    Any reply, positive or negative, proves an ECU lives at the id pair."""
    for data, opens_session in (
        (bytes([0x3E, 0x00]), False),
        (bytes([0x22, 0xF1, 0x90]), False),
        (bytes([0x10, 0xC0]), True),
    ):
        try:
            reply = client.request(req_id, data, timeout=1.2)
        except (ObdError, TransportError):
            continue
        if reply:
            if opens_session and reply[0] == 0x50:
                client.mark_session_open(req_id)
            return True
    trace.append("3E 00 / 22 F190 / 10 C0 -> silence")
    return False


def _read_ident(client: PsaElm327UdsClient, req_id: int, trace: List[str]) -> List[dict]:
    """Best-effort identification: UDS DIDs first, KWP zone 80 as fallback."""
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

    # Every UDS DID refused: a KWP-flavour module. Open its PSA session and
    # read the classic ident zone instead.
    if not out and refused == len(IDENT_DIDS) and client.open_session(req_id, trace):
        for zone, label in KWP_ZONES:
            try:
                payload = client.request(req_id, bytes([0x21, zone]), timeout=2.0)
            except (ObdError, TransportError):
                continue
            if len(payload) < 3 or payload[0] != 0x61:
                continue
            value = _printable(payload[2:])
            if value:
                trace.append(f"21 {zone:02X} -> OK")
                out.append({"label": label, "value": value})
                found = _VIN_RE.search(value.upper())
                if found:
                    out.append({"label": "VIN", "value": found.group(0)})
    return out


def _read_dtcs_uds(client: PsaElm327UdsClient, req_id: int, trace: List[str]) -> List[dict]:
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
            if reply[2] in (0x12, 0x31):
                continue
            trace.append(f"19 02 {mask:02X} -> {_nrc_text(last_error)}")
            raise last_error
    if payload is None:
        if last_error is not None:
            trace.append(f"19 02 -> {_nrc_text(last_error)}")
            raise last_error
        raise ObdError("Fault read failed.", kind="no_data")

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


def _read_dtcs_kwp(client: PsaElm327UdsClient, req_id: int, trace: List[str]) -> List[dict]:
    """KWP ReadStatusOfDTC: 17 FF 00 -> 57 <n> then (hi, lo, status) per DTC."""
    client.open_session(req_id, trace)
    try:
        payload = client.request(req_id, bytes([0x17, 0xFF, 0x00]), timeout=3.0)
    except (ObdError, TransportError) as exc:
        trace.append("17 FF 00 -> no answer")
        raise exc
    if not payload or payload[0] != 0x57:
        if payload and payload[0] == 0x7F and len(payload) >= 3:
            exc = uds.UdsNegativeResponse(0x17, payload[2])
            trace.append(f"17 FF 00 -> {_nrc_text(exc)}")
            raise exc
        raise ObdError("KWP fault read failed.", kind="no_data")
    trace.append("17 FF 00 -> OK (KWP)")

    # 0x57 <count> <triples> is the book format; some ECUs skip the count.
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


def _read_dtcs(client: PsaElm327UdsClient, req_id: int, trace: List[str]) -> List[dict]:
    """Fault memory, laddered: UDS with PSA's preferred mask first; on a
    session refusal open the PSA session ladder and retry; if the module
    doesn't speak 19 02 at all, fall back to the KWP fault read."""
    try:
        return _read_dtcs_uds(client, req_id, trace)
    except uds.UdsNegativeResponse as exc:
        if exc.nrc in _SESSION_NRCS and client.open_session(req_id, trace):
            try:
                return _read_dtcs_uds(client, req_id, trace)
            except uds.UdsNegativeResponse as exc2:
                trace.append(f"19 02 after session -> {_nrc_text(exc2)}")
        return _read_dtcs_kwp(client, req_id, trace)
    except (ObdError, TransportError):
        return _read_dtcs_kwp(client, req_id, trace)


def _walk_and_report(client, obd_vin, adapter_name, protocol_label,
                     dongle_pins_hint, session=None, transport_note="") -> dict:
    """Walk PEUGEOT_ECU_TABLE with ``client`` and assemble the scan report.

    Shared by the dongle (ELM327) and pass-thru-cable (J2534) paths -- the walk,
    ident/DTC ladders, VIN cross-check and clear are all transport-agnostic. The
    only difference is the two flags: ``dongle_pins_hint`` adds the pins-3/8
    explainer when body modules are silent on a plain dongle, and ``session`` (an
    ELM327 session, or None on J2534) enables the engine's generic Mode 03
    fallback.
    """
    client.begin()
    modules: List[dict] = []
    try:
        for req_id, resp_id, code, name, desc in PEUGEOT_ECU_TABLE:
            trace: List[str] = []
            mod = {
                "address": f"0x{req_id:03X}", "response": f"0x{resp_id:03X}",
                "code": code, "name": name, "description": desc,
                "present": False, "ident": [], "dtcs": [], "error": None,
                "trace": trace,
            }
            target = req_id
            via_standard_obd = False
            present = _probe(client, req_id, trace)
            if not present and req_id == ENGINE:
                # Mode 01 PID 00 on the standard OBD slot: the one request
                # an emissions ECU must answer with the ignition on.
                try:
                    reply = client.request(OBD_ENGINE, bytes([0x01, 0x00]), timeout=2.0)
                    present = bool(reply)
                except (ObdError, TransportError):
                    present = False
                if present:
                    target = OBD_ENGINE
                    via_standard_obd = True
                    trace.append("answered on standard OBD 7E0")
            if not present:
                modules.append(mod)
                continue
            mod["present"] = True
            try:
                mod["ident"] = _read_ident(client, target, trace)
                mod["dtcs"] = _read_dtcs(client, target, trace)
            except uds.UdsNegativeResponse as exc:
                mod["error"] = str(exc)
            except (ObdError, TransportError) as exc:
                if via_standard_obd and session is not None:
                    # Engine on the plain OBD channel may not speak UDS
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
    # reporting a different VIN was swapped in from another car (crash
    # repair, or the cluster/BSI dance used to disguise mileage).
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

    # Body modules silent while only powertrain-bus modules answered = the
    # dongle is on the engine bus (OBD pins 6/14) and this car keeps its
    # body diagnostics on pins 3/8. That's wiring, not car trouble. Over a
    # PSA pass-thru cable we're already on the 3/8 bus, so the hint is off.
    bus_note = ""
    absent_hint = None
    if dongle_pins_hint:
        engine_reachable = {f"0x{i:03X}" for i in ENGINE_BUS}
        body_silent = bool(present) and all(
            m["address"] in engine_reachable for m in present
        ) and any(m["address"] == "0x752" and not m["present"] for m in modules)
        if body_silent:
            bus_note = (
                " The body modules (BSI, airbag, cluster, radio...) aren't missing -- "
                "on Peugeot/Citroen they sit on a separate diagnostic CAN wired to "
                "OBD pins 3 & 8, which a standard dongle can't reach. A cheap 'PSA "
                "pin 3/8' adapter between dongle and socket brings them all in."
            )
            absent_hint = (
                "On the pins-3/8 diagnostic CAN -- reachable with a PSA pin-swap "
                "adapter, not over a standard dongle. Not a fault."
            )

    return {
        "ok": True, "simulated": False,
        "vehicle": {"vin": vin, "protocol": protocol_label, "adapter": adapter_name},
        "modules": modules,
        "absentHint": absent_hint,
        "message": (
            (f"VIN {vin}. " if vin else "")
            + f"{len(present)} module(s) answered, {faults} fault"
            f"{'s' if faults != 1 else ''} stored.{vin_warning}{bus_note}{transport_note} "
            "Read-only scan -- nothing was changed on the car."
        ),
        "timestamp": _now(),
    }


def _j2534_read_vin(link) -> Optional[str]:
    """Best-effort VIN over J2534, before the module walk: engine Mode 09 first
    (any ~2001+ car), then BSI 22 F190. Returns None if the car stays quiet."""
    for tx, rx, req in ((OBD_ENGINE, OBD_ENGINE + 8, bytes([0x09, 0x02])),
                        (0x752, 0x652, bytes([0x22, 0xF1, 0x90]))):
        try:
            reply = link.uds_request(tx, rx, req, timeout=3.0)
        except (ObdError, TransportError):
            continue
        if not reply:
            continue
        if reply[0] == 0x49 and len(reply) > 3:        # Mode 09 positive: 49 02 <cnt> ...
            body = reply[3:]
        elif reply[0] == 0x62 and len(reply) > 3:      # ReadDataByIdentifier: 62 F1 90 ...
            body = reply[3:]
        else:
            continue
        text = "".join(chr(b) for b in body if 32 < b < 127).strip("\x00 ")
        if len(text) >= 11:
            return text[:17]
    return None


def _scan_over_j2534(adapter: Optional[dict], mode: str) -> dict:
    """Module walk over a J2534 pass-thru cable (e.g. a PSA DiagBox/Lexia cable
    wired to the pins-3/8 diagnostic CAN, so body modules answer too)."""
    link = _make_j2534_link((adapter or {}).get("config"))
    try:
        try:
            link.open()
        except (TransportError, ObdError) as exc:
            return {
                "ok": False, "simulated": False, "vehicle": None, "modules": [],
                "message": str(exc), "timestamp": _now(),
            }
        obd_vin = _j2534_read_vin(link)
        client = PsaJ2534UdsClient(link, PEUGEOT_ID_MAP)
        return _walk_and_report(
            client, obd_vin, (adapter or {}).get("name"),
            "ISO 15765-4 (CAN 11/500) via J2534 cable",
            dongle_pins_hint=False, session=None,
            transport_note=" Connected through the PSA pass-thru cable on the "
                           "pins-3/8 diagnostic bus.",
        )
    finally:
        try:
            link.close()
        except Exception:
            pass


def scan(adapter: Optional[dict], mode: str = "real") -> dict:
    """Walk the Peugeot's reachable modules and read each one's fault memory.

    ``adapter`` is a saved adapter-instance dict (None for sim). A J2534 adapter
    drives the pass-thru-cable path; any ELM327 adapter drives the dongle path.
    Read-only.
    """
    if mode == "sim":
        return _simulated_scan()

    if adapter and adapter.get("typeId") == "j2534":
        return _scan_over_j2534(adapter, mode)

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
                    "11-bit/500k CAN PSA module diagnostics use. If this is the Peugeot, turn "
                    "the ignition on and retry. Pre-2004 (VAN bus) and many 2004-2010 (KWP) "
                    "cars can't be module-scanned through a plain OBD dongle."
                ),
                "timestamp": _now(),
            }

        # The car's identity first, while the receive filter is still open:
        # the read_vin ladder covers UDS-era cars (Mode 09) AND the KWP-era
        # engines that ignore it (the 308 CC MED17 among them).
        obd_vin = read_vin(session)
        client = PsaElm327UdsClient(session, PEUGEOT_ID_MAP)
        return _walk_and_report(
            client, obd_vin, (adapter or {}).get("name"),
            session.protocol or "ISO 15765-4 (CAN 11/500)",
            dongle_pins_hint=True, session=session,
        )

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {
            "ok": False, "simulated": False, "vehicle": None, "modules": [],
            "message": str(exc), "timestamp": _now(),
        }


def _clear_with_client(client, wanted: Optional[int]) -> dict:
    """Clear one module (or every present module) with ``client``. Shared by the
    dongle and pass-thru-cable paths."""
    client.begin()
    cleared, errors = [], []
    try:
        if wanted is not None:
            targets = [wanted]
        else:
            targets = [rid for rid, _, _, _, _ in PEUGEOT_ECU_TABLE
                       if _probe(client, rid, [])]
        for rid in targets:
            # Ladder matches the read side: UDS clear, then the KWP clear
            # PSA's own tooling sends (14 FF 00) after opening the session.
            try:
                uds.clear_dtcs(client, rid)
                cleared.append(f"0x{rid:03X}")
            except Exception as exc:
                try:
                    client.open_session(rid)
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


def _clear_over_j2534(adapter: Optional[dict], wanted: Optional[int]) -> dict:
    link = _make_j2534_link((adapter or {}).get("config"))
    try:
        try:
            link.open()
        except (TransportError, ObdError) as exc:
            return {
                "ok": False, "simulated": False, "cleared": [],
                "message": str(exc), "timestamp": _now(),
            }
        client = PsaJ2534UdsClient(link, PEUGEOT_ID_MAP)
        return _clear_with_client(client, wanted)
    finally:
        try:
            link.close()
        except Exception:
            pass


def clear(adapter: Optional[dict], mode: str = "real", address: Optional[str] = None) -> dict:
    """Clear fault memory on one module (address like "0x752") or all present."""
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
                "message": f"Bad module address '{address}' (expected hex like 0x752).",
                "timestamp": _now(),
            }

    if adapter and adapter.get("typeId") == "j2534":
        return _clear_over_j2534(adapter, wanted)

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "cleared": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k CAN needed to clear Peugeot module faults."
                ),
                "timestamp": _now(),
            }

        client = PsaElm327UdsClient(session, PEUGEOT_ID_MAP)
        return _clear_with_client(client, wanted)

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
        mod("0x752", "0x652", "BSI", "Body Systems Interface", "Central body computer / gateway",
            True, ident=[{"label": "VIN", "value": "SIMULATED00PEUGEOT"},
                         {"label": "Part number", "value": "9822009980"}]),
        mod("0x6A8", "0x688", "CMM", "Engine control", "Engine / injection (petrol or diesel)",
            True, ident=[{"label": "Software", "value": "1037564321"}],
            dtcs=[
                {"code": "P0234", "hex": "023400", "failureType": "00",
                 "status": "2F", "flags": ["test failed", "confirmed"]},
                {"code": "P1338", "hex": "133800", "failureType": "00",
                 "status": "08", "flags": ["confirmed"]},
            ]),
        mod("0x744", "0x644", "AIRBAG", "Airbags / SRS", "Restraint / airbag control",
            True, dtcs=[{"code": "B1004", "hex": "100400", "failureType": "00",
                         "status": "08", "flags": ["confirmed"]}]),
        mod("0x6AD", "0x68D", "ABS/ESP", "Braking / stability", "ABS anti-lock + ESP stability",
            True),
        mod("0x6A1", "0x681", "SCR", "AdBlue / SCR", "Diesel selective catalytic reduction",
            True, dtcs=[{"code": "P20E8", "hex": "20E800", "failureType": "00",
                         "status": "08", "flags": ["confirmed"]}]),
        mod("0x75D", "0x65D", "AAS", "Parking aid", "Park distance sensors (if fitted)", False),
        mod("0x764", "0x664", "TELEMAT", "Telematics", "Telematics / emergency call (if fitted)",
            False),
    ]
    return {
        "ok": True, "simulated": True,
        "vehicle": {"vin": "SIMULATED00PEUGEOT", "protocol": "ISO 15765-4 (CAN 11/500)",
                    "adapter": "Simulated"},
        "modules": modules,
        "message": "SIMULATED -- VIN SIMULATED00PEUGEOT. 5 modules answered, 4 faults stored. "
                   "No real car connected.",
        "timestamp": _now(),
    }
