"""A clock that can be replaced, so latency budgets are tested without waiting.

Production code asks `Clock.now()` for seconds on a monotonic scale; a test passes a `FakeClock`
and advances it from inside its fake providers and model calls, so a "slow" call costs no real time.
"""

from __future__ import annotations

import threading
import time


class Clock:
    def now(self) -> float:
        return time.monotonic()


class FakeClock(Clock):
    """Time that moves only when told to (`advance`). Thread-safe."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._t

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += seconds
