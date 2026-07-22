"""Persistent BLE UART link to an ELM327-style dongle (e.g. Veepeak OBDCheck BLE).

Verified live against a real VEEPEAK: the UART pipe is a vendor GATT service
(FFF0 on the Veepeak -- notify on FFF1, write on FFF2). The critical rule is
that the write and notify characteristics must come from the SAME vendor
service; picking the first notify characteristic on the whole device grabs the
standard GATT "Service Changed" channel and the ELM's replies are never seen.

bleak is asyncio-only, so this class runs a private event loop on a worker
thread and exposes a small blocking API (open / exchange / close) that the
synchronous OBD engine can call from FastAPI's threadpool.
"""

import asyncio
import threading

# Standard Bluetooth SIG services that are never the UART pipe.
_SKIP_SERVICES = {"1800", "1801", "180a", "180f"}
# Known UART-style vendor services, best first. FFF0 = Veepeak and most clones,
# 49535343-... = Microchip transparent UART, FFE0 = HM-10 style, 6E40... = Nordic NUS.
_PREFERRED = ["fff0", "49535343-fe7d-4ae5-8fa9-9fafd205e455", "ffe0", "6e400001", "ffb0", "18f0"]

_KNOWN_NAME_HINTS = ("OBD", "VEEPEAK", "VLINK", "IOS-VLINK", "ELM", "VGATE", "OBDII")

_SCAN_SECONDS = 10.0
_CONNECT_TIMEOUT = 15.0


def _short(uuid) -> str:
    u = str(uuid).lower()
    if u.endswith("-0000-1000-8000-00805f9b34fb"):
        return u[4:8]
    return u


class BleLinkError(Exception):
    """Honest failure opening or using the BLE link."""


class BleUartLink:
    """A blocking facade over a persistent bleak GATT connection."""

    def __init__(self, address: str = "", name_hint: str = ""):
        self.address = (address or "").strip()
        self.name_hint = (name_hint or "").strip()
        self.info: dict = {}
        self._loop = None
        self._thread = None
        self._client = None
        self._write_char = None
        self._notify_char = None
        self._use_response = False
        self._buf = bytearray()
        self._prompt_evt = None  # asyncio.Event, created on the loop
        self._xchg_lock = None   # asyncio.Lock, created on the loop
        self._open = False

    # ------------------------------------------------------------------ #
    # thread / loop plumbing
    # ------------------------------------------------------------------ #
    def _ensure_loop(self):
        if self._loop is not None:
            return
        loop = asyncio.new_event_loop()

        def runner():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=runner, daemon=True, name="ble-uart-link")
        t.start()
        self._loop = loop
        self._thread = t

    def _run(self, coro, timeout: float):
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except asyncio.TimeoutError:
            fut.cancel()
            raise BleLinkError("BLE operation timed out.")
        except TimeoutError:
            fut.cancel()
            raise BleLinkError("BLE operation timed out.")

    # ------------------------------------------------------------------ #
    # public blocking API
    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return bool(self._open and self._client is not None and self._client.is_connected)

    def open(self) -> dict:
        """Scan, connect and wire up the UART characteristics. Returns info
        about what it connected to. Raises BleLinkError with an honest message
        on any failure."""
        info = self._run(self._open_async(), _SCAN_SECONDS + _CONNECT_TIMEOUT + 20.0)
        self.info = info
        self._open = True
        return info

    def exchange(self, data: bytes, timeout: float) -> bytes:
        """Write ``data`` and collect notifications until the ELM327 '>' prompt
        (or ``timeout``). Returns everything received (prompt included)."""
        if not self.connected:
            raise BleLinkError("BLE link is not open.")
        return self._run(self._exchange_async(data, timeout), timeout + 5.0)

    def close(self):
        if self._loop is None:
            return
        try:
            self._run(self._close_async(), 10.0)
        except Exception:
            pass
        self._open = False
        loop = self._loop
        self._loop = None
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # coroutines (run on the private loop)
    # ------------------------------------------------------------------ #
    async def _open_async(self) -> dict:
        from bleak import BleakClient, BleakScanner

        self._prompt_evt = asyncio.Event()
        self._xchg_lock = asyncio.Lock()

        # 1) Find the dongle. These dongles advertise on a slow interval (and
        #    nap), so one missed scan window is common -- always try twice.
        target = None
        hint = self.name_hint.upper()

        def matches(dev):
            if self.address:
                return (dev.address or "").upper() == self.address.upper()
            dev_name = (dev.name or "").upper()
            if hint:
                return hint in dev_name
            return any(h in dev_name for h in _KNOWN_NAME_HINTS)

        seen = "nothing"
        for _attempt in range(2):
            devices = await BleakScanner.discover(timeout=_SCAN_SECONDS)
            target = next((d for d in devices if matches(d)), None)
            if target is not None:
                break
            seen = ", ".join(
                sorted({f"{d.name or '?'} [{d.address}]" for d in devices})
            ) or "nothing"
        if target is None:
            what = f"BLE device {self.address}" if self.address else "matching BLE OBD dongle"
            raise BleLinkError(
                f"No {what} found after two scans. Is it plugged into the car's "
                "OBD port and in Bluetooth range? Unplugging and reseating the "
                "dongle wakes it up. (Another phone/app connected to it also "
                f"hides it.) Devices seen: {seen}."
            )

        # 2) Connect and pick the UART pipe: write+notify from the SAME
        #    vendor service, preferring known UART services.
        def on_disconnect(_client):
            self._open = False

        client = BleakClient(
            target, timeout=_CONNECT_TIMEOUT, disconnected_callback=on_disconnect
        )

        # Windows/WinRT sometimes reports ZERO GATT services on a fresh
        # connection (stale cache / discovery race); a clean reconnect re-runs
        # service discovery, so try the connect+enumerate twice in that case.
        pairs = []
        all_services = []
        for _attempt in range(2):
            await client.connect()
            pairs = []
            all_services = list(client.services)
            for service in all_services:
                s = _short(service.uuid)
                if s in _SKIP_SERVICES:
                    continue
                w = n = None
                for ch in service.characteristics:
                    props = set(ch.properties)
                    if n is None and ("notify" in props or "indicate" in props):
                        n = ch
                    if w is None and ("write" in props or "write-without-response" in props):
                        w = ch
                if w is not None and n is not None:
                    pairs.append((s, w, n))
            if pairs or all_services:
                break
            await client.disconnect()
            await asyncio.sleep(1.0)
        pairs.sort(key=lambda t: _PREFERRED.index(t[0]) if t[0] in _PREFERRED else 99)

        if not pairs:
            seen_services = ", ".join(sorted(_short(s.uuid) for s in all_services)) or "none"
            await client.disconnect()
            raise BleLinkError(
                f"Connected to {target.name or target.address} but found no "
                "UART-style (write + notify) characteristic pair on it. "
                f"GATT services seen: {seen_services}. Unplug and reseat the "
                "dongle, then retry."
            )

        service_id, write_char, notify_char = pairs[0]
        self._client = client
        self._write_char = write_char
        self._notify_char = notify_char
        self._use_response = "write-without-response" not in set(write_char.properties)

        def on_notify(_sender, data: bytearray):
            self._buf.extend(data)
            if b">" in data:
                self._prompt_evt.set()

        await client.start_notify(notify_char, on_notify)

        # 3) Wake the dongle. NOTE: a bare CR makes an ELM327 REPEAT its last
        #    command, which may run for seconds -- so wait for the '>' prompt
        #    (not a fixed sleep) before letting the real conversation start.
        try:
            await self._exchange_async(b"\r", 3.0)
        except Exception:
            pass
        self._buf.clear()
        self._prompt_evt.clear()

        return {
            "address": target.address,
            "name": target.name or "",
            "service": service_id,
            "write": _short(write_char.uuid),
            "notify": _short(notify_char.uuid),
        }

    async def _exchange_async(self, data: bytes, timeout: float) -> bytes:
        async with self._xchg_lock:
            self._buf.clear()
            self._prompt_evt.clear()
            await self._client.write_gatt_char(
                self._write_char, data, response=self._use_response
            )
            try:
                await asyncio.wait_for(self._prompt_evt.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass  # return whatever arrived; caller decides if that's enough
            return bytes(self._buf)

    async def _close_async(self):
        client = self._client
        self._client = None
        if client is not None:
            try:
                if self._notify_char is not None:
                    await client.stop_notify(self._notify_char)
            except Exception:
                pass
            try:
                await client.disconnect()
            except Exception:
                pass
