"""The one live-data engine every car in the harness goes through.

Two layers, merged into a single stream of readings:

  1. **Standard OBD-II (mode 01)** -- works on every car built since roughly
     2001, whatever the badge. The car itself tells us which PIDs it supports,
     so the list is what this exact vehicle can give, never a guess.
  2. **Manufacturer extras (mode 22 and friends)** -- whatever
     backend/profiles/<brand>/pids.py can read. Deeper, but brand by brand.

A channel is one readable value. `catalog()` finds every channel this car
answers; `read()` fetches the ones the user picked. Splitting it that way is
what makes a streaming dashboard possible: discovery is slow and happens once,
reads are fast and happen repeatedly.

THE CONTRACT FOR A NEW MANUFACTURER
-----------------------------------
Drop in backend/profiles/<brand>/pids.py exposing ``live(adapter, mode)`` that
returns ``{"ok": bool, "readings": [{"name", "value", "unit"}], "message": str}``
and the brand appears in Live Data automatically -- no edits here, none in the
front end. That is the floor, and all eight current brands meet it.

A brand may ALSO expose the richer, faster interface, which this engine prefers
when it finds it:

    CHANNELS = [{"key", "label", "unit", "group"}, ...]
    def read_channels(adapter, mode, keys) -> [{"key", "value", "unit"}]

With it, only the values on screen get read instead of the module's whole DID
table -- which over a Bluetooth dongle is the difference between a gauge that
updates every second and one that updates every thirty.
"""

import math
import re
import time
from datetime import datetime, timezone

from backend.core import brands as brand_registry
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.pids import (
    ELECTRICAL,
    EMISSIONS,
    ENGINE,
    FUEL_AIR,
    GROUP_ORDER,
    PIDS_BY_KEY,
    PIDS_FOR_PID,
    TEMPS,
    TRIP,
    TURBO,
    read_supported_pids,
)
from backend.obd.transport import TransportError

OBD_SOURCE = "OBD-II"
_OBD_PREFIX = "obd."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Normalising a manufacturer profile's readings into channels
# --------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")
# A value we can plot has to be a plain number and nothing else. "1,234 mi
# (1,987 km)" is a perfectly good thing to show a dealer but there is no honest
# single number in it, so it stays text and simply isn't offered as a graph.
_NUMERIC_RE = re.compile(r"^[-+]?\d{1,12}(?:\.\d+)?$")

_GROUP_HINTS = [
    (TEMPS, ("temperature", "temp", "coolant", "thermostat")),
    (TURBO, ("boost", "turbo", "charge air", "exhaust", "wastegate", "egt", "dpf",
             "soot", "regen", "particulate", "adblue", "nox")),
    (ELECTRICAL, ("battery", "voltage", "alternator", "current", "soc", "charge",
                  "state of health", "soh")),
    (FUEL_AIR, ("fuel", "injection", "rail", "lambda", "air", "maf", "map",
                "throttle", "pedal", "octane", "knock", "manifold")),
    (EMISSIONS, ("o2", "oxygen", "catalyst", "egr", "evap", "emission")),
    (TRIP, ("odometer", "distance", "mileage", "service", "interval", "since",
            "age", "hours", "count")),
    (ENGINE, ("engine", "rpm", "torque", "gear", "gearbox", "transmission",
              "speed", "load", "oil")),
]


def _slug(text: str) -> str:
    return _SLUG_RE.sub("_", text.strip().lower()).strip("_") or "value"


def _group_for(name: str, unit: str) -> str:
    """Best-effort bucket for a manufacturer reading, which arrives as free
    text. Wrong bucket costs a scroll; it never changes the number."""
    haystack = f"{name} {unit}".lower()
    for group, words in _GROUP_HINTS:
        if any(word in haystack for word in words):
            return group
    return ENGINE


def _numeric(text) -> float | None:
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    return float(cleaned) if _NUMERIC_RE.match(cleaned) else None


# How many decimals a unit deserves on a gauge. Rounding is display only --
# `value` keeps full precision for graphing and CSV. A fixed number of decimals
# per unit is what stops the digits jittering as the needle moves; "603.78 rpm"
# is also just wrong-looking on a dashboard.
_DECIMALS = {
    "rpm": 0, "km/h": 0, "count": 0, "s": 0, "min": 0, "km": 0, "mi": 0,
    "Pa": 0, "kPa": 0, "Nm": 0, "mA": 0, "days": 0,
    "°C": 1, "%": 1, "°": 1, "g/s": 1, "L/h": 1, "L": 1, "psi": 1,
    "V": 2,
}


def _format(value: float, unit: str) -> str:
    dp = _DECIMALS.get(unit, 3 if unit == "" else 2)
    return f"{value:.{dp}f}"


def _channel(key, label, unit, group, source, value=None, text=None):
    numeric = _numeric(value if text is None else text)
    if text is None:
        text = "" if value is None else (
            _format(numeric, unit or "") if numeric is not None else str(value)
        )
    return {
        "key": key,
        "label": label,
        "unit": unit or "",
        "group": group,
        "source": source,
        "value": numeric,
        "text": text,
        "graphable": numeric is not None,
    }


def _brand_source(brand: str) -> str:
    meta = brand_registry.profile_meta(brand) or {}
    return f"{meta.get('name', brand.upper())} enhanced"


def _brand_channels_from_live(brand: str, result: dict) -> list:
    """Turn a profile's ``live()`` readings into channels."""
    source = _brand_source(brand)
    channels, seen = [], set()
    for reading in result.get("readings") or []:
        name = str(reading.get("name") or reading.get("label") or "").strip()
        if not name:
            continue
        key = f"{brand}.{_slug(name)}"
        if key in seen:  # two DIDs with the same display name -- keep the first
            continue
        seen.add(key)
        channels.append(
            _channel(
                key, name, reading.get("unit") or "",
                _group_for(name, reading.get("unit") or ""),
                source, text=str(reading.get("value", "")),
            )
        )
    return channels


def _rich_brand_module(brand: str):
    """The faster interface, if this brand implements it."""
    module = brand_registry.brand_module(brand, "pids")
    if module is None:
        return None
    if hasattr(module, "CHANNELS") and hasattr(module, "read_channels"):
        return module
    return None


# --------------------------------------------------------------------------
# Simulation -- a full, animated car so the dashboard can be judged with
# nothing plugged in. Always labelled; never mixed with real readings.
# --------------------------------------------------------------------------

def _sim_value(channel, t: float) -> float | None:
    """A plausible, moving value derived from the channel's own unit and
    bounds, so every simulated gauge behaves like the thing it represents."""
    key = channel["key"]
    unit = channel["unit"]
    pid = PIDS_BY_KEY.get(key[len(_OBD_PREFIX):]) if key.startswith(_OBD_PREFIX) else None

    wave = math.sin(t / 2.1 + (hash(key) % 100) / 16.0)
    slow = math.sin(t / 23.0 + (hash(key) % 50) / 8.0)

    named = {
        "rpm": 820 + 260 * wave + 40 * math.sin(t * 1.7),
        "speed": max(0.0, 32 + 30 * math.sin(t / 9.0)),
        "coolant": min(92.0, 55 + (t % 600) / 8.0),
        "oil_temp": min(95.0, 40 + (t % 600) / 7.0),
        "module_voltage": 13.9 + 0.15 * wave,
        "fuel_level": 62.0,
        "load": 22 + 12 * wave,
        "throttle": 14 + 9 * wave,
    }
    if pid is not None and pid.key in named:
        return named[pid.key]

    # Unit first, bounds only as a fallback. Driving the sim off a PID's legal
    # range puts signed channels at an extreme -- an evaporative pressure whose
    # range is +/-8192 Pa would idle at -8100, which is not what a car looks like.
    by_unit = {
        "°C": 82 + 9 * slow,
        "V": 13.8 + 0.2 * wave,
        "%": 45 + 20 * wave,
        "rpm": 850 + 200 * wave,
        "km/h": abs(30 + 25 * wave),
        "kPa": 101 + 14 * wave,
        "Pa": 260 * wave,
        "mA": 42 * wave,
        "g/s": 6.5 + 3.0 * wave,
        "L/h": 1.4 + 0.8 * wave,
        "°": 12 + 5 * wave,
        "Nm": 250 + 20 * wave,
        "s": 900 + 60 * slow,
        "min": 42 + 3 * slow,
        "km": 12800 + 40 * slow,
        "mi": 7900 + 25 * slow,
        "count": 12,
        "days": 640,
        "": 1.0 + 0.03 * wave,      # lambda and the other ratios
    }
    if unit in by_unit:
        value = by_unit[unit]
    elif pid is not None:
        lo = pid.lo if pid.lo is not None else 0.0
        hi = pid.hi if pid.hi is not None else 100.0
        value = lo + (hi - lo) * 0.5 + (hi - lo) * 0.05 * wave
    else:
        return 40 + 18 * wave

    if pid is not None:  # never simulate a value the real PID could not report
        if pid.lo is not None:
            value = max(value, pid.lo)
        if pid.hi is not None:
            value = min(value, pid.hi)
    return value


# The PIDs that answer with a word rather than a measurement. Everything else
# is a number, including the unit-less ratios like lambda -- those must not be
# mistaken for text just because they have no unit to show.
_TEXT_KEYS = {"fuel_system", "fuel_type", "obd_standard", "secondary_air",
              "emission_req", "pto_active"}


def _sim_catalog(brand: str | None) -> list:
    """Every standard channel plus a believable manufacturer set."""
    channels = [
        _channel(_OBD_PREFIX + p.key, p.label, p.unit, p.group, OBD_SOURCE)
        for p in PIDS_BY_KEY.values()
    ]
    if brand:
        source = _brand_source(brand)
        for name, unit, group in (
            ("Engine oil temperature", "°C", TEMPS),
            ("Gearbox (ATF) temperature", "°C", TEMPS),
            ("Battery state of charge", "%", ELECTRICAL),
            ("Alternator output", "V", ELECTRICAL),
            ("Boost pressure", "kPa", TURBO),
        ):
            channels.append(
                _channel(f"{brand}.{_slug(name)}", name, unit, group, source)
            )
    return channels


def _sim_fill(channels: list) -> list:
    t = time.time()
    out = []
    for channel in channels:
        filled = dict(channel)
        bare = channel["key"][len(_OBD_PREFIX):] if channel["key"].startswith(_OBD_PREFIX) else ""
        if bare in _TEXT_KEYS:
            pid = PIDS_BY_KEY.get(bare)
            filled["text"] = pid.decode(bytes([0x02 if bare == "fuel_system" else 0x01])) or "—"
            filled["value"] = None
            filled["graphable"] = False
        else:
            value = _sim_value(channel, t)
            filled["value"] = round(value, 3) if value is not None else None
            filled["text"] = "" if value is None else _format(value, channel["unit"])
            filled["graphable"] = value is not None
        out.append(filled)
    return out


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def catalog(adapter, mode: str, brand: str | None = None) -> dict:
    """Everything this car will actually give us, found by asking it.

    The standard layer is authoritative -- the car reports its own supported
    PID bitmap. The manufacturer layer is discovered by doing one real read,
    because a profile's DID list is a list of things worth *trying*; only the
    car says which of them exist.
    """
    if mode == "sim":
        channels = _sim_fill(_sim_catalog(brand))
        return {
            "ok": True, "simulated": True, "channels": channels,
            "groups": _groups_present(channels),
            "message": (
                f"SIMULATED — {len(channels)} channels, no real car connected."
            ),
            "timestamp": _now(),
        }

    channels, notes = [], []

    def run(session, entry):
        session.ensure_protocol()
        if entry.get("supported") is None:
            entry["supported"] = read_supported_pids(session)
        supported = entry["supported"] or set()
        found = []
        for pid_num in sorted(supported):
            for pid in PIDS_FOR_PID.get(pid_num, []):
                found.append(
                    _channel(_OBD_PREFIX + pid.key, pid.label, pid.unit,
                             pid.group, OBD_SOURCE)
                )
        return found, len(supported)

    try:
        channels, n_supported = obd_service._with_session(adapter, run)
        notes.append(
            f"{len(channels)} standard OBD-II values "
            f"(the car reports {n_supported} supported PIDs)"
        )
    except obd_service.ObdServiceError as exc:
        return {
            "ok": False, "simulated": False, "channels": [], "groups": [],
            "message": str(exc), "timestamp": _now(),
        }

    if brand:
        brand_channels, note = _discover_brand(adapter, mode, brand)
        channels.extend(brand_channels)
        if note:
            notes.append(note)

    return {
        "ok": True, "simulated": False, "channels": channels,
        "groups": _groups_present(channels),
        "message": " · ".join(notes),
        "timestamp": _now(),
    }


def _discover_brand(adapter, mode: str, brand: str):
    """Manufacturer channels, via the rich interface if the profile has one."""
    rich = _rich_brand_module(brand)
    source = _brand_source(brand)
    if rich is not None:
        channels = [
            _channel(f"{brand}.{c['key']}", c["label"], c.get("unit", ""),
                     c.get("group") or _group_for(c["label"], c.get("unit", "")),
                     source)
            for c in rich.CHANNELS
        ]
        return channels, f"{len(channels)} {brand} enhanced values"

    module = brand_registry.brand_module(brand, "pids")
    if module is None or not hasattr(module, "live"):
        # Say so rather than showing nothing. A brand with no enhanced layer is
        # a gap in our data, not a limitation of the car, and the user is
        # entitled to know which of the two they are looking at.
        meta = brand_registry.profile_meta(brand) or {}
        name = meta.get("name", brand.upper())
        return [], (
            f"No {name}-specific live values yet — standard OBD-II only. "
            f"{name}'s extra values are not publicly documented as readable "
            "identifiers, and inventing them would put wrong numbers on screen."
        )
    try:
        if brand_registry.needs_adapter(brand):
            result = module.live(adapter, mode)
        else:
            result = module.live(mode)
    except Exception as exc:  # a brand profile must never break the standard layer
        return [], f"{brand} enhanced values unavailable ({exc})"
    if not result.get("ok"):
        return [], result.get("message") or f"No {brand} enhanced values answered."
    channels = _brand_channels_from_live(brand, result)
    return channels, f"{len(channels)} {brand} enhanced values answered"


def read(adapter, mode: str, keys: list, brand: str | None = None) -> dict:
    """One pass over the requested channels. Missing channels are simply
    absent from the result -- a car that stops answering a PID mid-drive is a
    real thing and must not look like an error."""
    keys = [k for k in (keys or []) if k]
    if mode == "sim":
        wanted = set(keys)
        channels = [c for c in _sim_catalog(brand) if not wanted or c["key"] in wanted]
        return {
            "ok": True, "simulated": True, "readings": _sim_fill(channels),
            "message": "SIMULATED — no real car connected.", "timestamp": _now(),
        }

    obd_keys = [k[len(_OBD_PREFIX):] for k in keys if k.startswith(_OBD_PREFIX)]
    brand_keys = [k for k in keys if brand and k.startswith(f"{brand}.")]
    readings = []

    if obd_keys:
        def run(session, entry):
            session.ensure_protocol()
            if entry.get("supported") is None:
                entry["supported"] = read_supported_pids(session)
            supported = entry["supported"] or set()
            out = []
            # Group by PID: one query can carry several readings (an oxygen
            # sensor row is a voltage AND a trim), so never ask twice.
            by_pid = {}
            for key in obd_keys:
                pid = PIDS_BY_KEY.get(key)
                if pid is None or (supported and pid.pid not in supported):
                    continue
                by_pid.setdefault(pid.pid, []).append(pid)
            for pid_num, pid_rows in by_pid.items():
                try:
                    payloads = session.query(0x01, pid_num, timeout=4.0)
                except (ObdError, TransportError):
                    continue
                for payload in payloads:
                    if len(payload) < 2 or payload[1] != pid_num:
                        continue
                    for pid in pid_rows:
                        value = pid.decode(payload[2:])
                        if value is None:
                            continue
                        out.append(
                            _channel(_OBD_PREFIX + pid.key, pid.label, pid.unit,
                                     pid.group, OBD_SOURCE, value=value)
                        )
                    break
            return out

        try:
            readings.extend(obd_service._with_session(adapter, run))
        except obd_service.ObdServiceError as exc:
            return {
                "ok": False, "simulated": False, "readings": [],
                "message": str(exc), "timestamp": _now(),
            }

    if brand_keys:
        readings.extend(_read_brand(adapter, mode, brand, brand_keys))

    return {
        "ok": True, "simulated": False, "readings": readings,
        "message": "" if readings else "The car did not answer any of the selected values.",
        "timestamp": _now(),
    }


def _read_brand(adapter, mode: str, brand: str, keys: list) -> list:
    """Manufacturer values. With the rich interface only the wanted ones are
    read; without it the profile reads its whole table and we filter, which is
    correct but slow -- hence the note in the module docstring."""
    source = _brand_source(brand)
    rich = _rich_brand_module(brand)
    prefix = f"{brand}."
    bare = [k[len(prefix):] for k in keys]

    if rich is not None:
        try:
            rows = rich.read_channels(adapter, mode, bare)
        except Exception:
            return []
        meta = {c["key"]: c for c in rich.CHANNELS}
        out = []
        for row in rows or []:
            info = meta.get(row.get("key"), {})
            label = info.get("label") or row.get("key", "")
            unit = row.get("unit") or info.get("unit") or ""
            out.append(
                _channel(prefix + row["key"], label, unit,
                         info.get("group") or _group_for(label, unit),
                         source, text=str(row.get("value", "")))
            )
        return out

    module = brand_registry.brand_module(brand, "pids")
    if module is None or not hasattr(module, "live"):
        return []
    try:
        if brand_registry.needs_adapter(brand):
            result = module.live(adapter, mode)
        else:
            result = module.live(mode)
    except Exception:
        return []
    if not result.get("ok"):
        return []
    wanted = set(keys)
    return [c for c in _brand_channels_from_live(brand, result) if c["key"] in wanted]


def _groups_present(channels: list) -> list:
    """Group names in display order, only those that have channels."""
    present = {c["group"] for c in channels}
    ordered = [g for g in GROUP_ORDER if g in present]
    return ordered + sorted(present - set(ordered))
