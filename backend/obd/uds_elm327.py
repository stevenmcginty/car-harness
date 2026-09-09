"""UDS (ISO 14229) over a generic ELM327, using standard 11-bit physical
addressing (ISO 15765-4). This is the transport the Mazda profile -- and any
future "it's just an OBD dongle" profile -- talks manufacturer-level UDS over.

The BMW ENET path speaks UDS over HSFZ (backend/core/hsfz.py). This module is
its ELM327 counterpart: it drives the dongle with the AT commands that set a
per-module CAN header + receive filter + flow control, sends a UDS request,
reassembles the ISO-TP reply, and returns the raw UDS bytes -- INCLUDING
negative (0x7F) responses, which the generic OBD parser in elm327.py
deliberately discards.

It exposes the same tiny ``request(dst, data, timeout)`` interface the service
helpers in backend/core/uds.py expect (tester_present / read_dtcs / clear_dtcs),
so those work unchanged over an ELM327 instead of over HSFZ.

Ported from the proven Android recipe (android/.../obd/BmwUds.kt), adapted from
BMW's extended addressing (6F1 / ATCEA) to the standard 11-bit request/response
pairs Mazda uses (request 0x7E0-0x7E7, response = request + 8, unless an
explicit id_map says otherwise -- see Elm327UdsClient).
"""

import re
import time

from backend.obd.elm327 import (
    _HEX_LINE,
    _ISO_TP_CONT,
    _ISO_TP_LEN,
    _LINK_ERROR_TOKENS,
    _NO_DATA_TOKENS,
    ObdError,
)


def reassemble(text: str, echo: str):
    """An ELM327 reply -> a list of raw records (bytes), UNFILTERED.

    Handles single-line hex and the ELM's ISO-TP "length + NN:" multi-frame
    format (the same reassembly elm327.py does for the VIN). Unlike the generic
    parser it keeps negative responses (0x7F ...) -- module scanning must see
    them. Raises ObdError(kind='link'|'no_data'|'overflow') when the dongle
    reports it could not reach the car (link) or dropped the tail of an
    over-long reply (overflow), so both stay distinguishable from an ECU that
    genuinely answered a negative.
    """
    upper = text.upper()
    # BUFFER FULL is its own kind, not a generic link error: the module DID
    # answer -- the reply was simply bigger than the dongle's buffer (XF X260
    # ABS 0x7E6, a 299-byte 19 02 FF fault read, on-car 2026-08-20). Callers
    # can re-ask in smaller status-mask chunks; treating it as a dead link
    # would misreport a talking module as absent.
    if "BUFFER FULL" in upper:
        raise ObdError(
            "The dongle's buffer overflowed -- the reply tail was lost "
            "(re-ask in smaller chunks).", kind="overflow")
    for token in _LINK_ERROR_TOKENS:
        if token in upper:
            raise ObdError(f"The dongle reported '{token}'.", kind="link")
    for token in _NO_DATA_TOKENS:
        if token in upper:
            raise ObdError("The car returned no data for this request.", kind="no_data")

    records = []
    multi = {}
    multi_len = None
    for line in re.split(r"[\r\n]+", text):
        clean = line.strip().replace(" ", "").upper()
        if not clean or clean == ">":
            continue
        if clean in ("SEARCHING...", "SEARCHING", "BUSINIT...", "BUSINIT...OK", "OK"):
            continue
        if clean == echo.upper():  # command echo (belt & braces after ATE0)
            continue
        m = _ISO_TP_CONT.match(clean)
        if m:
            multi[int(m.group(1), 16)] = m.group(2)
            continue
        if _ISO_TP_LEN.match(clean):
            multi_len = int(clean, 16)
            continue
        if _HEX_LINE.match(clean) and len(clean) % 2 == 0:
            records.append(bytes.fromhex(clean))

    if multi:
        joined = "".join(multi[k] for k in sorted(multi))
        if len(joined) % 2 == 1:
            joined = joined[:-1]
        payload = bytes.fromhex(joined)
        if multi_len is not None and len(payload) > multi_len:
            payload = payload[:multi_len]
        records.append(payload)
    return records


class Elm327UdsClient:
    """A UDS client over one already-open Elm327Session, 11-bit addressing.

    Lifecycle: begin() once, then request()/the core.uds helpers per module,
    then restore() in a finally to hand the ELM back in the state the generic
    OBD stack expects (functional header, auto receive, adaptive timing on).
    """

    def __init__(self, session, id_map=None):
        self.session = session
        self._target = None  # request id the ELM is currently pointed at
        # id_map: request CAN id -> response CAN id, for modules that don't
        # answer on request + 8 (optional; unset modules fall back to +8).
        self.id_map = dict(id_map) if id_map else {}
        self._st = "64"     # the ATST ceiling begin() last set
        self._stn = None    # None = not asked yet; see _is_stn()

    # ------------------------------------------------------------------ #
    def begin(self):
        """One-off ELM setup for a UDS module session (undone by restore())."""
        self.session.at("ATAT0")   # adaptive timing off -- we manage the timeout
        self.session.at("ATST64")  # ~400ms per-frame ceiling, plenty for a live ECU
        self._st = "64"

    def restore(self):
        """Return the ELM to the defaults the generic OBD-II scan relies on."""
        for cmd in ("ATCRA", "ATSH7DF", "ATFCSM0", "ATAT1", "ATST32"):
            try:
                self.session.at(cmd)
            except Exception:
                pass
        self._target = None
        self._st = "32"

    def _aim(self, req_id: int):
        """Point the ELM at one module: TX header + RX filter + flow control.

        Standard OBD-II 11-bit pairing: a request on 0x7E0..0x7E7 is answered on
        request + 8 (0x7E8..0x7EF), unless id_map gives an explicit override for
        modules that don't follow that pairing. Flow control 30 00 00 =
        clear-to-send, no block-size limit, no separation time.
        """
        if self._target == req_id:
            return
        resp_id = self.id_map.get(req_id, req_id + 8)
        self.session.at(f"ATSH{req_id:03X}")
        self.session.at(f"ATFCSH{req_id:03X}")
        self.session.at(f"ATCRA{resp_id:03X}")
        self.session.at("ATFCSD300000")
        self.session.at("ATFCSM1")
        self._target = req_id

    # ------------------------------------------------------------------ #
    def _set_st(self, value: str):
        """Raise/lower the ELM's per-response wait ceiling (ATST, x4 ms)."""
        if value != self._st:
            self.session.at(f"ATST{value}")
            self._st = value

    @staticmethod
    def _pending_only(reply, data) -> bool:
        """True when the only usable reply was a bare NRC 0x78
        (responsePending): the ECU is still computing, not refusing."""
        return bool(reply) and len(reply) >= 3 and reply[0] == 0x7F \
            and reply[2] == 0x78

    def request(self, dst: int, data: bytes, timeout: float = 3.0) -> bytes:
        """Send one UDS request to module ``dst`` (its request CAN id) and return
        the reassembled UDS reply bytes.

        The reply may be a 0x7F negative response -- that is returned as-is so
        the caller's _check() (backend/core/uds.py) can raise a proper
        UdsNegativeResponse. Raises ObdError(kind='no_data'|'link'|'overflow')
        when nothing usable comes back, which tester_present() reads as "no ECU
        here".

        NRC 0x78 (responsePending) is handled here, once, for every profile:
        a slow ECU answers 7F <sid> 78 first and the real data up to a second
        later. The session's ~400ms ATST window can close between the two, so
        the bare pending is re-sent behind a ~1s window (ATSTFF) before being
        believed -- exactly the manual recipe that read the XF X260 PCM
        (0x7E0 19 02 FF, on-car 2026-08-20) after the timed-out read failed.
        """
        self._aim(dst)
        cmd = data.hex().upper()
        reply = self._request_once(cmd, data, timeout)
        retries = 0
        while self._pending_only(reply, data) and retries < 3:
            retries += 1
            time.sleep(0.15)
            self._set_st("FF")          # ~1s window for the finished answer
            try:
                reply = self._request_once(cmd, data, timeout)
            finally:
                self._set_st("64")      # back to the session ceiling
        return reply

    # ------------------------------------------------------------------ #
    # Requests longer than one CAN frame
    # ------------------------------------------------------------------ #
    # An ELM327 command line carries at most 7 data bytes: that is one CAN
    # frame's payload, and the chip will not segment a request for you -- it
    # answers a longer line with a bare "?" that looks exactly like a syntax
    # error and nothing like "too long". Mercedes' 03A2 "set battery capacity"
    # is 8 bytes (31 01 03 A2 + a 4-byte parameter), so it hit this on the CLA
    # on 2026-09-09 and came back as "no reply" until it was sent by hand.
    #
    # The STN chips in an OBDLink (MX+, SX, EX) add STPX, which takes the whole
    # payload as one argument and does the ISO-TP segmentation itself. So a
    # long request goes out as STPX when the dongle is an STN, and a plain
    # ELM327 is told plainly that it cannot send this rather than being handed
    # a line it will silently reject.
    _MAX_SINGLE_FRAME = 7

    def _is_stn(self) -> bool:
        """Cached: does this dongle understand STPX? STDI names STN chips."""
        if self._stn is None:
            try:
                reply = self.session.at("STDI") or ""
            except Exception:
                reply = ""
            self._stn = "OBDLink" in reply or "STN" in reply
        return self._stn

    def _line(self, cmd: str) -> bytes:
        """The ELM command line that carries one request payload of hex digits."""
        if len(cmd) <= self._MAX_SINGLE_FRAME * 2:
            return cmd.encode("ascii") + b"\r"
        if not self._is_stn():
            raise ObdError(
                f"This request is {len(cmd) // 2} bytes and an ELM327 command "
                "line carries at most 7. Only an STN-based dongle (OBDLink "
                "MX+/SX/EX) can send it, via STPX.", kind="link")
        target = self._target if self._target is not None else 0x7DF
        return f"STPX h:{target:03X}, d:{cmd}, r:1".encode("ascii") + b"\r"

    def _request_once(self, cmd: str, data: bytes, timeout: float) -> bytes:
        text = self.session._raw(self._line(cmd), timeout)
        records = [bytes(r) for r in reassemble(text, cmd) if r]
        if records:
            # Prefer a record that actually answers this service (positive SID
            # or 0x7F negative); a stray broadcast frame that slipped past the
            # receive filter must not shadow the real reply. Reassembled
            # multi-frame payloads land last, so take the last match.
            expected = data[0] | 0x40
            matching = [r for r in records if r[0] == expected or r[0] == 0x7F]
            if matching:
                return matching[-1]
            return records[-1]
        raise ObdError(f"No usable reply to {cmd}.", kind="no_data")


class Elm327Uds29Client:
    """A UDS client over one already-open Elm327Session, 29-bit FCA addressing.

    Sibling of Elm327UdsClient for the Fiat / Alfa Romeo (FCA / Stellantis) UDS
    era, which addresses ECUs with 29-bit extended CAN at 500k
    (docs/private/fiat-alfa-research.md §2a, §3a): the physically-addressed
    request id is 18DAxxF1 (xx = destination ECU, F1 = tester), the response is
    18DAF1xx, and the functional broadcast is 18DB33F1. Same lifecycle as the
    11-bit client -- begin() once, then request()/the core.uds helpers per module,
    then restore() in a finally -- and reassemble() is shared.

    ``dst`` throughout is the ONE-BYTE ECU address (0x10 engine, 0x18 TCM, ...),
    NOT a full CAN id. The 29-bit era needs the ELM on ATSP7; scan.py owns that
    switch (it also restores the discovered protocol afterwards), so this client
    never touches ATSP.
    """

    def __init__(self, session):
        self.session = session
        self._target = None    # dst the ELM is currently aimed at
        self._priority = True  # ATCP accepted -> 3-byte ATSH; else full 8-byte ATSH

    # ------------------------------------------------------------------ #
    def begin(self):
        """One-off ELM setup for a 29-bit UDS module session (undone by restore()).

        Sets the CAN priority byte (ATCP 18) so ATSH need only carry the low three
        header bytes (DAxxF1). A cheap clone that rejects ATCP answers "?", in
        which case _aim() falls back to the full 18DAxxF1 header some firmwares
        accept instead (research §3a, §7)."""
        self.session.at("ATAT0")   # adaptive timing off -- we manage the timeout
        self.session.at("ATST64")  # ~400ms per-frame ceiling; DA10F1 reads are slow
        reply = self.session.at("ATCP18")
        self._priority = "?" not in reply

    def restore(self):
        """Undo the per-module receive filter, flow control and timing. ATSP is
        the caller's (scan.py owns the protocol switch), so restore only sets the
        29-bit functional header 18DB33F1 -- the 29-bit analogue of the 11-bit
        client's ATSH7DF -- and clears the filters back to defaults."""
        for cmd in ("ATCRA", "ATSH18DB33F1", "ATFCSM0", "ATAT1", "ATST32"):
            try:
                self.session.at(cmd)
            except Exception:
                pass
        self._target = None

    def _aim(self, dst: int):
        """Point the ELM at one ECU by its one-byte address: TX header + RX filter
        + flow control for the 18DAxxF1 / 18DAF1xx pair (research §3a). With ATCP
        accepted the TX header is the 3-byte DAxxF1; otherwise the full 18DAxxF1.
        Flow control 30 00 00 = clear-to-send, no block-size limit, no gap."""
        if self._target == dst:
            return
        tx_low = f"DA{dst:02X}F1"
        tx_full = f"18DA{dst:02X}F1"
        rx_full = f"18DAF1{dst:02X}"
        self.session.at(f"ATSH{tx_low if self._priority else tx_full}")
        self.session.at(f"ATCRA{rx_full}")
        self.session.at(f"ATFCSH{tx_full}")
        self.session.at("ATFCSD300000")
        self.session.at("ATFCSM1")
        self._target = dst

    # ------------------------------------------------------------------ #
    def request(self, dst: int, data: bytes, timeout: float = 3.0) -> bytes:
        """Send one UDS request to ECU ``dst`` (its one-byte address) and return
        the reassembled UDS reply bytes.

        The reply may be a 0x7F negative response -- returned as-is so the caller's
        _check() (backend/core/uds.py) can raise a proper UdsNegativeResponse.
        Raises ObdError(kind='no_data'|'link') when nothing usable comes back,
        which tester_present() reads as "no ECU here".
        """
        self._aim(dst)
        cmd = data.hex().upper()
        text = self.session._raw(cmd.encode("ascii") + b"\r", timeout)
        records = [bytes(r) for r in reassemble(text, cmd) if r]
        if records:
            expected = data[0] | 0x40
            matching = [r for r in records if r[0] == expected or r[0] == 0x7F]
            if matching:
                return matching[-1]
            return records[-1]
        raise ObdError(f"No usable reply to {cmd}.", kind="no_data")
