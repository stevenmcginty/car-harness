"""Mercedes 12 V battery: read the energy manager, and register a replacement.

WHY THIS FILE EXISTS
--------------------
Fitting a new battery to a car with ECO start/stop is not just a mechanical
job. The car keeps a running model of the battery it thinks it has -- capacity,
state of charge, internal resistance, a charge balance built up over months --
and it goes on applying that model to the new battery until it is told
otherwise. On a Mercedes that telling is what "battery registration" means.

WHERE IT LIVES ON THIS PLATFORM
-------------------------------
Not where the folklore says. On the MFA cars (W117 CLA, W176 A-Class, X156 GLA)
the 12 V energy management (the AGK/ESOC/EDIAG signal family) lives INSIDE the
engine ECU -- CRD3-MFA1 at 0x7E0/0x7E8, the same ECU the cold-start work
already talks to. Mined from CRD3MFA1.cbf:

  * The IBS current sensor (0x74F/0x757 on the 125 kbit chassis CAN, and
    0x6D5/0x6B0) carries F1xx identification DIDs only. Nothing to register.
  * The classic rear-SAM "Batteriewechsel" write (DID 0x0562, charge balance
    index) exists for W204/W212/W231 and NOT for this platform.

So a plain ELM327 on the OBD port reaches it, on 11-bit 500 kbit CAN, with no
DoIP and no dealer transport. And the mined per-service security level for
every routine used here is 0: no SecurityAccess, which is the one thing that
would have made this XENTRY-only (the seed/key algorithm is not in the corpus).

WHAT THIS MODULE WILL AND WILL NOT DO
-------------------------------------
`register()` defaults to the conservative half of the job: routine 03A0, "set
or reset currentless condition", which tells the energy manager the car was
disconnected from its battery, plus a full read of the battery block before and
after so the change is visible rather than asserted.

Two deliberate limits:

  * 03A2 "set battery capacity" is only sent when a capacity is passed in.
    The CBF gives four payload bytes, declares one 16-bit input at byte 4, and
    says nothing at all about scaling; the ampere-seconds/10 reading used here
    is inference from read-back DID 6701, not mined fact. A like-for-like swap
    (same Ah, same chemistry) does not need it, so the default is not to send
    it.
  * 03A3 "set OCV characteristic" is refused outright. It writes the 21-point
    open-circuit-voltage curve that IS the battery's chemistry on this
    platform -- there is no separate AGM/flooded byte. The only payload the
    corpus carries is 42 zero bytes, and sending that would replace the
    battery model with zeros. No correct AGM curve was mined, so the harness
    does not offer one.

Read `data/om651_battery.json` for the mined provenance and for which parts of
this are fact and which are inference.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient
from backend.profiles.mercedes.scan import MERCEDES_ID_MAP

ENGINE = 0x7E0

DATA_PATH = Path(__file__).resolve().parent / "data" / "om651_battery.json"

# Ampere-seconds per ampere-hour. The ECU works in As throughout; every human
# on the planet reads the label in Ah.
_AS_PER_AH = 3600.0

# 03A2 carries the capacity as a 16-bit field. The read-back DID 6701 reports
# capacity as (u16 x 10) ampere-seconds, so the same packing is assumed for the
# write. INFERENCE -- see the module docstring.
_CAPACITY_AS_PER_COUNT = 10.0
_CAPACITY_MAX_COUNT = 0xFFFF


def _load() -> dict:
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except OSError:
        return {}


DATA = _load()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _readings() -> List[dict]:
    return DATA.get("readings", [])


def _routine(key: str) -> dict:
    return (DATA.get("routines") or {}).get(key) or {}


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #
def decode_reading(spec: dict, reply: bytes) -> dict:
    """One 0x22 reply into a number, keeping the raw bytes either way.

    start_bit in the mined table is absolute in the whole reply and includes
    the `62 <DID>` header, which is why it starts at 24 rather than 0.
    """
    row = {"did": spec["did"], "label": spec["label"], "unit": spec["unit"],
           "why": spec.get("why"), "value": None, "ah": None, "raw": None}
    if not reply:
        row["error"] = "no reply"
        return row
    row["raw"] = reply.hex().upper()
    if reply[0] == 0x7F:
        row["error"] = f"negative response, NRC 0x{reply[2]:02X}" \
            if len(reply) >= 3 else "negative response"
        return row

    off, size = spec["start_bit"] // 8, spec["length_bits"] // 8
    if len(reply) < off + size:
        row["error"] = "reply shorter than the mined field"
        return row
    chunk = reply[off:off + size]
    row["rawField"] = chunk.hex().upper()
    raw = int.from_bytes(chunk, "big")
    offset = spec.get("offset", 0.0)
    # Only channels whose mined offset is a large negative (current: -1000 A,
    # temperature: -50 degC) are signed-by-offset; the rest are plain unsigned.
    if offset <= -50.0 and raw >= 1 << (8 * size - 1):
        raw -= 1 << (8 * size)
    row["value"] = round(raw * spec.get("multiplier", 1.0) + offset, 4)
    if spec.get("also_ah") and row["value"] is not None:
        row["ah"] = round(row["value"] / _AS_PER_AH, 2)
    return row


def _send(client: Elm327UdsClient, payload: str, timeout: float = 3.0) -> dict:
    """One request. A negative response is recorded, not raised."""
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
        "0x33": "SecurityAccess required. The mined table says level 0 for "
                "this routine, so either the mining is wrong or this car's "
                "software differs from the CBF.",
        "0x22": "conditions not correct -- the ECU wants a state it does not "
                "have. Ignition on, engine off is the usual want for an "
                "energy-management routine.",
        "0x31": "request out of range -- this routine id is not in this car's "
                "software even though the CBF lists it.",
        "0x13": "wrong message length -- the payload byte count is not what "
                "this car's software expects for this routine.",
    }.get(nrc)


def _read_block(client: Elm327UdsClient) -> List[dict]:
    rows = []
    for spec in _readings():
        sent = _send(client, spec["payload"])
        if sent["error"] or sent["nrc"]:
            rows.append({"did": spec["did"], "label": spec["label"],
                         "unit": spec["unit"], "why": spec.get("why"),
                         "value": None, "ah": None, "raw": sent["reply"],
                         "error": sent["error"]
                         or f"negative response, NRC {sent['nrc']}"})
            continue
        rows.append(decode_reading(spec, bytes.fromhex(sent["reply"])))
    return rows


# --------------------------------------------------------------------------- #
# Reference, no car
# --------------------------------------------------------------------------- #
def catalogue() -> dict:
    """What was mined and how far it can be trusted. Touches no car."""
    routines = DATA.get("routines") or {}
    return {"ok": True, "title": DATA.get("title"),
            "provenance": DATA.get("provenance"),
            "notes": DATA.get("notes", []),
            "readings": [{k: r[k] for k in ("did", "label", "unit", "why")}
                         for r in _readings()],
            "routines": {k: {"routineId": v.get("routine_id"),
                             "title": v.get("title"),
                             "why": v.get("why"),
                             "inputNote": v.get("input_note"),
                             "blocked": bool(v.get("blocked"))}
                         for k, v in routines.items()},
            "message": ("Mercedes 12 V energy management on CRD3-MFA1 (0x7E0). "
                        "No SecurityAccess needed. 03A3 (OCV curve) is blocked "
                        "-- the only mined payload is all zeros."),
            "timestamp": _now()}


def _sim_rows() -> List[dict]:
    return [{"did": r["did"], "label": r["label"], "unit": r["unit"],
             "why": r.get("why"), "value": None, "ah": None, "raw": None,
             "error": "simulated"} for r in _readings()]


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #
def status(mode: str = "real", adapter: Optional[dict] = None) -> dict:
    """The energy manager's own view of the battery. Read-only (0x22)."""
    if mode == "sim":
        return {"ok": True, "simulated": True, "readings": _sim_rows(),
                "message": "SIMULATED -- no real car connected.",
                "timestamp": _now()}

    def _go(session, entry):
        session.ensure_protocol()
        client = Elm327UdsClient(session, MERCEDES_ID_MAP)
        client.begin()
        try:
            opened = _send(client, "1003", timeout=3.0)
            rows = _read_block(client)
        finally:
            client.restore()
        good = [r for r in rows if r.get("value") is not None]
        return {"ok": bool(good), "simulated": False,
                "session": opened, "readings": rows,
                "message": (f"{len(good)}/{len(rows)} battery channel(s) read "
                            "from the engine ECU's energy manager."),
                "timestamp": _now()}

    try:
        return obd_service._with_session(adapter, _go)
    except obd_service.ObdServiceError as exc:
        return {"ok": False, "simulated": False, "message": str(exc),
                "timestamp": _now()}


# --------------------------------------------------------------------------- #
# Register
# --------------------------------------------------------------------------- #
def _capacity_payload(capacity_ah: float) -> tuple[Optional[str], Optional[str]]:
    """The 03A2 payload for a capacity in Ah, or a reason it cannot be built."""
    counts = round(capacity_ah * _AS_PER_AH / _CAPACITY_AS_PER_COUNT)
    if not 1 <= counts <= _CAPACITY_MAX_COUNT:
        return None, (f"{capacity_ah} Ah does not fit the 16-bit field under "
                      f"the assumed scaling ({counts} counts).")
    return f"310103A2{counts:04X}0000", None


def register(mode: str = "real", confirm: bool = False,
             adapter: Optional[dict] = None,
             capacity_ah: Optional[float] = None) -> dict:
    """Register a replacement 12 V battery. CHANGES THE CAR.

    Default (capacity_ah omitted) is the conservative registration: read the
    battery block, send routine 03A0 "set or reset currentless condition", read
    the block again. That is the right and sufficient action for a like-for-like
    swap -- same capacity, same chemistry -- because the capacity and the OCV
    curve already in the ECU still describe the battery that is fitted.

    Passing capacity_ah ALSO sends 03A2 "set battery capacity". Only do that
    when the new battery's capacity genuinely differs, and read the result:
    the scaling is inferred from read-back DID 6701, not mined, so the write
    is verified by comparing 6153/6154/6701 before and after rather than
    trusted.

    The OCV characteristic (03A3) is never sent. See the module docstring.
    """
    routine = _routine("currentless")
    if not routine.get("start", {}).get("payload"):
        return {"ok": False, "message": "The 03A0 routine is not in the mined "
                                        "table; nothing to send.",
                "timestamp": _now()}

    plan = ["03A0 set/reset currentless condition (mined default byte 0x00)"]
    cap_payload = None
    if capacity_ah is not None:
        cap_payload, why_not = _capacity_payload(capacity_ah)
        if cap_payload is None:
            return {"ok": False, "message": why_not, "timestamp": _now()}
        plan.append(f"03A2 set battery capacity to {capacity_ah} Ah "
                    f"({cap_payload}) -- INFERRED scaling")

    if confirm is not True:
        return {"ok": False, "confirmed": False, "plan": plan,
                "message": ("Refused: registering a battery WRITES to the "
                            "engine ECU's energy manager (UDS 0x31). "
                            + routine["why"]
                            + " Ignition on, engine off. Pass confirm=true "
                              "once the person at the car has agreed."),
                "timestamp": _now()}

    if mode == "sim":
        return {"ok": True, "simulated": True, "plan": plan,
                "before": _sim_rows(), "after": _sim_rows(), "steps": [],
                "message": "SIMULATED -- no real car connected.",
                "timestamp": _now()}

    def _go(session, entry):
        session.ensure_protocol()
        client = Elm327UdsClient(session, MERCEDES_ID_MAP)
        client.begin()
        steps: List[dict] = []
        try:
            opened = _send(client, "1003", timeout=3.0)
            opened["step"] = "extended session (10 03)"
            steps.append(opened)

            before = _read_block(client)

            sent = _send(client, routine["start"]["payload"], timeout=5.0)
            sent["step"] = f"currentless ({routine['start']['name']})"
            sent["hint"] = _nrc_hint(sent.get("nrc"))
            steps.append(sent)

            if cap_payload is not None and sent["ok"]:
                cap = _send(client, cap_payload, timeout=5.0)
                cap["step"] = "set battery capacity (RT_03A2)"
                cap["hint"] = _nrc_hint(cap.get("nrc"))
                steps.append(cap)

            after = _read_block(client)
        finally:
            client.restore()

        changed = []
        for old, new in zip(before, after):
            if old.get("raw") != new.get("raw"):
                changed.append({"did": new["did"], "label": new["label"],
                                "unit": new["unit"],
                                "before": old.get("value"),
                                "after": new.get("value"),
                                "beforeAh": old.get("ah"),
                                "afterAh": new.get("ah")})

        nrcs = [s for s in steps if s.get("nrc")]
        accepted = any(s.get("ok") and s.get("step", "").startswith("currentless")
                       for s in steps)
        return {
            "ok": accepted,
            "simulated": False,
            "plan": plan,
            "before": before,
            "after": after,
            "changed": changed,
            "steps": steps,
            "message": (
                ("The ECU accepted the currentless reset. "
                 if accepted else "The ECU did NOT accept the currentless "
                                  "reset. ")
                + (f"{len(changed)} channel(s) changed across the write. "
                   if changed else "No battery channel changed value across "
                                   "the write -- expected for a bookkeeping "
                                   "reset, and not by itself a failure. ")
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
        return {"ok": False, "simulated": False, "message": str(exc),
                "timestamp": _now()}
