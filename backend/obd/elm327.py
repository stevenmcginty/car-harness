"""ELM327 driver: init sequence, AT commands, OBD queries and reply parsing.

Works over any transport from transport.py. The parser handles single-line
replies, one-line-per-ECU replies, and the ELM327's multi-frame (ISO-TP)
"NN:" continuation format used for e.g. the VIN.
"""

import re

from backend.obd.transport import TransportError

# Reply tokens that mean "the command reached the dongle but produced no data".
_NO_DATA_TOKENS = ("NO DATA", "NODATA")
# Reply tokens that mean the dongle couldn't talk to the car at all.
_LINK_ERROR_TOKENS = (
    "UNABLE TO CONNECT",
    "BUS INIT: ...ERROR",
    "BUS INIT:ERROR",
    "CAN ERROR",
    "BUS ERROR",
    "FB ERROR",
    "DATA ERROR",
    "STOPPED",
    # The dongle's receive buffer overflowed mid-reply -- the tail of the
    # response was LOST, so whatever parsed before it is a silent truncation.
    # A huge fault-memory read (19 02 FF on a module with dozens of codes,
    # XF X260 ABS 0x7E6 on-car 2026-08-20) is the known trigger. Error, never
    # return partial data as if it were whole.
    "BUFFER FULL",
)

_HEX_LINE = re.compile(r"^[0-9A-F]+$")
_ISO_TP_LEN = re.compile(r"^[0-9A-F]{3}$")
_ISO_TP_CONT = re.compile(r"^([0-9A-F]{1,2}):([0-9A-F]+)$")

# STN-based dongles (OBDLink MX+/SX/LX) never say "ELM327" -- they answer ATZ
# with "OBDLink MX+ v5.x" or "STN...". Same chip family, same command set.
_DONGLE_BANNERS = ("ELM327", "OBDLINK", "STN")


def is_elm_banner(reply: str) -> bool:
    """True if an ATZ reply looks like a supported ELM327-family dongle."""
    upper = reply.upper()
    return any(token in upper for token in _DONGLE_BANNERS)



class ObdError(Exception):
    """An honest OBD-level failure. ``kind`` is 'no_data', 'link', or 'protocol'."""

    def __init__(self, message: str, kind: str = "protocol"):
        super().__init__(message)
        self.kind = kind


class Elm327Session:
    """One live conversation with an ELM327 dongle."""

    def __init__(self, transport):
        self.transport = transport
        self.firmware = ""
        self.protocol = ""       # human text from ATDP, e.g. "ISO 15765-4 (CAN 11/500)"
        self.protocol_num = None  # from ATDPN; >= 6 means CAN
        self.transport_info = {}
        self.opened = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def open(self):
        """Connect the transport and run the ELM init sequence.

        If the transport connects but the init fails (no ELM327 banner, link
        error mid-init), the transport is closed before raising -- otherwise a
        serial port / BLE GATT link would leak on every half-failed connect.
        """
        import time

        self.transport_info = self.transport.open()
        try:
            # ATZ with retries: 'STOPPED' means we interrupted a running command
            # (e.g. the wake-CR made it repeat its last one) and an empty reply
            # means it was mid-reset -- both settle after a moment.
            reply = ""
            for _attempt in range(3):
                reply = self._raw(b"ATZ\r", timeout=7.0)
                if is_elm_banner(reply):
                    break
                time.sleep(1.0)
            if not is_elm_banner(reply):
                got = reply.strip() or "nothing"
                raise ObdError(
                    f"The dongle answered the reset with {got!r} instead of an "
                    "ELM327-family banner.",
                    kind="link",
                )
            m = re.search(r"ELM327\s+(v[\w.]+)", reply, re.IGNORECASE)
            if m:
                self.firmware = m.group(1)
            else:
                # OBDLink/STN banners: "OBDLink MX+ v5.7" -> "v5.7".
                m = re.search(r"v[\w.]+", reply, re.IGNORECASE)
                self.firmware = m.group(0) if m else reply.strip().splitlines()[0].strip() or "ELM327"

            # echo off, linefeeds off, spaces off, headers off, auto protocol.
            for cmd in (b"ATE0\r", b"ATL0\r", b"ATS0\r", b"ATH0\r", b"ATSP0\r"):
                self._raw(cmd, timeout=3.0)
            self.opened = True
        except Exception:
            self.close()
            raise

    def close(self):
        self.opened = False
        try:
            self.transport.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # low level
    # ------------------------------------------------------------------ #
    def _raw(self, data: bytes, timeout: float) -> str:
        raw = self.transport.exchange(data, timeout)
        return raw.decode("ascii", errors="replace")

    def at(self, cmd: str, timeout: float = 3.0) -> str:
        """Send an AT command; returns the cleaned single-line-ish reply."""
        reply = self._raw(cmd.encode("ascii") + b"\r", timeout)
        text = reply.replace(">", " ").replace("\r", " ").replace("\n", " ")
        # the dongle may echo the command even after ATE0 -- strip it.
        text = text.replace(cmd, " ")
        return " ".join(text.split()).strip()

    def voltage(self) -> str:
        """Battery voltage as the dongle measures it, e.g. '12.3V'."""
        v = self.at("ATRV")
        return v if re.match(r"^\d+(\.\d+)?V$", v) else v

    # ------------------------------------------------------------------ #
    # protocol detection
    # ------------------------------------------------------------------ #
    def ensure_protocol(self):
        """Force protocol auto-detection with a 0100 query (the first real OBD
        request makes the ELM search). Raises ObdError('link') if the car does
        not answer -- usually the ignition is off."""
        if self.protocol_num is not None:
            return
        try:
            self.query(0x01, 0x00, timeout=15.0)
        except ObdError as exc:
            if exc.kind == "link" or exc.kind == "no_data":
                raise ObdError(
                    "The dongle is answering but the car is not. Turn the "
                    "ignition ON (engine can stay off), then try again.",
                    kind="link",
                )
            raise
        dpn = self.at("ATDPN")  # e.g. 'A6' (auto-detected protocol 6)
        m = re.search(r"([0-9A-F])\s*$", dpn)
        if m:
            try:
                self.protocol_num = int(m.group(1), 16)
            except ValueError:
                self.protocol_num = None
        self.protocol = self.at("ATDP")

    @property
    def is_can(self) -> bool:
        # ELM protocol numbers 6..F are all CAN flavours; None = unknown,
        # assume CAN (every car sold since ~2008 is CAN).
        return self.protocol_num is None or self.protocol_num >= 6

    # ------------------------------------------------------------------ #
    # OBD queries
    # ------------------------------------------------------------------ #
    def query(self, service: int, pid: int = None, timeout: float = 8.0):
        """Send an OBD request and return a list of per-ECU payloads, each a
        ``bytes`` starting with the response service byte (service|0x40)."""
        if pid is None:
            cmd = f"{service:02X}"
        else:
            cmd = f"{service:02X}{pid:02X}"
        reply = self._raw(cmd.encode("ascii") + b"\r", timeout)
        return self._parse(reply, expected=service | 0x40, echo=cmd)

    def _parse(self, text: str, expected: int, echo: str):
        upper = text.upper()
        for token in _LINK_ERROR_TOKENS:
            if token in upper:
                raise ObdError(
                    f"The dongle reported '{token}' -- it could not get an "
                    "answer from the car.",
                    kind="link",
                )
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

        matching = [r for r in records if r and r[0] == expected]
        if not matching:
            got = text.replace("\r", " ").replace("\n", " ").strip()
            raise ObdError(
                f"Unexpected reply to {echo}: {got!r}.",
                kind="protocol",
            )
        return matching
