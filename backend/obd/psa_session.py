"""The PSA diagnostic-session ladder, shared by every PSA UDS client.

PSA / Stellantis ECUs disagree about how to open a read session. DiagBox-era
modules want KWP's 0x10 0xC0, plain UDS ones take 0x10 0x03, and a few only
answer the bare KWP StartCommunication 0x81. The read side is safe: a session
lapses on its own after a few seconds of silence and writes nothing to the car.

Both PsaElm327UdsClient (dongle) and PsaJ2534UdsClient (pass-thru cable) drive
this identical ladder, so it lives here once. A client only has to expose
``request(req_id, data, timeout)`` and a ``_session_open`` set.
"""

from backend.obd.elm327 import ObdError
from backend.obd.transport import TransportError

# Tried in order; the first positive reply wins and marks the session open.
SESSION_LADDER = (
    (bytes([0x10, 0x03]), 0x50, "session 10 03"),      # UDS extended session
    (bytes([0x10, 0xC0]), 0x50, "PSA session 10 C0"),  # the DiagBox-era session
    (bytes([0x81]), 0xC1, "KWP start 81"),             # bare KWP StartCommunication
)


def open_psa_session(client, req_id: int, trace=None, timeout: float = 1.5) -> bool:
    """Open a diagnostic session PSA-style. Read-safe. Returns True on success.

    ``client`` is any object with ``request(req_id, data, timeout)`` and a
    ``_session_open`` set -- the ELM327 and J2534 PSA clients both qualify.
    """
    if req_id in client._session_open:
        return True
    for data, positive, label in SESSION_LADDER:
        try:
            reply = client.request(req_id, data, timeout=timeout)
        except (ObdError, TransportError):
            if trace is not None:
                trace.append(f"{label} -> no answer")
            continue
        if reply and reply[0] == positive:
            if trace is not None:
                trace.append(f"{label} -> OK")
            client._session_open.add(req_id)
            return True
        if trace is not None and reply and reply[0] == 0x7F and len(reply) >= 3:
            trace.append(f"{label} -> NRC {reply[2]:02X}")
    return False
