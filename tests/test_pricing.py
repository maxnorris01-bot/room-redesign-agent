from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.pricing import PRICING, estimate_cost, is_priced, most_expensive_rates


def _usage(
    inp: int, out: int, *, read: int = 0, write: int = 0, searches: int = 0
) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=read,
        cache_creation_input_tokens=write,
        server_tool_use=SimpleNamespace(web_search_requests=searches),
    )


def test_tokens_only() -> None:
    # sonnet 5.5: $2 in / $10 out per MTok
    cost = estimate_cost("claude-sonnet-5-5", _usage(10_000, 2_000))
    assert cost == pytest.approx(0.02 + 0.02)


def test_searches_are_billed_on_top_and_configurable() -> None:
    usage = _usage(0, 0, searches=3)
    assert estimate_cost("claude-sonnet-5-5", usage) == pytest.approx(0.03)  # $10 / 1,000
    assert estimate_cost("claude-sonnet-5-5", usage, web_search_usd=0.02) == pytest.approx(0.06)


def test_cache_read_and_write_rates() -> None:
    usage = _usage(0, 0, read=1_000_000, write=1_000_000)
    # sonnet 5.5: cache read $0.10, 5-minute cache write $2.50 per MTok
    assert estimate_cost("claude-sonnet-5-5", usage) == pytest.approx(2.60)


def test_haiku_55_long_prompt_tier_applies_to_whole_request() -> None:
    short = estimate_cost("claude-haiku-5-5", _usage(100_000, 1_000_000))
    assert short == pytest.approx(0.01 + 0.5)  # exactly at the threshold is still the short tier
    long = estimate_cost("claude-haiku-5-5", _usage(100_001, 1_000_000))
    assert long == pytest.approx(100_001 * 0.5 / 1e6 + 2.5)
    # cache reads count toward prompt length
    cached = estimate_cost("claude-haiku-5-5", _usage(1, 0, read=100_000))
    assert cached == pytest.approx((1 * 0.5 + 100_000 * 0.05) / 1e6)


def test_unknown_model_is_priced_conservatively_not_free() -> None:
    assert not is_priced("claude-future-9")
    worst = most_expensive_rates()
    assert estimate_cost("claude-future-9", _usage(1_000_000, 0)) == pytest.approx(worst.input)
    assert all(worst.input >= p.rates.input for p in PRICING.values())


def test_missing_optional_usage_fields_do_not_crash() -> None:
    bare = SimpleNamespace(input_tokens=1000, output_tokens=1000)
    assert estimate_cost("claude-haiku-4-5", bare) == pytest.approx(0.001 + 0.005)
