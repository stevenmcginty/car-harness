"""Self-test for the phone-relay hub (backend/obd/remote_link.py).

There is no phone on the bench, so a fake WebSocket stands in for one and the
REAL hub is driven through it -- on a real asyncio loop in its own thread,
because the whole point of this class is that its two halves live in different
worlds: on_text() on the event loop, exchange() on a worker thread.

What is asserted here is exactly what cannot be observed without a car: that a
command comes back with its own answer, that an answer landing the instant the
frame goes out is not wiped by our own reset, that a silent dongle times out
instead of hanging, that a phone dropping mid-command is reported rather than
returned as empty, and that two worker threads cannot interleave commands on
the one socket.

Run:  .venv\\Scripts\\python.exe -m unittest backend.tests.test_remote_link -v
"""

import asyncio
import base64
import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.obd.remote_link import RelayError, RemotePhoneHub  # noqa: E402


def _frame(data: bytes) -> str:
    """One phone -> server data frame, exactly as the app sends it."""
    return json.dumps({"type": "data", "b64": base64.b64encode(data).decode("ascii")})


class FakePhone:
    """Stands in for the Starlette WebSocket the endpoint attaches.

    send_text is a coroutine run on the relay's own loop, like the real one.
    The answer comes back through hub.on_text() -- the same door the endpoint
    pushes received frames through -- either inline (the dongle answered
    before the send even returned) or after `delay` seconds on the loop.
    """

    def __init__(self, hub, loop, delay=0.0, inline=False, mute=False, answer=None):
        self._hub = hub
        self._loop = loop
        self._delay = delay
        self._inline = inline
        self._mute = mute
        self._answer = answer or (lambda data: b"OK\r\r>")
        self.sent = []
        self.sent_at = {}   # command bytes -> when it went out on the wire

    async def send_text(self, text: str) -> None:
        msg = json.loads(text)
        data = base64.b64decode(msg["b64"])
        self.sent.append(data)
        self.sent_at[data] = time.monotonic()
        if self._mute:
            return
        reply = _frame(self._answer(data))
        if self._inline:
            self._hub.on_text(reply)
        else:
            self._loop.call_later(self._delay, self._hub.on_text, reply)


class RelayHubTest(unittest.TestCase):
    """Every case gets its own hub and its own loop thread."""

    def setUp(self):
        self.hub = RemotePhoneHub()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()

    def attach(self, **kwargs):
        phone = FakePhone(self.hub, self.loop, **kwargs)
        self.hub.attach(phone, "Bench phone", self.loop)
        return phone

    # ------------------------------------------------------------------ round
    def test_exchange_returns_the_dongles_answer(self):
        phone = self.attach(delay=0.02, answer=lambda d: b"ELM327 v1.5\r\r>")
        got = self.hub.exchange(b"ATZ\r", timeout=2.0)
        self.assertEqual(got, b"ELM327 v1.5\r\r>")
        self.assertEqual(phone.sent, [b"ATZ\r"])

    def test_a_fast_reply_is_not_wiped_by_our_own_reset(self):
        # The dongle answers before send_text has even returned. The buffer
        # must already have been cleared by then, or the answer is lost and
        # the caller waits out the full timeout for bytes it was handed.
        self.attach(inline=True, answer=lambda d: b"41 00 BE 3F A8 13\r\r>")
        started = time.monotonic()
        got = self.hub.exchange(b"0100\r", timeout=3.0)
        self.assertEqual(got, b"41 00 BE 3F A8 13\r\r>")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_two_commands_in_a_row_do_not_see_each_others_bytes(self):
        self.attach(delay=0.02, answer=lambda d: d.strip() + b"-ok\r\r>")
        self.assertEqual(self.hub.exchange(b"ATE0\r", timeout=2.0), b"ATE0-ok\r\r>")
        self.assertEqual(self.hub.exchange(b"ATL0\r", timeout=2.0), b"ATL0-ok\r\r>")

    # ---------------------------------------------------------------- timeout
    def test_a_silent_dongle_times_out_and_returns_what_arrived(self):
        # No prompt is the ATMA monitor case as well as the dead-car case:
        # the transports all return the buffer as-is rather than raising.
        self.attach(mute=True)
        started = time.monotonic()
        got = self.hub.exchange(b"0100\r", timeout=0.3)
        self.assertEqual(got, b"")
        self.assertGreaterEqual(time.monotonic() - started, 0.25)

    def test_bytes_without_a_prompt_still_come_back(self):
        self.attach(mute=True)
        self.loop.call_later(0.05, self.hub.on_text, _frame(b"7E8 03 41 00"))
        self.assertEqual(self.hub.exchange(b"ATMA\r", timeout=0.4), b"7E8 03 41 00")

    # ------------------------------------------------------------- phone gone
    def test_exchange_without_a_phone_raises(self):
        with self.assertRaises(RelayError):
            self.hub.exchange(b"ATZ\r", timeout=0.2)

    def test_the_phone_dropping_mid_command_raises(self):
        phone = self.attach(mute=True)
        threading.Timer(0.05, self.hub.detach, args=(phone,)).start()
        with self.assertRaises(RelayError) as caught:
            self.hub.exchange(b"ATZ\r", timeout=0.5)
        self.assertIn("dropped", str(caught.exception))

    def test_a_second_phone_is_refused(self):
        self.attach()
        with self.assertRaises(RelayError):
            self.hub.attach(object(), "Another phone", self.loop)

    # ----------------------------------------------------------- one at a time
    def test_two_worker_threads_cannot_interleave_commands(self):
        # Both threads dive in at once. Serialised means the second command
        # only reaches the wire after the first exchange has handed its answer
        # back -- so the windows are measured from the fake phone's own send
        # times, not from when each thread called in.
        phone = self.attach(delay=0.15, answer=lambda d: d.strip() + b"-ok\r\r>")
        replies = {}
        done_at = {}
        lock = threading.Lock()

        def worker(cmd):
            reply = self.hub.exchange(cmd, timeout=3.0)
            with lock:
                replies[cmd] = reply
                done_at[cmd] = time.monotonic()

        threads = [threading.Thread(target=worker, args=(c,))
                   for c in (b"0100\r", b"0902\r")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(replies, {b"0100\r": b"0100-ok\r\r>", b"0902\r": b"0902-ok\r\r>"})
        first, second = sorted(phone.sent, key=phone.sent_at.get)
        self.assertLessEqual(
            done_at[first], phone.sent_at[second],
            "the second command went out before the first answer came back "
            "-- exchange() is not serialised",
        )

    # -------------------------------------------------------------- housekeeping
    def test_status_reports_the_attached_phone(self):
        self.attach()
        st = self.hub.status()
        self.assertTrue(st["connected"])
        self.assertEqual(st["deviceName"], "Bench phone")
        self.assertTrue(st["connectedAt"])

    def test_junk_and_unknown_types_are_ignored(self):
        self.attach(mute=True)
        for text in ("not json at all", '{"type":"ping"}', '{"type":"data","b64":"!!"}'):
            self.hub.on_text(text)
        self.assertEqual(self.hub.exchange(b"ATZ\r", timeout=0.2), b"")


if __name__ == "__main__":
    unittest.main(verbosity=2)
