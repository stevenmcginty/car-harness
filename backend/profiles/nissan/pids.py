"""Nissan candidate diagnostic-node map + the audio (Bose amp) DTC text table.

Scope of THIS file: AUDIO / AV diagnosis only. It is deliberately kept
audio/scan-scoped so a separate keys.py / nats.py (immobiliser work, owned by
another session) can be dropped into this package later without touching
anything here.

Nissan Z34-era (2016 370Z / NISMO, UK) uses ISO 15765 (CAN, 11-bit) UDS/KWP.
Two addressing conventions live on the same bus:

  * Powertrain answers the standard ISO 15765-4 way -- physical requests
    0x7E0-0x7E7 answered on request + 8 (0x7E8-0x7EF), functional 0x7DF.
  * Body / comfort / AV modules use Nissan's own convention: request in the
    0x74x range, answered on request + 0x20 (0x76x). This is the widely
    documented pattern -- e.g. the Z34 BCM at 0x745 request / 0x765 response.

CRUCIAL UNVERIFIED POINT -- the AV / head unit ("AV control unit" / "display
unit" / "Multi-AV") is the node that STORES the Bose-amp fault codes (the amp
itself is not an independently addressable diagnostic node and has no DTC table
of its own). Its exact diagnostic CAN request/response id was NOT recoverable
from our mined dealer data (the vehicle->system map shipped as a zero-byte
placeholder), and it is not published anywhere we could confirm -- only the
car's broadcast frame ids are documented, which are a different thing. So we do
NOT hard-code one address: we sweep the whole Nissan body/AV block (0x740-0x74F)
and let discover()/audio_faults() report whichever node actually answers and
carries the amp codes. Every body-block entry is therefore flagged is_av=True
so audio_faults() reads them all. Our leading (still UNVERIFIED) guess for the
AV unit is 0x74D request / 0x76D response -- see the note on that row.

Each table row: (req_id, resp_id, code, name, is_av, note).
Read-only data module -- no transport here.
"""

from datetime import datetime, timezone
from typing import Dict, List, Tuple

# (req_id, resp_id, code, name, is_av, note)
NISSAN_ECU_TABLE: List[Tuple[int, int, str, str, bool, str]] = [
    # ---- Standard ISO 15765-4 powertrain block (response = request + 8) -----
    (0x7DF, 0x7E8, "FUNC", "Functional broadcast (any powertrain ECU answers)",
     False, "ISO 15765-4 functional address -- use only to prove the bus is alive."),
    (0x7E0, 0x7E8, "ECM", "Engine control module (VQ37VHR)", False, "Standard powertrain."),
    (0x7E1, 0x7E9, "TCM", "Transmission control (7AT, if fitted)", False, "Standard powertrain."),
    (0x7E2, 0x7EA, "ECU @ 0x7E2", "HS-CAN powertrain module", False, ""),
    (0x7E3, 0x7EB, "ECU @ 0x7E3", "HS-CAN powertrain module", False, ""),
    (0x7E4, 0x7EC, "ECU @ 0x7E4", "HS-CAN powertrain module", False, ""),
    (0x7E5, 0x7ED, "ECU @ 0x7E5", "HS-CAN powertrain module", False, ""),
    (0x7E6, 0x7EE, "ECU @ 0x7E6", "HS-CAN powertrain module", False, ""),
    (0x7E7, 0x7EF, "ECU @ 0x7E7", "HS-CAN powertrain module", False, ""),
    # ---- Nissan body / comfort / AV block (response = request + 0x20) --------
    # The AV control unit that holds the Bose-amp DTCs lives somewhere in this
    # block; the exact id is UNVERIFIED, so every row is flagged is_av=True and
    # audio_faults() reads all of them. Names on rows we could reason about are
    # best-effort; a row that does not answer just times out as "not present".
    (0x740, 0x760, "ABS/VDC", "ABS / stability control", True,
     "UNVERIFIED name (Nissan body convention resp = req + 0x20)."),
    (0x741, 0x761, "ECU @ 0x741", "Nissan body/comfort module", True, "UNVERIFIED."),
    (0x742, 0x762, "EPS", "Electric power steering (if fitted)", True, "UNVERIFIED name."),
    (0x743, 0x763, "METER", "Combination meter (instrument cluster)", True, "UNVERIFIED name."),
    (0x744, 0x764, "HVAC", "Air-conditioning / climate control", True, "UNVERIFIED name."),
    (0x745, 0x765, "BCM", "Body control module", True,
     "Nissan Z34 BCM diagnostic id is documented as 0x745 req / 0x765 resp."),
    (0x746, 0x766, "SRS", "Airbag / occupant classification", True, "UNVERIFIED name."),
    (0x747, 0x767, "ECU @ 0x747", "Nissan body/comfort module", True, "UNVERIFIED."),
    (0x748, 0x768, "ECU @ 0x748", "Nissan body/comfort module", True, "UNVERIFIED."),
    (0x749, 0x769, "ECU @ 0x749", "Nissan body/comfort module", True, "UNVERIFIED."),
    (0x74A, 0x76A, "ECU @ 0x74A", "Nissan body/comfort module", True, "UNVERIFIED."),
    (0x74B, 0x76B, "ECU @ 0x74B", "Nissan body/comfort module", True, "UNVERIFIED."),
    (0x74C, 0x76C, "ECU @ 0x74C", "Nissan body/comfort module", True, "UNVERIFIED."),
    (0x74D, 0x76D, "AV?", "AV control unit / head unit (holds Bose-amp DTCs)", True,
     "UNVERIFIED BEST GUESS for the AV/head unit. This is the node we EXPECT to "
     "carry B1F00-49 / U1263, but the id could not be confirmed -- trust whatever "
     "audio_faults() actually finds, not this label."),
    (0x74E, 0x76E, "ECU @ 0x74E", "Nissan body/comfort module", True, "UNVERIFIED."),
    (0x74F, 0x76F, "ECU @ 0x74F", "Nissan body/comfort module", True, "UNVERIFIED."),
]

# Codes we treat as an amp / Bose fault when deciding the audio_faults() flag.
# Matched on the bare 5-char code (the "-NN" suffix is the ISO failure-type byte).
AMP_FAULT_CODES = {"B1F00", "U1263"}

# Known audio/amp DTC text held by the AV control unit. Keys accept both the
# dealer "code-failuretype" form and the bare code. Entries marked UNVERIFIED
# are reasoned from Nissan CAN-comms conventions, not confirmed against dealer
# data, and are harmless -- a code the car does not store is simply never shown.
AUDIO_DTCS: Dict[str, str] = {
    # ---- sourced this session (mined dealer data) ---------------------------
    "B1F00-49": "BOSE AMP",
    "B1F00": "BOSE AMP",
    "U1263": "Bose amp communication line to AV control unit",
    "U1263-00": "Bose amp communication line to AV control unit",
    # ---- UNVERIFIED (general Nissan CAN-comms codes an AV unit may store) ----
    "U1000": "CAN communication circuit",            # UNVERIFIED
    "U1000-49": "CAN communication circuit",          # UNVERIFIED
    "U1001": "CAN communication circuit (lost comms)",  # UNVERIFIED
    "U1010": "Control unit (CAN) initialisation",     # UNVERIFIED
}


def id_map() -> Dict[int, int]:
    """request-id -> response-id for Elm327UdsClient, so 0x74x body nodes are
    read on request + 0x20 instead of the default request + 8."""
    return {req: resp for req, resp, *_ in NISSAN_ECU_TABLE}


def av_candidates() -> List[Tuple[int, int, str, str, str]]:
    """The body/AV block rows audio_faults() sweeps: (req, resp, code, name, note)."""
    return [(req, resp, code, name, note)
            for req, resp, code, name, is_av, note in NISSAN_ECU_TABLE if is_av]


def dtc_text(code: str, failure_type: str = "") -> str:
    """AV/amp DTC text for a code (+ optional failure-type byte), or '' if unknown."""
    if failure_type:
        hit = AUDIO_DTCS.get(f"{code}-{failure_type}")
        if hit:
            return hit
    return AUDIO_DTCS.get(code, "")


def is_amp_fault(code: str) -> bool:
    """True if this bare code is an amp / Bose fault (drives the audio_faults flag)."""
    return code in AMP_FAULT_CODES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
