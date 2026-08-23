"""The phone-relay bridge: one WebSocket from the car's phone to this server.

The setup this exists for: the laptop sits at home, the car is elsewhere. A
phone in the car holds the Bluetooth LE link to the dongle (the same GATT UART
the desktop would otherwise speak itself) and opens a WebSocket back here.
This hub is where that socket meets the rest of the harness.

The two halves live in different worlds:

* the WebSocket handler runs on the asyncio event loop (async);
* every OBD transport is blocking, called from FastAPI's threadpool.

So the hub is deliberately tiny and thread-safe. The phone side speaks JSON:
{"type":"data","b64":"..."} both ways -- base64 ELM327 bytes, nothing more.
Interpretation of those bytes stays where it always was: elm327.py. The phone
is a dumb radio pipe; it decodes nothing, decides nothing.
"""

import asyncio
import base64
import json
import threading
from datetime import datetime, timezone


class RelayError(Exception):
    """Honest failure moving bytes through the relay."""


class RemotePhoneHub:
    """Holds the one connected relay phone and moves bytes across worlds."""

    def __init__(self):
        self._lock = threading.Lock()
        # One command at a time on the one socket. The ELM327 has no request
        # ids: two worker threads interleaving here would each read back the
        # other's answer. Held for the whole send-and-wait, never with _lock.
        self._turn = threading.Lock()
        self._ws = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.device_name = ""
        self.connected_at = ""
        self._buf = bytearray()
        self._prompt = threading.Event()

    # ---------------------------------------------------------------- state
    @property
    def connected(self) -> bool:
        with self._lock:
            return self._ws is not None

    def status(self) -> dict:
        with self._lock:
            return {
                "connected": self._ws is not None,
                "deviceName": self.device_name,
                "connectedAt": self.connected_at,
            }

    # ------------------------------------------------------- phone lifecycle
    def attach(self, ws, device_name: str, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            if self._ws is not None:
                raise RelayError(
                    "A phone relay is already connected "
                    f"({self.device_name or 'unknown device'})."
                )
            self._ws = ws
            self._loop = loop
            self.device_name = device_name.strip() or "phone"
            self.connected_at = datetime.now(timezone.utc).isoformat()

    def detach(self, ws) -> None:
        with self._lock:
            if self._ws is ws:
                self._ws = None
                self._loop = None

    # ------------------------------------------------------- phone -> server
    def on_text(self, text: str) -> None:
        """One message from the phone. Event-loop side; never blocks."""
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if msg.get("type") != "data":
            return  # hello/ping/pong are handled by the endpoint itself
        try:
            raw = base64.b64decode(msg.get("b64") or "")
        except Exception:
            return
        # Both under the one lock: exchange() clears the buffer and the event
        # together, so setting the event outside it could raise a flag over a
        # buffer that was cleared a moment later.
        with self._lock:
            self._buf.extend(raw)
            if b">" in raw:
                self._prompt.set()

    # ------------------------------------------------------- server -> phone
    def exchange(self, data: bytes, timeout: float) -> bytes:
        """Blocking send-and-wait-for-prompt. Worker-thread side; this is the
        same contract SerialTransport/TcpTransport/BleTransport meet."""
        with self._turn:
            return self._exchange(data, timeout)

    def _exchange(self, data: bytes, timeout: float) -> bytes:
        with self._lock:
            ws = self._ws
            loop = self._loop
            if ws is not None and loop is not None:
                # Clear BEFORE the send: the phone's answer can land the
                # instant the frame goes out, and a reset afterwards would
                # wipe it. Same order BleTransport.exchange() uses.
                self._buf.clear()
                self._prompt.clear()
        if ws is None or loop is None:
            raise RelayError(
                "No phone relay is connected. Start Relay mode in the phone app."
            )
        payload = json.dumps(
            {"type": "data", "b64": base64.b64encode(data).decode("ascii")}
        )
        try:
            future = asyncio.run_coroutine_threadsafe(ws.send_text(payload), loop)
            future.result(timeout=10.0)
        except RelayError:
            raise
        except Exception as exc:
            raise RelayError(f"Sending to the relay phone failed: {exc}")

        got_prompt = self._prompt.wait(timeout)
        if not got_prompt and not self.connected:
            raise RelayError(
                "The relay phone dropped before the dongle answered."
            )
        with self._lock:
            return bytes(self._buf)


hub = RemotePhoneHub()
