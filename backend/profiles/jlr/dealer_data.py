"""The decrypted SDD 169 dealer data, loaded as a live reference layer.

data/sdd_dids.json is the authoritative extract from Jaguar Land Rover's own
SDD 169 dealer database (3DES-ECB decryption method and key recorded in the
file's provenance block; reproducible with backend/tools/jlr_sdd_decrypt.py).
It holds three things:

    * the 57-module L494 MY16 CAN address map -- what actually lives at each
      request id, so a discovery sweep can print "ABS" instead of "0x7E6",
    * 37 diesel-PCM (306DT / AJD-V6D) data identifiers with SDD's own names,
      byte sizes, scaling and units,
    * 24 PCM/SCR routine identifiers with their names.

Until this module existed the file was documentation: nothing loaded it, so
none of it reached either screen. This is the loader that puts it on screen.

CONFIDENCE GRADES
-----------------
Same three-rung convention as the Vauxhall Astra-K TCM map
(backend/profiles/vauxhall/tcm.py), worded for what SDD can and cannot settle:

    verified    SDD names it AND gives scaling AND the decoded value was
                cross-checked against the live L494 SALWA2EK8HA capture.
                Safe to display as a plain number with its unit.
    candidate   SDD's NAME is authoritative -- it comes from the manufacturer's
                own database -- but the value is not proven on this car: either
                SDD publishes no scaling for it, or the scaling has never been
                checked against a real reading. Displayed WITH the raw payload
                and marked, never as a bare confident number. The urea/SCR
                cluster 05FA-05FD sits here.
    unknown     Answers on the car, nothing names it. Logged raw. Not this
                module's business -- external_pids.json carries those.

WHAT THIS MODULE WILL NOT DO
----------------------------
The routine identifiers are reference only. They are the first published
SCR/DPF/EGR actuation ids the harness has ever had for this family, and the
harness still sends none of them: they are listed so Steve can see what the
dealer tool would do, and nothing here can be pressed to make the car do it.

Absence from SDD does NOT mean a DID is dead -- SDD names a curated 37 of the
~191 the PCM physically answers. That caveat rides with the reference output.
"""

import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

_DATA_PATH = Path(__file__).resolve().parent / "data" / "sdd_dids.json"


def _load() -> dict:
    """Defensive load, exactly like pids.py's external-PID loader: a missing or
    corrupt file degrades to an empty reference layer, never a broken import."""
    try:
        with open(_DATA_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


SDD = _load()
PROVENANCE: dict = SDD.get("provenance", {})
NOTES: List[str] = SDD.get("notes", [])

# The extract is L494 MY16, and the X260 MY16 diesel PCM was confirmed in the
# same extraction to share 54 of 57 identifiers -- but the ADDRESS map is the
# L494's, so every label says which platform it came from rather than implying
# it holds for every JLR.
PLATFORM = "L494 MY16"


# ---- module address map ---------------------------------------------------

def _load_modules() -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    for key, row in (SDD.get("module_address_map_L494_MY16") or {}).items():
        try:
            req = int(row.get("request", key), 16)
            resp = int(row["response"], 16)
        except (KeyError, TypeError, ValueError):
            continue
        out[req] = {"module": row.get("module", ""), "request": req, "response": resp}
    return out


MODULE_MAP: Dict[int, dict] = _load_modules()


def module_code(req_id: int) -> str:
    """The SDD module code at a request id ("ABS", "DSM", "TPM"), or ""."""
    return (MODULE_MAP.get(req_id) or {}).get("module", "")


def module_label(req_id: int) -> str:
    """A display label for a swept address, or "" when SDD does not know it.

    The platform rides in the label on purpose: this is the L494's map, and a
    Freelander 2 answering the same id is not promised the same module."""
    code = module_code(req_id)
    return f"{code} — {PLATFORM} address map" if code else ""


# ---- diesel-PCM data identifiers -------------------------------------------

def _u(body: bytes, size: int) -> Optional[int]:
    if len(body) < size:
        return None
    v = 0
    for b in body[:size]:
        v = (v << 8) | b
    return v


def _signed(value: int, size: int) -> int:
    top = 1 << (size * 8 - 1)
    return value - (top << 1) if value >= top else value


def _fmt(v: float) -> str:
    """Scaled values print at the precision the resolution justifies and no
    more -- a 0.00152590219 g/l resolution does not make a soot figure exact."""
    if v == int(v):
        return str(int(v))
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _scaler(size: int, signed: bool, resolution: float,
            offset: float) -> Callable[[bytes], Optional[str]]:
    def decode(body: bytes) -> Optional[str]:
        raw = _u(body, size)
        if raw is None:
            return None
        if signed:
            raw = _signed(raw, size)
        return _fmt(raw * resolution + offset)
    return decode


# DIDs whose SDD scaling was checked against a real payload from the live L494
# SALWA2EK8HA sweep (captures/jlr/car-sweep-*.json). Everything else that has
# scaling is a "candidate": SDD's arithmetic, not yet seen produce a sane
# number on a car.
_CROSS_CHECKED = {0x0396, 0xF405}

# Named DIDs whose live payload on SALWA2EK8HA was recorded in the extract.
# Presence of this string is what lets the reference view say "answers on the
# validation car" rather than leaving Steve to guess.
_LIVE_KEY = "live_L494_SALWA2EK8HA"


class SddDid:
    """One SDD-named diesel-PCM identifier, with whatever SDD could settle.

    ``decode`` is None when SDD publishes no scaling for the row -- the urea
    cluster, the injector-code blobs, the regen statistics blocks. Those are
    displayed raw, with their SDD name, and marked candidate. Nothing is
    invented to fill the gap."""

    def __init__(self, row: dict):
        self.did: int = int(row["did"], 16)
        self.name: str = _clean_name(row.get("name")) or f"SDD DID {row['did']}"
        self.byte_size: Optional[int] = _int_or_none(row.get("byte_size"))
        self.data_type: str = (row.get("data_type") or "").lower()
        self.units: str = _clean_units(row.get("units") or "")
        self.live: str = row.get(_LIVE_KEY, "")
        self.answers_on_l494: bool = self.live.startswith("answered")
        self.resolution: Optional[float] = _float_or_none(row.get("resolution"))
        self.offset: float = _float_or_none(row.get("offset")) or 0.0

        scalable = (
            self.resolution is not None
            and self.byte_size is not None
            and 1 <= self.byte_size <= 4
            and self.data_type in ("unsigned", "signed")
        )
        self.decode: Optional[Callable[[bytes], Optional[str]]] = (
            _scaler(self.byte_size, self.data_type == "signed",
                    self.resolution, self.offset)
            if scalable else None
        )
        self.confidence: str = (
            "verified" if (self.decode is not None and self.did in _CROSS_CHECKED)
            else "candidate"
        )

    @property
    def hex(self) -> str:
        return f"{self.did:04X}"

    def label(self) -> str:
        """Display name. A candidate carries its grade in the label so the
        number can never be read as settled (the Vauxhall tcm.py convention)."""
        return self.name if self.confidence == "verified" else f"{self.name} [candidate]"

    def describe(self) -> str:
        """The provenance sentence that rides with every reading."""
        bits = [f"SDD 169 diesel-PCM DID {self.hex} ({PLATFORM})"]
        if self.byte_size:
            bits.append(f"{self.byte_size} byte(s)")
        if self.decode is not None:
            bits.append(f"SDD scaling ×{self.resolution} offset {self.offset:g}"
                        + (f" {self.units}" if self.units else ""))
        else:
            bits.append("SDD publishes no scaling for this row — payload shown raw")
        if self.live:
            bits.append(f"live L494 SALWA2EK8HA: {self.live}")
        bits.append(f"confidence: {self.confidence}")
        return ". ".join(bits) + "."


def _int_or_none(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_TM_RE = re.compile(r"<tm\b[^>]*>(.*?)</tm>", re.DOTALL)


def _clean_name(name) -> str:
    """SDD wraps some labels in its own translation markup
    (``<tm id="@J_I_ENG_INPUT_OUTPUT_PARAMETERS">Engine input/output
    parameters</tm>``). Keep the human text, drop the tag -- the raw markup on
    a phone card is worse than useless."""
    if not name:
        return ""
    text = _TM_RE.sub(r"\1", str(name))
    return re.sub(r"<[^>]+>", "", text).strip()


def _clean_units(units: str) -> str:
    """SDD writes units as prose ("torque, newton metre, Nm", "Deg C"). Keep
    the last comma-separated token, which is the symbol, and normalise the
    couple of spellings SDD uses for degrees Celsius."""
    token = units.split(",")[-1].strip()
    return {"Deg C": "°C", "degC": "°C"}.get(token, token)


def _load_dids() -> Dict[int, SddDid]:
    out: Dict[int, SddDid] = {}
    for row in SDD.get("pcm_diesel_dids") or []:
        try:
            entry = SddDid(row)
        except (KeyError, TypeError, ValueError):
            continue
        out[entry.did] = entry
    return out


PCM_DIDS: Dict[int, SddDid] = _load_dids()


def did_name(did: int) -> str:
    """SDD's own name for a diesel-PCM DID, or "" when SDD does not name it."""
    entry = PCM_DIDS.get(did)
    return entry.name if entry else ""


def did_label(did: int) -> str:
    """SDD's name with its confidence grade, or "" when SDD does not name it."""
    entry = PCM_DIDS.get(did)
    return entry.label() if entry else ""


def decode_did(did: int, body: bytes) -> Tuple[Optional[str], str, str]:
    """(decoded value or None, unit, confidence) for a raw payload.

    None as the decoded value is the honest answer for every row SDD names but
    does not scale -- the caller shows the raw hex and says so."""
    entry = PCM_DIDS.get(did)
    if entry is None or entry.decode is None:
        return None, "", "candidate" if entry else "unknown"
    try:
        return entry.decode(body), entry.units, entry.confidence
    except Exception:
        return None, entry.units, entry.confidence


# The urea/SCR cluster: SDD-named, PCM-managed (there is no separate SCR ECU on
# L494), live-answering on SALWA2EK8HA, and completely unscaled. This is the
# next real-world calibration target -- 05FC is a monotonic total, so it is the
# one that can be pinned by watching it climb over a warm drive.
UREA_DIDS: List[int] = [0x05FA, 0x05FB, 0x05FC, 0x05FD]
UREA_WATCH_NOTE = (
    "The urea/SCR cluster 05FA-05FD is SDD-named and PCM-managed (there is no separate "
    "SCR controller on L494). SDD publishes no scaling for any of the four, so every "
    "payload stays raw: name authoritative, number NOT. 05FC 'total mass fluid injected "
    "by urea dosing system' is a monotonic total — log it warm over a short drive and "
    "the rate it climbs is what pins the scaling."
)


def unscaled_int(body: bytes) -> Optional[int]:
    """The payload read as one big-endian unsigned integer.

    This is NOT a decode -- it claims no unit and no scaling, it is the same
    bytes written in base 10 so a monotonic counter can be seen to count. The
    urea rows use it beside their raw hex."""
    if not body or len(body) > 8:
        return None
    return _u(body, len(body))


# ---- routine identifiers ---------------------------------------------------

class SddRoutine:
    """One PCM/SCR routine identifier. Reference only -- see the module header."""

    def __init__(self, row: dict):
        self.routine: int = int(row["routine"], 16)
        self.name: str = row.get("name") or f"Routine {row['routine']}"

    @property
    def hex(self) -> str:
        return f"{self.routine:04X}"


def _load_routines() -> List[SddRoutine]:
    out: List[SddRoutine] = []
    for row in SDD.get("pcm_diesel_routines") or []:
        try:
            out.append(SddRoutine(row))
        except (KeyError, TypeError, ValueError):
            continue
    return out


ROUTINES: List[SddRoutine] = _load_routines()

# The aftertreatment-relevant subset, called out because these are the ones the
# AdBlue/DPF job cares about and the ones no public source had before.
_AFTERTREATMENT_ROUTINES = {0x4026, 0x4051, 0x4050, 0x4076, 0x4094, 0x4095, 0x4096, 0x402C}

ROUTINES_NOTE = (
    "Reference only. These are the real routine identifiers SDD sends for this engine "
    "family — the first published SCR/DPF/EGR actuation ids the harness has for a 306DT "
    "— and the harness sends NONE of them. They are listed so you can see what the "
    "dealer tool would do; running one needs SDD or an equivalent, not this app."
)


# ---- the reference view ----------------------------------------------------

def _module_readings() -> List[dict]:
    rows = []
    for req in sorted(MODULE_MAP):
        entry = MODULE_MAP[req]
        rows.append({
            "name": f"0x{req:03X} → 0x{entry['response']:03X}",
            "value": entry["module"],
            "unit": "",
            "description": f"{PLATFORM} CAN address map, decrypted from SDD 169.",
        })
    return rows


def _did_readings() -> List[dict]:
    rows = []
    for did in sorted(PCM_DIDS):
        entry = PCM_DIDS[did]
        if entry.decode is not None:
            value = (f"scaled ×{entry.resolution}"
                     + (f" {entry.units}" if entry.units else ""))
        else:
            value = "raw — SDD gives no scaling"
        if entry.answers_on_l494:
            value += " · answers on SALWA2EK8HA"
        elif entry.live:
            value += " · dead on SALWA2EK8HA"
        rows.append({
            "name": f"DID {entry.hex} · {entry.label()}",
            "value": value,
            "unit": "",
            "description": entry.describe(),
        })
    return rows


def _routine_readings() -> List[dict]:
    rows = []
    for entry in sorted(ROUTINES, key=lambda r: r.routine):
        rows.append({
            "name": f"Routine {entry.hex}"
                    + (" ★" if entry.routine in _AFTERTREATMENT_ROUTINES else ""),
            "value": entry.name,
            "unit": "",
            "description": ROUTINES_NOTE,
        })
    return rows


def _worksheet() -> str:
    lines = [f"JLR SDD 169 reference — {PLATFORM}", ""]
    lines.append(f"MODULE ADDRESS MAP ({len(MODULE_MAP)} modules)")
    for req in sorted(MODULE_MAP):
        entry = MODULE_MAP[req]
        lines.append(f"  0x{req:03X} -> 0x{entry['response']:03X}  {entry['module']}")
    lines.append("")
    lines.append(f"DIESEL-PCM DATA IDENTIFIERS ({len(PCM_DIDS)} named by SDD)")
    for did in sorted(PCM_DIDS):
        entry = PCM_DIDS[did]
        scaling = (f"x{entry.resolution} offset {entry.offset:g} {entry.units}".strip()
                   if entry.decode is not None else "no SDD scaling - raw")
        lines.append(f"  {entry.hex}  [{entry.confidence}] {entry.name}")
        lines.append(f"        {entry.byte_size or '?'} byte(s), {scaling}"
                     + (f", live: {entry.live}" if entry.live else ""))
    lines.append("")
    lines.append(f"PCM / SCR ROUTINE IDENTIFIERS ({len(ROUTINES)}) - reference only, never sent")
    for entry in sorted(ROUTINES, key=lambda r: r.routine):
        star = " *" if entry.routine in _AFTERTREATMENT_ROUTINES else ""
        lines.append(f"  {entry.hex}  {entry.name}{star}")
    lines.append("")
    for note in NOTES:
        lines.append(f"NOTE: {note}")
    return "\n".join(lines)


def reference(adapter=None, mode: str = "real") -> dict:
    """The whole SDD layer as panel data. Reads nothing from the car.

    Signature matches the other brand actions (adapter, mode) so main.py and
    the MCP layer dispatch to it the same way, but both arguments are ignored:
    this is a data view, and it is identical with or without a car attached.
    That is stated in the message rather than left to be discovered."""
    from datetime import datetime, timezone

    ok = bool(MODULE_MAP or PCM_DIDS or ROUTINES)
    readings = [{
        "name": "Source",
        "value": f"SDD 169 dealer database, decrypted ({PLATFORM})",
        "unit": "",
        "description": PROVENANCE.get("encryption", ""),
    }, {
        "name": "Coverage",
        "value": f"{len(MODULE_MAP)} modules · {len(PCM_DIDS)} PCM DIDs · "
                 f"{len(ROUTINES)} routines",
        "unit": "",
        "description": "SDD names a curated subset: the diesel PCM physically answers "
                       "~191 DIDs, SDD names 37 of them. Absence here does NOT mean a "
                       "DID is dead.",
    }, {
        "name": "Urea / SCR cluster",
        "value": "05FA-05FD — named, unscaled, live",
        "unit": "",
        "description": UREA_WATCH_NOTE,
    }]
    readings += _module_readings() + _did_readings() + _routine_readings()

    message = (
        f"SDD 169 reference: the {len(MODULE_MAP)}-module {PLATFORM} CAN address map, "
        f"{len(PCM_DIDS)} diesel-PCM data identifiers with SDD's own names and scaling, "
        f"and {len(ROUTINES)} PCM/SCR routine identifiers. Rows marked [candidate] have an "
        "authoritative SDD name but a value this car has not proved — they are shown raw "
        "beside any decode, never as a settled number. Routines are reference only and are "
        "never sent. No car contact: this view is the same with the dongle unplugged."
        if ok else
        "data/sdd_dids.json could not be read — the SDD reference layer is empty. "
        "Nothing is invented to fill it."
    )
    return {
        "ok": ok,
        "simulated": False,
        "modules": len(MODULE_MAP),
        "dids": len(PCM_DIDS),
        "routines": len(ROUTINES),
        "readings": readings,
        "worksheet": _worksheet(),
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
