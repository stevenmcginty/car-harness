"""Astra K T76 gearbox procedures: the definition, and the gates on running it.

WHAT THIS FILE IS FOR
---------------------
resets.py used to say that Transmission Adaptive Values Learn could not be
described at all, because nobody outside GM knew which CPID it used or what
bytes it carried, and this repository will not guess at a byte sequence whose
job is to command clutches. That objection has been answered: the CPID and the
control packet were recovered by static analysis of the GDS2 dealer data (see
dealer_data.py and ``data/gds2_t76_tcm.json``), and every field is traceable to
a file offset.

Answered is not the same as proven, and that distinction is now the whole
argument of this file. What follows is the *description* of the exchange -- the
frames, the preconditions, the state table, the provenance of each field --
which is the thing every other surface displays. The execution lives in
learn.py and gmlan_control.py, and this module's job is to say what it is
allowed to do.

WHAT CHANGED, AND WHAT DELIBERATELY DID NOT
-------------------------------------------
This module used to hold three gates. One of them has gone, one has been
downgraded on purpose, and one is untouched.

1. **Gone: "there is no transport."** There is one now -- gmlan_control.py
   implements GMW3110 DeviceControl, and learn.py runs the sequence. That gate
   was always meant to be removed deliberately rather than eroded, and this is
   the deliberate removal: written on purpose, with the release owned by a
   ``finally``, with a simulated module to rehearse against, and with every
   failure path tested before anything was plugged in.

2. **Downgraded: ``verified`` is still False, and now it warns rather than
   refuses.** The byte reconstruction has been checked independently and holds
   as a static decode. No part of it has ever been put on a car. Keeping the
   flag as an absolute refusal would mean it could never be cleared, because
   the only thing that can clear it is a run. So it is now a warning that every
   operator-facing surface repeats, and ``execute`` returns it in the result of
   every run. It flips to True when a real car has answered, and not before.

3. **Untouched: ``confirm`` must be passed explicitly.** No function here or in
   learn.py emits anything on a default call, there is no stored flag that
   means yes, and the confirmation reaches the $AE transmit as an argument at
   the call site rather than as session state. A second per-run confirmation
   was added on top: the operator has to acknowledge the live fluid temperature
   they can see, because the dealer data's threshold has two defensible
   readings and the harness will not pick one behind their back.

WHAT THE VERIFICATION PASS SETTLED, AND WHAT IT DID NOT
-------------------------------------------------------
Settled: the frame ``AE 30 00 00 00 04 00`` rebuilt from raw file offsets
without reusing the original analysis, and reproduced byte for byte in five
separately built deliverables across two product lines. The module pin to this
car's TCM node was walked twice by independent routes.

Not settled, and these are the ones that decide whether a live attempt works:

* **Whether the module demands SecurityAccess.** GDS2 sends no $27 for this
  test -- the ``InstSetSecurity`` step exists but is gated on a flag that is 0.
  That proves the dealer tool does not *offer* a key. It does not prove the
  TCM will not *demand* one. A real attempt may still meet NRC 0x33, and the
  deliverable carries no working seed/key algorithm to answer it with.
* **Whether an ELM327 can hold the channel.** The exchange needs 0x3E
  TesterPresent kept alive across a routine that runs for a minute or more.
  Both frames fit an ISO-TP single frame, so no flow control is needed, but
  BLE round-trip latency against the module's timeout has never been measured.
* **Where the 500 ms delay sits.** The number is read from the data; whether
  it is applied before or after the trailing ``20`` was not established.

None of those three has been settled by building the executable path, and it
would be dishonest to imply otherwise. What the implementation does is refuse
to be surprised by them: NRC 0x33 is an expected outcome reported in plain
words rather than a crash, the keep-alive interval is measured and a late one
aborts the run (which still releases), and the 500 ms sits in a named constant
next to a comment saying its placement is an inference.

THE PRECONDITIONS ARE DATA TOO
------------------------------
GDS2 gates this test on a named condition set, ``Park/Neutral & Transmission
Temp High``, whose comparisons are stored in the same file. Those are recorded
below with the parameter each one reads, because they are checkable over a
plain dongle -- every one of them is a service 0x22 read the harness can
already do. Checking them costs nothing and is the difference between an
attempt that the module will entertain and one it will refuse.

Separately, the module has its own runtime interlocks, which it reports through
the 17-state status parameter rather than through anything GDS2 checks up
front: brake pressed, Park then Drive, engine speed in band, no road speed.
Those are the module's own gate and are the reason the status parameter is the
thing to poll rather than a timer.
"""

from typing import Optional

from . import dealer_data, learn, sim_tcm

# The parameter that reports what the routine is doing. This is the single most
# useful thing recovered, because it is a plain read: it can be polled on any
# car right now, with nothing written, and it will say whether the module
# considers a fast learn possible.
STATUS_PARAMETER = {
    "ref": "8682_51909",
    "did": 0x2823,
    "request": "22 28 23",
    "objectOffset": "0x05DF78D4",
    "equation": "(X(0)>>0)&0xFF",
    "states": dealer_data.states_for("8682_51909"),
    # The module will not start, or will abort, on anything outside these.
    "readyStates": (0, 1),
    "runningStates": (11, 12, 13, 14),
    # Only state 1 means the routine finished. An earlier version of this list
    # also counted 13, "Drive Calibration Test Complete", as finished. That was
    # the wrong side to be wrong on: nothing in the data says the drive
    # calibration is the LAST thing the routine does, the extraction report
    # reads the exit condition as "poll until status = 1", and releasing the
    # control at 13 while a reverse calibration was still to come would produce
    # exactly the half-finished learn this whole procedure is careful about.
    # So 13 is a progress state here and learn.py keeps polling through it.
    "completeStates": (1,),
    "driveCalibrationComplete": (13,),
    "abortStates": (15, 16),
}

# The window the module's own calibration classifies as its operating range,
# read out of the second conversion on the same fluid-temperature parameter:
# raw < 126 is "below operating temperature", raw > 166 is "above", and
# anything between is the state GDS2 labels "Start". With the -40 offset that
# is 86 °C to 126 °C. This is the module's classification, not a workshop
# recommendation, and it is wider than any published figure -- see resets.py
# for why the published windows disagree with each other and with this.
MODULE_TEMPERATURE_WINDOW_C = (86, 126)


def _precondition(name, request, ref, equation, holds, evidence):
    return {
        "name": name, "request": request, "parameterRef": ref,
        "equation": equation, "holds": holds, "evidence": evidence,
    }


ADAPTIVE_VALUES_LEARN = {
    "id": "vauxhall-t76-adaptive-values-learn",
    "name": "Transmission Adaptive Values Learn (T76 / 6T35)",

    # ---- gate 1 -----------------------------------------------------------
    "verified": False,
    "verification": {
        "staticDecode": "confirmed",
        "onCar": "never attempted",
        "confirmedBy": (
            "Rebuilt from raw file offsets without reusing the original "
            "arithmetic, and reproduced byte for byte in five separately "
            "built GDS2 deliverables across two product lines."
        ),
        "openQuestions": [
            "Whether the TCM demands SecurityAccess ($27). GDS2 offers no key "
            "for this test, which is not the same as the module not asking "
            "for one. NRC 0x33 remains possible.",
            "Whether an ELM327 over BLE can keep 0x3E TesterPresent inside the "
            "module's timeout for the length of the routine.",
            "Whether the 500 ms delay in the exit sequence is applied before "
            "or after the trailing 0x20.",
        ],
        "notValidatedByTheLiveCar": (
            "This car answering 22 19 40 with the extracted scaling confirms "
            "the read half of the decode rule -- that the extracted parameter "
            "id is the wire id and the extracted conversion is the scaling. "
            "It exercises a different code path from the $AE frame assembly "
            "and says nothing about it."
        ),
    },

    # ---- what it talks to -------------------------------------------------
    "module": {
        "name": "Transmission Control Module",
        "node": "Transmission Control Module$TCM - T76 - (Conti) - GME",
        "canRequest": "0x7E2",
        "canResponse": "0x7EA",
        "bus": "GMLAN_HS_PRIMARY_500k_Pin6&14",
        "pidSet": "0x05AC7CAC",
        "pidSetWarning": (
            "The control packet is a property of THIS PID set, not of CPID "
            "0x30 in general. Another set in the same deliverable encodes "
            "Start as 8 rather than 4 on the same reference name. Any reuse "
            "on a different Vauxhall or Opel must re-walk the menu tree to "
            "that car's own module node; the byte cannot be carried across."
        ),
        "source": "GDS2 2023.3 Opel/Vauxhall, connection record 0x068A1E2C",
    },

    # ---- preconditions GDS2 itself checks, all readable over a dongle -----
    "preconditions": {
        "conditionSet": "Park/Neutral & Transmission Temp High",
        "source": (
            "Bound to the test at slot 32 (InstConditionCheck), condition "
            "object 0x0691F100; the comparisons are at 0x070DC1D4 onward."
        ),
        "note": (
            "The comparison equations are read as operating on the CONVERTED "
            "value rather than the raw byte. Two things point that way: a "
            "sibling condition tests X(0)=2 against a parameter whose "
            "conversion only ever outputs 0, 1 or 2, which can only be the "
            "converted output; and the engine-coolant conditions use 37 and "
            "15, which are sane as °C and nonsense as raw counts under the "
            "same -40 offset. This is an argued reading of the data, not a "
            "line found in the runtime."
        ),
        "ifThatReadingIsWrong": (
            "The fluid threshold would be a raw count instead, i.e. 44 °C "
            "rather than 84 °C. That is the one number here worth being "
            "careful about, because somebody could time a fast-learn attempt "
            "by it. 84 °C is also the figure consistent with the module's own "
            "86 °C classification boundary and with every published workshop "
            "window, and 44 °C is consistent with none of them, which is the "
            "third reason for preferring it."
        ),
        "checks": [
            _precondition(
                "Selector in Park or Neutral", "22 19 4F", "7975_51909",
                '(_M_SEV(0,"Neutral")=1)||(_M_SEV(0,"Park")=1)',
                "Internal Mode Switch reads Park (1) or Neutral (4)",
                "Dealer condition set. The harness has separately confirmed on "
                "this car that 194F reads 1 in Park and 6 in Drive.",
            ),
            _precondition(
                "Transmission fluid above 84 °C", "22 19 40", "8442_6736",
                "X(0)>84",
                "Fluid temperature, scaled (X*1)-40, above 84 °C",
                "Dealer condition set. The scaling is the one datum in this "
                "whole extract confirmed against live data from this car.",
            ),
            _precondition(
                "Engine running", "22 00 0C", "7639_6738",
                "X(0)>100",
                "Engine speed, scaled (X*0.25), above 100 rpm",
                "Dealer condition set. Read from the TCM's own copy of engine "
                "speed rather than from the engine controller.",
            ),
        ],
        "operatorConditions": [
            "Wheels chocked, park brake on, foot on the service brake.",
            "Zero throttle; no external rpm control.",
            "Fluid level and condition correct first -- a fast learn on low "
            "or burnt fluid teaches the module the wrong numbers.",
            "Selector cycled P to R three times beforehand to purge air from "
            "the reverse clutches.",
        ],
        "moduleEnforced": (
            "Brake pressed, Park then Drive, engine speed in band and no road "
            "speed are enforced by the module itself and reported through the "
            "status parameter, not checked by the tool up front."
        ),
    },

    # ---- the exchange, as a description ----------------------------------
    "sequence": [
        {
            "step": "enter",
            "request": "10 81",
            "meaning": "InitiateDiagnosticOperation",
            "source": "State 413 InstEnterStateOptional, template sB_[13]",
            "confidence": "read verbatim from the template table; the 0x81 "
                          "sub-function is not interpreted here",
        },
        {
            "step": "keep-alive",
            "request": "3E",
            "meaning": "TesterPresent, repeated for the whole routine",
            "source": "aA[0] / aA[28] in the runtime's message scheduler",
            "confidence": "the requirement is read from the runtime; the "
                          "interval an ELM327 can actually sustain is untested",
        },
        {
            "step": "poll-before",
            "request": "22 28 23",
            "meaning": "Read the learn status and refuse to go on unless it "
                       "is 0 (Fast Learn Disabled) or 1 (Fast Learn Complete)",
            "source": "Watched item bound at slot 23, object 0x05DF7418",
            "confidence": "plain read; the state table is complete in the data",
        },
        {
            "step": "control",
            "request": "AE 30 00 00 00 04 00",
            "meaning": "DeviceControl on CPID 0x30, command value 4 = Start",
            "source": (
                "PID object 0x05DF74E4 in set 0x05AC7CAC: CPID from p[0]=48, "
                "byte pattern 00,00,00,00,00, value offset p[5]=3, width "
                "p[1]=1, value 4 from the sole write-conversion state, "
                "template sB_[26] = AE 00, OR-in at index 5."
            ),
            "confidence": "static decode confirmed; never sent to a car",
            "gated": True,
        },
        {
            "step": "poll-during",
            "request": "22 28 23",
            "meaning": "Poll until the status reaches 1 (Fast Learn Complete) "
                       "or 13 (Drive Calibration Test Complete), or aborts on "
                       "15 or 16",
            "source": "Same watched item as poll-before",
            "confidence": "plain read",
        },
        {
            "step": "release",
            "request": "AE 00 00 00 00 00 00",
            "meaning": "Null CPID, long form -- cancels device control",
            "source": (
                "Setup 'Return To Normal' command 1, reference 6331_51909_3, "
                "PID object 0x05A40268 in this set."
            ),
            "confidence": "static decode confirmed. Note the seven-byte form "
                          "is what this copy of the test binds; the two-byte "
                          "AE 00 is a separate Null CPID setup on slot 89",
            "gated": True,
        },
        {
            "step": "delay",
            "milliseconds": 500,
            "meaning": "Pause in the release sequence",
            "source": "Field .3 of the second Return To Normal command, "
                      "object 0x0692C4D4, the string '500'",
            "confidence": "the NUMBER is read from data; whether it applies "
                          "before or after the following 0x20 is an inference",
        },
        {
            "step": "return-to-normal",
            "request": "20",
            "meaning": "ReturnToNormalOperation",
            "source": "Reference 176314_51909, PID object 0x05B88264, p[0]=0x20 "
                      "with PID type 4 and p[1]=0 so no value is encoded",
            "confidence": "static decode confirmed",
            "gated": True,
        },
        {
            "step": "exit",
            "request": "20",
            "meaning": "InstExitStateOptional",
            "source": "State 414, template sB_[14]",
            "confidence": "read verbatim from the template table",
            "gated": True,
        },
    ],

    "afterwards": [
        "Switch the engine off and let the module power down fully before "
        "restarting. Skipping the power-down is the most common reason a fast "
        "learn does not take.",
        "If the gearbox is still in neutral a couple of minutes after the "
        "routine, disconnect the battery for 5 to 10 minutes.",
        "Relearn drive of roughly 45 minutes on smooth level roads, light to "
        "moderate throttle, through every ratio.",
        "Torque converter clutch adapts can take a few hundred miles to "
        "settle. Do not re-judge the car on the first drive.",
    ],

    "safeProbe": {
        "request": "AE 00",
        "meaning": (
            "The Null CPID release on its own. It commands nothing, and the "
            "module either accepts it or answers NRC 0x33 -- which is the one "
            "open question worth the least effort to settle."
        ),
        "source": "Reference 6331_51909, PID object 0x05B7BAF0, PID type 4",
        "gated": True,
        "warning": (
            "Do not substitute a gear-select frame for this probe. An earlier "
            "write-up recommended one carrying 0x81 in the command byte as a "
            "harmless first test. It is neither harmless nor a gear command: "
            "the gear sits in the high nibble, because the write equation is "
            "X(0)<<4, so gear 1 is 0x90 and 0x81 requests an undefined state "
            "on a transmission solenoid driver. That frame is deliberately "
            "not written out anywhere in this profile, and the self-test "
            "fails if it reappears."
        ),
    },
}

PROCEDURES = {ADAPTIVE_VALUES_LEARN["id"]: ADAPTIVE_VALUES_LEARN}


def describe(procedure_id: str) -> Optional[dict]:
    """The definition, for display. Reading it sends nothing."""
    return PROCEDURES.get(procedure_id)


def list_procedures() -> dict:
    """The catalogue, for a surface that wants to browse without a car."""
    return {
        "brand": "vauxhall",
        "procedures": [
            {
                "id": p["id"],
                "name": p["name"],
                "verified": p.get("verified", False),
                "module": p.get("module"),
                "warning": UNVERIFIED_WARNING,
                "simulationScenarios": sim_tcm.describe_scenarios(),
                "preconditions": p.get("preconditions"),
                "sequence": p.get("sequence"),
                "afterwards": p.get("afterwards"),
                "operatorConfirmations": OPERATOR_CONFIRMATIONS,
            }
            for p in PROCEDURES.values()
        ],
    }


UNVERIFIED_WARNING = (
    "This exchange has never been run on a car. The frames are a static decode "
    "of the GDS2 dealer database -- independently re-derived from raw file "
    "offsets and reproduced byte for byte across five separately built "
    "deliverables -- and that is the strongest evidence a static decode can "
    "carry, but it is not the same kind of evidence as a car. Whether the "
    "module demands SecurityAccess, and whether an ELM327 over Bluetooth can "
    "hold the channel for the length of the routine, are both still open."
)

OPERATOR_CONFIRMATIONS = [
    "confirm: true -- nothing sends a device control on a default call.",
    "temperatureAck -- the live fluid temperature in °C, as read on screen. "
    "The dealer condition's threshold has two defensible readings (84 °C "
    "converted, 44 °C raw); the harness gates on the conservative one and asks "
    "you to look at the number rather than picking it for you.",
]


def blocking_reasons(procedure_id: str, confirm: bool = False) -> list:
    """Every HARD reason this procedure will not run.

    Hard means: nothing past this list is checked, and no car is contacted.
    The live preconditions -- fluid temperature, selector, engine speed, the
    module's own 17-state readiness -- are deliberately NOT here, because they
    can only be answered by reading the car, and reading them is learn.py's
    first act. Returned rather than raised so a caller can show the operator
    the whole list at once instead of one obstacle at a time.
    """
    proc = PROCEDURES.get(procedure_id)
    if proc is None:
        return [f"There is no procedure with id '{procedure_id}'."]
    reasons = []
    if not confirm:
        reasons.append(
            "No explicit confirmation was given. Nothing in this module sends "
            "anything on a default call."
        )
    return reasons


def warnings(procedure_id: str) -> list:
    """What the operator must be shown before they confirm.

    Separate from blocking_reasons on purpose. A warning that blocks can never
    be cleared by the only thing that could clear it, and a block that is only
    a warning is not a block; conflating the two is how a safety gate quietly
    becomes a dialog somebody clicks through.
    """
    proc = PROCEDURES.get(procedure_id)
    if proc is None:
        return []
    out = []
    if not proc.get("verified"):
        out.append(UNVERIFIED_WARNING)
    out.extend(proc.get("verification", {}).get("openQuestions", []))
    out.append(
        "If this routine starts and does not finish, the gearbox is left with "
        "its adaptive cells cleared and never relearned -- worse than before. "
        "The harness releases the control on every failure path, but an "
        "interrupted learn still has to be run again to completion."
    )
    return out


def execute(adapter: Optional[dict], procedure_id: str, confirm: bool = False,
            mode: str = "real", temperature_ack=None,
            scenario: str = "complete") -> dict:
    """Run one procedure, or say exactly why it was not run.

    The single auditable place a device control can be sent from. It checks the
    hard gates itself, then hands over to learn.py, which reads the live
    preconditions before it sends anything and owns the release on every path
    out.

    ``mode="sim"`` runs the whole exchange against the simulated TCM with no
    car attached -- including each failure path, chosen with ``scenario``.
    """
    proc = PROCEDURES.get(procedure_id)
    reasons = blocking_reasons(procedure_id, confirm)
    if proc is None or reasons:
        return {
            "ok": False,
            "runnable": proc is not None,
            "simulated": mode == "sim",
            "sent": [],
            "procedure": proc,
            "blockedBy": reasons,
            "warnings": warnings(procedure_id),
            "message": (
                f"'{proc['name']}' was not run. " + " ".join(reasons)
                if proc else reasons[0]
            ),
        }

    result = learn.run(
        adapter, mode=mode, confirm=confirm, temperature_ack=temperature_ack,
        scenario=scenario,
    )
    result["procedure"] = proc
    result["warnings"] = warnings(procedure_id)
    return result


def request_abort() -> dict:
    """Ask a run in progress to stop and release. See learn.py for why an
    abort still releases -- and why an aborted learn is not a harmless
    cancellation."""
    learn.request_abort()
    return {"ok": True}


def preflight(adapter: Optional[dict], procedure_id: str, mode: str = "real",
              scenario: str = "complete") -> dict:
    """Read the preconditions and send nothing. The step before deciding."""
    proc = PROCEDURES.get(procedure_id)
    if proc is None:
        return {"ok": False, "message": f"There is no procedure with id "
                                        f"'{procedure_id}'."}
    result = learn.preflight(adapter, mode=mode, scenario=scenario)
    result["procedure"] = proc
    result["warnings"] = warnings(procedure_id)
    return result
