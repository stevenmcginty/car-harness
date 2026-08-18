"""Astra K T76 gearbox procedures, as data. Nothing here runs.

WHAT THIS FILE IS FOR
---------------------
resets.py used to say that Transmission Adaptive Values Learn could not be
described at all, because nobody outside GM knew which CPID it used or what
bytes it carried, and this repository will not guess at a byte sequence whose
job is to command clutches. That objection has been answered: the CPID and the
control packet were recovered by static analysis of the GDS2 dealer data (see
dealer_data.py and ``data/gds2_t76_tcm.json``), and every field is traceable to
a file offset.

Answered is not the same as proven. What follows is therefore a *description*
of the exchange, in a shape the harness can display now and could execute
later, sitting behind three separate gates. It is not an implementation.

THE THREE GATES, AND WHY EACH ONE IS THERE
------------------------------------------
1. ``verified`` is False. The byte reconstruction has been checked
   independently and it holds as a static decode, but no part of it has ever
   been put on a car. Until it has, the definition refuses on its own flag.
2. ``confirm`` must be passed explicitly. No function in this module emits
   anything on a default call, and there is no default that means yes.
3. There is no GMLAN device-control transport in this repository. gmlan.py
   implements the read side only and says so. So even past gates 1 and 2 the
   execute path has nothing to send with, and reports that rather than
   improvising one.

Gate 3 is the load-bearing one and it is deliberate. Adding a $AE writer is a
decision that should be taken on purpose, with a live car, a charger and
somebody watching the gearbox -- not as a side effect of a data import.

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

from . import dealer_data

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
    "runningStates": (11, 12, 14),
    "completeStates": (1, 13),
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


def blocking_reasons(procedure_id: str, confirm: bool = False) -> list:
    """Every reason this procedure will not run, in the order they are hit.

    Returned rather than raised so a caller can show the operator the whole
    list at once instead of one obstacle at a time.
    """
    proc = PROCEDURES.get(procedure_id)
    if proc is None:
        return [f"There is no procedure with id '{procedure_id}'."]
    reasons = []
    if not proc.get("verified"):
        reasons.append(
            "The byte reconstruction is marked unverified. It is a confirmed "
            "static decode of the dealer data, but no part of it has been put "
            "on a car, and whether the module demands SecurityAccess is still "
            "open."
        )
    if not confirm:
        reasons.append(
            "No explicit confirmation was given. Nothing in this module sends "
            "anything on a default call."
        )
    reasons.append(
        "There is no GMLAN DeviceControl transport in this harness. gmlan.py "
        "implements the read side only. Writing one is a deliberate decision "
        "to be taken with a car, a charger and somebody watching the gearbox "
        "-- not a side effect of importing dealer data."
    )
    return reasons


def execute(adapter: Optional[dict], procedure_id: str,
            confirm: bool = False) -> dict:
    """Refuse, and say exactly why.

    This is the only entry point that could ever send a device control, and it
    currently cannot. It exists so that the refusal is a single auditable place
    rather than an absence, and so that adding the transport later means
    satisfying gates that are already written down.
    """
    proc = PROCEDURES.get(procedure_id)
    reasons = blocking_reasons(procedure_id, confirm)
    return {
        "ok": False,
        "runnable": False,
        "sent": [],
        "procedure": proc,
        "blockedBy": reasons,
        "message": (
            f"'{proc['name']}' was not run. " + " ".join(reasons)
            if proc else reasons[0]
        ),
    }
