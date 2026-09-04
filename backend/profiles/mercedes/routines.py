"""OM651 (CRD3-MFA1) XENTRY guided tests over a generic OBD dongle.

WHY THIS FILE EXISTS
--------------------
The cold-start capture in coldstart.py watches a start happen. It cannot make
one happen, and it cannot ask the ECU to measure its own fuel system. XENTRY
can, and the routines it uses were mined out of CRD3MFA1.cbf into
data/om651_tests.json by tools/dealer_index/build_xentry_om651_tests.py:

    dyn_hydraulic (F004)  spins the starter and reports the AVERAGED rail
                          pressure during cranking + engine speed at the end
    high_pressure (032A)  rise time, leak time, pressure reached, pressure at
                          the end of the test
    rail_air (0327)       purges air from the high pressure circuit
    lift_pump (F10D)      runs the in-tank low pressure pump
    glow_control (F104)   commands the glow controller and reports its result
    compression (0325)    the ECU's own cranking compression comparison
    injector_0..3         per-injector activation
    imv_diagnosis (F00F)  the rail pressure error table

THESE ARE NOT READ-ONLY. Service 0x31 tells the ECU to do something, and
several of these spin the starter, drive injectors or run the fuel pump. That
is a different kind of act from the 0x22 reads everywhere else in this profile,
so every run here is gated behind an explicit confirm and stop is always sent,
including when the run fails partway.

Everything is graded `candidate`: no routine here has run on a car yet. A
routine the ECU refuses comes back as a recorded NRC, not an exception -- a
refusal IS a result and usually means the ECU wants SecurityAccess or a
condition (engine off, ignition on, fuel level) this code cannot see.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Dict, List, Optional

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient
from backend.profiles.mercedes.scan import MERCEDES_ID_MAP

ENGINE = 0x7E0
DATA_PATH = Path(__file__).resolve().parent / "data" / "om651_tests.json"

_POLL_S = 1.0           # gap between 31 03 result polls
_DEFAULT_WAIT_S = 30.0  # how long to wait for TEST_FINISHED
_FINISHED = "TEST_FINISHED"
_RUNNING = "TEST_IN_PROGRESS"


def _load() -> dict:
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except OSError:
        return {}


TESTS = _load()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _routines() -> Dict[str, dict]:
    return TESTS.get("routines", {})


# --------------------------------------------------------------------------- #
# Reading the reference without a car
# --------------------------------------------------------------------------- #
def catalogue(key: Optional[str] = None) -> dict:
    """The mined routine list, and the cold-start fault codes beside it.

    No car needed. This is what to read BEFORE deciding to run anything: each
    row says what the routine measures, whether it commands the engine, and
    which bytes of the reply carry which number.
    """
    routines = _routines()
    if key is not None and key not in routines:
        return {"ok": False,
                "message": f"Unknown routine '{key}'. Known: {sorted(routines)}.",
                "routines": {}, "dtcs": []}
    chosen = {key: routines[key]} if key else routines
    return {"ok": True,
            "title": TESTS.get("title"),
            "provenance": TESTS.get("provenance"),
            "notes": TESTS.get("notes", []),
            "routines": chosen,
            "dtcCount": len(TESTS.get("dtcs", [])),
            "message": (f"{len(chosen)} routine(s), all graded candidate. "
                        "Service 0x31 COMMANDS the car -- read `why` and "
                        "`cranks_or_commands_engine` before running one.")}


def lookup_dtcs(codes: List[str]) -> dict:
    """XENTRY fault text for codes a scan returned, cold-start families only.

    Codes are matched on the 5-character SAE stem (P0087 out of P008721) as
    well as exactly, because a dongle reports the stem and XENTRY stores the
    stem plus its two-digit fault type.
    """
    table = TESTS.get("dtcs", [])
    out = []
    for code in codes:
        stem = (code or "").strip().upper()[:5]
        hits = [d for d in table if (d["code"] or "").upper().startswith(stem)]
        out.append({"code": code, "matches": hits})
    return {"ok": True, "results": out,
            "message": f"{sum(1 for r in out if r['matches'])}/{len(out)} "
                       "code(s) found in the mined OM651 cold-start table."}


# --------------------------------------------------------------------------- #
# Decoding a routine reply
# --------------------------------------------------------------------------- #
def decode_field(field: dict, reply: bytes, endian: str = "big") -> dict:
    """One mined result field out of a 0x71 reply.

    byte_offset here is absolute in the whole reply INCLUDING the service byte,
    which is how the CBF's start_bit is expressed -- unlike the 0x22 tables,
    whose offsets start after the `62 <DID>` header.
    """
    off, size = field["byte_offset"], field["byte_size"]
    row = {"label": field.get("label"), "name": field.get("name"),
           "unit": field.get("unit"), "byte_offset": off,
           "value": None, "state": None, "raw": None}
    if len(reply) < off + size:
        row["error"] = "reply shorter than this field's offset"
        return row
    chunk = reply[off:off + size]
    row["raw"] = chunk.hex().upper()
    raw = int.from_bytes(chunk, endian)
    conv = field.get("conversion", {})
    kind = conv.get("type")
    if kind == "linear":
        offset = conv.get("offset", 0.0)
        if offset <= -100.0 and raw >= 1 << (8 * size - 1):
            raw -= 1 << (8 * size)      # only channels that can go negative
        row["value"] = round(raw * conv.get("multiplier", 1.0) + offset, 4)
    elif kind == "table":
        row["value"] = float(raw)
        for state in conv.get("states", []):
            if state["start"] <= raw <= state["end"]:
                row["state"] = state["name"]
                break
        if row["state"] is None:
            row["state"] = f"unlisted({raw})"
    elif kind == "bool":
        row["value"] = float(raw)
        row["state"] = conv.get("true") if raw else conv.get("false")
    elif kind == "string":
        row["state"] = "".join(chr(b) for b in chunk if 32 <= b < 127).strip()
    else:
        row["value"] = float(raw)
    return row


def _status_of(fields: List[dict]) -> Optional[str]:
    for row in fields:
        if row.get("name") == "ROUTINE_STATUS":
            return row.get("state")
    return None


def _send(client: Elm327UdsClient, payload: str,
          timeout: float = 3.0) -> dict:
    """One request. A negative response is recorded, never raised: an NRC from
    a guided test is the most informative answer it can give."""
    row = {"request": payload.upper(), "reply": None, "ok": False,
           "nrc": None, "error": None}
    try:
        reply = client.request(ENGINE, bytes.fromhex(payload), timeout=timeout)
    except (ObdError, TransportError) as exc:
        row["error"] = str(exc)
        return row
    if not reply:
        row["error"] = "no reply"
        return row
    row["reply"] = reply.hex().upper()
    if reply[0] == 0x7F:
        row["nrc"] = f"0x{reply[2]:02X}" if len(reply) >= 3 else "unknown"
        return row
    row["ok"] = True
    return row


def _nrc_hint(nrc: Optional[str]) -> Optional[str]:
    return {
        "0x7F": "service not supported in the active session -- the extended "
                "session (10 03) did not take.",
        "0x33": "SecurityAccess required. The mined corpus does not carry the "
                "seed/key algorithm for this ECU, so this routine is "
                "XENTRY-only for us.",
        "0x22": "conditions not correct -- the ECU wants a state it does not "
                "have (engine off vs running, coolant temperature, fuel "
                "level, gear selector, bonnet). Read the routine's `why` and "
                "set the car up for it.",
        "0x31": "request out of range -- this routine id or sub-function is "
                "not in this car's software, even though the CBF lists it.",
        "0x24": "request sequence error -- start before results.",
    }.get(nrc)


# --------------------------------------------------------------------------- #
# Running one
# --------------------------------------------------------------------------- #
def _run_client(client: Elm327UdsClient, routine: dict, wait_s: float,
                endian: str) -> dict:
    steps: List[dict] = []

    session = _send(client, "1003", timeout=3.0)
    session["step"] = "extended session (10 03)"
    steps.append(session)

    start = routine.get("start")
    if not start or not start.get("payload"):
        return {"steps": steps, "fields": [], "status": None,
                "finished": False,
                "error": "no start payload mined for this routine"}
    # A start that declares input parameters is NOT automatically unusable.
    # The CBF labels almost every input "Injektorcode" regardless of what it
    # really is, and the mined payload already carries the bytes those inputs
    # would fill. On 032A that byte is the request-type selector, and its
    # mined default 0x00 means "take the test parameters from the control
    # unit's own data set" -- which is exactly what a tester wants and is
    # safer than inventing values. So the payload is sent as mined, and the
    # result records that the inputs were left at their defaults rather than
    # pretending none existed.
    if start.get("needs_input"):
        steps.append({"step": "note", "request": None, "reply": None,
                      "ok": True, "nrc": None, "error": None,
                      "hint": "This routine declares input parameters. The "
                              "mined payload's own default bytes were sent "
                              "unchanged -- nothing was invented."})

    started = _send(client, start["payload"], timeout=5.0)
    started["step"] = f"start ({start['name']})"
    started["hint"] = _nrc_hint(started.get("nrc"))
    steps.append(started)

    fields: List[dict] = []
    status = None
    finished = False
    results = routine.get("results") or {}
    payload = results.get("payload")

    if started["ok"] and payload:
        deadline = monotonic() + max(1.0, wait_s)
        while True:
            poll = _send(client, payload, timeout=3.0)
            poll["step"] = "results (31 03)"
            poll["hint"] = _nrc_hint(poll.get("nrc"))
            steps.append(poll)
            if poll["ok"]:
                reply = bytes.fromhex(poll["reply"])
                fields = [decode_field(f, reply, endian)
                          for f in results.get("fields", [])]
                status = _status_of(fields)
                if status == _FINISHED:
                    finished = True
                    break
                if status is not None and status != _RUNNING:
                    break   # aborted / not launched -- polling will not fix it
            if monotonic() >= deadline:
                break
            sleep(_POLL_S)

    # Stop ALWAYS, including after a failed start: an ECU left with a guided
    # test half-open is a car that behaves oddly until the next ignition cycle.
    stop = routine.get("stop")
    if stop and stop.get("payload"):
        stopped = _send(client, stop["payload"], timeout=3.0)
        stopped["step"] = f"stop ({stop['name']})"
        steps.append(stopped)

    return {"steps": steps, "fields": fields, "status": status,
            "finished": finished, "error": None}


def run(adapter: Optional[dict], key: str, confirm: bool = False,
        wait_seconds: float = _DEFAULT_WAIT_S, endian: str = "big",
        mode: str = "real") -> dict:
    """Run one mined XENTRY guided test -- COMMANDS THE CAR.

    confirm must be True, and only after the person at the car has agreed and
    the car is set up for it. Several of these spin the starter.

    endian: the mined table declares BigEndian for every parameter, but the
    only Mercedes ECU on file that has actually answered contradicted that, so
    this is adjustable and every field keeps its raw bytes either way.
    """
    routines = _routines()
    if key not in routines:
        return {"ok": False, "message": f"Unknown routine '{key}'. "
                                        f"Known: {sorted(routines)}.",
                "timestamp": _now()}
    routine = routines[key]
    if confirm is not True:
        return {"ok": False, "routine": key, "title": routine["title"],
                "cranks_or_commands_engine": routine["cranks_or_commands_engine"],
                "message": ("Refused: this is an ACTIVE test (UDS 0x31), not a "
                            "read. " + routine["why"] + " Pass confirm=true "
                            "once the person at the car has agreed and the car "
                            "is safe for it."),
                "timestamp": _now()}
    if endian not in ("big", "little"):
        return {"ok": False, "message": "endian must be 'big' or 'little'.",
                "timestamp": _now()}
    if mode == "sim":
        return {"ok": True, "simulated": True, "routine": key,
                "title": routine["title"], "steps": [], "fields": [],
                "status": None, "finished": False,
                "message": "SIMULATED -- no real car connected.",
                "timestamp": _now()}

    def _go(session, entry):
        session.ensure_protocol()
        client = Elm327UdsClient(session, MERCEDES_ID_MAP)
        client.begin()
        try:
            result = _run_client(client, routine, wait_seconds, endian)
        finally:
            client.restore()

        nrcs = [s for s in result["steps"] if s.get("nrc")]
        named = [f"{f['label']}: "
                 f"{f['state'] if f['state'] is not None else f['value']}"
                 f"{' ' + f['unit'] if f.get('unit') and f['state'] is None else ''}"
                 for f in result["fields"] if f.get("raw")]
        return {
            "ok": result["finished"] or bool(result["fields"]),
            "simulated": False,
            "routine": key, "routineId": routine["routine_id"],
            "title": routine["title"],
            "endian": endian,
            "status": result["status"],
            "finished": result["finished"],
            "fields": result["fields"],
            "steps": result["steps"],
            "message": (
                (result["error"] + " " if result["error"] else "")
                + f"{routine['title']} -> status {result['status'] or 'none'}. "
                + ("; ".join(named) + ". " if named else "")
                + (f"{len(nrcs)} negative response(s): "
                   + ", ".join(f"{s['step']} NRC {s['nrc']}"
                               + (f" ({s['hint']})" if s.get("hint") else "")
                               for s in nrcs) + ". " if nrcs else "")
                + "Graded candidate -- first run of this routine on any car."),
            "timestamp": _now(),
        }

    try:
        return obd_service._with_session(adapter, _go)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "routine": key,
                "message": str(exc), "timestamp": _now()}
