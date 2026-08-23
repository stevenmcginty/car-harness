"""Self-test for the background job registry (backend/core/jobs.py).

No car and no HTTP server here: the "capture" is a stub function that ticks a
few times, and what is asserted is exactly the behaviour a ten-minute drive
log depends on and nobody wants to discover on the car -- that a finished run
hands back its result, that a crashing run is recorded rather than lost (with
the samples it managed to collect still readable), that stop() winds a run up
without killing it, that a poller using `since` sees each sample once, and
that yesterday's runs are evicted while a job still running never is.

Everything is driven off threading.Event and short deadline polls, so the
tests are fast and do not depend on any thread being scheduled promptly.

Run:  .venv\\Scripts\\python.exe -m unittest backend.tests.test_jobs -v
"""

import sys
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core import jobs  # noqa: E402


class JobsTest(unittest.TestCase):
    """Each case starts from an empty registry -- the module state is global."""

    def setUp(self):
        with jobs._lock:
            jobs._jobs.clear()
        self._release = []      # events to set in tearDown so no thread lingers

    def tearDown(self):
        for event in self._release:
            event.set()

    # ---------------------------------------------------------------- helpers
    def gate(self) -> threading.Event:
        """An event the test releases, and tearDown releases regardless."""
        event = threading.Event()
        self._release.append(event)
        return event

    def wait_state(self, job_id: str, state: str, timeout: float = 2.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = jobs.status(job_id)
            if st and st["state"] == state:
                return st
            time.sleep(0.005)
        self.fail(f"job never reached {state!r}: {jobs.status(job_id)}")

    def run_quick(self, kind: str = "test", label: str = "") -> str:
        """A job that finishes immediately, waited on."""
        job_id = jobs.start(kind, lambda p, s: "ok", label=label)
        self.wait_state(job_id, "done")
        return job_id

    # ------------------------------------------------------------- completion
    def test_a_finished_job_reports_done_with_its_result(self):
        def capture(progress, should_stop):
            progress(rpm=900)
            progress({"tick": 1}, rpm=1200)
            return {"samples": 1}

        job_id = jobs.start("slk-misfire", capture, label="SLK drive log")
        st = self.wait_state(job_id, "done")

        self.assertEqual(st["id"], job_id)
        self.assertEqual(st["kind"], "slk-misfire")
        self.assertEqual(st["label"], "SLK drive log")
        self.assertEqual(st["result"], {"samples": 1})
        self.assertEqual(st["partial"], [{"tick": 1}])
        self.assertEqual(st["partialCount"], 1)
        self.assertEqual(st["progress"], {"rpm": 1200})
        self.assertIsNone(st["error"])

    def test_timestamps_are_iso_utc_and_ordered(self):
        st = jobs.status(self.run_quick())
        for stamp in (st["startedAt"], st["finishedAt"]):
            self.assertTrue(stamp.endswith("+00:00"), stamp)
            datetime.fromisoformat(stamp)  # raises if it is not ISO-8601
        self.assertLessEqual(st["startedAt"], st["finishedAt"])

    def test_a_running_job_has_no_finish_stamp_and_no_result(self):
        gate = self.gate()
        job_id = jobs.start("slow", lambda p, s: gate.wait(5))
        st = jobs.status(job_id)
        self.assertEqual(st["state"], "running")
        self.assertIsNone(st["finishedAt"])
        self.assertIsNone(st["result"])
        gate.set()
        self.wait_state(job_id, "done")

    def test_status_light_leaves_out_the_heavy_fields(self):
        job_id = jobs.start("test", lambda p, s: [p({"n": n}) for n in range(3)])
        self.wait_state(job_id, "done")
        light = jobs.status_light(job_id)
        self.assertNotIn("partial", light)
        self.assertNotIn("result", light)
        self.assertEqual(light["partialCount"], 3)
        self.assertEqual(light["state"], "done")

    def test_unknown_ids_read_as_none(self):
        self.assertIsNone(jobs.status("nope"))
        self.assertIsNone(jobs.status_light("nope"))
        self.assertIsNone(jobs.partial("nope"))
        self.assertFalse(jobs.stop("nope"))

    # -------------------------------------------------------------- the crash
    def test_a_crash_is_recorded_and_keeps_the_samples_already_taken(self):
        def capture(progress, should_stop):
            progress({"tick": 1})
            progress({"tick": 2})
            raise ValueError("dongle went away")

        job_id = jobs.start("slk-misfire", capture)
        st = self.wait_state(job_id, "error")

        self.assertEqual(st["error"], "ValueError: dongle went away")
        self.assertIsNotNone(st["finishedAt"])
        self.assertIsNone(st["result"])
        # The whole point: two ticks of a real drive survive the crash.
        self.assertEqual(st["partial"], [{"tick": 1}, {"tick": 2}])

    # ------------------------------------------------------- cooperative stop
    def test_stop_winds_a_job_up_and_marks_it_stopped(self):
        started = threading.Event()

        def capture(progress, should_stop):
            started.set()
            ticks = 0
            while not should_stop():
                ticks += 1
                time.sleep(0.005)
            return {"ticks": ticks, "endedEarly": True}

        job_id = jobs.start("slk-misfire", capture)
        self.assertTrue(started.wait(2), "worker never started")
        self.assertTrue(jobs.stop(job_id))
        st = self.wait_state(job_id, "stopped")

        self.assertEqual(st["state"], "stopped")          # not "done"
        self.assertTrue(st["result"]["endedEarly"])       # result still kept
        self.assertIsNotNone(st["finishedAt"])
        self.assertIsNone(st["error"])

    def test_a_job_that_ignores_the_stop_flag_still_reads_as_stopped(self):
        # State is decided by the flag after fn returns, so a job that never
        # calls should_stop() is still reported as stopped, never as a clean
        # "done" the operator would read as a complete drive log.
        gate = self.gate()
        job_id = jobs.start("stubborn", lambda p, s: gate.wait(5) and "finished")
        self.assertTrue(jobs.stop(job_id))
        gate.set()
        self.assertEqual(self.wait_state(job_id, "stopped")["result"], "finished")

    def test_a_crash_after_stop_is_still_an_error(self):
        gate = self.gate()

        def capture(progress, should_stop):
            gate.wait(5)
            raise RuntimeError("boom")

        job_id = jobs.start("test", capture)
        jobs.stop(job_id)
        gate.set()
        self.assertEqual(self.wait_state(job_id, "error")["error"], "RuntimeError: boom")

    # ---------------------------------------------------- streaming partials
    def test_since_streams_each_sample_exactly_once(self):
        step = self.gate()

        def capture(progress, should_stop):
            progress({"tick": 1})
            progress({"tick": 2})
            step.wait(5)
            progress({"tick": 3})
            return "done"

        job_id = jobs.start("slk-misfire", capture)

        deadline = time.monotonic() + 2.0
        while jobs.partial(job_id)["nextIndex"] < 2 and time.monotonic() < deadline:
            time.sleep(0.005)

        first = jobs.partial(job_id)
        self.assertEqual(first["items"], [{"tick": 1}, {"tick": 2}])
        self.assertEqual(first["nextIndex"], 2)

        # Nothing new yet: an empty page, and the cursor does not move.
        idle = jobs.partial(job_id, since=first["nextIndex"])
        self.assertEqual(idle["items"], [])
        self.assertEqual(idle["nextIndex"], 2)

        step.set()
        self.wait_state(job_id, "done")
        tail = jobs.partial(job_id, since=idle["nextIndex"])
        self.assertEqual(tail["items"], [{"tick": 3}])
        self.assertEqual(tail["nextIndex"], 3)

    def test_out_of_range_since_is_clamped_rather_than_exploding(self):
        job_id = jobs.start("test", lambda p, s: p({"tick": 1}))
        self.wait_state(job_id, "done")
        self.assertEqual(jobs.partial(job_id, since=99)["items"], [])
        self.assertEqual(jobs.partial(job_id, since=99)["nextIndex"], 1)
        self.assertEqual(jobs.partial(job_id, since=-5)["items"], [{"tick": 1}])

    def test_a_snapshot_is_a_copy_the_worker_cannot_mutate_underneath(self):
        step = self.gate()

        def capture(progress, should_stop):
            progress({"tick": 1}, phase="warm-up")
            step.wait(5)
            progress({"tick": 2}, phase="load")
            return None

        job_id = jobs.start("test", capture)
        deadline = time.monotonic() + 2.0
        while jobs.status(job_id)["partialCount"] < 1 and time.monotonic() < deadline:
            time.sleep(0.005)

        snap = jobs.status(job_id)
        step.set()
        self.wait_state(job_id, "done")
        self.assertEqual(snap["partial"], [{"tick": 1}])
        self.assertEqual(snap["progress"], {"phase": "warm-up"})

    # -------------------------------------------------------------- retention
    def test_only_the_last_twenty_finished_jobs_are_kept(self):
        keep_from = jobs.MAX_FINISHED_JOBS
        ids = [self.run_quick(label=str(n)) for n in range(keep_from + 5)]

        self.assertEqual(len(jobs.list_jobs(limit=100)), keep_from)
        for gone in ids[:5]:
            self.assertIsNone(jobs.status(gone), "an evicted job is still readable")
        for kept in ids[5:]:
            self.assertIsNotNone(jobs.status(kept), "a recent job was evicted")

    def test_a_running_job_is_never_evicted(self):
        gate = self.gate()
        long_id = jobs.start("slk-misfire", lambda p, s: gate.wait(10))
        for n in range(jobs.MAX_FINISHED_JOBS + 5):
            self.run_quick(label=str(n))

        self.assertEqual(jobs.status(long_id)["state"], "running")
        self.assertEqual(len(jobs.list_jobs(limit=100)), jobs.MAX_FINISHED_JOBS + 1)
        gate.set()
        # And it survives its own completion: retention orders by finish, so
        # the long capture is the NEWEST result, not the oldest job.
        self.wait_state(long_id, "done")
        self.assertEqual(len(jobs.list_jobs(limit=100)), jobs.MAX_FINISHED_JOBS)

    # ------------------------------------------------------------- list_jobs
    def test_list_jobs_is_newest_first_and_honours_the_limit(self):
        ids = [self.run_quick(label=str(n)) for n in range(4)]
        listed = jobs.list_jobs(limit=2)
        self.assertEqual([j["id"] for j in listed], list(reversed(ids))[:2])
        self.assertNotIn("partial", listed[0])
        self.assertEqual(jobs.list_jobs(limit=0), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
