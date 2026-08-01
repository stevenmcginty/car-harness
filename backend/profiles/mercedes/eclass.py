"""Mercedes-Benz E-Class (W/S 213) hybrid enhanced live values over a plain ELM327.

This is the Mercedes counterpart of the Porsche Taycan DID work (docs/private/
mercedes-research.md §4b): the E-Class answers a small, published UDS ``0x22``
DID library on 11-bit CAN *at the OBD port*, so a dealer can read chassis and
mild-hybrid telemetry over an ordinary VeePeak-style dongle -- no STAR/XENTRY
cable needed. (Body modules like ABS/ESP, SRS and the SAM remain STAR-only; these
DIDs live on the engine-side 0x7E2/0x7E5 pair the filtering gateway passes.)

PROVENANCE. Every value below is transcribed from the OBDb Mercedes-Benz-E-Class
repo (CC-BY-SA-4.0 -- see the bundled ``obdb_eclass_signalset.json`` NOTICE) and
each of its decodes was DERIVED and verified against every on-car test vector in
``tests/test_cases/{2018,2021}/commands/`` (verify_decodes.py: 520 checks, all
pass). That is a VERIFIED decode table for the E-Class mild-hybrid specifically.
The full raw capture library stays in docs/private/ for later work; the bundled
JSON is the curated, decoded subset that ships here.

TRANSPORT. The hybrid DIDs answer on ECU 0x7E5 (responder 0x7ED); the chassis
group (wheel speeds, steering, accelerations, brake pressures) answers on 0x7E2
(responder 0x7EA). Both are the standard request+8 pairing, so no id_map is
needed. The request is plain UDS ``22 <did_hi> <did_lo>`` with a 2-byte DID and
the reply echoes ``62 <did_hi> <did_lo>``.

A single cheap probe DID gates the whole set (``_GATE``, the hybrid HV battery
SoC on 0x7E5): on a non-hybrid Mercedes the probe returns nothing and the caller
falls straight through to its generic extras, paying one short timeout, not
seven.

NOT WIRED: 226053 ``MERCEDES_HV_A`` (HV current) is left out -- the only OBDb
vector for it is all-zero, so its scale is unproven and nothing is invented.
"""

from typing import Callable, List, Optional, Tuple

from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError

# The gate: the E-Class mild-hybrid HV battery state-of-charge DID on the hybrid
# ECU 0x7E5 (standard +8 responder 0x7ED). Only a hybrid answers it; a non-hybrid
# pays one short timeout and the caller falls through to the generic extras.
_GATE = (0x7E5, 0x6050)

# Request CAN id -> response CAN id. Both ECU pairs follow the standard +8
# pairing (0x7E5->0x7ED, 0x7E2->0x7EA), so no override is needed -- kept as a
# comment so it is explicit.
# ID_MAP = {}

# ---- one decoded value: bit-slice of the response body, then the formula
# scaled = raw * mul / div + add, two's-complement when ``sign``. (name, unit,
# bit-index, bit-length, sign, mul, div, add, decimals, lo, hi) -- lo/hi are
# sanity bounds; a value outside them is dropped rather than shown.
Field = Tuple[str, str, int, int, bool, float, float, float, int, float, float]

# ---- the curated E-Class hybrid live set, grouped by (request id, DID) so one
# UDS read yields every value packed in that response. Each cites its OBDb
# signal id. (req_id, did, [Field, ...])
ECLASS_DIDS: List[Tuple[int, int, List[Field]]] = [
    # Chassis / 0x7E2 ----------------------------------------------------------
    (0x7E2, 0x2001, [  # MERCEDES_{FL,FR,RL,RR}_VSS (== TIRE_*_VSS twins)
        ("Wheel speed, front-left", "m/s", 0, 16, False, 9, 160, 0, 2, 0, 120),
        ("Wheel speed, front-right", "m/s", 16, 16, False, 9, 160, 0, 2, 0, 120),
        ("Wheel speed, rear-left", "m/s", 32, 16, False, 9, 160, 0, 2, 0, 120),
        ("Wheel speed, rear-right", "m/s", 48, 16, False, 9, 160, 0, 2, 0, 120),
    ]),
    (0x7E2, 0x2002, [  # MERCEDES_YAW_RATE / STEER_ANGLE / LAT_ACC / LON_ACC /
                       # MASTER_BREAK_CYLINDER_PRESSURE / VACC_BREAK
        ("Longitudinal acceleration", "m/s²", 8, 8, True, 16, 1250, 0, 3, -20, 20),
        ("Lateral acceleration", "m/s²", 32, 8, True, 1, 64, 0, 3, -20, 20),
        ("Yaw rate", "rad/s", 48, 16, True, 2, 32768, 0, 4, -1, 1),
        ("Steering angle", "°", 72, 16, True, 1, 10, 0, 1, -720, 720),
        ("Brake master-cylinder pressure", "bar", 96, 16, True, 153, 10000, 0, 2, -10, 300),
        ("Brake vacuum-brake pressure", "bar", 148, 12, False, 1, 1000, 0, 3, 0, 10),
    ]),
    # Hybrid / 0x7E5 -----------------------------------------------------------
    (0x7E5, 0x2005, [  # MERCEDES_AUX_V
        ("Auxiliary battery voltage", "V", 0, 8, False, 1015686, 10000000, 0, 2, 0, 20),
    ]),
    (0x7E5, 0x6050, [  # MERCEDES_HV_SOC
        ("HV battery state of charge", "%", 0, 16, False, 1, 100, 0, 1, 0, 100),
    ]),
    (0x7E5, 0x6071, [  # MERCEDES_CNVRTR_REQ_V
        ("HV converter requested voltage", "V", 0, 16, False, 25, 1000, 0, 2, 0, 60),
    ]),
]

# The E-Class set is community-captured (OBDb) -- this caveat rides with the
# readings so an absolute figure is sanity-checked against the car.
CAVEAT = {
    "name": "Note",
    "value": ("These E-Class values were derived and fully verified against the OBDb "
              "Mercedes-Benz-E-Class on-car captures (CC-BY-SA-4.0; tests/test_cases "
              "2018 + 2021, 520 checks pass) and read over the dongle — sanity-check an "
              "absolute figure against the car. They answer on the E-Class mild-hybrid "
              "(W/S 213); HV current (226053) has no verified scale and is not shown."),
    "unit": "",
}


def _slice(body: bytes, bix: int, length: int) -> Optional[int]:
    """Big-endian bit-slice of ``length`` bits at bit index ``bix``, or None if
    the body is too short."""
    if bix + length > len(body) * 8:
        return None
    val = 0
    for i in range(length):
        bit = bix + i
        val = (val << 1) | ((body[bit // 8] >> (7 - (bit % 8))) & 1)
    return val


def _decode(body: bytes, field: Field) -> Optional[str]:
    """One field -> formatted string, or None if absent/out of sanity bounds."""
    _name, _unit, bix, length, sign, mul, div, add, dp, lo, hi = field
    raw = _slice(body, bix, length)
    if raw is None:
        return None
    if sign and raw >= (1 << (length - 1)):
        raw -= (1 << length)
    value = raw * (mul or 1) / (div or 1) + (add or 0)
    if not (lo <= value <= hi):
        return None
    return f"{value:.{dp}f}"


def _read_did(client, req_id: int, did: int) -> Optional[bytes]:
    """One 11-bit RDBI (0x22) read -> the response body after ``62 <did>``, or
    None. Uses the client's id_map so a non-+8 response address still matches."""
    try:
        reply = client.request(req_id, bytes([0x22, did >> 8, did & 0xFF]), timeout=3.0)
    except (ObdError, TransportError):
        return None
    if len(reply) < 4 or reply[0] != 0x62 or reply[1] != (did >> 8) or reply[2] != (did & 0xFF):
        return None
    return reply[3:]


def probe(client) -> bool:
    """Is this a hybrid E-Class? One cheap gate read; True only if it answers cleanly."""
    return _read_did(client, _GATE[0], _GATE[1]) is not None


def read_live(client) -> List[dict]:
    """Read the curated E-Class hybrid set over an already-begun Elm327UdsClient.

    Returns a list of ``{"name", "value", "unit"}`` readings (with the caveat row
    appended when anything answered), or ``[]`` on a non-hybrid so the caller can
    fall through to its generic extras. Read-only; skips DIDs that don't answer.
    """
    if not probe(client):
        return []
    readings: List[dict] = []
    for req_id, did, fields in ECLASS_DIDS:
        body = _read_did(client, req_id, did)
        if body is None:
            continue
        for field in fields:
            try:
                value = _decode(body, field)
            except Exception:
                continue
            if value is not None:
                readings.append({"name": field[0], "value": value, "unit": field[1]})
    if readings:
        readings.append(dict(CAVEAT))
    return readings


# A believable E-Class mild-hybrid for sim mode (clearly labelled by the caller).
SIM_READINGS: List[dict] = [
    {"name": "HV battery state of charge", "value": "42.0", "unit": "%"},
    {"name": "HV converter requested voltage", "value": "25.00", "unit": "V"},
    {"name": "Auxiliary battery voltage", "value": "14.93", "unit": "V"},
    {"name": "Steering angle", "value": "-176.1", "unit": "°"},
    {"name": "Yaw rate", "value": "-0.0015", "unit": "rad/s"},
    {"name": "Longitudinal acceleration", "value": "0.000", "unit": "m/s²"},
    {"name": "Wheel speed, front-left", "value": "12.71", "unit": "m/s"},
    {"name": "Wheel speed, front-right", "value": "13.73", "unit": "m/s"},
]
