"""What software is actually flashed into each module on a GM Global A car.

This is the read half of "can I update the gearbox software?". The write half
is not here and is not coming -- see resets.py and the WHY NOT A FLASH note at
the bottom of this file. What the harness can do honestly is tell you, module
by module, exactly which calibration parts are in the car right now, when they
were last written and by whom. That is the same information GM's SPS matches
against a VIN when it decides whether an update exists, so it is the step that
turns "I heard there's a gearbox update" into a yes or a no.

GMW3110 keeps identification behind service 0x1A with a ONE-byte identifier
(UDS uses 0x22 with a two-byte DID). The identifiers that describe flashed
software are, per GMW3110-2010:

    $CB EndModelPartNumber      the whole calibration set as one GM part number
    $CC BaseModelPartNumber     the hardware/base variant it was built for
    $C0 BootSoftwarePartNumber  the bootloader
    $C1..$CA SoftwareModuleIdentifier 01..10
                                the individual flashed blocks -- on a powertrain
                                module these are the operating system and the
                                calibrations, and they are what actually changes
                                when a gearbox recalibration is applied
    $D0..$DC                    the two-character alpha code belonging to each
                                of the part numbers above
    $99 ProgrammingDate         when this module was last flashed
    $98 RepairShopCodeOrTesterSerialNumber
                                which tester wrote it (a dealer SPS session
                                leaves its own code here; an aftermarket tuner
                                leaves a different one, or zeros)
    $9F PreviousStoredRepairShopCodeOrTesterSerialNumbers
                                the ones before that -- reprogramming history

Identifier names are taken from Scapy's GMLAN contrib (scapy.contrib.
automotive.gm.gmlan, GMLAN_RDBI.dataIdentifiers), which is a reading of
GMW3110-2010. Nothing here is guessed: an identifier the harness cannot name is
not asked for, and a value whose encoding is not certain is reported raw with
its confidence marked rather than dressed up in a unit it may not have.

Late Global A nodes that answer plain UDS instead get the ISO 14229-1 F18x
identification block, which covers the same ground under different names.

Read-only. Every request in this file is a read; none of them can change the
car.
"""

from datetime import datetime, timezone
from typing import List, Optional

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

from . import gmlan
from .scan import (
    VAUXHALL_ECU_TABLE,
    VAUXHALL_ID_MAP,
    _WRONG_BUS,
    _probe,
    _printable,
)

SVC_1A = gmlan.SVC_READ_DATA_BY_ID

# --------------------------------------------------------------------------- #
# GMW3110 0x1A identifiers, grouped by what the value MEANS so the decoder can
# be picked per row instead of guessing from the bytes.
#
# Each row: (identifier, label, kind, alpha_code_identifier or None)
#
# ``kind`` drives decoding:
#   "part"  4-byte big-endian GM part number, shown as the 8-digit decimal a
#           parts catalogue and SPS both use
#   "date"  3-byte BCD YY MM DD
#   "text"  printable if it reads as text, else hex
#   "raw"   hex only -- the encoding is not certain, so nothing is claimed
# --------------------------------------------------------------------------- #
SOFTWARE_IDS = [
    (0xCB, "End model part number", "part", 0xDB),
    (0xCC, "Base model part number", "part", 0xDC),
    (0xC0, "Boot software part number", "part", 0xD0),
]

# $C1..$CA SWMI 01..10 with their $D1..$DA alpha codes. These are the flashed
# blocks -- the ones that change when a calibration is updated.
SOFTWARE_MODULE_IDS = [
    (0xC0 + n, f"Software module {n:02d}", "part", 0xD0 + n) for n in range(1, 11)
]

# Context around the software: when, by whom, on what hardware.
CONTEXT_IDS = [
    (0x99, "Last programmed", "date", None),
    (0x98, "Programmed by (tester / shop code)", "text", None),
    (0x9F, "Previous programming tester codes", "text", None),
    (0x92, "System supplier id", "text", None),
    (0x97, "System name / engine type", "text", None),
    (0x9B, "XML configuration compatibility id", "text", None),
    (0x9C, "XML data file part number", "part", 0x9D),
    (0xB5, "GM broadcast code", "text", None),
    (0x90, "VIN", "text", None),
]

# ISO 14229-1 identification block, for the modules that answer plain UDS.
# Standard DIDs, so these labels are the specification's own.
UDS_SOFTWARE_DIDS = [
    (0xF187, "Manufacturer spare part number", "text"),
    (0xF188, "Manufacturer ECU software number", "text"),
    (0xF189, "Manufacturer ECU software version", "text"),
    (0xF191, "Manufacturer ECU hardware number", "text"),
    (0xF194, "Supplier ECU software number", "text"),
    (0xF195, "Supplier ECU software version", "text"),
    (0xF180, "Boot software identification", "text"),
    (0xF181, "Application software identification", "text"),
    (0xF182, "Application data identification", "text"),
    (0xF18C, "ECU serial number", "text"),
    (0xF18B, "ECU manufacturing date", "text"),
    (0xF190, "VIN", "text"),
]

# Stop asking for software modules after this many consecutive refusals. Real
# modules carry a handful, not ten, and every unanswered request costs a
# timeout on a dongle.
SWMI_MISS_LIMIT = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Decoders
# --------------------------------------------------------------------------- #
def _gm_part_number(body: bytes) -> Optional[str]:
    """4 bytes big-endian -> the 8-digit decimal GM prints on the part.

    GMW3110 stores part numbers as a 32-bit binary value rather than as text,
    which is why a raw dump of $CB looks like nothing until it is read this
    way. Anything that is not exactly 4 bytes is not a part number in this
    encoding, so the caller falls back to printable/hex rather than inventing
    a number.
    """
    if len(body) != 4:
        return None
    return str(int.from_bytes(body, "big"))


def _bcd_date(body: bytes) -> Optional[str]:
    """3 bytes BCD YY MM DD -> ISO date. None if the bytes are not valid BCD."""
    if len(body) != 3:
        return None
    parts = []
    for b in body:
        hi, lo = b >> 4, b & 0x0F
        if hi > 9 or lo > 9:
            return None
        parts.append(hi * 10 + lo)
    year, month, day = parts
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    # GMW3110 dates are two-digit years within the module's service life.
    return f"20{year:02d}-{month:02d}-{day:02d}"


def _decode(kind: str, body: bytes) -> tuple:
    """(value, confidence) for one identifier's payload."""
    if not body:
        return "", "empty"
    if kind == "part":
        part = _gm_part_number(body)
        if part is not None:
            return part, "decoded"
        return body.hex().upper(), "raw (not a 4-byte part number)"
    if kind == "date":
        date = _bcd_date(body)
        if date is not None:
            return date, "decoded"
        return body.hex().upper(), "raw (not valid BCD)"
    if kind == "text":
        value = _printable(body)
        return value, "decoded" if value and not _looks_hex(value, body) else "raw"
    return body.hex().upper(), "raw"


def _looks_hex(value: str, body: bytes) -> bool:
    return value == body.hex().upper()


def _is_blank(body: bytes) -> bool:
    """All 0x00 or all 0xFF -- an identifier the module supports but never had
    written. Reporting it as a value would read as "part number 0"."""
    return not body or all(b == 0x00 for b in body) or all(b == 0xFF for b in body)


# --------------------------------------------------------------------------- #
# GMLAN dialect
# --------------------------------------------------------------------------- #
def _read_1a(client, req_id: int, ident: int, trace: List[str]) -> Optional[bytes]:
    """Payload of one 0x1A read, or None. Never raises for a refusal."""
    try:
        reply = client.request(req_id, bytes([SVC_1A, ident]), timeout=2.0)
    except (ObdError, TransportError):
        return None
    if not reply:
        return None
    if reply[0] == 0x7F and len(reply) >= 3:
        trace.append(f"1A {ident:02X} -> NRC {reply[2]:02X} ({gmlan.nrc_name(reply[2])})")
        return None
    if len(reply) < 2 or reply[0] != SVC_1A + 0x40 or reply[1] != ident:
        return None
    return reply[2:]


def _entry(ident_label: str, ident: int, body: bytes, kind: str,
           alpha: Optional[bytes]) -> dict:
    value, confidence = _decode(kind, body)
    entry = {
        "id": f"${ident:02X}",
        "label": ident_label,
        "value": value,
        "raw": body.hex().upper(),
        "confidence": confidence,
    }
    if alpha is not None and not _is_blank(alpha):
        code = _printable(alpha)
        entry["alphaCode"] = code
        # SPS and the parts catalogue quote a calibration as number + alpha,
        # e.g. 12655424 AB, so the display string carries both.
        if confidence == "decoded" and code:
            entry["value"] = f"{value} {code}"
    return entry


def _gmlan_software(client, req_id: int, trace: List[str]) -> List[dict]:
    entries: List[dict] = []

    def read_row(ident, label, kind, alpha_id):
        body = _read_1a(client, req_id, ident, trace)
        if body is None or _is_blank(body):
            return False
        alpha = _read_1a(client, req_id, alpha_id, trace) if alpha_id else None
        entries.append(_entry(label, ident, body, kind, alpha))
        return True

    for ident, label, kind, alpha_id in SOFTWARE_IDS:
        read_row(ident, label, kind, alpha_id)

    misses = 0
    for ident, label, kind, alpha_id in SOFTWARE_MODULE_IDS:
        if read_row(ident, label, kind, alpha_id):
            misses = 0
        else:
            misses += 1
            if misses >= SWMI_MISS_LIMIT:
                trace.append(
                    f"1A {ident:02X} -> stopped after {SWMI_MISS_LIMIT} empty "
                    "software-module slots"
                )
                break

    for ident, label, kind, alpha_id in CONTEXT_IDS:
        read_row(ident, label, kind, alpha_id)

    return entries


# --------------------------------------------------------------------------- #
# UDS dialect
# --------------------------------------------------------------------------- #
def _uds_software(client, req_id: int, trace: List[str]) -> List[dict]:
    entries: List[dict] = []
    for did, label, kind in UDS_SOFTWARE_DIDS:
        try:
            reply = client.request(req_id, bytes([0x22, did >> 8, did & 0xFF]), timeout=2.0)
        except (ObdError, TransportError):
            continue
        if not reply or len(reply) < 4 or reply[0] != 0x62:
            continue
        body = reply[3:]
        if _is_blank(body):
            continue
        value, confidence = _decode(kind, body)
        if not value:
            continue
        entries.append({
            "id": f"${did:04X}",
            "label": label,
            "value": value,
            "raw": body.hex().upper(),
            "confidence": confidence,
        })
    return entries


# --------------------------------------------------------------------------- #
# Summary: the two or three lines that answer "what is in this module"
# --------------------------------------------------------------------------- #
_SUMMARY_KEYS = {
    "$CB": "endModelPartNumber",
    "$CC": "baseModelPartNumber",
    "$C0": "bootSoftware",
    "$99": "lastProgrammed",
    "$98": "programmedBy",
    "$F188": "softwareNumber",
    "$F189": "softwareVersion",
    "$F187": "sparePartNumber",
}


def _summarise(entries: List[dict]) -> dict:
    summary = {}
    for entry in entries:
        key = _SUMMARY_KEYS.get(entry["id"])
        if key and entry["value"]:
            summary[key] = entry["value"]
    modules = [e["value"] for e in entries if e["label"].startswith("Software module")]
    if modules:
        summary["softwareModules"] = modules
    return summary


def _headline(module: dict) -> str:
    """One line per module for the chip list and the worksheet."""
    summary = module.get("summary") or {}
    bits = []
    for key, prefix in (
        ("endModelPartNumber", "end model"),
        ("softwareNumber", "software"),
        ("softwareVersion", "version"),
    ):
        if summary.get(key):
            bits.append(f"{prefix} {summary[key]}")
    count = len(summary.get("softwareModules") or [])
    if count:
        bits.append(f"{count} software module{'s' if count != 1 else ''}")
    if summary.get("lastProgrammed"):
        bits.append(f"last flashed {summary['lastProgrammed']}")
    if not bits:
        bits.append("answered, but reported no software identifiers")
    return " · ".join(bits)


def _worksheet(modules: List[dict], vin: Optional[str]) -> str:
    """A plain-text block to sit next to an SPS calibration lookup.

    Deliberately plain: this gets pasted into a notes field or a forum post and
    compared line by line against what the manufacturer's tool offers for the
    same VIN.
    """
    lines = ["Vauxhall / Opel (GM Global A) module software as read from the car",
             f"VIN: {vin or 'not reported'}",
             f"Read: {_now()}",
             ""]
    for module in modules:
        if not module.get("present"):
            continue
        lines.append(f"{module['code']}  {module['name']}  [{module['dialect']}]")
        if not module.get("entries"):
            lines.append("    (no software identifiers returned)")
        for entry in module["entries"]:
            note = "" if entry["confidence"] == "decoded" else f"   <- {entry['confidence']}"
            lines.append(f"    {entry['id']:<6} {entry['label']:<44} {entry['value']}{note}")
        lines.append("")
    lines.append(
        "Compare against the calibrations the manufacturer's own programming "
        "system offers for this VIN. Reading these numbers changed nothing on "
        "the car; writing them is not something this harness does."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# versions() -- the capability the API and MCP expose
# --------------------------------------------------------------------------- #
def versions(adapter: Optional[dict], mode: str = "real") -> dict:
    """Read every module's flashed software identification. Read-only."""
    if mode == "sim":
        return _simulated()

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "vehicle": None, "modules": [],
                "readings": [], "worksheet": "",
                "message": f"The car answered {session.protocol or 'an unknown protocol'}, not "
                           + _WRONG_BUS,
                "timestamp": _now(),
            }

        obd_vin = obd_service._read_vin(session)
        client = Elm327UdsClient(session, VAUXHALL_ID_MAP)
        client.begin()
        modules: List[dict] = []
        try:
            for req_id, resp_id, code, name, desc in VAUXHALL_ECU_TABLE:
                trace: List[str] = []
                module = {
                    "address": f"0x{req_id:03X}", "response": f"0x{resp_id:03X}",
                    "code": code, "name": name, "description": desc,
                    "present": False, "dialect": None, "entries": [],
                    "summary": {}, "error": None, "trace": trace,
                }
                hit = _probe(client, req_id, trace)
                if hit is None:
                    modules.append(module)
                    continue
                module["present"] = True

                # GMLAN first -- the expected Global A dialect -- then UDS. The
                # reported dialect is whichever actually produced identifiers,
                # not whatever probing guessed.
                order = (("uds", _uds_software), ("gmlan", _gmlan_software)) \
                    if hit[1] == "uds" else \
                    (("gmlan", _gmlan_software), ("uds", _uds_software))
                for dialect, reader in order:
                    try:
                        entries = reader(client, req_id, trace)
                    except (ObdError, TransportError) as exc:
                        module["error"] = f"{dialect.upper()} identification failed: {exc}"
                        continue
                    if entries:
                        module["entries"] = entries
                        module["summary"] = _summarise(entries)
                        module["dialect"] = dialect
                        module["error"] = None
                        break
                if module["dialect"] is None:
                    module["dialect"] = "unproven"
                modules.append(module)
        finally:
            client.restore()

        present = [m for m in modules if m["present"]]
        with_software = [m for m in present if m["entries"]]
        vin = next(
            (e["value"][:17] for m in modules for e in m["entries"]
             if e["label"] == "VIN" and len(e["value"]) >= 11),
            None,
        ) or obd_vin

        readings = [
            {"name": f"{m['code']} {m['name']}", "value": _headline(m), "unit": "",
             "description": f"Read over {m['dialect']} at {m['address']}."}
            for m in present
        ]

        tcm = next((m for m in present if m["code"] == "7E1"), None)
        gearbox_note = ""
        if tcm and tcm["entries"]:
            gearbox_note = (
                " Gearbox (TCM, 0x7E1): " + _headline(tcm) + "."
            )
        elif tcm:
            gearbox_note = (
                " The gearbox module answered but returned no software identifiers."
            )

        return {
            "ok": True, "simulated": False,
            "vehicle": {"vin": vin,
                        "protocol": session.protocol or "ISO 15765-4 (CAN 11/500)",
                        "adapter": (adapter or {}).get("name")},
            "modules": modules,
            "readings": readings,
            "worksheet": _worksheet(modules, vin),
            "message": (
                (f"VIN {vin}. " if vin else "")
                + f"{len(with_software)} of {len(present)} answering module(s) reported "
                "flashed software identification."
                + gearbox_note
                + " Part numbers are GMW3110 4-byte values shown as the decimal GM "
                "quotes, with their two-character alpha code where the module has "
                "one. Take these to the manufacturer's programming system to find "
                "out whether a newer calibration exists for this VIN. Read-only -- "
                "nothing was written to the car."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "vehicle": None, "modules": [],
                "readings": [], "worksheet": "", "message": str(exc),
                "timestamp": _now()}


def _simulated() -> dict:
    def module(code, name, address, response, entries):
        mod = {
            "address": address, "response": response, "code": code, "name": name,
            "description": "", "present": True, "dialect": "gmlan",
            "entries": entries, "summary": _summarise(entries), "error": None,
            "trace": [],
        }
        return mod

    ecm = module("7E0", "Engine control (ECM)", "0x7E0", "0x7E8", [
        {"id": "$CB", "label": "End model part number", "value": "12655424 AB",
         "raw": "00C11B40", "confidence": "decoded", "alphaCode": "AB"},
        {"id": "$C0", "label": "Boot software part number", "value": "12643636",
         "raw": "00C0ED34", "confidence": "decoded"},
        {"id": "$C1", "label": "Software module 01", "value": "12655430",
         "raw": "00C11B46", "confidence": "decoded"},
        {"id": "$99", "label": "Last programmed", "value": "2017-03-14",
         "raw": "170314", "confidence": "decoded"},
    ])
    tcm = module("7E1", "Transmission (TCM)", "0x7E1", "0x7E9", [
        {"id": "$CB", "label": "End model part number", "value": "24268431 AC",
         "raw": "01724E8F", "confidence": "decoded", "alphaCode": "AC"},
        {"id": "$C1", "label": "Software module 01", "value": "24268440",
         "raw": "01724E98", "confidence": "decoded"},
        {"id": "$C2", "label": "Software module 02", "value": "24268455",
         "raw": "01724EA7", "confidence": "decoded"},
        {"id": "$99", "label": "Last programmed", "value": "2016-11-02",
         "raw": "161102", "confidence": "decoded"},
        {"id": "$98", "label": "Programmed by (tester / shop code)",
         "value": "SIMULATED", "raw": "53494D", "confidence": "decoded"},
    ])
    modules = [ecm, tcm]
    readings = [
        {"name": f"{m['code']} {m['name']}", "value": _headline(m), "unit": "",
         "description": f"SIMULATED -- read over {m['dialect']} at {m['address']}."}
        for m in modules
    ]
    return {
        "ok": True, "simulated": True,
        "vehicle": {"vin": "SIMULATEDVAUXHALL",
                    "protocol": "ISO 15765-4 (CAN 11/500)", "adapter": "Simulated"},
        "modules": modules,
        "readings": readings,
        "worksheet": _worksheet(modules, "SIMULATEDVAUXHALL"),
        "message": "SIMULATED -- no real car connected. Part numbers here are made up.",
        "timestamp": _now(),
    }


# --------------------------------------------------------------------------- #
# WHY THERE IS NO FLASH FUNCTION HERE
#
# Anyone who reads this file is the person who would otherwise try to add one,
# so the reasons are recorded rather than left to be rediscovered:
#
# 1. The transport cannot do it. Reprogramming a GM module means holding it in
#    its bootloader while $34 RequestDownload / $36 TransferData / $37
#    RequestTransferExit move on the order of a megabyte with tight inter-frame
#    timing and a live watchdog. An ELM327 is an ASCII request/response bridge
#    with no frame scheduler and no useful throughput for that. It is not a
#    matter of writing better code against it.
# 2. The calibration is not obtainable here. GM serves calibrations per VIN
#    from its own programming system under a paid subscription. There is no
#    public feed of Global A calibration binaries, and a binary of unknown
#    provenance flashed into a transmission controller is how a module gets
#    bricked or mismatched to its hardware variant.
# 3. It is gated anyway. Programming needs SecurityAccess ($27) with a
#    per-module GM-proprietary seed-to-key algorithm. Without it every
#    programming request returns NRC 0x33.
#
# The honest surface is therefore: READ what is in the car (this file), MEASURE
# how it behaves (pids.py), DOCUMENT what the write would involve (resets.py),
# and leave the write to the tool that owns the calibration and the transport.
# --------------------------------------------------------------------------- #
