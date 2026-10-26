"""What a slow Shop Mode action reports while it runs: steps for the status bar and a timeline.

`Progress` is what a background job hands its work. It is callable (`progress("Tagging results")`
sets the one-line phase text, as before) and it holds the ordered `steps` of the action: each has an
id, a label, a state (pending, active, done, skipped) and optional sub-progress (`done_count` of
`total`). A step that is skipped or already satisfied is marked done at once. `snapshot()` is what
`GET /api/job/<id>` sends the page.

`Timeline` records every model call and search (start offset, duration, the time it waited for a
worker or a slot, retries) and every phase (start and end), relative to the job's start, for the
trace. Both are thread safe and use the replaceable clock, so tests need no waiting.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from app.shop.clock import Clock

PENDING, ACTIVE, DONE, SKIPPED = "pending", "active", "done", "skipped"

SHOP_AGAIN_STEPS = (
    ("ratings", "Finishing your last ratings"),
    ("searching", "Searching"),
    ("tagging", "Tagging"),
    ("ranking", "Ranking"),
)
SHOP_AGAIN_REFINE_STEPS = (("refine", "Reading your refinement"), *SHOP_AGAIN_STEPS)
SEARCH_STEPS = (("searching", "Searching"), ("tagging", "Tagging"), ("ranking", "Ranking"))


@dataclass
class Step:
    id: str
    label: str
    state: str = PENDING
    done_count: int | None = None
    total: int | None = None


class Progress:
    """The steps of one job, the current phase text and the time since it began."""

    def __init__(self, steps: tuple[tuple[str, str], ...] = (), clock: Clock | None = None) -> None:
        self.clock = clock or Clock()
        self.started = self.clock.now()
        self.phase = "Starting"
        self.steps = [Step(i, label) for i, label in steps]
        self._lock = threading.Lock()

    def __call__(self, text: str) -> None:
        self.phase = text[:80]

    def _step(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def begin(self, step_id: str, total: int | None = None) -> None:
        with self._lock:
            step = self._step(step_id)
            if step is not None and step.state in (PENDING, ACTIVE):
                step.state = ACTIVE
                if total is not None:
                    step.total, step.done_count = total, 0

    def set_total(self, step_id: str, total: int) -> None:
        with self._lock:
            step = self._step(step_id)
            if step is not None and step.state != DONE:
                step.total = total
                step.done_count = step.done_count or 0

    def advance(self, step_id: str, by: int = 1) -> None:
        with self._lock:
            step = self._step(step_id)
            if step is not None and step.state != DONE:
                step.state = ACTIVE
                step.done_count = (step.done_count or 0) + by
                if step.total is not None:
                    step.done_count = min(step.done_count, step.total)

    def finish(self, step_id: str) -> None:
        with self._lock:
            step = self._step(step_id)
            if step is not None:
                step.state = DONE
                if step.total is not None:
                    step.done_count = step.total

    def finish_all(self) -> None:
        """The job is over: every step that was not skipped is done."""
        with self._lock:
            for step in self.steps:
                if step.state != SKIPPED:
                    step.state = DONE
                    if step.total is not None:
                        step.done_count = step.total

    def skip(self, step_id: str) -> None:
        """A step that is not needed (or already satisfied) shows as done at once."""
        with self._lock:
            step = self._step(step_id)
            if step is not None and step.state != DONE:
                step.state = SKIPPED

    def elapsed(self) -> float:
        return round(max(0.0, self.clock.now() - self.started), 1)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "steps": [
                    {
                        "id": s.id,
                        "label": s.label,
                        "state": s.state,
                        "done_count": s.done_count,
                        "total": s.total,
                    }
                    for s in self.steps
                ],  # fmt: skip
                "elapsed_s": self.elapsed(),
            }


class Timeline:
    """Every model call, search and phase of one run, relative to its start."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or Clock()
        self.origin = self.clock.now()
        self._calls: list[dict[str, Any]] = []
        self._phases: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    def event(
        self,
        kind: str,
        label: str,
        start: float,
        end: float,
        queue_wait: float = 0.0,
        retries: int = 0,
        **extra: Any,
    ) -> None:
        row = {
            "kind": kind, "label": label[:60],
            "start": round(start - self.origin, 2), "duration": round(max(0.0, end - start), 2),
            "queue_wait": round(max(0.0, queue_wait), 2), "retries": retries, **extra,
        }  # fmt: skip
        with self._lock:
            self._calls.append(row)

    @contextmanager
    def span(
        self, kind: str, label: str, queued_at: float | None = None
    ) -> Iterator[dict[str, Any]]:
        """Time a block. `queued_at` is when the work was submitted: the wait before it began is the
        queue wait. The yielded dict takes extra fields (`retries`, `items`, ...)."""
        begun = self.clock.now()
        info: dict[str, Any] = {}
        try:
            yield info
        finally:
            wait = begun - queued_at if queued_at is not None else 0.0
            self.event(
                kind, label, begun, self.clock.now(), wait, int(info.pop("retries", 0)), **info
            )

    def phase_start(self, name: str) -> None:
        with self._lock:
            self._phases.setdefault(name, {"start": round(self.clock.now() - self.origin, 2)})

    def phase_end(self, name: str) -> None:
        with self._lock:
            row = self._phases.setdefault(name, {"start": round(self.clock.now() - self.origin, 2)})
            row["end"] = round(self.clock.now() - self.origin, 2)

    def export(self) -> dict[str, Any]:
        with self._lock:
            calls = sorted(self._calls, key=lambda c: (c["start"], c["label"]))
            phases = [
                {"phase": k, **v}
                for k, v in sorted(self._phases.items(), key=lambda kv: kv[1]["start"])
            ]
        return {"calls": calls[:80], "phases": phases}
