"""Porsche Taycan (J1) enhanced live values over a plain ELM327 dongle.

This is Phase 0.1 of docs/private/porsche-suite-research.md: the Taycan answers a
large, PUBLISHED UDS ``0x22`` DID library on 11-bit CAN *at the OBD port*, so a
dealer can read its high-voltage telemetry over an ordinary VeePeak-style dongle
-- no ENET cable needed. (The ENET path in ``porsche_enet`` still earns its keep
for the deep, per-cell battery-health report; this is the cheap-dongle summary.)

PROVENANCE. Every value below is transcribed from the OBDb Porsche-Taycan
signalset bundled next to this file as ``taycan_obdb_signalset.json`` (OBDb,
CC-BY-SA-4.0 -- see the NOTICE). The full set is 653 signals; the vast bulk of
that is 429 individual per-cell voltages and 99 per-module temperatures. Dumping
600 slow per-cell channels onto a Bluetooth dashboard would be unreadable and
take a minute a sweep, so -- exactly like every other profile here (JLR ships 7
curated DIDs, not its whole tree) -- this is a CURATED set of the ~15
single-shot, dealer-relevant summary values: state of charge, pack voltage,
usable energy, power and range, the 12 V DC-DC rail, drive-unit speed and
temperatures, and cabin/ambient. The full library stays bundled for later work.

These are VERIFIED community captures (OBDb is multi-contributor and on-car), but
OBDb itself carries no formal guarantee, so the same house caveat rides along:
sanity-bounded, read-only, and a note tells the user to sanity-check an absolute
figure against the car. Nothing here is invented.

TRANSPORT. The Taycan's control units do NOT all answer on request+8: the DC-DC
and thermal ECUs sit on 0x710/0x713/0x744/0x746/0x742/0x714/0x71E and reply on
0x77A/0x77D/0x7AE/0x7B0/0x7AC/0x77E/0x788. ``Elm327UdsClient`` already supports a
request->response ``id_map`` for exactly this; ``ID_MAP`` below feeds it. The
0x7E0/0x7E5 values follow the standard +8 pairing and need no override.

A single cheap probe DID gates the whole set (``_GATE``): on any non-Taycan the
probe returns nothing and the caller falls straight through to its CAN-era tiers,
so a 997/981/Cayenne pays one short timeout, not fifteen.
"""

from typing import Callable, List, Optional, Tuple

from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError

# Request CAN id -> response CAN id for the Taycan ECUs that don't answer on
# request+8 (from the OBDb signalset's ``hdr``/``rax`` pairs). 0x7E0->0x7E8 and
# 0x7E5->0x7ED follow the standard pairing and are intentionally omitted.
ID_MAP = {
    0x710: 0x77A,
    0x713: 0x77D,
    0x714: 0x77E,
    0x71E: 0x788,
    0x742: 0x7AC,
    0x744: 0x7AE,
    0x746: 0x7B0,
}

# The gate: one Taycan-unique DID (VCU state-of-charge, display) on the standard
# 0x7E0 engine/VCU address. If this answers, it's a Taycan and the full sweep is
# worth doing; if not, don't waste fifteen timeouts on a CAN-era car.
_GATE = (0x7E0, 0x08D2)


# ---- one decoded value: bit-slice of the response body, then the OBDb formula
# scaled = raw * mul / div + add, two's-complement when ``sign``. (name, unit,
# bit-index, bit-length, sign, mul, div, add, decimals, lo, hi) -- lo/hi are
# sanity bounds; a value outside them is dropped rather than shown.
Field = Tuple[str, str, int, int, bool, float, float, float, int, float, float]

# ---- the curated Taycan live set, grouped by (request id, DID) so one UDS read
# yields every value packed in that response. Each cites its OBDb signal id.
# (req_id, did, [Field, ...])
TAYCAN_DIDS: List[Tuple[int, int, List[Field]]] = [
    # VCU / 0x7E0 --------------------------------------------------------------
    (0x7E0, 0x08D2, [  # TAYCAN_HVBAT_SOC_DISP
        ("Battery charge (SoC, display)", "%", 0, 16, False, 1, 100, 0, 1, 0, 100),
    ]),
    (0x7E0, 0x3DF8, [  # TAYCAN_ENG_MOTOR_RPM
        ("Drive-unit motor speed", "rpm", 0, 16, True, 1, 2, 0, 0, -20000, 20000),
    ]),
    # BMS / 0x7E5 --------------------------------------------------------------
    (0x7E5, 0x028C, [  # TAYCAN_BMS_SOC
        ("Battery charge (SoC, BMS)", "%", 0, 8, False, 1, 1, 0, 0, 0, 100),
    ]),
    (0x7E5, 0x1801, [  # TAYCAN_HVBAT_VOLT
        ("HV battery voltage", "V", 0, 16, False, 1, 10, 0, 1, 0, 1000),
    ]),
    # HV energy manager / 0x710 (resp 0x77A) -----------------------------------
    (0x710, 0x2AB2, [  # TAYCAN_HVBAT_E_MAX (native watts -> kW)
        ("HV battery max power", "kW", 0, 16, False, 50, 1000, 0, 0, 0, 700),
    ]),
    (0x710, 0x2AB6, [  # TAYCAN_RANGE
        ("Estimated range", "mi", 0, 16, False, 1, 1, 0, 0, 0, 500),
    ]),
    (0x710, 0x2A53, [  # TAYCAN_DCDC_V_LOW + TAYCAN_DCDC_I
        ("12 V system voltage (DC-DC)", "V", 0, 8, False, 1, 10, 0, 1, 0, 25.5),
        ("DC-DC output current", "A", 8, 16, False, 1, 1, -511, 0, -600, 600),
    ]),
    # On-board charger / 0x744 (resp 0x7AE) ------------------------------------
    (0x744, 0x4965, [  # TAYCAN_HVBAT_KWH
        ("HV battery energy (usable)", "kWh", 0, 16, False, 1, 20, 0, 1, 0, 150),
    ]),
    (0x744, 0x15E2, [  # TAYCAN_CHRGR_COOLANT_T
        ("Charger coolant temperature", "°C", 0, 8, False, 1, 1, -40, 0, -40, 215),
    ]),
    # Drive unit / 0x71E (resp 0x788) ------------------------------------------
    (0x71E, 0xF105, [  # TAYCAN_DIFF_MOTOR_T + TAYCAN_DIFF_OIL_T
        ("Drive-unit motor temperature", "°C", 0, 8, False, 2, 1, -50, 0, -50, 300),
        ("Drive-unit oil temperature", "°C", 8, 8, False, 2, 1, -50, 0, -50, 300),
    ]),
    # HVAC / 0x746 (resp 0x7B0) ------------------------------------------------
    (0x746, 0x2609, [  # TAYCAN_HVAC_OUTSIDE_T
        ("Outside air temperature", "°C", 0, 16, True, 1, 10, 0, 1, -60, 80),
    ]),
    (0x746, 0x2613, [  # TAYCAN_HVAC_INTERIOR_T
        ("Interior temperature", "°C", 0, 16, True, 1, 10, 0, 1, -40, 90),
    ]),
]

# The Taycan set is community-captured (OBDb) -- this caveat rides with the
# readings so an absolute figure is sanity-checked against the car.
CAVEAT = {
    "name": "Note",
    "value": ("These Taycan values come from the OBDb Porsche-Taycan signal database "
              "(CC-BY-SA-4.0, community-captured) read over the dongle — sanity-check an "
              "absolute figure against the car. Per-cell voltages and per-module temperatures "
              "are in the bundled signalset; the ENET battery-health report summarises them."),
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
    """Is this a Taycan? One cheap gate read; True only if it answers cleanly."""
    return _read_did(client, _GATE[0], _GATE[1]) is not None


def read_live(client) -> List[dict]:
    """Read the curated Taycan set over an already-begun Elm327UdsClient.

    Returns a list of ``{"name", "value", "unit"}`` readings (with the caveat row
    appended when anything answered), or ``[]`` on a non-Taycan so the caller can
    fall through to its CAN-era tiers. Read-only; skips DIDs that don't answer.
    """
    if not probe(client):
        return []
    readings: List[dict] = []
    for req_id, did, fields in TAYCAN_DIDS:
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


# A believable Taycan for sim mode (clearly labelled by the caller).
SIM_READINGS: List[dict] = [
    {"name": "Battery charge (SoC, display)", "value": "72.0", "unit": "%"},
    {"name": "Battery charge (SoC, BMS)", "value": "73", "unit": "%"},
    {"name": "HV battery voltage", "value": "772.0", "unit": "V"},
    {"name": "HV battery energy (usable)", "value": "62.5", "unit": "kWh"},
    {"name": "HV battery max power", "value": "270", "unit": "kW"},
    {"name": "Estimated range", "value": "188", "unit": "mi"},
    {"name": "12 V system voltage (DC-DC)", "value": "14.1", "unit": "V"},
    {"name": "Drive-unit motor temperature", "value": "48", "unit": "°C"},
    {"name": "Outside air temperature", "value": "17.0", "unit": "°C"},
]
