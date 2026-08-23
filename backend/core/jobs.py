"""Background jobs: start a long capture on a thread, poll it over HTTP.

WHY THIS FILE EXISTS
--------------------
A Mercedes rough-running drive capture (backend/profiles/mercedes/misfire.py)
runs for ten minutes. It cannot sit inside one HTTP request: the request would
time out, and a dropped connection would throw away the whole drive -- ten
minutes of someone's day, on a car that only misbehaves once it is warm.

It also cannot be pushed out to a separate process. The capture talks to the
car through the phone relay, and ``backend.obd.remote_link.hub`` is a
module-level singleton: only the FastAPI server process holds the phone's
WebSocket, so a worker process would find an empty hub and no car.

So the capture runs on a worker thread owned by the server, and the caller
polls. start() hands back an id; status()/partial() read the run as it goes;
stop() asks it to wind up early. Threads, not asyncio, because the capture
code is blocking to its bones -- it sits in time.sleep() between ticks.

Two things this registry promises the poller, both learned the hard way:

1. A crash never takes the server down, and never loses what was already
   captured. The exception is recorded in ``error``, the state goes to
   "error", and every sample collected before the crash stays readable.
2. Samples can be streamed. partial(job_id, since=n) returns only what
   arrived after index n, so a phone polling once a second does not re-fetch
   600 dicts of raw hex to see the one new tick.
"""

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

# A finished job is kept only so the caller can come back for its result.
# Twenty is a couple of days of bench work; the oldest goes first. Running
# jobs are never evicted -- nothing else holds a reference to them.
MAX_FINISHED_JOBS = 20

_lock = threading.RLock()
_jobs: Dict[str, "_Job"] = {}  # insertion-ordered: oldest STARTED first
_finish_seq = 0                # ticks on every finish, so eviction can order by it


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Job:
    """One run's shared state. Every field is touched under ``_lock``."""

    def __init__(self, job_id: str, kind: str, label: str):
        self.id = job_id
        self.kind = kind
        self.label = label
        self.state = "running"          # running | done | error | stopped
        self.started_at = _now()
        self.finished_at: Optional[str] = None
        self.progress: Dict[str, Any] = {}
        self.partial: List[Any] = []
        self.result: Any = None
        self.error: Optional[str] = None
        self.stop_requested = False
        self.finish_seq = 0


def _snapshot(job: "_Job", light: bool) -> dict:
    """Caller-owned view of a job. Lists and dicts are copied, so a poller
    reading a snapshot can never trip over the worker appending to it."""
    out = {
        "id": job.id,
        "kind": job.kind,
        "label": job.label,
        "state": job.state,
        "startedAt": job.started_at,
        "finishedAt": job.finished_at,
        "progress": dict(job.progress),
        "partialCount": len(job.partial),
    }
    if not light:
        out["partial"] = list(job.partial)
        out["result"] = job.result
    out["error"] = job.error
    return out


def _finish_locked(job: "_Job") -> None:
    """Stamp a job as finished and re-run retention. Ordering is by FINISH,
    not by start: a ten-minute capture started before twenty short ones is
    still the newest result, and evicting it the instant it completed would
    take its result away before the poller ever read it."""
    global _finish_seq
    _finish_seq += 1
    job.finish_seq = _finish_seq
    job.finished_at = _now()
    _prune_locked()


def _prune_locked() -> None:
    """Drop the longest-finished jobs down to MAX_FINISHED_JOBS."""
    finished = sorted(
        (j for j in _jobs.values() if j.state != "running"),
        key=lambda j: j.finish_seq,
    )
    for job in finished[: max(0, len(finished) - MAX_FINISHED_JOBS)]:
        _jobs.pop(job.id, None)


# --------------------------------------------------------------------------- #
# starting and stopping
# --------------------------------------------------------------------------- #
def start(kind: str, fn: Callable[[Callable, Callable], Any], *, label: str = "") -> str:
    """Run ``fn`` on a daemon thread and return the job id to poll it with.

    ``fn`` is called as ``fn(progress, should_stop)``:

        progress(item=None, **fields)  appends ``item`` to the job's partial
                                       list (when given) and merges ``fields``
                                       into the job's progress dict
        should_stop()                  True once someone has called stop()

    Whatever ``fn`` returns becomes the job's ``result``. A long capture is
    expected to check should_stop() between ticks and return what it has.
    """
    job_id = uuid.uuid4().hex
    job = _Job(job_id, kind, label)
    with _lock:
        _jobs[job_id] = job
        _prune_locked()

    def progress(item: Any = None, **fields: Any) -> None:
        with _lock:
            if item is not None:
                job.partial.append(item)
            if fields:
                job.progress.update(fields)

    def should_stop() -> bool:
        with _lock:
            return job.stop_requested

    def runner() -> None:
        try:
            result = fn(progress, should_stop)
        except BaseException as e:  # noqa: BLE001 -- a job must never kill the server
            with _lock:
                job.error = f"{type(e).__name__}: {e}"
                job.state = "error"
                _finish_locked(job)
            return
        with _lock:
            job.result = result
            # The stop flag decides how this reads: a run that wound up early
            # on request is "stopped", not a clean "done", even though it
            # returned normally and its result is kept.
            job.state = "stopped" if job.stop_requested else "done"
            _finish_locked(job)

    threading.Thread(
        target=runner, daemon=True, name=f"carharness-job-{kind}-{job_id[:8]}"
    ).start()
    return job_id


def stop(job_id: str) -> bool:
    """Ask a job to wind up. Cooperative -- nothing is killed; the job stops
    when it next checks should_stop(). False if the id is unknown."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return False
        job.stop_requested = True
        return True


# --------------------------------------------------------------------------- #
# polling
# --------------------------------------------------------------------------- #
def status(job_id: str) -> Optional[dict]:
    """Everything about a job, partial samples and result included. None if
    the id is unknown (or the job has aged out of retention)."""
    with _lock:
        job = _jobs.get(job_id)
        return _snapshot(job, light=False) if job else None


def status_light(job_id: str) -> Optional[dict]:
    """status() without ``partial`` and ``result`` -- for the once-a-second
    poll, where copying 600 samples per call is the whole cost."""
    with _lock:
        job = _jobs.get(job_id)
        return _snapshot(job, light=True) if job else None


def partial(job_id: str, since: int = 0) -> Optional[dict]:
    """``{items, nextIndex}`` -- the samples from index ``since`` onward.

    Feed ``nextIndex`` back as the next ``since`` and the poller streams new
    samples without ever re-fetching the ones it already has.
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        start_at = min(max(int(since or 0), 0), len(job.partial))
        return {"items": job.partial[start_at:], "nextIndex": len(job.partial)}


def list_jobs(limit: int = 20) -> List[dict]:
    """Known jobs, newest first, in the status_light shape."""
    with _lock:
        jobs = list(_jobs.values())[::-1][: max(0, int(limit))]
        return [_snapshot(j, light=True) for j in jobs]
