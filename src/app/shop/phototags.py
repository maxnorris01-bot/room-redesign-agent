"""Photo tags for the top candidates (ADR 0012, stage 2p).

Titles say little: on the first live eval 18 of 24 titles in a "navy velvet accent chairs" batch
named no colour, and candidates were tagged from titles only, so a typed colour could not be
enforced. After the pool is ranked and de-duplicated, the top `shop_photo_tag_top_n` candidates are
tagged again from title AND thumbnail: `PHOTO_TAG_BATCH` per call, `PHOTO_TAG_WORKERS` calls at
once, `PHOTO_TAG_BUDGET_S` seconds for the whole step. A photo tag overrides the title and keyword
tags (a dimension the photo leaves empty keeps the title's value); a candidate not done by the end
of the budget keeps its title tags, and the trace says how many. Thumbnails come from `ThumbCache`,
shared with the near-duplicate fingerprints, so no image is fetched twice.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

from app.config import Config
from app.images import PreparedImage
from app.llm import Budget
from app.shop.candidates import (
    CUES_KEY,
    MODE_TOOL,
    NOTE_PREFIX,
    TagBook,
    Taggable,
    note_key,
    run_call,
)
from app.shop.clock import Clock
from app.shop.progress import Timeline
from app.shop.thresholds import (
    PHOTO_CACHE_MAX,
    PHOTO_TAG_BATCH,
    PHOTO_TAG_BUDGET_S,
    PHOTO_TAG_WORKERS,
)
from app.thumbnails import Thumbnailer, fetch_traced


class ThumbCache:
    """Prepared thumbnails by URL (successes and failures), bounded, shared by every step of a
    service that needs a thumbnail's bytes. `fetches` counts the real fetches (tests assert one per
    URL)."""

    def __init__(
        self, thumbnailer: Thumbnailer | None, config: Config, max_items: int = PHOTO_CACHE_MAX
    ) -> None:
        self.thumbnailer = thumbnailer
        self.config = config
        self.max_items = max_items
        self.fetches = 0
        self.hits = 0
        self._items: OrderedDict[str, PreparedImage | None] = OrderedDict()
        self._lock = threading.Lock()

    def image(self, url: str) -> PreparedImage | None:
        """The prepared image for an https URL, fetched once (None when it cannot be fetched)."""
        if not url.startswith("https://") or self.thumbnailer is None:
            return None
        with self._lock:
            if url in self._items:
                self._items.move_to_end(url)
                self.hits += 1
                return self._items[url]
        try:
            got = fetch_traced(self.thumbnailer, url, self.config)
            image = got.image if got.ok else None
        except Exception:  # a fetcher bug leaves the item to its title tags
            image = None
        with self._lock:
            self.fetches += 1
            self._items[url] = image
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)
        return image

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


@dataclass
class PhotoReport:
    wanted: int = 0  # candidates asked for (top N with a thumbnail, not already photo-tagged)
    with_image: int = 0  # of those, with an image that could be fetched
    tagged: int = 0  # tagged from the photo
    calls: int = 0
    failed_calls: int = 0
    skipped: int = 0  # left to their title tags because the budget ran out
    fetched: int = 0  # real thumbnail fetches this step made
    cache_hits: int = 0  # thumbnails already in the cache (fingerprints, earlier rounds)
    seconds: float = 0.0
    budget_s: float = PHOTO_TAG_BUDGET_S
    fallback: bool = False  # true when title tags were used for some candidates

    def as_dict(self) -> dict[str, Any]:
        return {
            "wanted": self.wanted, "with_image": self.with_image, "tagged": self.tagged,
            "calls": self.calls, "failed_calls": self.failed_calls, "skipped": self.skipped,
            "thumbnails_fetched": self.fetched, "thumbnails_cached": self.cache_hits,
            "seconds": round(self.seconds, 2), "budget_s": self.budget_s,
            "title_tags_for_the_rest": self.fallback,
        }  # fmt: skip


def merge_tags(
    old: Mapping[str, str | None] | None, photo: Mapping[str, str | None]
) -> dict[str, str | None]:
    """The photo's value for every dimension it shows, the earlier tag's for the rest."""
    merged: dict[str, str | None] = dict(old or {})
    for dim, value in photo.items():
        if dim == CUES_KEY:
            continue
        if dim.startswith(NOTE_PREFIX):  # a free note's match flag: the newest answer stands
            merged[dim] = value
            continue
        if value:
            merged[dim] = value
        else:
            merged.setdefault(dim, None)
    return merged


def photo_tag_top(
    candidates: list[Taggable],
    book: TagBook,
    budget: Budget,
    clock: Clock,
    config: Config,
    cache: ThumbCache,
    tag_source: dict[str, str],
    timeline: Timeline | None = None,
    time_left_s: float | None = None,
    use_images: bool = True,
    limit: int | None = None,
) -> PhotoReport:
    """Tag the first `shop_photo_tag_top_n` of `candidates` (best ranked first) from title and
    photo. Updates `book.new` and `tag_source` ("photo"); never raises except on a budget error."""
    report = PhotoReport(budget_s=PHOTO_TAG_BUDGET_S)
    limit = config.shop_photo_tag_top_n if limit is None else limit
    flags = [note_key(i) for i, _ in book.notes]  # a free note needs its flag on every candidate

    def needs_photo(c: Taggable) -> bool:
        return (
            use_images
            and c.thumbnail_url.startswith("https://")
            and tag_source.get(c.id) != "photo"
        )

    def needs_flags(c: Taggable) -> bool:
        have = book.get(c.id) or {}
        return any(k not in have for k in flags)

    wanted = [c for c in candidates[:limit] if needs_photo(c) or needs_flags(c)]
    report.wanted = len(wanted)
    if not wanted:
        return report
    started = time.monotonic()
    allowed = (
        PHOTO_TAG_BUDGET_S
        if time_left_s is None
        else max(0.0, min(PHOTO_TAG_BUDGET_S, time_left_s))
    )
    deadline = started + allowed
    hits_before, fetches_before = cache.hits, cache.fetches
    closed = threading.Event()
    pool = ThreadPoolExecutor(max_workers=PHOTO_TAG_WORKERS + 6, thread_name_prefix="phototag")
    try:
        # the images first (cached ones are free), all at once, inside the budget
        image_futures = {
            c.id: pool.submit(cache.image, c.thumbnail_url)
            for c in wanted
            if use_images and c.thumbnail_url.startswith("https://")
        }
        wait(list(image_futures.values()), timeout=max(0.0, deadline - time.monotonic()))
        images: dict[str, PreparedImage] = {}
        for c in wanted:
            fut = image_futures.get(c.id)
            if fut is not None and fut.done():
                got = fut.result()
                if got is not None:
                    images[c.id] = got
        report.with_image = len(images)
        with_images = [c for c in wanted if c.id in images]
        without = [c for c in wanted if c.id not in images and needs_flags(c)]  # flags from titles
        chunk = PHOTO_TAG_BATCH
        batches = [with_images[i : i + chunk] for i in range(0, len(with_images), chunk)]
        batches += [without[i : i + chunk] for i in range(0, len(without), chunk)]

        def one(
            batch: list[Taggable],
        ) -> tuple[list[Taggable], dict[int, dict[str, str | None]], bool, bool]:
            """(batch, rows, failed, ran): a batch that never started (no time left) is skipped,
            not a failed call."""
            remaining = deadline - time.monotonic()
            if remaining <= 0.2 or closed.is_set():
                return batch, {}, False, False
            rows, entry = run_call(
                batch, book, budget, MODE_TOOL, False, clock,
                max(1.0, min(config.shop_tag_call_timeout_s, remaining)),
                images={c.id: images[c.id] for c in batch} if batch[0].id in images else None,
                timeline=timeline,
            )  # fmt: skip
            return batch, rows, bool(entry["error"]) or not rows, True

        workers = ThreadPoolExecutor(max_workers=PHOTO_TAG_WORKERS, thread_name_prefix="phototags")
        futures = [workers.submit(one, b) for b in batches]
        done, pending = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
        closed.set()  # whatever is still running is abandoned: its answer is not used
        workers.shutdown(wait=False, cancel_futures=True)
        for job in done:
            batch, rows, failed, ran = job.result()
            if not ran:
                report.skipped += len(batch)
                continue
            report.calls += 1
            if failed:
                report.failed_calls += 1
            with book.lock:
                for n, tags in rows.items():
                    item = batch[n - 1]
                    book.new[item.id] = merge_tags(book.get(item.id), tags)
                    if item.id in images:
                        tag_source[item.id] = "photo"
                    report.tagged += 1
        report.skipped += sum(len(b) for f, b in zip(futures, batches, strict=True) if f in pending)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    report.fetched = cache.fetches - fetches_before
    report.cache_hits = cache.hits - hits_before
    report.seconds = time.monotonic() - started
    report.fallback = report.tagged < report.wanted
    return report
