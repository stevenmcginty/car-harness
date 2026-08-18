"""Jaguar / Land Rover live "extras" over a plain ELM327
(docs/private/jlr-research-modules.md §5, §10b).

The values are read over 11-bit CAN with Elm327UdsClient, service 0x22
(ReadDataByIdentifier). Every JLR-specific row below is a REAL on-car capture --
the "Old Jaguar Group" Torque PID pack recorded on X351 / XK X150 petrols
(AJ133/AJ126, "should also work" on the diesels) -- but from a SINGLE source, so
the scaling must be re-checked against the car before absolute values are
trusted (a caveat rides on the readings). The stars are the dipstick-less oil
readings: measured oil LEVEL in mm and calculated oil VOLUME in litres, values
the dash only shows indirectly.

Values live on three modules: engine 0x7E0, ZF gearbox 0x7E1 and (where fitted)
the rear e-diff 0x795. Each DID is polled ONE at a time with a generous timeout
-- BLE dongles are slow.

The research's biggest gaps are deliberately NOT here: air-suspension corner
heights, DPF soot mass and battery SoC have no publicly-captured JLR DIDs
(capture targets for a real car, research §5a) -- nothing is invented.

One external seed now exists: data/external_pids.json (the 306DT pre-contact
research, docs/private/jlr-306dt-external-pid-research.md) holds externally-
sourced candidate DIDs -- 22042C DPF soot (two JLR-diesel provenances, still
unverified on the 3.0 V6) and the confirmed-dead Ford-family ids. It is loaded
as EXTERNAL_DIDS, separate from JLR_DIDS on purpose: nothing from it is
promoted into the verified table until a real capture on the car confirms it.

If the JLR-specific reads all fail (a DoIP-era or gateway-filtered car), live()
falls back to a curated generic Mode-01 set on the 11-bit engine so the car
still gets something honest, exactly like the Mercedes/Fiat profiles.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient
from backend.profiles.jlr import sdd

ENGINE = 0x7E0
TRANS = 0x7E1
RDCM = 0x795


def _u8(b: bytes, i: int = 0) -> Optional[int]:
    return b[i] if len(b) > i else None


def _u16(b: bytes, i: int = 0) -> Optional[int]:
    return (b[i] << 8) | b[i + 1] if len(b) > i + 1 else None


def _fmt(v: float, dp: int = 1) -> str:
    return f"{v:.{dp}f}" if dp else f"{v:.0f}"


def _byte(scale: float = 1.0, offset: float = 0.0, dp: int = 0,
          lo: float = float("-inf"), hi: float = float("inf")):
    """First payload byte (A) -> scaled string, or None outside sanity bounds."""
    def decode(b: bytes) -> Optional[str]:
        v = _u8(b)
        if v is None:
            return None
        val = v * scale + offset
        return _fmt(val, dp) if lo <= val <= hi else None
    return decode

def _u16_scaled(scale: float, offset: float = 0.0, dp: int = 1,
                lo: float = float("-inf"), hi: float = float("inf")):
    """Big-endian A*256+B -> scaled string, or None outside sanity bounds."""
    def decode(b: bytes) -> Optional[str]:
        v = _u16(b)
        if v is None:
            return None
        val = v * scale + offset
        return _fmt(val, dp) if lo <= val <= hi else None
    return decode


# ---- the Old Jaguar Group X351/XK decoders (research §10b, single on-car
# source, UNVERIFIED scalings) -- bytes A,B are the payload after 62 <did>.
# (module_addr, did, name, unit, decode) -- polled ONE at a time.
JLR_DIDS: List[Tuple[int, int, str, str, Callable[[bytes], Optional[str]]]] = [
    (ENGINE, 0x03F3, "Engine oil temperature", "°C", _byte(1, -40, dp=0, lo=-40, hi=215)),
    (ENGINE, 0x03E6, "Engine oil level (measured)", "mm", _byte(1, 0, dp=0, lo=0, hi=120)),
    (ENGINE, 0x03F2, "Engine oil volume (calculated)", "L", _byte(0.1, 0, dp=1, lo=0, hi=12)),
    (ENGINE, 0x033E, "Boost / manifold pressure", "kPa", _byte(1, 0, dp=0, lo=0, hi=350)),
    (ENGINE, 0x0520, "Charge air temp (post-intercooler)", "°C", _byte(1, 0, dp=0, lo=0, hi=150)),
    # SDD-169-verified diesel-PCM DIDs (data/sdd_dids.json). Scaling is from the
    # decrypted MDX_PCM_D tables and cross-checks against the L494 SALWA2EK8HA
    # sweep (0396 answered 5AB6 -> 23222 km; F405 answered 6E -> 70 C).
    (ENGINE, 0x0396, "Distance to next service", "km", _u16_scaled(1, 0, dp=0, lo=0, hi=65535)),
    (ENGINE, 0xF405, "Engine coolant temperature (UDS DID)", "°C", _byte(1, -40, dp=0, lo=-40, hi=215)),
    (TRANS, 0x1E69, "Gearbox (ZF) oil temperature", "°C", _byte(1, -40, dp=0, lo=-40, hi=215)),
    (RDCM, 0x1E8A, "Rear e-diff oil temperature", "°C", _byte(1, -40, dp=0, lo=-40, hi=215)),
]

# Curated generic Mode-01 set for the DoIP-era / gateway-filtered fallback
# (standard ISO 15031 PIDs on the 11-bit engine), mirroring Mercedes/Fiat.
OBD_PIDS: List[Tuple[int, str, str, Callable[[bytes], Optional[str]]]] = [
    (0x05, "Coolant temperature", "°C", _byte(1, -40, lo=-40, hi=215)),
    (0x5C, "Engine oil temperature", "°C", _byte(1, -40, lo=-40, hi=215)),
    (0x42, "Battery voltage (control module)", "V", _u16_scaled(1 / 1000, dp=2, lo=0, hi=32)),
    (0x2F, "Fuel level", "%", _byte(100 / 255, dp=0, lo=0, hi=100)),
]


# ---- externally-researched candidates (data/external_pids.json) ------------
# Loaded defensively like the Peugeot EP6 catalog: a missing or corrupt file
# degrades to an empty candidate list, never a broken import. Every entry is
# provenance-flagged and verified=false -- the drive log polls them RAW and
# only a real capture on the car promotes anything into JLR_DIDS.
_DATA_PATH = Path(__file__).resolve().parent / "data" / "external_pids.json"


def _load_external_data() -> dict:
    try:
        with open(_DATA_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


_EXTERNAL_DATA = _load_external_data()
EXTERNAL_DIDS: List[dict] = _EXTERNAL_DATA.get("dids", [])


# ---- the SDD-named diesel-PCM candidates ----------------------------------
# SDD 169 names 37 diesel-PCM DIDs (data/sdd_dids.json, loaded by sdd.py). Two
# of them -- 0396 and F405 -- came with scaling that reproduced a sane number
# from the live L494 payload, so they sit in JLR_DIDS above as verified values.
# The rest of the SDD rows the validation car ANSWERED are listed here instead:
# the name is the manufacturer's own and is therefore trustworthy, but the
# value is not settled, either because SDD publishes no scaling for the row or
# because nothing has yet checked SDD's arithmetic against a real reading.
# They are read on the live pass and displayed as candidates -- raw payload
# always shown, decoded figure only beside it and only when SDD gives one.
#
# The urea/SCR cluster 05FA-05FD is the headline entry: PCM-managed reagent
# telemetry, named by the manufacturer, and completely unscaled.
SDD_CANDIDATE_DIDS: List[int] = [
    did for did, entry in sorted(sdd.PCM_DIDS.items())
    if entry.answers_on_l494 and entry.confidence != "verified"
]

# A 64-byte injector-correction blob is not a phone-card reading. Long raw
# payloads are shown head-and-tail so the row stays legible and the full
# capture stays the DID sweep's job.
_RAW_HEAD_BYTES = 8


def _raw_display(body: bytes) -> str:
    if len(body) <= _RAW_HEAD_BYTES * 2:
        return body.hex().upper()
    return (body[:_RAW_HEAD_BYTES].hex().upper() + "…"
            + body[-_RAW_HEAD_BYTES:].hex().upper()
            + f" ({len(body)} bytes)")


def sdd_candidate_reading(did: int, body: bytes) -> dict:
    """One SDD-named-but-unproven DID as a display reading.

    The rule the whole SDD layer turns on: a candidate NEVER appears as a bare
    number. The raw payload is always in the value; a decoded figure joins it
    only when SDD publishes scaling, and it is labelled as SDD's arithmetic
    rather than as a measurement. 05FC and friends additionally show the
    payload as an unscaled integer -- not a decode, just the same bytes in
    base 10, which is what makes a monotonic counter visibly count."""
    entry = sdd.PCM_DIDS.get(did)
    name = entry.label() if entry else f"SDD DID {did:04X} [candidate]"
    raw = _raw_display(body)
    decoded, unit, _ = sdd.decode_did(did, body)
    parts = [f"raw {raw}"]
    if decoded is not None:
        parts.append(f"SDD scaling → {decoded}{(' ' + unit) if unit else ''}")
    elif did in sdd.UREA_DIDS and len(body) <= 4:
        # Only for payloads narrow enough to BE one number. 05FA is six bytes
        # and is plainly several fields; reading it as one integer would be a
        # decode, and an invented one.
        as_int = sdd.unscaled_int(body)
        if as_int is not None:
            parts.append(f"unscaled integer {as_int}")
    return {
        "name": f"{name} — DID {did:04X}",
        "value": " · ".join(parts),
        "unit": "",
        "description": entry.describe() if entry else "",
    }


# The caveat that rides with the candidate block, so a raw payload is never
# mistaken for a reading the harness stands behind.
_SDD_CAVEAT = {
    "name": "Note",
    "value": ("Rows marked [candidate] carry Jaguar Land Rover's own SDD 169 name, which is "
              "authoritative — but the number beside it is not. Where SDD publishes no "
              "scaling (the urea cluster 05FA-05FD among them) the payload stays raw. "
              "Log 05FC warm over a drive to pin its scaling: it is a monotonic total."),
    "unit": "",
}

# The engine-module candidates worth polling raw (the dead-list rows carry no
# formula and are excluded; their silence is documented, not chased).
EXTERNAL_ENGINE_DIDS: List[Tuple[int, str, str]] = [
    (int(row["did"], 16), row["name"], row.get("provenance", ""))
    for row in EXTERNAL_DIDS
    if row.get("module") == "0x7E0" and row.get("formula")
]

# The legislated Mode-01 aftertreatment set (SAE J1979DA formulas -- public
# standard, not JLR-invented; per-car support unknown until asked). The drive
# log adds these to its baseline tick; unsupported PIDs answer silence and are
# skipped without comment.
AFTERTREATMENT_PIDS: List[Tuple[int, str, str]] = [
    (int(row["pid"], 16), row["name"], row.get("provenance", ""))
    for row in _EXTERNAL_DATA.get("mode01", [])
]

# The Old Jaguar Group values are a single on-car capture -- this caveat rides
# with them so the scaling is checked against the car before figures are trusted.
_OJG_CAVEAT = {
    "name": "Note",
    "value": ("These JLR values are from a single on-car X351/XK petrol PID capture — "
              "verify the scaling against the car before trusting absolute figures."),
    "unit": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_did(client, addr: int, did: int) -> Optional[bytes]:
    """One 11-bit RDBI read -> payload after ``62 <did>``, or None."""
    try:
        payload = client.request(addr, bytes([0x22, did >> 8, did & 0xFF]), timeout=3.0)
    except (ObdError, TransportError):
        return None
    if len(payload) < 4 or payload[0] != 0x62:
        return None
    return payload[3:]


def _read_pid(client, header: int, pid: int) -> Optional[bytes]:
    """One generic Mode-01 read -> payload after ``41 <pid>``, or None."""
    try:
        payload = client.request(header, bytes([0x01, pid]), timeout=2.0)
    except (ObdError, TransportError):
        return None
    if len(payload) < 3 or payload[0] != 0x41 or payload[1] != pid:
        return None
    return payload[2:]


def live(adapter: Optional[dict], mode: str = "real") -> dict:
    """One read-only pass over the JLR live extras. Skips non-answers."""
    if mode == "sim":
        return {
            "ok": True, "simulated": True,
            "readings": [
                {"name": "Engine oil temperature", "value": "96", "unit": "°C"},
                {"name": "Engine oil level (measured)", "value": "52", "unit": "mm"},
                {"name": "Engine oil volume (calculated)", "value": "6.8", "unit": "L"},
                {"name": "Boost / manifold pressure", "value": "101", "unit": "kPa"},
                {"name": "Gearbox (ZF) oil temperature", "value": "78", "unit": "°C"},
                {"name": "Distance to next service", "value": "23222", "unit": "km"},
                dict(_OJG_CAVEAT),
                # The SDD candidate tier, in the shape a real pass produces it:
                # manufacturer name, raw payload, never a bare number.
                sdd_candidate_reading(0x05FA, bytes.fromhex("048A00000301")),
                sdd_candidate_reading(0x05FB, bytes.fromhex("0FA0")),
                sdd_candidate_reading(0x05FC, bytes.fromhex("00039CC5")),
                sdd_candidate_reading(0x05FD, bytes.fromhex("0651")),
                dict(_SDD_CAVEAT),
            ],
            "message": "SIMULATED -- no real car connected.",
            "timestamp": _now(),
        }

    def run(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "readings": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, not the "
                    "11-bit/500k CAN JLR live values use."
                ),
                "timestamp": _now(),
            }

        readings = []
        client = Elm327UdsClient(session)
        client.begin()
        try:
            # TIER 1: the Old Jaguar Group DIDs on the driveline modules.
            for addr, did, name, unit, decode in JLR_DIDS:
                body = _read_did(client, addr, did)
                if body is None:
                    continue
                try:
                    value = decode(body)
                except Exception:
                    continue
                if value is not None:
                    readings.append({"name": name, "value": value, "unit": unit})
            if readings:
                readings.append(dict(_OJG_CAVEAT))

            # TIER 1b: the SDD-named diesel-PCM rows this car answers but has
            # not proved. Only read them when the verified tier answered --
            # a car that ignores every JLR DID is not a 306DT and should fall
            # straight through to the generic set below.
            if readings:
                candidates = []
                for did in SDD_CANDIDATE_DIDS:
                    body = _read_did(client, ENGINE, did)
                    if body:
                        candidates.append(sdd_candidate_reading(did, body))
                if candidates:
                    readings.extend(candidates)
                    readings.append(dict(_SDD_CAVEAT))

            # TIER 2: nothing JLR-specific -> curated generic Mode-01 set
            # (a DoIP-era or gateway-filtered car still gets something honest).
            if not readings:
                for pid, name, unit, decode in OBD_PIDS:
                    body = _read_pid(client, ENGINE, pid)
                    if body is None:
                        continue
                    try:
                        value = decode(body)
                    except Exception:
                        continue
                    if value is not None:
                        readings.append({"name": name, "value": value, "unit": unit})
        finally:
            client.restore()

        return {
            "ok": True, "simulated": False, "readings": readings,
            "message": (
                f"{len(readings)} JLR value(s) answered. On a 2006-2016 CAN-era car these are "
                "the Old Jaguar Group oil/gearbox/diff values (single-source capture -- verify "
                "scaling against the car), plus the SDD-169-named diesel-PCM rows: two "
                "verified with their manufacturer scaling and the rest shown as [candidate] "
                "with the raw payload always in view. A DoIP-era or gateway-filtered car "
                "falls back to a curated generic Mode-01 set. Values this car doesn't support "
                "are skipped. Read-only pass; nothing was changed on the car."
            ),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, run)
    except obd_service.ObdServiceError as exc:
        return {
            "ok": False, "simulated": False, "readings": [],
            "message": str(exc), "timestamp": _now(),
        }
