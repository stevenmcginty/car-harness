"""Byte transports the ELM327 driver speaks over.

Every transport exposes the same tiny blocking API:

    open() -> dict          connect; returns info about what it connected to
    exchange(data, timeout) write ``data``, read until the ELM327 '>' prompt
                            (or timeout); returns the raw bytes received
    close()

Transports never interpret the ELM327 conversation -- that is elm327.py's job.
"""

import socket

from backend.obd.ble_link import BleLinkError, BleUartLink


class TransportError(Exception):
    """Honest failure opening or using a transport."""


class SerialTransport:
    """ELM327 on a COM port (USB dongle, or a Classic-Bluetooth dongle that
    Windows exposes as an outgoing COM port)."""

    def __init__(self, port: str, baud: int = 38400):
        self.port = port
        self.baud = baud
        self._ser = None

    def open(self) -> dict:
        import serial

        try:
            self._ser = serial.Serial(port=self.port, baudrate=self.baud, timeout=0.2)
        except Exception as exc:
            raise TransportError(
                f"Could not open {self.port}: {exc}. Check the adapter is plugged "
                "in and that this is the right COM port."
            )
        return {"port": self.port, "baud": self.baud}

    def exchange(self, data: bytes, timeout: float) -> bytes:
        import time

        if self._ser is None:
            raise TransportError("Serial port is not open.")
        try:
            self._ser.reset_input_buffer()
            self._ser.write(data)
        except Exception as exc:
            raise TransportError(f"Write to {self.port} failed: {exc}")
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = self._ser.read(256)
            except Exception as exc:
                raise TransportError(f"Read from {self.port} failed: {exc}")
            if chunk:
                buf.extend(chunk)
                if b">" in chunk:
                    break
        return bytes(buf)

    def close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None


class TcpTransport:
    """ELM327 over WiFi (the dongle runs its own access point; the default for
    almost every WiFi ELM327 clone is 192.168.0.10:35000)."""

    def __init__(self, host: str, port: int = 35000):
        self.host = host
        self.port = port
        self._sock = None

    def open(self) -> dict:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=5.0)
        except Exception as exc:
            raise TransportError(
                f"Could not reach {self.host}:{self.port}: {exc}. Is the laptop "
                "joined to the dongle's WiFi network (usually 'WiFi_OBDII')?"
            )
        return {"host": self.host, "port": self.port}

    def exchange(self, data: bytes, timeout: float) -> bytes:
        import time

        if self._sock is None:
            raise TransportError("TCP connection is not open.")
        try:
            self._sock.sendall(data)
        except Exception as exc:
            raise TransportError(f"Send to {self.host}:{self.port} failed: {exc}")
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            self._sock.settimeout(min(0.3, remaining))
            try:
                chunk = self._sock.recv(512)
            except socket.timeout:
                continue
            except Exception as exc:
                raise TransportError(f"Receive from {self.host}:{self.port} failed: {exc}")
            if not chunk:
                raise TransportError(f"{self.host}:{self.port} closed the connection.")
            buf.extend(chunk)
            if b">" in chunk:
                break
        return bytes(buf)

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None


class BleTransport:
    """ELM327 over Bluetooth LE (Veepeak OBDCheck BLE and clones)."""

    def __init__(self, address: str = "", name_hint: str = ""):
        self._link = BleUartLink(address=address, name_hint=name_hint)
        self.info = {}

    def open(self) -> dict:
        try:
            self.info = self._link.open()
        except BleLinkError as exc:
            raise TransportError(str(exc))
        except Exception as exc:
            raise TransportError(f"BLE connect failed: {exc}")
        return self.info

    def exchange(self, data: bytes, timeout: float) -> bytes:
        try:
            return self._link.exchange(data, timeout)
        except BleLinkError as exc:
            raise TransportError(str(exc))

    def close(self):
        try:
            self._link.close()
        except Exception:
            pass


class RemotePhoneTransport:
    """The dongle link is held by the phone in the car, which relays raw ELM
    bytes over its WebSocket (see remote_link.py). To every consumer above us
    -- session, livefeed, brand profiles -- this is just another dongle."""

    def __init__(self):
        from backend.obd.remote_link import hub

        self._hub = hub

    def open(self) -> dict:
        if not self._hub.connected:
            raise TransportError(
                "No phone relay is connected. Open the Car Harness app in the "
                "car and switch on Relay."
            )
        return self._hub.status()

    def exchange(self, data: bytes, timeout: float) -> bytes:
        from backend.obd.remote_link import RelayError

        try:
            return self._hub.exchange(data, timeout)
        except RelayError as exc:
            raise TransportError(str(exc))

    def close(self):
        pass  # never hang up on the phone; the relay outlives any one session


def transport_for_adapter(adapter: dict):
    """Build the right transport for a saved adapter-instance dict. Raises
    TransportError for adapter types that can't speak generic OBD-II."""
    type_id = (adapter or {}).get("typeId") or ""
    config = (adapter or {}).get("config") or {}

    if type_id == "elm327-remote":
        return RemotePhoneTransport()


    if type_id in ("elm327-serial", "elm327-bluetooth"):
        port = str(config.get("port") or "").strip()
        if not port:
            raise TransportError("This adapter has no COM port configured.")
        try:
            baud = int(config.get("baud") or 38400)
        except (TypeError, ValueError):
            baud = 38400
        return SerialTransport(port, baud)

    if type_id == "elm327-wifi":
        host = str(config.get("host") or "192.168.0.10").strip()
        try:
            port = int(config.get("port") or 35000)
        except (TypeError, ValueError):
            port = 35000
        return TcpTransport(host, port)

    if type_id == "elm327-ble":
        return BleTransport(
            address=str(config.get("address") or "").strip(),
            name_hint=str(config.get("name") or "").strip(),
        )

    raise TransportError(
        f"Adapter type '{type_id or 'none'}' cannot run a generic OBD-II scan. "
        "Use an ELM327-style adapter (USB, Bluetooth, Bluetooth LE or WiFi)."
    )
