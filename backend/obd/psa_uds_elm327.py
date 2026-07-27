"""UDS *and* PSA-flavoured KWP2000 over a generic ELM327 for PSA / Stellantis
cars (Peugeot, Citroen, DS and the newer Opel/Vauxhall on the same platform).

This is the "explicit id pair" cousin of the Mazda path in uds_elm327.py. Where a
standard OBD-II car answers a request on 0x7E0..0x7E7 with response = request+8,
PSA gives every module its OWN request/response CAN id pair that does NOT follow
that rule -- the BSI is 0x752 -> 0x652, the engine 0x6A8 -> 0x688, the airbag
0x744 -> 0x644, and so on. So this client is told each module's response id
explicitly (via a {tx: rx} map) instead of computing it.

WHAT THE FIRST FIELD TEST TAUGHT US (real car, VeePeak dongle):

* A standard dongle sits on OBD pins 6/14 -- on PSA that is the ENGINE bus, so
  only the powertrain modules (engine, gearbox, ABS, steering, SCR) can answer.
  The BSI, airbag, cluster etc. live on the diagnostic CAN at OBD pins 3/8 and
  need a pin-swap adapter; no software reaches them through a plain dongle.
* The modules that DO answer often speak PSA's KWP dialect, not pure UDS:
  session open is 0x10 0xC0 (not 0x10 0x03), fault read is 0x17 0xFF 0x00 (not
  0x19 0x02), ident is 0x21 <zone> (not 0x22 <did>). Hence the ladders below.

Everything else -- the ISO-TP reassembly, per-module flow control, negative
(0x7F) response handling -- is the shared Elm327UdsClient.
"""

from backend.obd.elm327 import ObdError
from backend.obd.uds_elm327 import Elm327UdsClient


class PsaElm327UdsClient(Elm327UdsClient):
    """Elm327UdsClient that resolves each module's RX id from an explicit map.

    ``id_map`` is {request_id: response_id}. A request id that is not in the map
    falls back to the standard request+8, so the client degrades gracefully to
    ordinary OBD-II behaviour if ever handed a plain 0x7E0-style address.
    """

    def __init__(self, session, id_map: dict):
        super().__init__(session)
        self._id_map = dict(id_map)
        self._session_open = set()  # request ids with a PSA/KWP session running

    def begin(self):
        super().begin()
        self._session_open.clear()

    def restore(self):
        super().restore()
        self._session_open.clear()

    def _aim(self, req_id: int):
        """Point the ELM at one PSA module: TX header + explicit RX filter + FC.

        Overrides the parent's request+8 rule with the PSA-specific response id.
        Flow control 30 00 00 = clear-to-send, no block-size limit, no separation
        time, sent on the request id (ATFCSH) as the module expects.
        """
        if self._target == req_id:
            return
        resp_id = self._id_map.get(req_id, req_id + 8)
        self.session.at(f"ATSH{req_id:03X}")
        self.session.at(f"ATFCSH{req_id:03X}")
        self.session.at(f"ATCRA{resp_id:03X}")
        self.session.at("ATFCSD300000")
        self.session.at("ATFCSM1")
        self._target = req_id

    def open_session(self, req_id: int, trace=None, timeout: float = 1.5) -> bool:
        """Open a diagnostic session, PSA style, via the shared session ladder
        (10 03 -> 10 C0 -> 81). Read-safe: session state lapses on its own after
        a few seconds of silence and writes nothing to the car."""
        from backend.obd.psa_session import open_psa_session

        return open_psa_session(self, req_id, trace, timeout)

    def mark_session_open(self, req_id: int):
        """Record that a probe already opened this module's session."""
        self._session_open.add(req_id)
