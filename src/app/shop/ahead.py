"""Resolve ahead (stage 2o): loving or liking an item looks its store link up in the background, so
Open is instant for it.

One product lookup per result per session, success or failure, in the same cache Open uses
(`Session.product_cache`). Automatic lookups are bounded in three ways: at most
`Config.shop_auto_lookups_max` per session (so Open keeps the rest of the cap of 12), at most
`AHEAD_WORKERS` running at once, and at most `AHEAD_PENDING` waiting. They never run on the
request's thread and never hold the session lock while the provider answers. A lookup that finishes
after Start over or a topic change may still fill the cache (it is the same product) but writes
nothing learned: only the cache, the lookup counters and the result's own star rating.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager

from app.config import Config
from app.shop.resolve import auto_lookups_used, fill_rating, record_lookup, resolve
from app.shop.store import SessionError, SessionStore
from app.shopping import ProductDetails, ProductLookup, ProviderError

AHEAD_WORKERS = 2  # lookups running at once
AHEAD_PENDING = 16  # lookups waiting for a worker; more are not queued


class LookAhead:
    def __init__(self, config: Config, store: SessionStore, lookup: ProductLookup) -> None:
        self.config = config
        self.store = store
        self.lookup = lookup
        self._pool = ThreadPoolExecutor(max_workers=AHEAD_WORKERS, thread_name_prefix="lookahead")
        self._guard = threading.Lock()
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._queued: dict[str, set[str]] = {}
        self._futures: dict[str, list[Future[None]]] = {}
        self._cancelled: set[str] = set()
        self.running = 0  # workers busy right now (tests read the peak)
        self.peak = 0

    @contextmanager
    def key_lock(self, sid: str, rid: str) -> Iterator[None]:
        """Held around one product's lookup, so Open and an automatic lookup of the same product
        never both call the provider: the second one finds the cache filled."""
        with self._guard:
            lock = self._locks.setdefault((sid, rid), threading.Lock())
        with lock:
            yield

    def cancel(self, sid: str) -> None:
        with self._guard:
            self._cancelled.add(sid)
            self._queued.pop(sid, None)

    def submit(self, sid: str, rid: str) -> bool:
        """Queue a lookup for a loved or liked result when it is allowed; True if queued."""
        config = self.config
        if not config.shop_auto_lookups or config.shop_auto_lookups_max <= 0:
            return False
        try:
            session = self.store.load(sid)
        except SessionError:
            return False
        result = session.results.get(rid)
        if result is None or not result.product_id or rid in session.product_cache:
            return False
        with self._guard:
            self._cancelled.discard(sid)
            queued = self._queued.setdefault(sid, set())
            if rid in queued or sum(len(v) for v in self._queued.values()) >= AHEAD_PENDING:
                return False
            planned = auto_lookups_used(session) + len(queued)
            if planned >= config.shop_auto_lookups_max:
                return False
            if session.lookups_used + len(queued) >= config.shop_lookups_per_session:
                return False
            queued.add(rid)
            future = self._pool.submit(self._run, sid, rid)
            self._futures.setdefault(sid, []).append(future)
        return True

    def wait(self, sid: str, timeout: float = 30.0) -> None:
        """Block until this session's queued lookups are done (tests and shutdown)."""
        with self._guard:
            futures = list(self._futures.get(sid, []))
        deadline = time.monotonic() + timeout
        for future in futures:
            future.result(timeout=max(0.0, deadline - time.monotonic()))

    def _run(self, sid: str, rid: str) -> None:
        with self._guard:
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            self._lookup(sid, rid)
        except Exception:  # resolving ahead never fails a request; Open does it the usual way
            pass
        finally:
            with self._guard:
                self.running -= 1
                self._queued.get(sid, set()).discard(rid)

    def _lookup(self, sid: str, rid: str) -> None:
        with self._guard:
            if sid in self._cancelled:
                return
        with self.key_lock(sid, rid):
            try:
                session = self.store.load(sid)
            except SessionError:
                return
            result = session.results.get(rid)
            if result is None or rid in session.product_cache:
                return  # Open got there first: one cache, one lookup
            if session.lookups_used >= self.config.shop_lookups_per_session:
                return
            started = time.monotonic()
            details: ProductDetails | None = None
            error: ProviderError | None = None
            try:
                details = self.lookup.lookup(result.product_id)
            except ProviderError as exc:
                error = exc  # not retried: the failure is cached and counted once
            seconds = time.monotonic() - started
            try:
                with self.store.transaction(sid) as live:
                    shown = live.results.get(rid)
                    if shown is None or rid in live.product_cache:
                        return
                    record_lookup(live, shown, details, error, seconds, "auto", self.config)
                    live.lookup_usd = round(live.lookup_usd + self.config.serpapi_usd_per_search, 6)
                    fill_rating(live, shown, details)
                    resolve(live, rid, self.lookup, self.config)  # reads the cache; no new call
            except SessionError:
                return
