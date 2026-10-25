"""Background jobs for the slow Shop Mode actions (a search, a Shop Again).

A POST starts a job and returns its id at once; the page polls `GET /api/job/<id>` for the current
phase ("Searching 7 retailers", "Reading your ratings", "Tagging results", "Ranking") and, when it
is done, the same view a synchronous call would have returned. Jobs live in memory only.

Safety: a job id is 128 random bits (`secrets.token_urlsafe(16)`), a job started for a session can
only be read with that session's id (a wrong or missing session id is a 404, the same as an unknown
id), at most one job runs per session and `MAX_RUNNING` in all, and a finished job is forgotten
after `ttl_s` seconds. The clock is replaceable, so expiry is tested without waiting.
"""

from __future__ import annotations

import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.shop.clock import Clock
from app.shop.progress import SEARCH_STEPS, SHOP_AGAIN_STEPS, Progress

MAX_RUNNING = 8
JOB_TTL_S = 600.0
JOB_ID_OK = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class JobError(Exception):
    """A job request that cannot be served: carries the HTTP status and a safe message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Job:
    id: str
    session_id: str | None
    kind: str
    state: str = "running"  # running | done | error
    progress: Progress | None = None
    result: dict[str, Any] | None = None
    error: tuple[int, str] | None = None
    started: float = 0.0
    finished: float | None = None
    thread: threading.Thread | None = field(default=None, repr=False)


class JobManager:
    def __init__(self, clock: Clock | None = None, ttl_s: float = JOB_TTL_S) -> None:
        self.clock = clock or Clock()
        self.ttl_s = ttl_s
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def _purge(self) -> None:
        now = self.clock.now()
        for jid in [
            j.id
            for j in self._jobs.values()
            if j.finished is not None and now - j.finished > self.ttl_s
        ]:
            del self._jobs[jid]

    def start(
        self,
        session_id: str | None,
        kind: str,
        work: Callable[[Progress], dict[str, Any]],
        failure: Callable[[Exception], tuple[int, str]],
    ) -> str:
        """Run `work(progress)` in the background; returns the job id. `progress` is callable (the
        phase text) and carries the steps for the status bar. `failure` turns an exception raised by
        `work` into (HTTP status, safe message)."""
        with self._lock:
            self._purge()
            running = [j for j in self._jobs.values() if j.state == "running"]
            if session_id is not None and any(j.session_id == session_id for j in running):
                raise JobError(409, "Something is already running for this session")
            if len(running) >= MAX_RUNNING:
                raise JobError(429, "Too many things are running; try again in a moment")
            job = Job(secrets.token_urlsafe(16), session_id, kind, started=self.clock.now())
            steps = SHOP_AGAIN_STEPS if kind == "shop_again" else SEARCH_STEPS
            job.progress = Progress(steps, self.clock)
            self._jobs[job.id] = job
        progress = job.progress

        def run() -> None:
            try:
                result = work(progress)
                progress.finish_all()
                job.result, job.state = result, "done"
            except Exception as exc:  # reported to the page, never raised into the thread
                job.error, job.state = failure(exc), "error"
            finally:
                job.finished = self.clock.now()

        job.thread = threading.Thread(target=run, daemon=True, name=f"shop-job-{job.kind}")
        job.thread.start()
        return job.id

    def status(self, job_id: str, session_id: str | None) -> dict[str, Any]:
        """The job's state for the page. An unknown, expired or other session's job is a 404."""
        with self._lock:
            self._purge()
            job = self._jobs.get(job_id) if JOB_ID_OK.match(job_id or "") else None
            if job is None or (job.session_id is not None and job.session_id != session_id):
                raise JobError(404, "unknown job")
            assert job.progress is not None
            out: dict[str, Any] = {
                "state": job.state, "phase": job.progress.phase, "kind": job.kind,
                **job.progress.snapshot(),
            }  # fmt: skip
            if job.finished is not None:
                out["elapsed_s"] = round(job.finished - job.started, 1)
            if job.state == "done":
                out["result"] = job.result
            elif job.state == "error" and job.error is not None:
                out["status"], out["error"] = job.error
            return out

    def wait(self, job_id: str, timeout: float | None = None) -> None:
        """Block until the job has finished (tests and scripts; the page polls instead)."""
        job = self._jobs.get(job_id)
        if job is not None and job.thread is not None:
            job.thread.join(timeout)
