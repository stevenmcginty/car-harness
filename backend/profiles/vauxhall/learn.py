"""Transmission Adaptive Values Learn on the Astra K T76 -- the executable path.

WHAT THIS IS, STATED HONESTLY BEFORE ANYTHING ELSE
---------------------------------------------------
This module can put GMW3110 DeviceControl frames on a car's transmission
controller. Until now the harness could describe this routine and refuse to run
it; now it can run it.

**Nothing in here has ever been seen on a wire.** The frames were recovered by
static analysis of the GDS2 dealer database, re-derived independently from raw
file offsets, and reproduced byte for byte across five separately built
deliverables. That is the strongest evidence a static decode can carry and it
is still not the same kind of evidence as a car. The procedure's own
``verified`` flag stays False, every operator-facing string says so, and the
first person to run this is running it for the first time.

Three things remain genuinely unknown, and only a car settles them:

* whether the module demands SecurityAccess, and answers NRC 0x33;
* whether an ELM327 over BLE can hold TesterPresent inside the module's
  timeout for the length of the routine;
* how long the routine actually takes, and what its status parameter really
  does while it runs -- the progression in sim_tcm.py is invented.

WHAT SHAPES THE CODE
--------------------
The dangerous failure is an aborted learn, not a refused one. A module that
says no has done nothing. A module that starts and does not finish has cleared
its adaptive cells without writing new ones, which is a worse gearbox than the
one that arrived, and the condition P287C exists to flag.

So the release is not a step this sequence takes at the end. It is
``ControlSession.__exit__`` in gmlan_control.py, and it runs on every path out
of the block -- normal completion, module refusal, module abort, keep-alive
loss, operator abort, dongle disconnect, and any unexpected exception. There is
no release call in the happy path here to forget, because there is no release
call here at all. The self-tests drive each of those paths and assert the
release happened.

WHAT THE OPERATOR HAS TO DO, AND WHY IT IS NOT ONE BUTTON
----------------------------------------------------------
Two confirmations, both per-run, neither sticky:

1. ``confirm=True``. Nothing sends $AE on a default call.
2. ``temperature_ack``: the fluid temperature in °C, as the operator has just
   read it on screen.

The second one exists because of a genuine unresolved ambiguity in the dealer
data. GDS2 gates this test on ``X(0)>84`` against the fluid-temperature
parameter. Read as the converted value that is 84 °C; read as the raw byte
under the same -40 offset it is 44 °C. procedures.py argues for 84 °C for three
reasons and this module gates on 84 °C -- the conservative reading, and the one
consistent with the module's own 86 °C classification boundary. But the
harness will not quietly pick a number that somebody might time a fast-learn
attempt by, so it shows both readings, shows the live temperature, and requires
the operator to type back what they can see. An acknowledgement that disagrees
with the live reading is refused, which makes it a check rather than a
formality.

THE PRECONDITIONS ARE READ, NOT TRUSTED
----------------------------------------
Every check in GDS2's own condition set ``Park/Neutral & Transmission Temp
High`` is a service 0x22 read, so all three are taken live before anything is
sent, and their comparison equations are evaluated from the data file rather
than reimplemented. A check whose equation has no implemented rule, or whose
parameter did not answer, counts as FAILED. Silence is not consent: a
precondition that could not be read is a precondition that is not known to
hold.

The module's own readiness parameter is read too, and its 17-state table is
what actually decides. The module enforces brake, selector, engine speed and
road speed itself and reports which one it is waiting for, which is a better
gate than anything the tool can check up front.
"""

import threading
import time
from datetime import datetime, timezone
from typing import Callable, List, Optional

from backend.core import registry
from backend.obd import service as obd_service
from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError
from backend.obd.uds_elm327 import Elm327UdsClient

from . import dealer_data
from .gmlan_control import (
    ControlRefused,
    ControlSession,
    ControlTransport,
    DeviceControlError,
    ForbiddenFrame,
    KeepAlive,
    KeepAliveLost,
    NotConfirmed,
    OperatorAbort,
    Transcript,
    build_control_frame,
    build_null_cpid_frame,
    format_frame,
)
from .sim_tcm import SimulatedTcm

# --------------------------------------------------------------------------- #
# What this procedure is bound to. All of it traceable to the extract.
# --------------------------------------------------------------------------- #
PROCEDURE_ID = "vauxhall-t76-adaptive-values-learn"

TCM_REQUEST_ID = 0x7E2          # connection record 0x068A1E2C
TCM_RESPONSE_ID = 0x7EA         # request + 8, per the runtime's own rule
PID_SET = "0x05AC7CAC"          # Transmission Control Module-TCM-T76-Delphi

LEARN_CONTROL_REF = "8682_51909_3"      # the $AE control
LEARN_STATUS_REF = "8682_51909"         # the 0x2823 status parameter
RELEASE_CONTROL_REF = "6331_51909_3"    # Null CPID, seven-byte form
START_COMMAND = "Start"                 # looked up by NAME, never by value

CONDITION_SET = "Park/Neutral & Transmission Temp High"

# The status states that decide what the sequence does. Read out of the
# extract's 17-state table by meaning, so a state this list does not mention is
# neither "ready" nor "finished" and the poll keeps waiting for one that is.
READY_STATES = (0, 1)               # Fast Learn Disabled / Fast Learn Complete
# Only state 1 counts as finished. State 13, "Drive Calibration Test Complete",
# is deliberately NOT terminal: nothing in the dealer data says the drive
# calibration is the last thing the routine does, and releasing the control
# there while a reverse calibration was still to come would produce precisely
# the half-finished learn this module exists to avoid. If a real car sits on 13
# and never reaches 1, the run times out and says so -- which is a question for
# the car to answer, not something to guess at here.
COMPLETE_STATES = (1,)
DRIVE_CAL_COMPLETE = 13
ABORT_STATES = (15, 16)             # Aborted Test Criteria Not Met / Test Failed

# How long the poll will wait for the module to reach a terminal state before
# giving up and releasing. NOT from the data -- nobody has timed this routine
# on a car. Five minutes is generous against every published description of a
# fast learn and is a bound, not an expectation.
LEARN_TIMEOUT_S = 300.0
POLL_INTERVAL_S = 1.0

# How close the operator's acknowledged temperature has to be to the live one
# for the acknowledgement to mean anything.
TEMPERATURE_ACK_TOLERANCE_C = 3

# The two readings of the dealer condition's threshold. The harness gates on
# the conservative one and shows both -- see the module header.
FLUID_THRESHOLD_CONVERTED_C = 84
FLUID_THRESHOLD_RAW_C = 44

CAPTURES_DIR = registry.PROJECT_ROOT / "captures" / "vauxhall"

# Operator abort. A run takes minutes and the person standing next to the car
# has to be able to stop it, so the abort is a process-wide flag a second
# request can set rather than something only the caller of run() can reach.
# Setting it aborts the routine, which -- per the module header -- still runs
# the release.
_ABORT = threading.Event()


def request_abort() -> None:
    """Ask any learn currently running in this process to stop and release."""
    _ABORT.set()


def clear_abort() -> None:
    _ABORT.clear()


def abort_requested() -> bool:
    return _ABORT.is_set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #
def start_frame() -> bytes:
    """``AE 30 00 00 00 04 00`` for this PID set -- assembled, not quoted.

    The 4 is the value this set encodes ``Start`` as. Another set in the same
    dealer deliverable encodes the same command as 8 on the same reference
    name, which is why this is a lookup and not a constant: a different car
    gets a different byte or an error, never a confidently wrong one.
    """
    return build_control_frame(LEARN_CONTROL_REF, START_COMMAND, PID_SET)


def release_frame() -> bytes:
    """``AE 00 00 00 00 00 00`` -- the Null CPID release this copy of the test
    binds. The two-byte ``AE 00`` is a different setup on a different path."""
    return build_null_cpid_frame(RELEASE_CONTROL_REF, PID_SET)


# --------------------------------------------------------------------------- #
# Preconditions, evaluated from the data file
# --------------------------------------------------------------------------- #
def _parameter_for(ref: str) -> Optional[dict]:
    return dealer_data.by_ref(ref)


def _evaluate(equation: str, value: Optional[float],
              state_name: Optional[str]) -> Optional[bool]:
    """Evaluate one dealer comparison. None means "no implemented rule".

    Only the forms this condition set actually uses are implemented, and an
    unrecognised form returns None rather than a plausible-looking verdict --
    the same rule dealer_data.apply_equation follows for scalings, for the same
    reason. A precondition the harness cannot evaluate must not be reported as
    satisfied.
    """
    if not equation:
        return None
    text = equation.replace(" ", "")

    # (_M_SEV(0,"Neutral")=1)||(_M_SEV(0,"Park")=1) -- a state-name test.
    if "_M_SEV(" in text:
        wanted = []
        rest = text
        while '_M_SEV(0,"' in rest:
            rest = rest.split('_M_SEV(0,"', 1)[1]
            name, rest = rest.split('"', 1)
            wanted.append(name)
        if not wanted or state_name is None:
            return None
        return state_name in wanted

    for operator in (">", "<", "="):
        marker = f"X(0){operator}"
        if text.startswith(marker):
            try:
                threshold = float(text[len(marker):])
            except ValueError:
                return None
            if value is None:
                return None
            if operator == ">":
                return value > threshold
            if operator == "<":
                return value < threshold
            return value == threshold
    return None


def _read_signal(transport, ref: str) -> dict:
    """Read one dealer-named parameter and decode it with the dealer scaling."""
    entry = _parameter_for(ref)
    out = {"ref": ref, "name": (entry or {}).get("name"),
           "request": (entry or {}).get("request"),
           "value": None, "raw": None, "state": None, "read": False}
    if entry is None:
        out["detail"] = f"The extract has no parameter '{ref}'."
        return out
    item = entry.get("item") or {}
    did = item.get("id")
    if did is None:
        out["detail"] = f"Parameter '{ref}' carries no identifier."
        return out
    payload = transport.read_pid(did, timeout=1.5)
    if payload is None:
        out["detail"] = (
            "The module did not answer this parameter. A precondition that "
            "cannot be read is not a precondition that holds."
        )
        return out
    decoded = dealer_data.decode(entry, payload) or {}
    out["read"] = True
    out["raw"] = decoded.get("raw")
    out["unit"] = decoded.get("unit")
    value = decoded.get("value")
    if isinstance(value, str):
        out["state"] = value
        out["value"] = decoded.get("state")
    else:
        out["value"] = value
    return out


def read_preconditions(transport) -> dict:
    """Take every check in GDS2's own condition set, live, and grade it.

    The checks, the parameters they read and the comparisons they apply all
    come out of the data file. Nothing here is a reimplementation of GDS2's
    rules -- if the extract's condition set changes, this changes with it.
    """
    condition = dealer_data.condition(CONDITION_SET)
    checks: List[dict] = []
    if condition is None:
        return {
            "conditionSet": CONDITION_SET, "checks": [], "satisfied": False,
            "detail": (
                f"The extract has no condition set called '{CONDITION_SET}', "
                "so this procedure's own preconditions cannot be evaluated."
            ),
        }

    for check in condition.get("checks", []):
        for comparison in check.get("comparisons", []):
            watches = comparison.get("watches") or []
            ref = next(
                (w.get("pidRef") for w in watches
                 if _parameter_for(w.get("pidRef") or "")), None
            )
            row = {
                "name": check.get("signal"),
                "equation": comparison.get("equation"),
                "parameterRef": ref,
            }
            if ref is None:
                row.update(holds=False, read=False, detail=(
                    "None of this comparison's parameters are in the extract "
                    "for this module, so it cannot be checked."
                ))
                checks.append(row)
                continue
            reading = _read_signal(transport, ref)
            row.update(reading)
            row["name"] = check.get("signal") or reading.get("name")
            verdict = _evaluate(
                comparison.get("equation", ""), reading.get("value"),
                reading.get("state"),
            )
            if verdict is None:
                row["holds"] = False
                row.setdefault("detail", (
                    f"The comparison {comparison.get('equation')!r} has no "
                    "implemented rule, so it is treated as not satisfied "
                    "rather than assumed."
                ))
            else:
                row["holds"] = bool(verdict)
            checks.append(row)

    return {
        "conditionSet": CONDITION_SET,
        "checks": checks,
        "satisfied": bool(checks) and all(c.get("holds") for c in checks),
        "source": (
            "GDS2 condition object bound to this test at slot 32; the "
            "comparisons are read from data/gds2_t76_tcm.json."
        ),
    }


def read_fluid_temperature(transport) -> dict:
    """The one number the operator has to look at, with both readings of the
    threshold and the module's own classification of where it sits.

    Two conversions live on the same parameter id in the dealer data: the
    scaling ``(X*1)-40`` giving °C, and a window classifier that reports
    Below / Above / Start against the module's own calibration boundaries. Both
    come out of a single 22 19 40, so the operator sees the temperature and
    what the module thinks of it at the same time.
    """
    scaled = _parameter_for("8442_6736")
    window = _parameter_for("8442_51909")
    item = (scaled or {}).get("item") or {}
    did = item.get("id")
    out = {
        "request": (scaled or {}).get("request"),
        "celsius": None, "raw": None, "moduleClassification": None,
        "thresholdC": FLUID_THRESHOLD_CONVERTED_C,
        "alternativeThresholdC": FLUID_THRESHOLD_RAW_C,
        "ambiguity": (
            "GDS2's condition is X(0)>84 on this parameter. Read as the "
            f"converted value that is {FLUID_THRESHOLD_CONVERTED_C} °C; read "
            f"as the raw byte it is {FLUID_THRESHOLD_RAW_C} °C. This harness "
            f"gates on {FLUID_THRESHOLD_CONVERTED_C} °C -- the conservative "
            "reading, and the one consistent with the module's own 86 °C "
            "classification boundary and with every published workshop window. "
            "It is an argued reading of the data, not a line found in the "
            "runtime, which is why you are being asked to look at the number "
            "yourself."
        ),
    }
    if did is None:
        out["detail"] = "The extract carries no fluid temperature parameter."
        return out
    payload = transport.read_pid(did, timeout=1.5)
    if payload is None:
        out["detail"] = "The module did not answer 22 19 40."
        return out
    decoded = dealer_data.decode(scaled, payload) or {}
    out["celsius"] = decoded.get("value")
    out["raw"] = decoded.get("raw")
    if window is not None:
        classified = dealer_data.decode(window, payload) or {}
        out["moduleClassification"] = classified.get("value")
    return out


def read_learn_status(transport) -> dict:
    """The 17-state readiness parameter -- the module's own opinion."""
    entry = _parameter_for(LEARN_STATUS_REF)
    item = (entry or {}).get("item") or {}
    did = item.get("id")
    out = {"request": (entry or {}).get("request"), "state": None, "name": None}
    if did is None:
        out["detail"] = "The extract carries no learn status parameter."
        return out
    payload = transport.read_pid(did, timeout=1.5)
    if payload is None:
        out["detail"] = "The module did not answer 22 28 23."
        return out
    decoded = dealer_data.decode(entry, payload) or {}
    out["state"] = decoded.get("state")
    out["name"] = decoded.get("value")
    return out


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def _gate(preconditions: dict, temperature: dict, status: dict,
          temperature_ack: Optional[float]) -> List[str]:
    """Every reason this run will not start, in the order they are hit.

    Returned as a list rather than raised so the operator sees the whole set at
    once -- fixing one obstacle only to be told about the next is how a person
    ends up leaving the car in the wrong state between attempts.
    """
    reasons: List[str] = []

    for check in preconditions.get("checks", []):
        if check.get("holds"):
            continue
        shown = check.get("state") or check.get("value")
        unit = check.get("unit") or ""
        if shown is None:
            reading = check.get("detail") or "not read"
        else:
            reading = f"reads {shown}{(' ' + unit) if unit else ''}"
        reasons.append(
            f"Precondition '{check.get('name')}' ({check.get('equation')}) is "
            f"not satisfied -- {reading}."
        )

    state = status.get("state")
    if state is None:
        reasons.append(
            "The module's learn status (22 28 23) could not be read, so there "
            "is no way to know whether it would accept the routine."
        )
    elif state not in READY_STATES:
        reasons.append(
            f"The module reports '{status.get('name')}' (state {state}). It "
            "will only start from Fast Learn Disabled or Fast Learn Complete. "
            "This is the module's own interlock and it names what it is "
            "waiting for."
        )

    celsius = temperature.get("celsius")
    if celsius is None:
        reasons.append(
            "Transmission fluid temperature could not be read, and this "
            "routine must not be started on an unknown temperature."
        )
    else:
        if celsius <= FLUID_THRESHOLD_CONVERTED_C:
            reasons.append(
                f"Fluid temperature is {celsius} °C. The gate is above "
                f"{FLUID_THRESHOLD_CONVERTED_C} °C. "
                + ("It is above the alternative reading of "
                   f"{FLUID_THRESHOLD_RAW_C} °C, and the harness will not "
                   "resolve that ambiguity in the permissive direction -- "
                   "drive the car until it is properly warm."
                   if celsius > FLUID_THRESHOLD_RAW_C else
                   "Drive the car until the gearbox is properly warm.")
            )
        if temperature_ack is None:
            reasons.append(
                f"The live fluid temperature is {celsius} °C. Confirm the run "
                "against that number: send it back as the acknowledged "
                "temperature. This is asked for once per run because the "
                "threshold in the dealer data has two defensible readings and "
                "the harness will not pick one behind your back."
            )
        elif abs(float(temperature_ack) - float(celsius)) > TEMPERATURE_ACK_TOLERANCE_C:
            reasons.append(
                f"The acknowledged temperature ({temperature_ack} °C) does not "
                f"match what the module is reporting now ({celsius} °C). "
                "Re-read it and acknowledge the current value."
            )

    classification = temperature.get("moduleClassification")
    if classification and "Above Operating" in classification:
        reasons.append(
            f"The module classifies its own fluid temperature as "
            f"'{classification}'. It will refuse or abort."
        )
    return reasons


# --------------------------------------------------------------------------- #
# The sequence
# --------------------------------------------------------------------------- #
def _sequence(client, confirm: bool, temperature_ack: Optional[float],
              abort_check: Callable[[], bool], simulated: bool,
              sleep: Callable[[float], None], clock: Callable[[], float]) -> dict:
    """One run of the exchange. Everything below the transport is shared
    between the real car and the simulated one -- that is what makes the
    simulation worth trusting as a rehearsal."""
    transcript = Transcript(
        ("SIMULATED " if simulated else "")
        + "Transmission Adaptive Values Learn -- Astra K T76 / 6T35",
        clock=clock,
    )
    transcript.note(
        f"module {TCM_REQUEST_ID:#05x} request / {TCM_RESPONSE_ID:#05x} "
        f"response, PID set {PID_SET}"
    )
    transcript.note(
        "NEVER RUN ON A CAR BEFORE. The frames are a confirmed static decode "
        "of the GDS2 dealer database; no part of this exchange has been "
        "observed on a wire."
    )
    if simulated:
        transcript.note("SIMULATED -- no car is connected. Virtual clock.")

    result = {
        "ok": False,
        "runnable": True,
        "simulated": simulated,
        "procedureId": PROCEDURE_ID,
        "neverSeenOnACar": True,
        "outcome": "failed",
        "released": None,
        "sent": [],
        "preconditions": None,
        "temperature": None,
        "status": None,
        "keepAlive": None,
        "message": "",
        "timestamp": _now(),
    }

    transport = ControlTransport(
        client, TCM_REQUEST_ID, PID_SET, transcript, sleep=sleep, clock=clock
    )
    keepalive: Optional[KeepAlive] = None
    session: Optional[ControlSession] = None

    try:
        start = start_frame()
        release = release_frame()
    except DeviceControlError as exc:
        result["outcome"] = "blocked"
        result["message"] = (
            f"The control frames could not be assembled from the dealer data: "
            f"{exc} Nothing was sent."
        )
        result["transcript"] = transcript.entries
        result["worksheet"] = transcript.text()
        return result

    transcript.note(f"start frame assembled from the extract: {format_frame(start)}")
    transcript.note(f"release frame assembled from the extract: {format_frame(release)}")

    try:
        session = ControlSession(transport, release)
        with session:
            # Preconditions FIRST, before even the diagnostic session request.
            # Service 0x22 answers on this module in the default session -- the
            # harness already reads these parameters that way on Steve's car --
            # so a run that is going to be refused can be refused without
            # having touched the module's state at all.
            #
            # (22 19 40 is read twice here: once as a precondition against the
            # dealer comparison, once through the module's own window
            # classifier on the same parameter id. Two conversions, one wire
            # id; the second read costs one frame and keeps the two functions
            # independent of each other's ordering.)
            preconditions = read_preconditions(transport)
            temperature = read_fluid_temperature(transport)
            status = read_learn_status(transport)
            result["preconditions"] = preconditions
            result["temperature"] = temperature
            result["status"] = status

            blocked = _gate(preconditions, temperature, status, temperature_ack)
            if blocked:
                result["outcome"] = "blocked"
                result["blockedBy"] = blocked
                result["message"] = (
                    "The learn was not started. " + " ".join(blocked)
                )
                transcript.note("preconditions not satisfied -- nothing sent")
                raise _Blocked()

            transport.enter_diagnostic()
            keepalive = KeepAlive(transport)

            # The confirmation is passed to the transmit call itself, per run.
            # There is no stored flag anywhere in this module that supplies it.
            transport.transmit_control(start, confirm=confirm,
                                       note="START the adaptive values learn")
            transcript.note(
                "the learn has started. From here the release is guaranteed by "
                "the enclosing session, on every path out."
            )

            outcome, final = _poll(transport, keepalive, abort_check, sleep, clock)
            result["outcome"] = outcome
            result["status"] = final
            result["ok"] = outcome == "complete"

    except _Blocked:
        pass
    except NotConfirmed as exc:
        result["outcome"] = "not-confirmed"
        result["message"] = str(exc)
    except ForbiddenFrame as exc:
        result["outcome"] = "refused-by-harness"
        result["message"] = str(exc)
    except ControlRefused as exc:
        result["outcome"] = "refused-by-module"
        result["nrc"] = f"{exc.nrc:02X}"
        result["nrcName"] = exc.short
        result["message"] = (
            f"The module refused with NRC 0x{exc.nrc:02X} ({exc.short}). "
            + exc.explanation
        )
    except KeepAliveLost as exc:
        result["outcome"] = "keep-alive-lost"
        result["message"] = (
            str(exc) + " This is the ELM327 limitation the analysis flagged and "
            "could not settle: an ELM327 is half duplex, so TesterPresent has "
            "to be interleaved between polls and a slow link pushes it late."
        )
    except OperatorAbort as exc:
        result["outcome"] = "aborted-by-operator"
        result["message"] = str(exc)
    except (ObdError, TransportError) as exc:
        result["outcome"] = "link-lost"
        result["message"] = (
            f"The link to the car failed during the routine: {exc}"
        )
    except DeviceControlError as exc:
        result["outcome"] = "failed"
        result["message"] = str(exc)
    except Exception as exc:                       # noqa: BLE001 -- deliberate
        # Anything at all. The release has already run by the time this is
        # caught, because it belongs to the session's exit rather than to this
        # handler, and that is the whole point of the arrangement.
        result["outcome"] = "failed"
        result["message"] = (
            f"The routine failed unexpectedly: {exc.__class__.__name__}: {exc}. "
            "The control release was still attempted -- check the transcript "
            "and the released flag."
        )

    if session is not None and session.release_result is not None:
        released = session.release_result.get("released")
        # None means no control was ever sent, so there was nothing to release.
        # That is a third answer and it is kept as one: reporting it as a
        # successful release would claim something that never happened.
        result["released"] = None if released is None else bool(released)
        result["releaseSkipped"] = bool(session.release_result.get("skipped"))
        result["releaseSteps"] = session.release_result.get("steps")
        if result["released"] is False:
            # A learn that finished but could not be released is not a run that
            # went well. The module may still be in device control, and the
            # operator has something to do about it.
            result["ok"] = False
    if keepalive is not None:
        result["keepAlive"] = {
            "sent": keepalive.sent, "unacknowledged": keepalive.missed,
            "intervalSeconds": keepalive.interval,
            "deadlineSeconds": keepalive.deadline,
        }

    result["sent"] = [e["frame"] for e in transcript.entries if e["dir"] == "->"]
    result["transcript"] = transcript.entries
    result["readings"] = _run_readings(result)
    result["worksheet"] = transcript.text()
    result["message"] = _message(result)
    result["timestamp"] = _now()

    # The transcript is written here rather than by the caller, so that it
    # lands on disk for a run that FAILED as surely as for one that worked --
    # the failures are the ones worth reading afterwards, and a run that threw
    # its way out of the sequence would never reach a caller's save step.
    # Simulated runs are never written; Transcript.save says why that rule is
    # not negotiable.
    if not simulated:
        try:
            result["savedTo"] = transcript.save(CAPTURES_DIR, "t76-adaptive-learn")
        except OSError:
            # A full disk must never lose the diagnostic itself, and the whole
            # transcript is in the result either way.
            result["transcriptSaveFailed"] = True
    return result


class _Blocked(Exception):
    """Preconditions refused. Not an error -- a decision, and the tidiest way
    to leave the session block so the (skipped) release logic stays in one
    place."""


def _poll(transport, keepalive, abort_check, sleep, clock):
    """Watch the status parameter until the module reaches a terminal state.

    The keep-alive goes out between polls, never alongside them -- an ELM327
    cannot do otherwise. Every loop checks the abort flag, so an operator can
    stop a run that is going wrong and still get a clean release.
    """
    deadline = clock() + LEARN_TIMEOUT_S
    final = {"state": None, "name": None}
    seen_drive_cal_complete = False
    while True:
        if abort_check is not None and abort_check():
            raise OperatorAbort(
                "The operator asked for the run to stop. The routine was "
                "abandoned and the control released. The gearbox's adaptive "
                "cells were cleared when the routine started and have NOT been "
                "relearned -- run the learn again to completion, or the car "
                "will shift worse than it did before."
            )
        keepalive.poke()

        status = read_learn_status(transport)
        if status.get("state") is not None:
            final = status
        state = status.get("state")
        transport.transcript.note(
            f"status {state} ({status.get('name')})" if state is not None
            else "status not readable this tick"
        )

        if state == DRIVE_CAL_COMPLETE:
            seen_drive_cal_complete = True
        if state in COMPLETE_STATES:
            return "complete", final
        if state in ABORT_STATES:
            return "aborted-by-module", final
        if clock() > deadline:
            final["sawDriveCalibrationComplete"] = seen_drive_cal_complete
            return "timed-out", final
        sleep(POLL_INTERVAL_S)


def _message(result: dict) -> str:
    """The sentence the operator reads. Every outcome says what happened to the
    gearbox, not just what happened to the request."""
    never = (
        " This procedure has never been run on a car -- the frames are a "
        "confirmed static decode of the GDS2 dealer database and nothing more."
    )
    sim = "SIMULATED -- no car was connected. " if result.get("simulated") else ""
    released = result.get("released")
    tail = ""
    if released is False:
        tail = (
            " THE CONTROL RELEASE DID NOT COMPLETE. The module may still be in "
            "device control. Switch the ignition off and let it power down "
            "before driving the car."
        )
    elif released is True:
        tail = " The device control was released and the module returned to normal."

    outcome = result.get("outcome")
    if outcome == "complete":
        state = (result.get("status") or {}).get("name")
        return (
            sim + f"The learn finished: the module reports '{state}'." + tail
            + " Now switch the engine off and let the module power down fully "
            "before restarting -- skipping that is the most common reason a "
            "fast learn does not take -- then do the relearn drive."
            + never
        )
    if outcome == "blocked":
        return sim + (result.get("message") or "The learn was not started.") + never
    if outcome == "aborted-by-module":
        state = (result.get("status") or {}).get("name")
        return (
            sim + f"The module aborted the routine: '{state}'. The adaptive "
            "cells were cleared when it started and have not been relearned, "
            "so the gearbox is currently worse off than before. Fix what the "
            "status names and run it again to completion." + tail + never
        )
    if outcome == "aborted-by-operator":
        return sim + (result.get("message") or "") + tail + never
    if outcome == "keep-alive-lost":
        return (
            sim + (result.get("message") or "") + " The adaptive cells may have "
            "been cleared without being relearned -- treat this as an "
            "incomplete learn and run it again." + tail + never
        )
    if outcome == "refused-by-module":
        return sim + (result.get("message") or "") + tail + never
    if outcome in ("refused-by-harness", "not-confirmed"):
        return sim + (result.get("message") or "") + never
    if outcome == "timed-out":
        saw = (result.get("status") or {}).get("sawDriveCalibrationComplete")
        extra = (
            " It did reach 'Drive Calibration Test Complete' along the way, "
            "which may mean the routine finishes at that state on a real car "
            "rather than at 'Fast Learn Complete'. The harness does not treat "
            "it as finished, because guessing that way round would release the "
            "control on a routine that was still running."
            if saw else ""
        )
        return (
            sim + "The module never reached a finished state inside "
            f"{int(LEARN_TIMEOUT_S)} seconds." + extra + " Treat this as an "
            "incomplete learn: the adaptive cells may have been cleared "
            "without being relearned." + tail + never
        )
    return sim + (result.get("message") or "The routine did not complete.") + tail + never


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def preflight(adapter: Optional[dict], mode: str = "real",
              scenario: str = "complete") -> dict:
    """Read everything the gate depends on and send nothing.

    This is what the operator runs first: it reports the live fluid
    temperature, the module's own readiness state and each of GDS2's own
    preconditions, so the decision to start is made against numbers rather
    than against hope. Service 0x22 only -- no session, no control, no writes.
    """
    if mode == "sim":
        car = SimulatedTcm(scenario)
        transcript = Transcript("SIMULATED learn preflight", clock=car.clock)
        transport = ControlTransport(car, TCM_REQUEST_ID, PID_SET, transcript,
                                     sleep=car.sleep, clock=car.clock)
        return _preflight_body(transport, transcript, simulated=True)

    def runner(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "simulated": False, "checks": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, "
                    "not the 11-bit/500k High Speed GMLAN this module lives on."
                ),
                "timestamp": _now(),
            }
        client = Elm327UdsClient(session, {TCM_REQUEST_ID: TCM_RESPONSE_ID})
        client.begin()
        transcript = Transcript("Learn preflight -- Astra K T76")
        transport = ControlTransport(client, TCM_REQUEST_ID, PID_SET, transcript)
        try:
            return _preflight_body(transport, transcript, simulated=False)
        finally:
            client.restore()

    return obd_service._with_session(adapter, runner)


def _preflight_body(transport, transcript, simulated: bool) -> dict:
    preconditions = read_preconditions(transport)
    temperature = read_fluid_temperature(transport)
    status = read_learn_status(transport)
    blocked = _gate(preconditions, temperature, status, temperature_ack=None)
    # The temperature acknowledgement is always outstanding on a preflight --
    # it is a per-run confirmation and a preflight is not a run. It is dropped
    # from the blocking list here so the operator sees the real obstacles, and
    # reported separately as the thing they will be asked for.
    real_blockers = [b for b in blocked if "acknowledged temperature" not in b
                     and "Confirm the run against that number" not in b]
    celsius = temperature.get("celsius")
    return {
        "ok": True,
        "simulated": simulated,
        "readyToStart": not real_blockers,
        "preconditions": preconditions,
        "temperature": temperature,
        "status": status,
        "blockedBy": real_blockers,
        "acknowledgementRequired": (
            f"Starting the learn needs the live fluid temperature "
            f"({celsius} °C) acknowledged, once, for that run."
            if celsius is not None else
            "Starting the learn needs the live fluid temperature acknowledged."
        ),
        "readings": _readings(temperature, status, preconditions),
        "worksheet": transcript.text(),
        "transcript": transcript.entries,
        "message": (
            ("SIMULATED -- no car connected. " if simulated else "")
            + ("Every precondition is satisfied and the module says it is "
               "willing. Nothing has been sent."
               if not real_blockers else
               "Not ready: " + " ".join(real_blockers))
            + " This read sends service 0x22 only."
        ),
        "timestamp": _now(),
    }


def _run_readings(result: dict) -> List[dict]:
    """The four things worth seeing at a glance after a run.

    ``Control released`` is on the list deliberately and is the one to read
    first: it is the difference between a car that can be driven away and one
    whose module may still be under device control.
    """
    status = result.get("status") or {}
    temperature = result.get("temperature") or {}
    released = result.get("released")
    return [
        {"name": "Outcome", "value": result.get("outcome") or "-", "unit": ""},
        {"name": "Module state", "value": status.get("name") or "-", "unit": ""},
        {"name": "Fluid temperature",
         "value": temperature.get("celsius") if temperature.get("celsius")
         is not None else "-", "unit": "°C" if temperature.get("celsius")
         is not None else ""},
        {"name": "Control released",
         "value": ("yes" if released is True else
                   "NO -- ignition off before driving" if released is False else
                   "nothing was sent"),
         "unit": ""},
    ]


def _readings(temperature: dict, status: dict, preconditions: dict) -> List[dict]:
    """Chip-shaped rows for the panel, which renders name/value/unit."""
    rows = [
        {"name": "Fluid temperature",
         "value": temperature.get("celsius"), "unit": "°C"},
        {"name": "Module temperature view",
         "value": temperature.get("moduleClassification") or "-", "unit": ""},
        {"name": "Learn status",
         "value": status.get("name") or "-", "unit": ""},
    ]
    for check in preconditions.get("checks", []):
        rows.append({
            "name": check.get("name"),
            "value": ("satisfied" if check.get("holds") else "NOT satisfied"),
            "unit": "",
        })
    return rows


def run(adapter: Optional[dict], mode: str = "real", confirm: bool = False,
        temperature_ack: Optional[float] = None, scenario: str = "complete",
        abort_check: Optional[Callable[[], bool]] = None) -> dict:
    """Run the adaptive values learn. CHANGES THE CAR.

    ``confirm`` must be True and reaches the $AE transmit as an argument rather
    than as stored state. ``temperature_ack`` is the fluid temperature in °C as
    the operator has just read it. Both are per-run.

    ``mode="sim"`` runs the whole sequence against the simulated TCM in
    sim_tcm.py, including every failure path, with no car and no dongle. That
    is the intended way to watch this before it is ever plugged in.
    """
    if mode not in ("real", "sim"):
        return {"ok": False, "runnable": False, "simulated": False,
                "message": "mode must be 'real' or 'sim'.", "timestamp": _now()}

    if abort_check is None:
        abort_check = abort_requested
    clear_abort()

    if mode == "sim":
        car = SimulatedTcm(scenario)
        result = _sequence(car, confirm, temperature_ack, abort_check,
                           simulated=True, sleep=car.sleep, clock=car.clock)
        result["scenario"] = scenario
        return result

    def runner(session, entry):
        session.ensure_protocol()
        if session.protocol_num not in (6, None):
            return {
                "ok": False, "runnable": False, "simulated": False,
                "outcome": "blocked", "sent": [],
                "message": (
                    f"The car answered {session.protocol or 'an unknown protocol'}, "
                    "not the 11-bit/500k High Speed GMLAN this module lives on. "
                    "Nothing was sent."
                ),
                "timestamp": _now(),
            }
        client = Elm327UdsClient(session, {TCM_REQUEST_ID: TCM_RESPONSE_ID})
        client.begin()
        try:
            return _sequence(client, confirm, temperature_ack, abort_check,
                             simulated=False, sleep=time.sleep,
                             clock=time.monotonic)
        finally:
            client.restore()

    # _with_session retries once after a dropped link. That is right for a
    # read and would be very wrong here -- a retry would re-run a learn that
    # had already started -- but it cannot happen: _sequence catches
    # ObdError/TransportError itself and returns a result rather than letting
    # one escape, so there is nothing for the retry to trigger on.
    return obd_service._with_session(adapter, runner)


