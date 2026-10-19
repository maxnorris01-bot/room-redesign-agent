"""The one pricing table. Nothing else in the repo may hardcode a per-token or per-search price.

Source: https://platform.claude.com/docs/en/about-claude/pricing (model table, long-context
section) and https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool
("Usage and pricing": $10 per 1,000 searches). Checked 2026-10-18 (fetched that day by the
spike-skeleton session). Re-check before trusting a live number: prices change.

The API returns token counts but no dollar figure, so every dollar amount in this repo is an
*estimate* built from `usage` plus these values. Cache writes below are the 5-minute TTL rate; the
1-hour rate is higher and is not modeled because nothing here requests it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Rates:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write_5m: float


@dataclass(frozen=True)
class ModelPrice:
    rates: Rates
    # Claude Haiku 5.5 is priced by prompt length: a request whose prompt (input + cache reads +
    # cache writes) is over the threshold pays the `long_rates` for the whole request.
    long_threshold: int | None = None
    long_rates: Rates | None = None


PRICING: dict[str, ModelPrice] = {
    "claude-fable-5-1": ModelPrice(Rates(10.0, 50.0, 0.25, 12.5)),
    "claude-opus-5-5": ModelPrice(Rates(4.0, 20.0, 0.20, 5.0)),
    "claude-opus-5": ModelPrice(Rates(5.0, 25.0, 0.50, 6.25)),
    "claude-sonnet-5-5": ModelPrice(Rates(2.0, 10.0, 0.10, 2.5)),
    "claude-sonnet-5": ModelPrice(Rates(2.0, 10.0, 0.20, 2.5)),
    "claude-sonnet-4-6": ModelPrice(Rates(3.0, 15.0, 0.30, 3.75)),
    "claude-haiku-5-5": ModelPrice(
        Rates(0.10, 0.50, 0.01, 0.125),
        long_threshold=100_000,
        long_rates=Rates(0.50, 2.50, 0.05, 0.625),
    ),
    "claude-haiku-4-5": ModelPrice(Rates(1.0, 5.0, 0.10, 1.25)),
}

# Web search is billed per search on top of tokens ($10 per 1,000). Failed searches are not billed
# by the API, but `usage` counts requests, so this slightly over-estimates when searches error.
WEB_SEARCH_USD_PER_REQUEST = 0.01


def most_expensive_rates() -> Rates:
    """Fallback for a model missing from the table, so the cost cap errs conservative."""
    return max((p.rates for p in PRICING.values()), key=lambda r: (r.input, r.output))


def is_priced(model: str) -> bool:
    return model in PRICING


def rates_for(model: str, prompt_tokens: int) -> Rates:
    price = PRICING.get(model)
    if price is None:
        return most_expensive_rates()
    if (
        price.long_threshold is not None
        and price.long_rates is not None
        and prompt_tokens > price.long_threshold
    ):
        return price.long_rates
    return price.rates


def web_searches(usage: Any) -> int:
    server_tool_use = getattr(usage, "server_tool_use", None)
    return int(getattr(server_tool_use, "web_search_requests", 0) or 0)


def estimate_cost(
    model: str, usage: Any, *, web_search_usd: float = WEB_SEARCH_USD_PER_REQUEST
) -> float:
    """Estimated USD for one response, from API-reported usage plus a per-search estimate."""
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    rates = rates_for(model, usage.input_tokens + cache_read + cache_write)
    token_cost = (
        usage.input_tokens * rates.input
        + cache_read * rates.cache_read
        + cache_write * rates.cache_write_5m
        + usage.output_tokens * rates.output
    ) / 1_000_000
    return float(token_cost + web_searches(usage) * web_search_usd)
