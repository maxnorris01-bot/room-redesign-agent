"""Stage 2q, phase 1: the photo tagging budget. The post-2p live re-run showed case-04 round 1
with `wanted 36, tagged 0, calls 0, skipped 36, seconds 6.01` and every thumbnail already cached:
the three calls had started, were still running when the 6 s budget ended, were abandoned and were
counted as skipped, so the trace read as if nothing had started. Offline: a stub model with
delays, a counting thumbnailer, a scripted search."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.llm import Budget, Completion
from app.shop import candidates as cands
from app.shop import phototags as pt
from app.shop.clock import Clock
from app.shop.providers import ShopFixtureLookup
from app.shop.service import ShopService
from app.shop.store import SessionStore
from app.shopping import SearchCandidate
from app.thumbnails import ThumbResult
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)
from test_shop_stage2p_typed import FakeThumbs, taggables


class SlowModel:
    """The tagging call, with a delay per call and a scripted failure for the first N calls."""

    def __init__(self, delay: float = 0.0, fail_calls: int = 0) -> None:
        self.delay = delay
        self.fail_calls = fail_calls
        self.starts: list[float] = []
        self.lock = threading.Lock()
        self.began = time.monotonic()

    def __call__(self, **kw: Any) -> Completion:
        with self.lock:
            self.starts.append(time.monotonic() - self.began)
            n = len(self.starts)
        time.sleep(self.delay)
        if n <= self.fail_calls:
            raise RuntimeError("tagging call failed")
        rows = [ln for ln in kw["user_text"].splitlines() if re.match(r"^\d+ \| ", ln)]
        items = [
            {"index": int(ln.split(" | ")[0]), "shape": None, "material": None, "finish": None,
             "frame_color": None, "style": [], "color": "navy_dark_blue", "quality_cues": []}
            for ln in rows
        ]  # fmt: skip
        return Completion(text="", tool_input={"items": items}, cost_usd=0.0001, input_tokens=100,
                          output_tokens=50, usage_known=True)  # fmt: skip


def run_step(
    monkeypatch: pytest.MonkeyPatch, model: SlowModel, n: int = 36, budget_s: float = 0.4,
    cached: bool = True, thumbs: FakeThumbs | None = None, **kw: Any,
) -> tuple[pt.PhotoReport, pt.ThumbCache, FakeThumbs]:  # fmt: skip
    monkeypatch.setattr(cands, "complete", model)
    monkeypatch.setattr(pt, "PHOTO_TAG_BUDGET_S", budget_s)
    cfg = Config(llm_mode="mock", tracing_disabled=True, shop_photo_tag_top_n=36)
    thumbs = thumbs or FakeThumbs()
    cache = pt.ThumbCache(thumbs, cfg)
    items = taggables(n)
    if cached:
        for it in items:
            cache.image(it.thumbnail_url)
    report = pt.photo_tag_top(items, cands.TagBook(), Budget(cfg), Clock(), cfg, cache, {}, **kw)
    return report, cache, thumbs


# ---- the reproduction (written first; it failed on the 2p code) --------------


def test_calls_still_running_when_the_budget_ends_are_not_reported_as_nothing_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case-04 round 1: thumbnails cached, three calls that take longer than the budget. 2p
    abandoned all three at the deadline and reported calls 0, skipped 36."""
    model = SlowModel(delay=0.7)
    report, _, _ = run_step(monkeypatch, model, budget_s=0.4)
    assert len(model.starts) == 3  # the first wave started at once (this part already held)
    assert report.calls_started == 3
    assert report.calls == 3 and report.tagged == 36 and report.skipped == 0  # they were let finish
    assert report.fallback is False and report.reason == ""


def test_the_budget_is_measured_from_the_first_call_not_from_the_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thumbnails that are slow to fetch (a thin first search that has not cached them) used to
    spend the budget before any call began; now the calls get their own budget."""
    thumbs = FakeThumbs()
    real = thumbs.fetch

    def slow(url: str) -> ThumbResult:
        time.sleep(0.15)
        return real(url)

    thumbs.fetch = slow  # type: ignore[method-assign]
    model = SlowModel(delay=0.05)
    report, _, _ = run_step(monkeypatch, model, n=36, budget_s=0.4, cached=False, thumbs=thumbs)
    assert report.calls == 3 and report.tagged == 36 and report.skipped == 0
    assert min(model.starts) >= 0.15  # no call began before the images were in
    assert report.seconds > 0.4  # the step took longer than the call budget; the calls did not


def test_a_thin_first_search_with_slow_variant_searches_still_runs_the_photo_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The case-04 shape end to end: a thin base search, two variant searches that each take a
    while, then the photo step with calls longer than its budget."""

    class SlowSearch:
        name = "serper"
        thread_safe = True

        def __init__(self) -> None:
            self.calls: list[str] = []

        def search(
            self, query: str, gl: str = "us", hl: str = "en", page: int = 1
        ) -> list[SearchCandidate]:
            self.calls.append(query)
            time.sleep(0.2)
            base = query == "walnut dining chair"
            n = 10 if base and page == 1 else (0 if base else 12)
            tag = re.sub(r"[^a-z0-9]", "", query)[:8]
            return [
                SearchCandidate(f"{tag}-{page}-{i}", f"Walnut Dining Chair {tag} {i}",
                                f"Seller {i % 9}", 60.0 + i, "USD", None, None,
                                "https://www.google.com/search?q=x",
                                thumbnail_url=f"https://img.example/{tag}-{page}-{i}.jpg")
                for i in range(n)
            ]  # fmt: skip

    model = SlowModel(delay=0.7)
    monkeypatch.setattr(cands, "complete", model)
    monkeypatch.setattr(pt, "PHOTO_TAG_BUDGET_S", 0.4)
    cfg = Config(tracing_disabled=True, shop_thin_variants=True, shop_photo_tag_top_n=36)
    store = SessionStore(tmp_path / "d")
    svc = ShopService(cfg, store, SlowSearch(), ShopFixtureLookup(), FakeThumbs())
    view = svc.search(None, "walnut dining chair")["search"]
    assert view["trace"]["thin"]["variants"]  # the thin path ran
    photo = view["trace"]["typed"]["photo"] if "typed" in view["trace"] else view["trace"]["photo"]
    assert photo["calls_started"] >= 1 and photo["calls"] == photo["calls_started"]
    assert photo["tagged"] > 0 and photo["skipped"] == 0


# ---- the fix ------------------------------------------------------------------


def test_the_first_wave_always_starts_even_when_the_budget_is_tiny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = SlowModel(delay=0.1)
    report, _, _ = run_step(monkeypatch, model, budget_s=0.05)
    assert report.calls_started == 3 and len(model.starts) == 3


def test_a_call_that_never_finishes_is_abandoned_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pt, "PHOTO_FIRST_WAVE_GRACE_S", 0.2)
    gate = threading.Event()
    model = SlowModel(delay=0.0)
    orig = model.__call__

    def hang(**kw: Any) -> Completion:
        gate.wait(timeout=3)
        return orig(**kw)

    run_model: Any = hang
    monkeypatch.setattr(cands, "complete", run_model)
    monkeypatch.setattr(pt, "PHOTO_TAG_BUDGET_S", 0.2)
    cfg = Config(llm_mode="mock", tracing_disabled=True, shop_photo_tag_top_n=36)
    cache = pt.ThumbCache(FakeThumbs(), cfg)
    report = pt.photo_tag_top(taggables(24), cands.TagBook(), Budget(cfg), Clock(), cfg, cache, {})
    gate.set()
    assert report.calls_started == 2 and report.calls == 0 and report.abandoned_calls == 2
    assert report.skipped == 24 and report.fallback is True
    assert "2 calls" in report.reason and "still running" in report.reason
    assert report.as_dict()["reason"] == report.reason and report.as_dict()["abandoned_calls"] == 2


def test_a_batch_that_cannot_start_before_the_budget_ends_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four batches, three workers, a call longer than the budget: the fourth never starts."""
    model = SlowModel(delay=0.5)
    report, _, _ = run_step(monkeypatch, model, n=48, budget_s=0.3, limit=48)
    assert report.calls_started == 3 and report.calls == 3 and report.tagged == 36
    assert report.skipped == 12 and "not started" in report.reason and "12" in report.reason


def test_a_failed_call_is_retried_once_when_budget_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = SlowModel(delay=0.0, fail_calls=1)
    report, _, _ = run_step(monkeypatch, model, n=24, budget_s=6.0)
    assert report.failed_calls == 0 and report.retries == 1  # the retry answered
    assert report.calls == 2 and report.tagged == 24 and report.as_dict()["retries"] == 1
    assert len(model.starts) == 3


def test_a_call_that_fails_twice_is_not_retried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    model = SlowModel(delay=0.0, fail_calls=2)  # the first batch fails, and so does its retry
    report, _, _ = run_step(monkeypatch, model, n=12, budget_s=6.0)
    assert report.retries == 1 and report.failed_calls == 1 and report.tagged == 0
    assert len(model.starts) == 2 and report.fallback is True


def test_no_retry_when_the_budget_is_spent(monkeypatch: pytest.MonkeyPatch) -> None:
    model = SlowModel(delay=0.0, fail_calls=1)
    report, _, _ = run_step(monkeypatch, model, n=12, budget_s=0.5)  # under the retry minimum
    assert report.retries == 0 and report.failed_calls == 1 and len(model.starts) == 1


def test_a_thumbnail_is_never_fetched_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    model = SlowModel(delay=0.0)
    report, cache, thumbs = run_step(monkeypatch, model, n=24, budget_s=6.0, cached=False)
    assert sorted(thumbs.calls) == sorted(set(thumbs.calls)) and len(thumbs.calls) == 24
    pt.photo_tag_top(taggables(24), cands.TagBook(), Budget(cache.config), Clock(), cache.config,
                     cache, {})  # fmt: skip
    assert len(thumbs.calls) == 24 and cache.hits >= 24


def test_a_hard_stop_with_no_time_left_starts_nothing_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = SlowModel()
    report, _, _ = run_step(monkeypatch, model, n=12, time_left_s=0.0)
    assert report.calls_started == 0 and report.fallback and "hard stop" in report.reason
