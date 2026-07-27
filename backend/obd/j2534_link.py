"""A real SAE J2534 Pass-Thru link for UDS-over-CAN, driven from Python.

This is the "pro cable" cousin of the ELM327 transports in transport.py. Where
an ELM327 speaks a text AT-command dialect, a J2534 device exposes a binary C
API through a vendor DLL. This module loads that DLL with ctypes and issues the
PassThru calls needed to send one UDS request on a CAN id and read the reply.

WHY THIS EXISTS (see docs/private/diagbox-*-research.md for the full story):

  * On a Peugeot / Citroen / DS, the body/BSI diagnostic CAN sits on OBD pins
    3/8. A normal dongle on pins 6/14 only reaches the powertrain bus, so BSI,
    airbag, cluster and radio stay silent. A genuine PSA "DiagBox / Lexia"
    cable is wired to pins 3/8 -- so driving that cable reaches exactly the
    modules a dongle can't. A community J2534 DLL (kolyandex/Lexia_J2534)
    exposes the Lexia clone as a standard J2534 device on ISO15765 CAN.
  * The J2534 ISO15765 protocol handles ISO-TP itself: segmentation, flow
    control and multi-frame reassembly happen inside the DLL. We send one full
    UDS request and read one full reassembled reply -- so every existing UDS
    helper (backend/core/uds.py) and the PSA scan machinery work unchanged, via
    the thin PsaJ2534UdsClient in psa_j2534.py.

HONEST LIMITS: J2534 is Windows-only (a vendor DLL), and this has been proved
against a simulated device in the self-test but NOT yet on real hardware + a
real car. A 32-bit DLL will not load into 64-bit Python (WinError 193) -- match
the bitness of the Python running the backend to the DLL. Nothing here writes
to a car beyond the diagnostic reads/clears the caller asks for.
"""

import ctypes

from backend.obd.transport import TransportError

# --- J2534 v04.04 constants ------------------------------------------------ #
# Protocol IDs
CAN = 5
ISO15765 = 6

# Connect flags (0 = 11-bit ids, standard PSA diagnostic addressing)
CAN_29BIT_ID = 0x00000100

# Filter types
PASS_FILTER = 1
BLOCK_FILTER = 2
FLOW_CONTROL_FILTER = 3

# TxFlags / RxStatus bits
ISO15765_FRAME_PAD = 0x00000040
TX_MSG_TYPE = 0x00000001          # RxStatus bit set on loopback/echo of our tx
ISO15765_FIRST_FRAME = 0x00000002  # RxStatus bit on the "long message coming" tick

# Return codes
STATUS_NOERROR = 0x00
ERR_BUFFER_EMPTY = 0x10
ERR_TIMEOUT = 0x11

BAUD_500K = 500000
_MSG_DATA_LEN = 4128  # PASSTHRU_MSG.Data buffer size (ISO-TP max 4095 + id + pad)


class PASSTHRU_MSG(ctypes.Structure):
    _fields_ = [
        ("ProtocolID", ctypes.c_ulong),
        ("RxStatus", ctypes.c_ulong),
        ("TxFlags", ctypes.c_ulong),
        ("Timestamp", ctypes.c_ulong),
        ("DataSize", ctypes.c_ulong),
        ("ExtraDataIndex", ctypes.c_ulong),
        ("Data", ctypes.c_ubyte * _MSG_DATA_LEN),
    ]


def _msg(protocol: int, tx_flags: int, payload: bytes) -> PASSTHRU_MSG:
    m = PASSTHRU_MSG()
    m.ProtocolID = protocol
    m.TxFlags = tx_flags
    m.DataSize = len(payload)
    for i, b in enumerate(payload):
        m.Data[i] = b
    return m


def _id_bytes(can_id: int) -> bytes:
    """A 4-byte big-endian CAN id, as ISO15765 messages carry it."""
    return bytes([(can_id >> 24) & 0xFF, (can_id >> 16) & 0xFF,
                  (can_id >> 8) & 0xFF, can_id & 0xFF])


class J2534Link:
    """One open J2534 channel on ISO15765 CAN @ 500k.

    Lifecycle mirrors the byte transports: open() -> uds_request()* -> close().
    A fresh flow-control filter is installed per target id pair (the J2534
    analogue of the ELM327's ATSH/ATCRA), because ISO15765 needs one filter per
    request/response pair to know where to send flow-control frames.
    """

    def __init__(self, dll_path: str):
        self.dll_path = str(dll_path or "").strip()
        self._lib = None
        self._device_id = ctypes.c_ulong(0)
        self._channel_id = ctypes.c_ulong(0)
        self._filter_id = None      # active FilterID, so we can stop it before the next
        self._filtered_pair = None  # (tx_id, rx_id) currently filtered
        self.opened = False

    # ------------------------------------------------------------------ #
    def open(self) -> dict:
        if not self.dll_path:
            raise TransportError("No J2534 DLL path configured for this adapter.")
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise TransportError("J2534 is Windows-only (ctypes.WinDLL unavailable).")
        try:
            self._lib = win_dll(self.dll_path)
        except OSError as exc:
            # WinError 193 = a 32-bit DLL loaded into 64-bit Python (or vice versa).
            if getattr(exc, "winerror", None) == 193:
                raise TransportError(
                    f"{self.dll_path} loaded but is the wrong architecture for this "
                    "Python (a 32-bit J2534 DLL needs 32-bit Python). Run the backend "
                    "on a Python that matches the DLL's bitness."
                )
            raise TransportError(f"Could not load J2534 DLL {self.dll_path}: {exc}")

        self._pt_open()
        self._pt_connect()
        self.opened = True
        return {"dll": self.dll_path, "protocol": "ISO15765 CAN 11/500"}

    def close(self):
        if self._lib is None:
            return
        try:
            if self._channel_id.value:
                self._call("PassThruDisconnect", self._channel_id)
        except Exception:
            pass
        try:
            if self._device_id.value:
                self._call("PassThruClose", self._device_id)
        except Exception:
            pass
        self._channel_id = ctypes.c_ulong(0)
        self._device_id = ctypes.c_ulong(0)
        self._filter_id = None
        self._filtered_pair = None
        self.opened = False

    # ------------------------------------------------------------------ #
    def uds_request(self, tx_id: int, rx_id: int, payload: bytes,
                    timeout: float = 3.0) -> bytes:
        """Send ``payload`` as a UDS request on ``tx_id`` and return the UDS
        reply from ``rx_id`` (ISO-TP reassembled by the DLL), stripped of the
        4-byte CAN id. Raises ObdError-compatible behaviour via TransportError
        on a dead link; returns negative (0x7F) responses as-is."""
        self._ensure_filter(tx_id, rx_id)
        self._pt_write(tx_id, bytes(payload), timeout)

        import time
        deadline = time.monotonic() + timeout
        pending = None
        while time.monotonic() < deadline:
            reply = self._pt_read(rx_id, max(0.05, deadline - time.monotonic()))
            if reply is None:
                continue
            if reply and reply[0] == 0x7F and len(reply) >= 3 and reply[2] == 0x78:
                # "response pending" -- the ECU is thinking; keep listening
                # (the DLL, unlike an ELM327, does not need us to re-send).
                pending = reply
                continue
            return reply
        if pending is not None:
            return pending
        from backend.obd.elm327 import ObdError
        raise ObdError(f"No reply from 0x{rx_id:03X}.", kind="no_data")

    # ------------------------------------------------------------------ #
    # Low-level PassThru primitives. Kept as small overridable methods so the
    # self-test can subclass this class with a simulated device (no real DLL).
    # ------------------------------------------------------------------ #
    def _call(self, name: str, *args):
        fn = getattr(self._lib, name)
        rc = fn(*args)
        if rc != STATUS_NOERROR:
            raise TransportError(f"{name} failed: J2534 status 0x{rc & 0xFFFFFFFF:X} "
                                 f"({self._last_error()}).")
        return rc

    def _last_error(self) -> str:
        try:
            buf = ctypes.create_string_buffer(80)
            self._lib.PassThruGetLastError(buf)
            return buf.value.decode("ascii", "replace").strip() or "no detail"
        except Exception:
            return "no detail"

    def _pt_open(self):
        self._call("PassThruOpen", None, ctypes.byref(self._device_id))

    def _pt_connect(self):
        self._call("PassThruConnect", self._device_id, ctypes.c_ulong(ISO15765),
                   ctypes.c_ulong(0), ctypes.c_ulong(BAUD_500K),
                   ctypes.byref(self._channel_id))

    def _ensure_filter(self, tx_id: int, rx_id: int):
        if self._filtered_pair == (tx_id, rx_id):
            return
        if self._filter_id is not None:
            try:
                self._call("PassThruStopMsgFilter", self._channel_id, self._filter_id)
            except Exception:
                pass
            self._filter_id = None
        self._pt_set_filter(tx_id, rx_id)
        self._filtered_pair = (tx_id, rx_id)

    def _pt_set_filter(self, tx_id: int, rx_id: int):
        """ISO15765 flow-control filter: mask FF..FF, pattern = the response id
        we listen for, flow-control = the id we send on."""
        mask = _msg(ISO15765, ISO15765_FRAME_PAD, b"\xFF\xFF\xFF\xFF")
        pattern = _msg(ISO15765, ISO15765_FRAME_PAD, _id_bytes(rx_id))
        flow = _msg(ISO15765, ISO15765_FRAME_PAD, _id_bytes(tx_id))
        filter_id = ctypes.c_ulong(0)
        self._call("PassThruStartMsgFilter", self._channel_id,
                   ctypes.c_ulong(FLOW_CONTROL_FILTER),
                   ctypes.byref(mask), ctypes.byref(pattern), ctypes.byref(flow),
                   ctypes.byref(filter_id))
        self._filter_id = filter_id

    def _pt_write(self, tx_id: int, payload: bytes, timeout: float):
        msg = _msg(ISO15765, ISO15765_FRAME_PAD, _id_bytes(tx_id) + payload)
        num = ctypes.c_ulong(1)
        self._call("PassThruWriteMsgs", self._channel_id, ctypes.byref(msg),
                   ctypes.byref(num), ctypes.c_ulong(int(timeout * 1000)))

    def _pt_read(self, rx_id: int, timeout: float):
        """Read one message; return its UDS payload (id stripped) or None on a
        timeout / an echo / a first-frame indication."""
        msg = PASSTHRU_MSG()
        num = ctypes.c_ulong(1)
        fn = self._lib.PassThruReadMsgs
        rc = fn(self._channel_id, ctypes.byref(msg), ctypes.byref(num),
                ctypes.c_ulong(int(timeout * 1000)))
        if rc in (ERR_BUFFER_EMPTY, ERR_TIMEOUT) or num.value == 0:
            return None
        if rc != STATUS_NOERROR:
            raise TransportError(f"PassThruReadMsgs failed: status 0x{rc & 0xFFFFFFFF:X} "
                                 f"({self._last_error()}).")
        if msg.RxStatus & TX_MSG_TYPE:          # our own transmit echoed back
            return None
        if msg.RxStatus & ISO15765_FIRST_FRAME:  # "long reply coming" indication
            return None
        size = msg.DataSize
        if size <= 4:
            return None
        return bytes(msg.Data[4:size])          # strip the 4-byte CAN id
