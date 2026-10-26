"""Background preparation: the model work a Shop Again would otherwise do while you wait.

While you browse, each stored batch is tagged from its titles AND its product photos (prompt
`shop_candidate_tags` v4, the same forced `record_tags` tool call, thumbnails fetched by the guarded
fetcher), and each comment you write is read as you write it (the preference call restricted to that
one item). The results are cached in the session file: tags by result id (`tag_cache`, with
`tag_source` "photo" or "title": a photo tag is never replaced by a title tag), quality cues the
photo showed, and comment readings keyed by item id plus comment text (`comment_cache`). A Shop
Again then finds them ready (`app.shop.prefs.cached_readings`) and calls the model only for
what is missing.

Limits: at most `shop_prep_max_concurrent` model calls at once across ALL sessions' jobs (the pool
has that many workers), `shop_prep_calls_per_batch` calls for one stored batch, and a session's
preparation stops spending at `shop_prep_max_usd`. `cancel(session_id)` (Start over) drops queued
work and discards what is running. Every write goes through the store's lock (atomic). A failure
here is recorded and never raised: the live path covers whatever is missing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial

from app.config import BudgetExceededError, Config
from app.images import PreparedImage
from app.llm import Budget
from app.shop.again import base_query
from app.shop.candidates import CUES_KEY, MODE_TOOL, TagBook, Taggable, run_call
from app.shop.clock import Clock
from app.shop.models import ShopResult
from app.shop.nearby import Fingerprinter
from app.shop.prefs import read_rated_items, reading_entry
from app.shop.redact import describe_exception
from app.shop.store import SessionError, SessionStore, comment_key
from app.thumbnails import Thumbnailer, fetch_traced

MAX_ERRORS_KEPT = 5


@dataclass
class _State:
    cancelled: threading.Event = field(default_factory=threading.Event)
    total: int = 0  # tasks submitted
    done: int = 0  # tasks finished (also when skipped or failed)
    futures: list[Future[None]] = field(default_factory=list)


class PrepManager:
    def __init__(
        self,
        config: Config,
        store: SessionStore,
        thumbnailer: Thumbnailer | None,
        clock: Clock | None = None,
        fingerprinter: Fingerprinter | None = None,
    ) -> None:
        self.fingerprinter = fingerprinter
        self.config = config
        self.store = store
        self.thumbnailer = thumbnailer
        self.clock = clock or Clock()
        self.enabled = config.shop_prep_enabled
        self._pool = ThreadPoolExecutor(
            max_workers=config.shop_prep_max_concurrent, thread_name_prefix="shopprep"
        )
        self._states: dict[str, _State] = {}
        self._cond = threading.Condition()

    # ---- submitting ---------------------------

    def _state(self, sid: str) -> _State:
        with self._cond:
            state = self._states.get(sid)
            if state is None or state.cancelled.is_set():
                state = self._states[sid] = _State()
            return state

    def _submit(self, sid: str, fn: Callable[[], None]) -> None:
        state = self._state(sid)
        with self._cond:
            state.total += 1

        def run() -> None:
            try:
                if not state.cancelled.is_set():
                    fn()
            except Exception as exc:  # preparation never fails a request; the live path covers it
                self._note_error(sid, describe_exception(exc)[0])
            finally:
                with self._cond:
                    state.done += 1
                    self._cond.notify_all()

        state.futures.append(self._pool.submit(run))

    def submit_batch(self, sid: str, results: list[ShopResult]) -> int:
        """Start tagging a stored batch from title and photo (2 calls of about 12 items for 24
        results); returns how many calls were started. Items already tagged from a photo are
        skipped."""
        if not self.enabled or not results:
            return 0
        try:
            session = self.store.load(sid)
        except SessionError:
            return 0
        todo = [r for r in results if session.tag_source.get(r.id) != "photo"]
        size = self.config.shop_prep_chunk
        chunks = [todo[i : i + size] for i in range(0, len(todo), size)]
        chunks = chunks[: self.config.shop_prep_calls_per_batch]
        for chunk in chunks:
            ids = [r.id for r in chunk]
            self._submit(sid, partial(self._photo_task, sid, ids))
        return len(chunks)

    def submit_comment(self, sid: str, rid: str, comment: str) -> bool:
        """Start reading a comment as it is written; False when preparation is off, the comment is
        empty or this exact comment is already read."""
        text = comment.strip()
        if not self.enabled or not text:
            return False
        try:
            session = self.store.load(sid)
        except SessionError:
            return False
        if comment_key(rid, text) in session.comment_cache:
            return False
        self._submit(sid, partial(self._comment_task, sid, rid, text))
        return True

    # ---- waiting and cancelling ---------------------------

    def outstanding(self, sid: str) -> tuple[int, int]:
        """(tasks finished, tasks submitted) for the session."""
        with self._cond:
            state = self._states.get(sid)
            return (state.done, state.total) if state is not None else (0, 0)

    def wait(
        self, sid: str, timeout_s: float, on_progress: Callable[[int, int], None] | None = None
    ) -> tuple[int, int]:
        """Wait for the session's outstanding preparation, at most `timeout_s` real seconds (a
        bound, not a sleep: it ends the moment the last task does). Returns (finished, submitted);
        it never raises and never waits when nothing is outstanding."""
        end = time.monotonic() + max(0.0, timeout_s)
        with self._cond:
            state = self._states.get(sid)
            if state is None:
                return 0, 0
            while state.done < state.total:
                if on_progress is not None:
                    on_progress(state.done, state.total)
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            if on_progress is not None:
                on_progress(state.done, state.total)
            return state.done, state.total

    def cancel(self, sid: str) -> None:
        """Start over: queued work is dropped and anything running is discarded when it finishes."""
        with self._cond:
            state = self._states.pop(sid, None)
        if state is not None:
            state.cancelled.set()
            for fut in state.futures:
                fut.cancel()
            with self._cond:
                self._cond.notify_all()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ---- the work ---------------------------

    def _note_error(self, sid: str, text: str) -> None:
        try:
            with self.store.transaction(sid) as session:
                errors = list(session.prep.get("errors", []))
                session.prep["errors"] = [*errors, text[:160]][-MAX_ERRORS_KEPT:]
        except (SessionError, OSError):
            pass

    def _over_cap(self, sid: str) -> bool:
        try:
            spent = float(self.store.load(sid).prep.get("usd", 0.0))
        except SessionError:
            return True
        return spent >= self.config.shop_prep_max_usd

    def _images(self, items: list[Taggable]) -> dict[str, PreparedImage]:
        out: dict[str, PreparedImage] = {}
        if self.thumbnailer is None:
            return out
        for item in items:
            if not item.thumbnail_url.startswith("https://"):
                continue
            try:
                got = fetch_traced(self.thumbnailer, item.thumbnail_url, self.config)
            except Exception:  # a fetcher bug leaves the item to its title
                continue
            if got.ok and got.image is not None:
                out[item.id] = got.image
        return out

    def _photo_task(self, sid: str, ids: list[str]) -> None:
        state = self._states.get(sid)
        if self._over_cap(sid):
            return
        session = self.store.load(sid)
        items = [
            Taggable(r.id, r.title, r.price, r.seller, r.thumbnail_url)
            for rid in ids
            if (r := session.results.get(rid)) is not None
        ]
        if not items:
            return
        images = self._images(items)
        budget = Budget(self.config)
        try:
            rows, entry = run_call(
                items, TagBook(), budget, MODE_TOOL, False, self.clock,
                self.config.shop_tag_call_timeout_s, images=images or None,
            )  # fmt: skip
        except BudgetExceededError:
            return
        if state is not None and state.cancelled.is_set():
            return  # Start over while this ran: discard it
        with self.store.transaction(sid) as live:
            if self.fingerprinter is not None:  # the thumbnails are already here: hash them too
                for it in items:
                    if it.id in images and it.id not in live.fingerprints:
                        live.fingerprints[it.id] = self.fingerprinter.from_prepared(
                            it.thumbnail_url, images[it.id]
                        )
            for n, tags in rows.items():
                item = items[n - 1]
                cues = tags.pop(CUES_KEY, None)
                from_photo = item.id in images
                if from_photo or item.id not in live.tag_cache:
                    live.tag_cache[item.id] = {k: v for k, v in tags.items()}
                    live.tag_source[item.id] = "photo" if from_photo else "title"
                if from_photo and cues:
                    live.quality_cues[item.id] = [c for c in cues.split(";") if c]
            live.prep["usd"] = round(float(live.prep.get("usd", 0.0)) + entry["usd"], 6)
            live.prep["calls"] = int(live.prep.get("calls", 0)) + 1
            live.prep["tagged"] = int(live.prep.get("tagged", 0)) + len(rows)
            if entry["error"]:
                live.prep["errors"] = [*live.prep.get("errors", []), entry["error"][:160]][
                    -MAX_ERRORS_KEPT:
                ]

    def _comment_task(self, sid: str, rid: str, comment: str) -> None:
        state = self._states.get(sid)
        if self._over_cap(sid):
            return
        session = self.store.load(sid)
        rec = session.ratings.get(rid)
        base = base_query(session)
        if rec is None or rec.comment.strip() != comment or base is None:
            return  # the comment changed or went away while this waited
        topic = session.current_topic
        budget = Budget(self.config)
        answer, _, error = read_rated_items(session, base, {rid}, budget, self.thumbnailer)
        if state is not None and state.cancelled.is_set():
            return
        with self.store.transaction(sid) as live:
            if live.current_topic != topic:
                return  # a new topic began while this ran: nothing learned crosses over
            current = live.ratings.get(rid)
            if answer is not None and current is not None and current.comment.strip() == comment:
                live.comment_cache[comment_key(rid, comment)] = reading_entry(answer, rid)
            live.prep["usd"] = round(float(live.prep.get("usd", 0.0)) + budget.cost_usd, 6)
            live.prep["calls"] = int(live.prep.get("calls", 0)) + 1
            live.prep["comments"] = int(live.prep.get("comments", 0)) + (1 if answer else 0)
            if error:
                live.prep["errors"] = [*live.prep.get("errors", []), error][-MAX_ERRORS_KEPT:]
