"""Stage 2r, phase 5: a tagging call is never given less than MIN_TAG_CALL_TIMEOUT_S (the post-2p
run had "APITimeoutError after 1.0 s"); one that cannot have it is not started; a timeout is
retried once when that much time remains."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest

from app.config import Config
from app.llm import Budget, Completion
from app.shop import candidates as cands
from app.shop import phototags as pt
from app.shop.clock import Clock
from app.shop.thresholds import MIN_TAG_CALL_TIMEOUT_S
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)
from test_shop_stage2g import FakeClock, taggables
from test_shop_stage2p_typed import FakeThumbs
from test_shop_stage2p_typed import taggables as photo_taggables


def answer(kw: dict[str, Any]) -> Completion:
    rows = [ln for ln in kw["user_text"].splitlines() if re.match(r"^\d+ \| ", ln)]
    items = [
        {"index": int(ln.split(" | ")[0]), "shape": "round", "material": None, "finish": None,
         "frame_color": None, "style": [], "color": None}
        for ln in rows
    ]  # fmt: skip
    return Completion(text="", tool_input={"items": items}, cost_usd=0.0001, input_tokens=10,
                      output_tokens=5, usage_known=True)  # fmt: skip


def timeout_error() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(request=SimpleNamespace(method="POST"))  # type: ignore[arg-type]


class Spy:
    def __init__(self, fail_first: int = 0) -> None:
        self.timeouts: list[float] = []
        self.fail_first = fail_first

    def __call__(self, **kw: Any) -> Completion:
        self.timeouts.append(kw["timeout_s"])
        if len(self.timeouts) <= self.fail_first:
            raise timeout_error()
        return answer(kw)


def test_the_minimum_is_eight_seconds() -> None:
    assert MIN_TAG_CALL_TIMEOUT_S == 8.0


def test_a_call_with_less_than_the_minimum_left_is_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = Spy()
    monkeypatch.setattr(cands, "complete", spy)
    clock = FakeClock()
    book = cands.TagBook()
    cfg = Config(llm_mode="mock", tracing_disabled=True)
    cands.tag_results(taggables(5), book, Budget(cfg), clock, workers=1, until=clock.now() + 1.0)
    assert spy.timeouts == [] and book.calls == 0 and book.skipped == 5  # no "after 1.0 s" call


def test_a_configured_timeout_below_the_minimum_is_raised_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = Spy()
    monkeypatch.setattr(cands, "complete", spy)
    cfg = Config(llm_mode="mock", tracing_disabled=True, shop_tag_call_timeout_s=1.0)
    cands.tag_results(taggables(5), cands.TagBook(), Budget(cfg), FakeClock(), workers=1)
    assert spy.timeouts == [MIN_TAG_CALL_TIMEOUT_S]


def test_a_timeout_is_retried_once_when_time_remains(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(fail_first=1)
    monkeypatch.setattr(cands, "complete", spy)
    cfg = Config(llm_mode="mock", tracing_disabled=True)
    book = cands.TagBook()
    cands.tag_results(taggables(10), book, Budget(cfg), FakeClock(), workers=1)
    assert len(spy.timeouts) >= 2 and all(t >= MIN_TAG_CALL_TIMEOUT_S for t in spy.timeouts)
    assert any(e["retry"] for e in book.log) and book.tagged == 10  # the retry answered


def test_a_second_timeout_is_not_retried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(fail_first=99)
    monkeypatch.setattr(cands, "complete", spy)
    cfg = Config(llm_mode="mock", tracing_disabled=True)
    book = cands.TagBook()
    cands.tag_results(taggables(4), book, Budget(cfg), FakeClock(), workers=1)
    assert book.tagged == 0 and book.failures  # tried, retried once in pieces, gave up
    assert len(spy.timeouts) <= 1 + 1 and all(t >= MIN_TAG_CALL_TIMEOUT_S for t in spy.timeouts)


def test_a_photo_call_gets_the_minimum_too_and_no_retry_without_the_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = Spy(fail_first=1)
    monkeypatch.setattr(cands, "complete", spy)
    monkeypatch.setattr(pt, "PHOTO_TAG_BUDGET_S", 6.0)
    cfg = Config(llm_mode="mock", tracing_disabled=True, shop_photo_tag_top_n=36,
                 shop_tag_call_timeout_s=1.0)  # fmt: skip
    report = pt.photo_tag_top(photo_taggables(12), cands.TagBook(), Budget(cfg), Clock(), cfg,
                              pt.ThumbCache(FakeThumbs(), cfg), {})  # fmt: skip
    assert spy.timeouts[0] == MIN_TAG_CALL_TIMEOUT_S
    # the first call timed out; the retry needs 8 s and the budget plus grace leaves 10: it ran
    assert report.retries in (0, 1) and all(t >= MIN_TAG_CALL_TIMEOUT_S for t in spy.timeouts)


def test_the_photo_step_does_not_start_when_the_hard_stop_leaves_less_than_the_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = Spy()
    monkeypatch.setattr(cands, "complete", spy)
    cfg = Config(llm_mode="mock", tracing_disabled=True, shop_photo_tag_top_n=36)
    report = pt.photo_tag_top(photo_taggables(6), cands.TagBook(), Budget(cfg), Clock(), cfg,
                              pt.ThumbCache(FakeThumbs(), cfg), {}, time_left_s=5.0)  # fmt: skip
    assert spy.timeouts == [] and report.calls_started == 0 and "hard stop" in report.reason
