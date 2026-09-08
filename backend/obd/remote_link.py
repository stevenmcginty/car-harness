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
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

# The phone's WebSocket lands in ONE process: the server on port 8765. Every
# other process that imports this module (the MCP server Claude talks to, a
# one-off tool script) has its own empty hub, and used to answer "no phone is
# connected" while the phone was plainly up. So an empty hub now forwards
# each exchange to that server over plain local HTTP (/api/remote/exchange).
# The server itself sets bridge_url = None so it never calls itself.
DEFAULT_BRIDGE_URL = os.environ.get("CAR_HARNESS_RELAY_BRIDGE", "http://127.0.0.1:8765")


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
        # Where to forward when no phone is attached here. None = never
        # forward (the process that IS the server, or a bench test).
        self.bridge_url: str | None = DEFAULT_BRIDGE_URL

    # ---------------------------------------------------------------- state
    @property
    def local(self) -> bool:
        """A phone is attached to THIS process."""
        with self._lock:
            return self._ws is not None

    @property
    def connected(self) -> bool:
        if self.local:
            return True
        st = self._bridge_status()
        return bool(st and st.get("connected"))

    def status(self) -> dict:
        with self._lock:
            if self._ws is not None:
                return {
                    "connected": True,
                    "deviceName": self.device_name,
                    "connectedAt": self.connected_at,
                }
        st = self._bridge_status()
        if st is not None:
            return {
                "connected": bool(st.get("connected")),
                "deviceName": st.get("deviceName", ""),
                "connectedAt": st.get("connectedAt", ""),
                "via": self.bridge_url,
            }
        return {"connected": False, "deviceName": "", "connectedAt": ""}

    # ------------------------------------------------- other-process bridge
    def _http(self, method: str, path: str, body: dict | None, timeout: float):
        """One local HTTP call to the relay server. Returns (status, json).
        Raises OSError/URLError when the server is not there at all."""
        url = f"{self.bridge_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            return exc.code, payload

    def _bridge_status(self) -> dict | None:
        if not self.bridge_url:
            return None
        try:
            code, payload = self._http("GET", "/api/remote/status", None, 3.0)
        except Exception:
            return None
        return payload if code == 200 and isinstance(payload, dict) else None

    def _bridge_exchange(self, data: bytes, timeout: float) -> bytes:
        body = {"b64": base64.b64encode(data).decode("ascii"), "timeout": timeout}
        try:
            code, payload = self._http(
                "POST", "/api/remote/exchange", body, timeout + 15.0
            )
        except Exception:
            raise RelayError(
                "No phone relay is connected. Start Relay mode in the phone app "
                f"(and check the desktop server at {self.bridge_url} is running)."
            )
        if code != 200:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise RelayError(
                detail or f"The relay server refused the exchange (HTTP {code})."
            )
        try:
            return base64.b64decode(payload.get("b64") or "")
        except Exception:
            raise RelayError("The relay server sent back bytes it could not encode.")

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
            if not self.local and self.bridge_url:
                return self._bridge_exchange(data, timeout)
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
        if not got_prompt and not self.local:
            raise RelayError(
                "The relay phone dropped before the dongle answered."
            )
        with self._lock:
            return bytes(self._buf)


hub = RemotePhoneHub()
