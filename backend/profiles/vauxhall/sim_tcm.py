"""A simulated Astra K T76 transmission controller, for running the learn
sequence with no car attached.

WHY THIS IS A WHOLE FAKE MODULE AND NOT A CANNED RESULT
-------------------------------------------------------
Every other simulated result in this repository is a fixed dictionary --
scan.py's ``_simulated_scan`` returns modules that were never asked for
anything. That is the right shape for a read: the thing being demonstrated is
the display, and nothing about the exchange matters.

It is the wrong shape here. The thing that has to be demonstrated for this
procedure is the EXCHANGE: that the release runs after an exception, that a
late keep-alive aborts, that NRC 0x33 is reported in words rather than
crashing. A canned result proves none of that, and those are exactly the paths
that will decide whether a real attempt leaves a working gearbox behind. So
this is a module that answers frames, and the sequence in learn.py cannot tell
it from the real thing -- it exposes the same ``request(dst, data, timeout)``
interface the ELM327 UDS client does.

It is a model of the dealer data, not of a gearbox. What it knows is what the
extract knows: the parameter ids, the scalings, the 17-state status table and
the control packets. What it does NOT know is how a real T76 behaves, because
nobody has watched one do this. In particular the state progression below is
invented -- a plausible walk through states the data names, at a plausible
pace. It proves the harness handles a progression; it proves nothing about
what the real progression is.

VIRTUAL TIME
------------
The sim carries its own clock and its own sleep. A self-test that had to wait
out a real 500 ms release delay and a real minute of polling would not get run
often enough to be worth having, and a keep-alive deadline cannot be tested at
all without being able to make time pass on demand. So learn.py takes its clock
and sleep as arguments, the simulated car supplies both, and the whole sequence
including its timeouts runs in microseconds and deterministically.
"""

from typing import Optional

from backend.obd.elm327 import ObdError

# Parameter ids this fake answers, all read out of the extract rather than
# written down here (the request strings are in data/gds2_t76_tcm.json).
DID_FLUID_TEMP = 0x1940         # 8442_6736, (X*1)-40 °C
DID_INTERNAL_MODE = 0x194F      # 7975_51909, selector state table
DID_LEARN_STATUS = 0x2823       # 8682_51909, the 17-state learn enum
PID_ENGINE_SPEED = 0x000C       # 7639_6738, (X*0.25) rpm

SVC_INITIATE_DIAG = 0x10
SVC_RETURN_TO_NORMAL = 0x20
SVC_READ_BY_PID = 0x22
SVC_TESTER_PRESENT = 0x3E
SVC_DEVICE_CONTROL = 0xAE

# How the status parameter walks once the learn has been started: (seconds
# since the start frame, state). INVENTED -- see the module header. States and
# their names are the dealer data's; the timing and the order are a guess at a
# routine nobody has watched.
DEFAULT_PROGRESSION = (
    (0.0, 11),    # Transmission Purge in Progress
    (4.0, 12),    # Drive Calibration Test in Progress
    (10.0, 14),   # Reverse Calibration Test in Progress
    (16.0, 13),   # Drive Calibration Test Complete
    (20.0, 1),    # Fast Learn Complete
)

ABORT_PROGRESSION = (
    (0.0, 11),
    (4.0, 12),
    (9.0, 15),    # Aborted Test Criteria Not Met
)


class SimulatedTcm:
    """A fake TCM at 0x7E2 that answers the frames this procedure sends.

    ``scenario`` picks which way the run goes. Each one exists because there is
    a code path in learn.py that has to be shown working before anything is
    plugged into a car:

        complete        the routine runs to Fast Learn Complete
        cold            fluid temperature below the gate -- refused up front
        not-in-park     selector in Drive -- refused up front
        engine-off      engine not running -- refused up front
        not-ready       status reports Brake Must be Applied -- refused up front
        nrc33           the module demands SecurityAccess
        nrc22           the module refuses on its own conditions
        nrc31           the module rejects the CPID or the value
        module-abort    the routine starts and the module aborts it
        keepalive-loss  the module stops acknowledging TesterPresent
        exception       something unexpected blows up mid-routine
        release-refused the release frame itself is refused
    """

    SCENARIOS = (
        "complete", "cold", "not-in-park", "engine-off", "not-ready",
        "nrc33", "nrc22", "nrc31", "module-abort", "keepalive-loss",
        "exception", "release-refused",
    )

    def __init__(self, scenario: str = "complete", latency: float = 0.12):
        if scenario not in self.SCENARIOS:
            raise ValueError(
                f"Unknown simulation scenario '{scenario}'. "
                f"Try one of: {', '.join(self.SCENARIOS)}."
            )
        self.scenario = scenario
        self.latency = latency
        self.now = 0.0
        self.frames = []            # every request this fake was handed
        self.started_at: Optional[float] = None
        self.released = False
        self.in_session = False
        self.polls_since_start = 0

        # Starting live values. Warm, in Park, idling -- the state a workshop
        # would have the car in -- unless the scenario says otherwise.
        self.fluid_temp_c = 92
        self.selector_state = 1     # Park
        self.engine_rpm = 780
        if scenario == "cold":
            self.fluid_temp_c = 48
        if scenario == "not-in-park":
            self.selector_state = 6  # Drive
        if scenario == "engine-off":
            self.engine_rpm = 0

        self.progression = (
            ABORT_PROGRESSION if scenario == "module-abort" else DEFAULT_PROGRESSION
        )

    # ------------------------------------------------------------------ #
    # virtual time -- learn.py takes these as its clock and sleep
    # ------------------------------------------------------------------ #
    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)

    # ------------------------------------------------------------------ #
    def _status_state(self) -> int:
        """The learn status this module would report right now."""
        if self.scenario == "not-ready" and self.started_at is None:
            return 2                      # Brake Must be Applied
        if self.started_at is None:
            return 0                      # Fast Learn Disabled -- willing
        elapsed = self.now - self.started_at
        state = self.progression[0][1]
        for at, value in self.progression:
            if elapsed >= at:
                state = value
        return state

    def _raw_temperature(self) -> int:
        """The byte the module would put on the wire for its fluid temperature,
        inverting the extract's own (X*1)-40 rather than assuming the offset."""
        return max(0, min(255, int(self.fluid_temp_c) + 40))

    # ------------------------------------------------------------------ #
    def request(self, dst: int, data: bytes, timeout: float = 2.0) -> bytes:
        """The Elm327UdsClient interface: one request in, the raw reply out.

        Negative responses come back as bytes, exactly as the real client
        returns them -- it is the caller's job to read a 0x7F, not the
        transport's to raise on one.
        """
        self.frames.append(data.hex().upper())
        self.now += self.latency

        # A scenario that models a module which has stopped listening: after
        # the control has gone out, TesterPresent goes unanswered. This is the
        # shape a lost session actually has -- the module does not announce
        # that it has dropped you, it just stops answering.
        if (self.scenario == "keepalive-loss" and self.started_at is not None
                and data[:1] == bytes([SVC_TESTER_PRESENT])):
            self.now += 1.8   # and the stall itself costs real time
            raise ObdError("The car returned no data for this request.",
                           kind="no_data")

        if data[:1] == bytes([SVC_INITIATE_DIAG]):
            self.in_session = True
            return bytes([SVC_INITIATE_DIAG + 0x40]) + data[1:]

        if data[:1] == bytes([SVC_TESTER_PRESENT]):
            return bytes([SVC_TESTER_PRESENT + 0x40])

        if data[:1] == bytes([SVC_RETURN_TO_NORMAL]):
            self.in_session = False
            return bytes([SVC_RETURN_TO_NORMAL + 0x40])

        if data[0] == SVC_READ_BY_PID and len(data) >= 3:
            return self._read(data)

        if data[0] == SVC_DEVICE_CONTROL:
            return self._device_control(data)

        return bytes([0x7F, data[0], 0x11])   # service not supported

    # ------------------------------------------------------------------ #
    def _read(self, data: bytes) -> bytes:
        did = (data[1] << 8) | data[2]
        head = bytes([SVC_READ_BY_PID + 0x40, data[1], data[2]])

        if did == DID_LEARN_STATUS:
            self.polls_since_start += 1 if self.started_at is not None else 0
            # An unexpected failure mid-routine: not a refusal, not a link
            # error, but the kind of thing that would normally propagate out of
            # a run and skip whatever came after it. The point of the scenario
            # is that here it does not skip the release.
            if self.scenario == "exception" and self.polls_since_start == 3:
                raise RuntimeError(
                    "simulated unexpected failure while polling learn status"
                )
            return head + bytes([self._status_state()])

        if did == DID_FLUID_TEMP:
            return head + bytes([self._raw_temperature()])

        if did == DID_INTERNAL_MODE:
            return head + bytes([self.selector_state])

        if did == PID_ENGINE_SPEED:
            raw = max(0, min(0xFFFF, int(self.engine_rpm * 4)))
            return head + bytes([raw >> 8, raw & 0xFF])

        return bytes([0x7F, SVC_READ_BY_PID, 0x31])   # request out of range

    # ------------------------------------------------------------------ #
    def _device_control(self, data: bytes) -> bytes:
        """$AE. The fake distinguishes the release from a command the same way
        the module would: by whether a CPID is named."""
        is_release = len(data) >= 2 and data[1] == 0x00
        if is_release:
            if self.scenario == "release-refused":
                return bytes([0x7F, SVC_DEVICE_CONTROL, 0x22])
            self.released = True
            return bytes([SVC_DEVICE_CONTROL + 0x40])

        refusals = {"nrc33": 0x33, "nrc22": 0x22, "nrc31": 0x31}
        if self.scenario in refusals:
            return bytes([0x7F, SVC_DEVICE_CONTROL, refusals[self.scenario]])

        self.started_at = self.now
        return bytes([SVC_DEVICE_CONTROL + 0x40])


def describe_scenarios() -> list:
    """The scenario list, for the operator-facing surfaces, with what each one
    is actually demonstrating rather than just its name."""
    catalogue = {
        "complete": "The routine runs through to Fast Learn Complete.",
        "cold": "Fluid too cold -- the precondition gate refuses before "
                "anything is sent.",
        "not-in-park": "Selector in Drive -- refused before anything is sent.",
        "engine-off": "Engine not running -- refused before anything is sent.",
        "not-ready": "The module's own status says Brake Must be Applied -- "
                     "refused before anything is sent.",
        "nrc33": "The module demands SecurityAccess. Reported in plain words, "
                 "released cleanly, nothing changed.",
        "nrc22": "The module refuses on its own conditions. Released cleanly.",
        "nrc31": "The module rejects the CPID or the command value. Released "
                 "cleanly.",
        "module-abort": "The routine starts and the module aborts it. This is "
                        "the outcome that leaves the gearbox needing another "
                        "attempt.",
        "keepalive-loss": "TesterPresent stops being acknowledged. The run is "
                          "abandoned and the control is still released.",
        "exception": "Something unexpected fails mid-routine. The release runs "
                     "anyway -- this is the one that matters most.",
        "release-refused": "The release itself is refused. Reported as an "
                           "incomplete release rather than as a success.",
    }
    return [
        {"id": name, "what": catalogue.get(name, "")}
        for name in SimulatedTcm.SCENARIOS
    ]
