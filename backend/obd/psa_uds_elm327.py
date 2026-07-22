"""UDS over a generic ELM327 for PSA / Stellantis cars (Peugeot, Citroen, DS and
the newer Opel/Vauxhall built on the same platform).

This is the "explicit id pair" cousin of the Mazda path in uds_elm327.py. Where a
standard OBD-II car answers a request on 0x7E0..0x7E7 with response = request+8,
PSA gives every module its OWN request/response CAN id pair that does NOT follow
that rule -- the BSI is 0x752 -> 0x652, the engine 0x6A8 -> 0x688, the airbag
0x744 -> 0x644, and so on. So this client is told each module's response id
explicitly (via a {tx: rx} map) instead of computing it.

Everything else -- the ISO-TP reassembly, per-module flow control, negative
(0x7F) response handling -- is the shared Elm327UdsClient. This subclass changes
only how a module's receive filter is chosen, and adds an optional, read-safe
extended diagnostic session (0x10 0x03) that some PSA modules want before they
will report fault memory.

Scope: 11-bit ISO 15765-4 CAN, i.e. AEE2010-era cars (roughly 2010+). Pre-2004
PSA cars use the VAN bus (unreachable by any ELM327); 2004-2010 cars often answer
KWP2000 (service 0x18) rather than UDS. Both are out of scope here -- the same
way the Mazda path is HS-CAN only.
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

    def start_extended_session(self, req_id: int, timeout: float = 1.0) -> bool:
        """Best-effort DiagnosticSessionControl -> extended (0x10 0x03).

        Read-safe: it changes only the ECU's session state (which the module
        drops on its own after a few seconds of silence) and writes nothing to
        the car. Some PSA modules -- notably the airbag and BSI -- refuse
        ReadDTC in the default session, so callers may escalate to this only when
        a plain read comes back negative. Returns True on a positive 0x50 reply.
        """
        try:
            reply = self.request(req_id, bytes([0x10, 0x03]), timeout=timeout)
        except ObdError:
            return False
        return bool(reply) and reply[0] == 0x50
