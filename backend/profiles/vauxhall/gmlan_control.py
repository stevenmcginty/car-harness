"""GMW3110 DeviceControl ($AE) -- the write side gmlan.py deliberately left out.

WHAT CHANGED, AND WHAT DID NOT
------------------------------
gmlan.py says, in its own header, that DeviceControl is not implemented because
writing to a GM powertrain module needs GDS2 rather than a dongle. That
objection was about not knowing the bytes. The bytes are known now -- recovered
from the GDS2 dealer deliverable, re-derived independently from raw file
offsets, and reproduced across five separately built copies (see
``data/gds2_t76_tcm.json``, ``procedures.py`` and
``captures/reports/verification-2026-08-18.md`` Part 2).

So this module exists. What has NOT changed is that none of it has ever been
seen on a wire. A confirmed static decode of a dealer database is not a live
handshake, and this file is written on the assumption that the first real
attempt may be refused, may be ignored, or may start something that then has
to be got out of safely.

THE FAILURE THAT MATTERS
------------------------
The dangerous outcome here is not a rejected command. A module that answers
NRC 0x33 has done nothing, and the car drives away exactly as it arrived. The
dangerous outcome is a learn that STARTS and does not FINISH: the adaptive
cells are cleared at the start of the routine and only rewritten when it
completes, so an aborted run leaves a gearbox with no adapts at all -- worse
than the one that was driven in, and the thing P287C exists to flag.

Everything about the shape of this module follows from that:

* Once the start frame has gone out, the release sequence is owned by a
  ``finally``. There is no path -- exception, timeout, keep-alive loss,
  operator abort, dongle disconnect -- that skips it. ``ControlSession`` is a
  context manager for exactly this reason, so the release cannot be forgotten
  by a caller that returns early.
* The release is attempted even when the link is already broken. It will
  usually fail in that case, and the transcript records that it failed, which
  is the honest outcome: the harness tried, and the operator now knows the
  module may still be in device control and needs an ignition cycle.
* The transcript is written as the run happens rather than assembled at the
  end, so a run that dies mid-sequence still leaves a record of where it got to.

WHAT AN ELM327 CAN AND CANNOT DO HERE, PLAINLY
----------------------------------------------
Both frames in this sequence are seven bytes, which is exactly what fits an
ISO-TP single frame, so no multi-frame handling and no flow control is needed
in either direction. That part is comfortable.

The uncomfortable part is TesterPresent. An ELM327 is half duplex: it accepts
one command, waits for the reply, and prints it. There is no way to emit a
0x3E on a timer from a second thread without corrupting the request/response
pairing of whatever poll is in flight. So the keep-alive here is INTERLEAVED --
sent between polls, never alongside them -- and that means its interval is
bounded below by however long the previous poll took. On a slow BLE link a
single poll that stalls for two seconds pushes the keep-alive late by two
seconds, and nothing in this design can prevent that.

That is not a solvable problem at this layer, so it is treated as a detectable
one instead: ``KeepAlive`` measures the real gap since the last ACCEPTED 0x3E
and raises ``KeepAliveLost`` when it exceeds the deadline. Losing the keep-alive
aborts the run -- and, per the paragraph above, aborting still runs the release.
Whether the module's own timeout is longer or shorter than the deadline used
here is unknown; the number is a guess made deliberately tight.

WHY THE FRAMES ARE LOOKED UP RATHER THAN WRITTEN DOWN
-----------------------------------------------------
No $AE frame in this module is a literal. Every one is rebuilt from the dealer
data's own fields -- CPID, byte pattern, payload offset, write equation, state
value -- and then checked against the request string the extract carries, so a
mismatch between the two is a hard error rather than a silent preference for
one of them.

That is not tidiness. The encoded command value is a property of the PID SET,
not of the CPID: the same reference name encodes Start as 8 rather than 4 in
another set in the same deliverable. A hard-coded 4 would be correct for this
Astra K and wrong for the next car, in a way nothing would catch. Looking it up
means a different car with a different set gets a different byte or an error,
and never a confidently wrong one.

The same rule gives the forbidden-frame guarantee for free. ``transmit`` will
only send an $AE frame that the dealer data lists as a command request for the
bound PID set, so the retracted gear probe -- the one a previous write-up
recommended as harmless, which sets bit 0 of a byte whose gear encoding lives
in the high nibble, i.e. an undefined state on a solenoid driver -- cannot be
produced by any input to this module. It is also blocked by name, because a
guarantee worth having is worth having twice.
"""

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError

from . import dealer_data
from .gmlan import nrc_name

# --------------------------------------------------------------------------- #
# Services. These are GMW3110, not UDS -- see gmlan.py's header for the map.
# --------------------------------------------------------------------------- #
SVC_INITIATE_DIAG = 0x10        # InitiateDiagnosticOperation
SVC_RETURN_TO_NORMAL = 0x20     # ReturnToNormalOperation
SVC_READ_BY_PID = 0x22          # ReadDataByParameterIdentifier (same as UDS)
SVC_TESTER_PRESENT = 0x3E       # same id as UDS
SVC_DEVICE_CONTROL = 0xAE       # DeviceControl, CPID based

ENTER_DIAGNOSTIC = bytes([SVC_INITIATE_DIAG, 0x81])
TESTER_PRESENT = bytes([SVC_TESTER_PRESENT])
RETURN_TO_NORMAL = bytes([SVC_RETURN_TO_NORMAL])

# The pause the dealer data puts in the release sequence. The NUMBER is read
# from the data -- field .3 of the second Return To Normal command, the string
# '500'. WHERE IT SITS IS AN INFERENCE: the independent verification could not
# establish whether the runtime applies it before or after sending the trailing
# 0x20, and this module applies it before, which is the ordering the extraction
# report assumed. A pause of half a second somewhere in a release sequence is
# harmless either way, but it is named rather than slept so that the assumption
# is visible instead of buried in a magic number.
RELEASE_DELAY_MS = 500
RELEASE_DELAY_PLACEMENT = (
    "The 500 ms value is read from the dealer data. Its position in the "
    "sequence -- here, between the Null CPID release and the trailing 0x20 -- "
    "is an inference and was not established from the runtime."
)

# Keep-alive timing. The interval is what the sequence AIMS for between 0x3E
# frames; the deadline is the gap after which the channel is treated as lost.
# Neither number comes from the dealer data: the runtime's own interval lives
# in a variable this extraction did not resolve, and the module's tolerance has
# never been measured on a car. They are set deliberately tight, because
# noticing a late keep-alive and releasing cleanly is a much better failure
# than discovering the module dropped the session halfway through.
KEEPALIVE_INTERVAL_S = 1.5
KEEPALIVE_DEADLINE_S = 4.0

# Negative response codes a live attempt is genuinely likely to meet, with the
# plain-words reading each one deserves. These are outcomes, not crashes: a
# module that refuses has done nothing to the gearbox.
EXPECTED_NRCS = {
    0x33: (
        "security access denied",
        "The module demanded SecurityAccess. GDS2 offers no key for this test, "
        "which was never the same thing as the module not asking for one -- "
        "this is the open question the static analysis could not settle, and "
        "it has now been settled in the unhelpful direction. There is no "
        "working seed/key algorithm in the dealer deliverable to answer with, "
        "so this car needs GDS2/MDI2 or an equivalent tool for this routine. "
        "Nothing was changed.",
    ),
    0x22: (
        "conditions not correct",
        "The module was listening and understood the request, but refused it "
        "on its own interlocks -- brake, selector, engine speed, road speed or "
        "fluid temperature. Read the 17-state status parameter (22 28 23): it "
        "names which one. Nothing was changed.",
    ),
    0x31: (
        "request out of range",
        "The module rejected the CPID or the command value. On this car that "
        "would mean the recovered control packet does not match this "
        "calibration -- the byte reconstruction is a static decode of the "
        "dealer database and this would be the first evidence against it. "
        "Nothing was changed.",
    ),
}


class DeviceControlError(Exception):
    """Base for the things that stop a control sequence."""


class NotConfirmed(DeviceControlError):
    """A $AE transmit was attempted without an explicit confirmation."""


class ForbiddenFrame(DeviceControlError):
    """A $AE frame was assembled that this profile refuses to put on a bus."""


class ControlRefused(DeviceControlError):
    """The module answered a negative response to a control frame."""

    def __init__(self, nrc: int, service: int = SVC_DEVICE_CONTROL):
        self.nrc = nrc
        self.service = service
        name, explanation = EXPECTED_NRCS.get(nrc, (nrc_name(nrc), ""))
        self.short = name
        self.explanation = explanation or (
            "The module refused the request and gave a code this profile has "
            "no specific reading for. Nothing was changed."
        )
        super().__init__(f"NRC 0x{nrc:02X} ({name})")


class KeepAliveLost(DeviceControlError):
    """TesterPresent went unanswered, or came too late to be trusted."""


class OperatorAbort(DeviceControlError):
    """The operator asked for the run to stop."""


# --------------------------------------------------------------------------- #
# Frames, built from the dealer data rather than written down
# --------------------------------------------------------------------------- #
# The retracted probe frame, named so it can be refused by identity as well as
# by construction. An earlier write-up recommended this as a harmless first
# test. It is not a gear command and it is not harmless: the gear-select write
# equation is X(0)<<4, so the gear lives in the high nibble, and this value
# instead sets bit 0 -- an undefined state on a transmission solenoid driver.
# The real gear-1 frame ends in a high nibble of 9. Built here from parts so
# that the frame's own text appears nowhere in this repository.
FORBIDDEN_FRAMES = frozenset({
    bytes([SVC_DEVICE_CONTROL, 0x30, 0x80 | 0x01, 0x00, 0x00, 0x00, 0x00]),
})


def format_frame(frame: bytes) -> str:
    """Frames are shown the way the dealer data and the reports write them:
    space-separated upper-case pairs. Every comparison in this module is on
    bytes; this is for humans."""
    return " ".join(f"{b:02X}" for b in frame)


def build_control_frame(control_ref: str, command_text: str, pid_set: str) -> bytes:
    """Assemble one $AE frame from the dealer data's own fields.

    Nothing here is a constant. The CPID comes from the data item's id, the
    five payload bytes from its byte pattern, the position of the command value
    from its payload offset, and the value itself from the write conversion's
    state table by NAME -- so asking for "Start" gets whatever this PID set
    encodes Start as, and a set that encodes it differently produces a
    different frame instead of a wrong one.

    The assembled frame is then checked against the request string the extract
    carries for the same command. Those are two independent renderings of the
    same fields, and disagreement means the extract or this builder is wrong;
    either way it is an error and not something to pick a favourite from.
    """
    control = dealer_data.device_control(control_ref)
    if control is None:
        raise DeviceControlError(
            f"The dealer data has no device control '{control_ref}'."
        )
    if control.get("pidSet") != pid_set:
        raise DeviceControlError(
            f"Device control '{control_ref}' belongs to PID set "
            f"{control.get('pidSet')}, not to the bound set {pid_set}. The "
            "encoded command value is a property of the set, so this frame "
            "must not be built from another car's data."
        )

    item = control.get("item") or {}
    write = control.get("write") or {}
    cpid = item.get("id")
    pattern = [int(b, 16) for b in (item.get("bytePattern") or "").split(",") if b]
    offset = item.get("payloadOffset")
    if cpid is None or offset is None or len(pattern) != 5:
        raise DeviceControlError(
            f"Device control '{control_ref}' does not carry a complete control "
            "packet in the extract (CPID, five-byte pattern and payload "
            "offset are all required)."
        )

    states = {s.get("text"): s.get("value") for s in write.get("states", [])}
    if command_text not in states:
        raise DeviceControlError(
            f"Device control '{control_ref}' has no command called "
            f"'{command_text}'. It offers: {sorted(states) or 'none'}."
        )
    encoded = dealer_data.apply_equation(write.get("equation", ""), states[command_text])
    if encoded is None:
        raise DeviceControlError(
            f"The write equation {write.get('equation')!r} on '{control_ref}' "
            "has no implemented rule, so the command value cannot be encoded. "
            "Refusing rather than guessing at a byte that drives a solenoid."
        )
    encoded = int(encoded)
    if not 0 <= encoded <= 0xFF:
        raise DeviceControlError(
            f"The encoded command value {encoded} for '{control_ref}' does not "
            "fit the single byte the control packet has room for."
        )

    frame = bytearray([SVC_DEVICE_CONTROL, cpid] + pattern)
    frame[2 + offset] |= encoded
    built = bytes(frame)

    expected = (control.get("commands") or [])
    for command in expected:
        if command.get("text") == command_text:
            if format_frame(built) != command.get("request"):
                raise DeviceControlError(
                    f"Rebuilt frame {format_frame(built)} disagrees with the "
                    f"extract's own {command.get('request')} for "
                    f"'{control_ref}' / '{command_text}'."
                )
            break
    return built


def build_null_cpid_frame(control_ref: str, pid_set: str) -> bytes:
    """The release frame: the Null CPID control, carrying no command value.

    Built the same way as a command frame minus the OR-in, which is what makes
    it a release -- it names no CPID and commands nothing. On this car's copy
    of the test the bound form is the seven-byte one; the two-byte AE 00 is a
    separate Null CPID setup reached by a different path in the dealer tool.
    """
    control = dealer_data.device_control(control_ref)
    if control is None:
        raise DeviceControlError(
            f"The dealer data has no device control '{control_ref}'."
        )
    if control.get("pidSet") != pid_set:
        raise DeviceControlError(
            f"Release control '{control_ref}' belongs to PID set "
            f"{control.get('pidSet')}, not to the bound set {pid_set}."
        )
    item = control.get("item") or {}
    cpid = item.get("id")
    pattern = [int(b, 16) for b in (item.get("bytePattern") or "").split(",") if b]
    if cpid is None or len(pattern) != 5:
        raise DeviceControlError(
            f"Release control '{control_ref}' does not carry a complete "
            "control packet in the extract."
        )
    built = bytes([SVC_DEVICE_CONTROL, cpid] + pattern)
    if control.get("request") and format_frame(built) != control["request"]:
        raise DeviceControlError(
            f"Rebuilt release frame {format_frame(built)} disagrees with the "
            f"extract's own {control['request']}."
        )
    return built


def known_control_frames(pid_set: str) -> set:
    """Every $AE frame the dealer data lists for one PID set.

    This is the allow-list ``transmit`` checks against. It is deliberately the
    whole set rather than just this procedure's two frames: the guarantee being
    made is "nothing invented", not "nothing but these", and narrowing it to
    two would make the guard look stronger than the argument behind it.
    """
    frames = set()
    for control in dealer_data.load().get("deviceControls", []):
        if control.get("pidSet") != pid_set:
            continue
        requests = [control.get("request")] + [
            c.get("request") for c in (control.get("commands") or [])
        ]
        for text in requests:
            if not text:
                continue
            try:
                frames.add(bytes.fromhex(text.replace(" ", "")))
            except ValueError:
                continue
    return frames


# --------------------------------------------------------------------------- #
# Transcript
# --------------------------------------------------------------------------- #
class Transcript:
    """Every frame, in order, with the time it happened.

    Written as the run happens rather than assembled at the end, so a sequence
    that dies halfway still leaves a record of how far it got. That is the
    whole point: a run that goes wrong on a car should be diagnosable
    afterwards rather than reconstructed from memory.

    Times are seconds since the first line, to three places. The wall clock is
    recorded once at the top; per-line wall clock would be noise, and the thing
    that matters when reading one of these back is the GAP between frames --
    that is what says whether the keep-alive was late.
    """

    def __init__(self, label: str, clock: Callable[[], float] = time.monotonic):
        self.label = label
        self._clock = clock
        self._t0 = clock()
        self.startedAt = datetime.now(timezone.utc).isoformat()
        self.entries: List[dict] = []

    def _stamp(self) -> float:
        return round(self._clock() - self._t0, 3)

    def _add(self, direction: str, text: str, note: str = "") -> dict:
        entry = {"t": self._stamp(), "dir": direction, "frame": text, "note": note}
        self.entries.append(entry)
        return entry

    def note(self, text: str) -> None:
        self._add("--", "", text)

    def sent(self, frame: bytes, note: str = "") -> None:
        self._add("->", format_frame(frame), note)

    def received(self, frame: Optional[bytes], note: str = "") -> None:
        self._add("<-", format_frame(frame) if frame else "(nothing)", note)

    def lines(self) -> List[str]:
        out = [f"# {self.label}", f"# started {self.startedAt}", ""]
        for entry in self.entries:
            stamp = f"{entry['t']:8.3f}"
            frame = entry["frame"]
            if entry["dir"] == "--":
                out.append(f"{stamp}         {entry['note']}")
                continue
            line = f"{stamp}  {entry['dir']}  {frame:<24}"
            out.append(line + ("  " + entry["note"] if entry["note"] else "").rstrip())
        return out

    def text(self) -> str:
        return "\n".join(self.lines()) + "\n"

    def save(self, directory: Path, stem: str) -> str:
        """Persist to captures/vauxhall/ the way other captures persist.

        Callers only reach here for a REAL run. Simulated results are never
        written to captures/ -- backend/core/captures.py makes that rule
        explicit and load-bearing, and a demo transcript sitting in the dataset
        next to real ones would be exactly the contamination it exists to
        prevent. A simulated run still returns its whole transcript in the
        result, which is what makes it watchable.
        """
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"{stem}-{stamp}.txt"
        path.write_text(self.text(), encoding="utf-8")
        return str(path)


# --------------------------------------------------------------------------- #
# Keep-alive
# --------------------------------------------------------------------------- #
class KeepAlive:
    """Interleaved 0x3E TesterPresent, with the gap actually measured.

    An ELM327 cannot send this on a timer -- see the module header -- so it
    goes out between polls, and its interval is bounded below by however long
    the previous poll took. ``due`` says a frame is owed; ``check`` decides
    whether the channel can still be trusted.

    ``deadline`` is measured from the last ACCEPTED keep-alive, not from the
    last attempt. An unanswered 0x3E is not on its own proof the channel is
    gone (an ELM327 will occasionally swallow one), but a run of them, or one
    long enough stall, is.
    """

    def __init__(self, transport, interval: float = KEEPALIVE_INTERVAL_S,
                 deadline: float = KEEPALIVE_DEADLINE_S):
        self.transport = transport
        self.interval = interval
        self.deadline = deadline
        self.last_ok = transport.clock()
        self.sent = 0
        self.missed = 0

    def due(self) -> bool:
        return (self.transport.clock() - self.last_ok) >= self.interval

    def poke(self) -> None:
        """Send one keep-alive if one is owed, then check the channel.

        Raises KeepAliveLost, which every caller lets propagate -- the release
        is owned by a finally, so an abort here still exits the module cleanly.
        """
        if self.due():
            self.sent += 1
            try:
                reply = self.transport.request(TESTER_PRESENT, timeout=1.0,
                                               note="keep-alive")
            except (ObdError, TransportError, DeviceControlError):
                reply = None
            if reply and reply[0] == SVC_TESTER_PRESENT + 0x40:
                self.last_ok = self.transport.clock()
            else:
                self.missed += 1
                self.transport.transcript.note(
                    "keep-alive not acknowledged "
                    f"({self.missed} unacknowledged so far)"
                )
        self.check()

    def check(self) -> None:
        gap = self.transport.clock() - self.last_ok
        if gap > self.deadline:
            raise KeepAliveLost(
                f"No TesterPresent has been acknowledged for {gap:.1f} s "
                f"(the deadline is {self.deadline:.1f} s). The module may have "
                "dropped the diagnostic session, so the routine is being "
                "abandoned and the control released."
            )


# --------------------------------------------------------------------------- #
# The transport
# --------------------------------------------------------------------------- #
class ControlTransport:
    """One $AE-capable channel to one module, over an Elm327UdsClient.

    ``client`` only has to provide ``request(dst, data, timeout) -> bytes``,
    which is the same tiny interface backend/core/uds.py's helpers expect. That
    is what lets the simulated TCM stand in for a real one without either side
    knowing.
    """

    def __init__(self, client, req_id: int, pid_set: str, transcript: Transcript,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.client = client
        self.req_id = req_id
        self.pid_set = pid_set
        self.transcript = transcript
        self.sleep = sleep
        self.clock = clock
        self.allowed_frames = known_control_frames(pid_set)
        self.sent_control = False   # has a $AE command gone out on this channel

    # ------------------------------------------------------------------ #
    def request(self, data: bytes, timeout: float = 2.0, note: str = "") -> Optional[bytes]:
        """Send one frame and record both halves. Never raises on a negative
        response -- a 0x7F is an answer, and the caller decides what it means."""
        self.transcript.sent(data, note)
        try:
            reply = self.client.request(self.req_id, data, timeout=timeout)
        except (ObdError, TransportError) as exc:
            self.transcript.received(None, f"link: {exc}")
            raise
        if reply and reply[0] == 0x7F and len(reply) >= 3:
            self.transcript.received(
                reply, f"NRC {reply[2]:02X} ({nrc_name(reply[2])})"
            )
        else:
            self.transcript.received(reply)
        return reply

    # ------------------------------------------------------------------ #
    def transmit_control(self, frame: bytes, confirm: bool = False,
                         note: str = "") -> Optional[bytes]:
        """Put one $AE frame on the bus. CHANGES THE CAR.

        ``confirm`` must be exactly True at this call site. It is not a stored
        property of the session and there is no value of the transport's own
        state that supplies it -- every $AE frame is confirmed where it is
        sent, once, for that frame. That is the same rule the inert version in
        procedures.py enforced, kept intact now that there is something behind
        it to enforce it against.

        The one place the transport asserts its own confirmation is the
        release, and that is deliberate: a release that the operator could
        withhold would be a way to leave a gearbox with no adapts, which is the
        failure this whole module is shaped around. The release commands
        nothing -- it is the Null CPID -- so there is nothing to consent to.
        """
        if confirm is not True:
            raise NotConfirmed(
                "A GMLAN device control was assembled but not confirmed. "
                "Nothing in this profile sends $AE on a default call, and "
                "there is no value of any stored flag that means yes."
            )
        if frame in FORBIDDEN_FRAMES:
            raise ForbiddenFrame(
                "That frame is on this profile's refusal list. It was once "
                "recommended as a harmless probe and it is neither harmless "
                "nor a valid command -- the gear-select equation puts the gear "
                "in the high nibble, so this value requests an undefined state "
                "on a transmission solenoid driver."
            )
        if frame not in self.allowed_frames:
            raise ForbiddenFrame(
                f"{format_frame(frame)} is not a control frame the dealer data "
                f"lists for PID set {self.pid_set}. This profile only sends "
                "control packets it can point at in GM's own database; it does "
                "not assemble new ones."
            )
        self.sent_control = True
        reply = self.request(frame, timeout=5.0, note=note)
        if reply and reply[0] == 0x7F and len(reply) >= 3:
            raise ControlRefused(reply[2])
        if not reply or reply[0] != SVC_DEVICE_CONTROL + 0x40:
            raise DeviceControlError(
                "The module did not answer the device control with a positive "
                f"response ({format_frame(reply) if reply else 'nothing came back'})."
            )
        return reply

    # ------------------------------------------------------------------ #
    def enter_diagnostic(self) -> None:
        """10 81 InitiateDiagnosticOperation.

        The 0x81 sub-function is read verbatim from the dealer tool's own
        template table and is not interpreted here. A module that refuses it
        has refused the session, which is a clean stop before anything has been
        commanded.
        """
        reply = self.request(ENTER_DIAGNOSTIC, timeout=3.0,
                             note="enter diagnostic session")
        if reply and reply[0] == 0x7F and len(reply) >= 3:
            raise ControlRefused(reply[2], service=SVC_INITIATE_DIAG)
        if not reply or reply[0] != SVC_INITIATE_DIAG + 0x40:
            raise DeviceControlError(
                "The module did not accept the diagnostic session request."
            )

    def read_pid(self, did: int, timeout: float = 1.5) -> Optional[bytes]:
        """22 xx xx, returning the payload after the echoed identifier.

        Reads are the safe half of this exchange and are used both before the
        run (preconditions) and during it (status polling). A refusal returns
        None rather than raising: a poll that cannot be read is handled by the
        sequence's own timeout, not by tearing down mid-routine.
        """
        frame = bytes([SVC_READ_BY_PID, (did >> 8) & 0xFF, did & 0xFF])
        try:
            reply = self.request(frame, timeout=timeout)
        except (ObdError, TransportError):
            return None
        if not reply or reply[0] != SVC_READ_BY_PID + 0x40 or len(reply) < 4:
            return None
        return reply[3:]

    # ------------------------------------------------------------------ #
    def release(self, release_frame: bytes) -> dict:
        """The exit sequence, run on every path out. CHANGES THE CAR.

        Null CPID release, then the data's 500 ms, then ReturnToNormalOperation
        twice -- the dealer tool's Return To Normal command and its
        InstExitStateOptional emit the same 0x20, and both are sent because
        both are in the recovered sequence and a second 0x20 costs nothing.

        Every step is attempted even if an earlier one failed. This runs when
        things have already gone wrong, so giving up at the first error would
        be exactly the wrong instinct; what the caller needs is a record of
        which parts of the release the module accepted.
        """
        steps: List[dict] = []

        def attempt(name: str, send):
            try:
                reply = send()
            except (ObdError, TransportError, DeviceControlError) as exc:
                self.transcript.note(f"release step '{name}' failed: {exc}")
                steps.append({"step": name, "ok": False, "detail": str(exc)})
                return None
            steps.append({"step": name, "ok": True})
            return reply

        # Confirmation is asserted rather than passed here -- see
        # transmit_control's docstring for why the release must not be
        # withholdable.
        attempt("null-cpid-release",
                lambda: self.transmit_control(release_frame, confirm=True,
                                              note="release device control"))
        self.transcript.note(
            f"pause {RELEASE_DELAY_MS} ms -- {RELEASE_DELAY_PLACEMENT}"
        )
        self.sleep(RELEASE_DELAY_MS / 1000.0)
        attempt("return-to-normal",
                lambda: self.request(RETURN_TO_NORMAL, timeout=3.0,
                                     note="ReturnToNormalOperation"))
        attempt("exit-diagnostic",
                lambda: self.request(RETURN_TO_NORMAL, timeout=3.0,
                                     note="InstExitStateOptional"))
        released = all(s["ok"] for s in steps)
        self.transcript.note(
            "release sequence complete" if released
            else "RELEASE INCOMPLETE -- the module may still be in device "
                 "control. Switch the ignition off and let the module power "
                 "down before driving the car."
        )
        return {"released": released, "steps": steps}


class ControlSession:
    """A context manager whose ``__exit__`` is the release.

    The release is not a step the sequence remembers to take; it is a property
    of leaving the block, on every path -- normal return, exception, abort,
    keep-alive loss. That is the whole safety argument of this file expressed
    as a language construct, and it is why the sequence in learn.py has no
    release call of its own to forget.

    The release is skipped in exactly one case: no control frame ever went out.
    A release before a start would be a stray write for no reason, so a session
    that failed at the diagnostic-session request leaves the bus alone.
    """

    def __init__(self, transport: ControlTransport, release_frame: bytes):
        self.transport = transport
        self.release_frame = release_frame
        self.release_result: Optional[dict] = None

    def __enter__(self) -> ControlTransport:
        return self.transport

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self.transport.sent_control:
            self.transport.transcript.note(
                "no device control was ever sent, so there is nothing to "
                "release -- leaving the bus alone"
            )
            # Not "released: True" -- there was nothing to release, and saying
            # otherwise would report a successful release that never happened.
            self.release_result = {"released": None, "steps": [], "skipped": True}
            return False
        if exc is not None:
            self.transport.transcript.note(f"exiting because: {exc}")
        self.release_result = self.transport.release(self.release_frame)
        return False  # never swallow the exception; the caller reports it
