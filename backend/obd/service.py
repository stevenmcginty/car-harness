"""High-level OBD-II operations the API exposes: scan / live / clear / pids.

Real mode drives a genuine ELM327 session over the chosen adapter and reports
exactly how far it got; sim mode returns clearly-labelled fakes from sim.py.

Sessions are cached per adapter instance so a scan followed by live-data
polling reuses one connection (opening a BLE link costs seconds). A session
idle for more than IDLE_SECONDS is closed and reopened; every operation is
serialised with a per-adapter lock because ELM327s are strictly one command
at a time.
"""

import threading
import time
from datetime import datetime, timezone

from backend.obd import dtc, readiness, sim
from backend.obd.elm327 import Elm327Session, ObdError
from backend.obd.pids import DASHBOARD_KEYS, PIDS_BY_KEY, read_supported_pids
from backend.obd.transport import TransportError, transport_for_adapter

IDLE_SECONDS = 90.0

_cache_lock = threading.Lock()
_sessions = {}  # adapter_id -> {"session", "last_used", "lock", "supported"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ObdServiceError(Exception):
    """Honest, user-facing failure for the API layer."""


def _entry_for(adapter: dict):
    adapter_id = adapter.get("id") or "anon"
    with _cache_lock:
        entry = _sessions.get(adapter_id)
        if entry is None:
            entry = {"session": None, "last_used": 0.0, "lock": threading.Lock(), "supported": None}
            _sessions[adapter_id] = entry
        return entry


def _open_session(adapter: dict, entry) -> Elm327Session:
    session = entry["session"]
    now = time.monotonic()
    if session is not None and session.opened and (now - entry["last_used"]) < IDLE_SECONDS:
        return session
    if session is not None:
        session.close()
        entry["session"] = None
        entry["supported"] = None
    transport = transport_for_adapter(adapter)
    session = Elm327Session(transport)
    session.open()
    entry["session"] = session
    return session


def _drop_session(entry):
    session = entry["session"]
    entry["session"] = None
    entry["supported"] = None
    if session is not None:
        session.close()


def close_all():
    """Close every cached session (called on server shutdown). Takes each
    entry's lock so an in-flight exchange finishes before its link is cut."""
    with _cache_lock:
        entries = list(_sessions.values())
        _sessions.clear()
    for entry in entries:
        with entry["lock"]:
            try:
                _drop_session(entry)
            except Exception:
                pass


def _with_session(adapter: dict, fn):
    """Run ``fn(session, entry)`` under this adapter's lock with one honest
    retry after a session drop (stale cached links die between calls)."""
    if not adapter:
        raise ObdServiceError(
            "No adapter selected. Pick an ELM327-style adapter (top right) or "
            "turn on Simulate."
        )
    entry = _entry_for(adapter)
    with entry["lock"]:
        attempts = 0
        while True:
            attempts += 1
            try:
                session = _open_session(adapter, entry)
                result = fn(session, entry)
                entry["last_used"] = time.monotonic()
                return result
            except (TransportError, ObdError) as exc:
                had_session = entry["session"] is not None
                _drop_session(entry)
                if attempts >= 2 or not had_session:
                    raise ObdServiceError(str(exc))
                # else: retry once from scratch with a fresh connection


def _read_vin(session):
    try:
        payloads = session.query(0x09, 0x02, timeout=10.0)
    except (ObdError, TransportError):
        return None
    for payload in payloads:
        if len(payload) < 4 or payload[1] != 0x02:
            continue
        raw = payload[3:]  # 49 02 <count> then the VIN bytes
        text = "".join(chr(b) for b in raw if 32 < b < 127)
        text = text.strip("\x00 ")
        if len(text) >= 11:  # tolerate slightly odd ECUs but never invent data
            return text[:17]
    return None


def scan(adapter: dict, mode: str) -> dict:
    if mode == "sim":
        return sim.scan()

    def run(session, entry):
        session.ensure_protocol()
        vin = _read_vin(session)

        mil_on = False
        dtc_count = 0
        monitors = None
        try:
            payloads = session.query(0x01, 0x01, timeout=8.0)
            for payload in payloads:
                if len(payload) >= 3 and payload[1] == 0x01:
                    mil_on = mil_on or bool(payload[2] & 0x80)
                    dtc_count += payload[2] & 0x7F
                    # The same response carries the emissions readiness monitors;
                    # we were throwing three of its four bytes away.
                    if monitors is None:
                        monitors = readiness.decode_monitors(payload[2:])
        except (ObdError, TransportError):
            pass

        def read_pid(key):
            """One mode-01 PID, or None if the car does not answer it."""
            pid = PIDS_BY_KEY.get(key)
            if pid is None:
                return None
            try:
                payloads = session.query(0x01, pid.pid, timeout=4.0)
            except (ObdError, TransportError):
                return None
            for payload in payloads:
                if len(payload) >= 2 and payload[1] == pid.pid:
                    return pid.decode(payload[2:])
            return None

        since_clear = {
            "distanceKm": read_pid("distance_since_clear"),
            "minutes": read_pid("minutes_since_clear"),
            "warmups": read_pid("warmups_since_clear"),
        }
        since_clear = {k: v for k, v in since_clear.items() if v is not None}

        def read(service):
            try:
                return dtc.read_codes(session, service)
            except (ObdError, TransportError):
                return None  # None = "couldn't read", [] = "genuinely none"

        stored = read(0x03)
        pending = read(0x07)
        permanent = read(0x0A)

        def codes(lst, status):
            if lst is None:
                return []
            return [
                {"code": c, "description": dtc.describe(c), "status": status}
                for c in lst
            ]

        n_stored = len(stored or [])
        if n_stored == 0 and dtc_count > 0:
            summary = (
                f"The car reports {dtc_count} fault(s) but they are in modules "
                "this generic scan can't list — a manufacturer-specific scan "
                "would be needed."
            )
        elif n_stored == 0:
            summary = "No stored fault codes." + (" MIL (engine light) is ON." if mil_on else "")
        else:
            summary = f"{n_stored} stored fault code(s)." + (
                " MIL (engine light) is ON." if mil_on else ""
            )

        assessment = readiness.assess(
            monitors, since_clear, len(permanent or []), n_stored
        )
        if assessment["recentlyCleared"]:
            summary += " " + assessment["verdict"]

        return {
            "ok": True,
            "simulated": False,
            "vin": vin,
            "protocol": session.protocol or "unknown",
            "voltage": session.voltage(),
            "firmware": session.firmware,
            "milOn": mil_on,
            "dtcCount": max(dtc_count, n_stored),
            "stored": codes(stored, "stored"),
            "pending": codes(pending, "pending"),
            "permanent": codes(permanent, "permanent"),
            "readiness": monitors,
            "sinceClear": since_clear,
            "clearCheck": assessment,
            "message": summary + ("" if vin else " (The car did not report a VIN over OBD.)"),
            "timestamp": _now(),
        }

    return _with_session(adapter, run)


def clear(adapter: dict, mode: str) -> dict:
    if mode == "sim":
        return sim.clear()

    def run(session, entry):
        session.ensure_protocol()
        try:
            session.query(0x04, None, timeout=10.0)
        except ObdError as exc:
            if exc.kind != "no_data":  # many cars answer mode 04 with no payload
                raise
        return {
            "ok": True,
            "simulated": False,
            "cleared": True,
            "message": (
                "Fault codes cleared and the MIL reset. Codes will return if "
                "the underlying fault is still present. Readiness monitors are "
                "reset too (matters for an MOT emissions check)."
            ),
            "timestamp": _now(),
        }

    return _with_session(adapter, run)


def live(adapter: dict, mode: str, keys=None) -> dict:
    if mode == "sim":
        return sim.live(keys)

    wanted = [k for k in (keys or DASHBOARD_KEYS) if k in PIDS_BY_KEY]

    def run(session, entry):
        session.ensure_protocol()
        if entry["supported"] is None:
            entry["supported"] = read_supported_pids(session)
        supported = entry["supported"]

        readings = []
        for key in wanted:
            pid = PIDS_BY_KEY[key]
            if supported and pid.pid not in supported:
                continue
            try:
                payloads = session.query(0x01, pid.pid, timeout=4.0)
            except (ObdError, TransportError):
                continue
            for payload in payloads:
                if len(payload) >= 2 and payload[1] == pid.pid:
                    value = pid.decode(payload[2:])
                    if value is not None:
                        readings.append(
                            {
                                "key": key,
                                "label": pid.label,
                                "value": value,
                                "unit": pid.unit,
                            }
                        )
                        break
        return {
            "ok": True,
            "simulated": False,
            "readings": readings,
            "message": "" if readings else (
                "The car answered but none of the requested PIDs returned data."
            ),
            "timestamp": _now(),
        }

    return _with_session(adapter, run)


def supported_pids(adapter: dict, mode: str) -> dict:
    if mode == "sim":
        return sim.supported()

    def run(session, entry):
        session.ensure_protocol()
        if entry["supported"] is None:
            entry["supported"] = read_supported_pids(session)
        keys = [p.key for p in PIDS_BY_KEY.values() if p.pid in entry["supported"]]
        return {
            "ok": True,
            "simulated": False,
            "keys": keys,
            "message": "",
            "timestamp": _now(),
        }

    return _with_session(adapter, run)
