"""UDS over a generic ELM327 with BMW's extended 11-bit addressing.

BMW F-series (and UKL MINIs) don't use the standard 0x7E0->0x7E8 pairing for
module diagnostics: the tester transmits on CAN id 6F1 with the target module's
address as the first data byte, and each module replies on 0x600 + its address
with 0xF1 (the tester) as the first byte. This is the proven recipe from the
Android app (android/.../obd/BmwUds.kt), ported so the desktop can talk to
F-series modules over an ordinary Bluetooth dongle -- e.g. for CBS service
resets -- instead of needing the ENET cable.

Exposes the same ``request(dst, data, timeout)`` interface backend/core/uds.py
expects, so all the shared UDS helpers work unchanged. ``dst`` here is the
module's diagnostic ADDRESS (KOMBI = 0x60), not a CAN id.
"""

from backend.obd.elm327 import ObdError
from backend.obd.uds_elm327 import Elm327UdsClient, reassemble


class Elm327BmwUdsClient(Elm327UdsClient):
    """A UDS client over one already-open Elm327Session, 6F1 gateway addressing.

    Lifecycle matches Elm327UdsClient: begin() once, request() per call,
    restore() in a finally.
    """

    def begin(self):
        self.session.at("ATFCSH6F1")
        self.session.at("ATFCSM1")
        self.session.at("ATAT0")   # adaptive timing off -- we manage the timeout
        self.session.at("ATST64")  # ~410ms silence window, plenty for a live ECU

    def restore(self):
        for cmd in ("ATCEA", "ATCRA", "ATSH7DF", "ATFCSM0", "ATAT1", "ATST32"):
            try:
                self.session.at(cmd)
            except Exception:
                pass
        self._target = None

    def _aim(self, dst: int):
        """Point the ELM at one module address. The flow control uses a finite
        block size (8 frames, 2ms gap) so a big reply pauses for our ACK every
        8 frames instead of flooding the dongle's tiny buffer."""
        if self._target == dst:
            return
        self.session.at("ATFCSH6F1")  # re-assert in case something moved it
        self.session.at("ATSH6F1")
        self.session.at(f"ATCEA{dst:02X}")
        self.session.at(f"ATCRA6{dst:02X}")
        self.session.at(f"ATFCSD{dst:02X}300802")
        self._target = dst

    def request(self, dst: int, data: bytes, timeout: float = 3.0) -> bytes:
        self._aim(dst)
        cmd = data.hex().upper()
        text = self.session._raw(cmd.encode("ascii") + b"\r", timeout)
        for record in reassemble(text, cmd):
            # Some clones leave the 0xF1 tester byte in front -- strip it.
            # A UDS first byte is never legitimately 0xF1.
            if record and record[0] == 0xF1:
                record = record[1:]
            if record:
                return bytes(record)
        raise ObdError(f"No usable reply to {cmd}.", kind="no_data")
