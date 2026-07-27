"""PSA UDS client over a J2534 pass-thru link (the DiagBox/Lexia cable route).

This is the exact functional twin of PsaElm327UdsClient in psa_uds_elm327.py,
but the bytes ride a J2534Link (psa_j2534 -> j2534_link) instead of an ELM327
session. It exposes the same tiny surface the PSA scan machinery and
backend/core/uds.py rely on -- request(), begin(), restore(), open_session(),
mark_session_open() and a _session_open set -- so peugeot/scan.py drives the
same module walk, DTC/ident ladders and clear over either transport.

The pay-off: a genuine PSA cable is wired to the pins-3/8 diagnostic CAN, so the
body modules (BSI, airbag, cluster, radio) that stay silent over a dongle
answer here -- with no change to the scan logic itself.
"""

from backend.obd.elm327 import ObdError
from backend.obd.psa_session import open_psa_session


class PsaJ2534UdsClient:
    """UDS/KWP client for PSA modules over an open J2534Link.

    ``id_map`` is {request_id: response_id}; a request id not in the map falls
    back to the standard request+8, exactly like the ELM327 client.
    """

    def __init__(self, link, id_map: dict):
        self.link = link
        self._id_map = dict(id_map)
        self._session_open = set()  # request ids with a PSA/KWP session running

    def begin(self):
        self._session_open.clear()

    def restore(self):
        self._session_open.clear()

    def request(self, req_id: int, data: bytes, timeout: float = 3.0) -> bytes:
        """Send one UDS request to module ``req_id`` and return the reply bytes.

        Negative (0x7F) responses are returned as-is so the caller's _check()
        raises a proper UdsNegativeResponse; ObdError(kind='no_data') is raised
        when nothing answers, which tester_present() reads as "no ECU here".
        """
        resp_id = self._id_map.get(req_id, req_id + 8)
        reply = self.link.uds_request(req_id, resp_id, bytes(data), timeout)
        if not reply:
            raise ObdError(f"No usable reply from 0x{req_id:03X}.", kind="no_data")
        return reply

    def open_session(self, req_id: int, trace=None, timeout: float = 1.5) -> bool:
        """Open a PSA diagnostic session via the shared 10 03 -> 10 C0 -> 81
        ladder. Read-safe (session lapses on its own)."""
        return open_psa_session(self, req_id, trace, timeout)

    def mark_session_open(self, req_id: int):
        """Record that a probe already opened this module's session."""
        self._session_open.add(req_id)
