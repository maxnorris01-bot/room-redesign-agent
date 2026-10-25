"""Model-written search queries for a Shop Again (ADR 0012, stage 2i amendment).

After the evidence step, one small forced tool call (`record_queries`, prompt `shop_queries` v1,
strict schema, thinking disabled, about 300 output tokens) writes one or two search queries from
the typed constraints and what the shopper's ratings lean toward and away from. The model
describes; code validates every query and drops the ones that break a rule, noting why in the
trace: the category word must be present, every must-have and typed material too, no URL, no
price, no retailer that is not one the shopper liked, at most `QUERY_MAX_WORDS` words. Accepted
queries run as page-1 searches. A failed or empty call is recorded and the Shop Again goes on
without it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.config import Config
from app.llm import Budget, LLMOutputError, complete
from app.prompts import load_prompt
from app.shop.models import MAX_SEARCH_TEXT_CHARS, ShopQuery
from app.shop.retailers import RETAILERS, seller_key
from app.shop.textutil import normalize_phrase
from app.shop.thresholds import (
    MODEL_QUERIES,
    QUERY_DISLIKED_TITLES,
    QUERY_LIKED_TITLES,
    QUERY_MAX_WORDS,
)

QUERY_TOOL = "record_queries"
QUERY_MAX_TOKENS = 300
QUERY_TOOL_DEF: dict[str, Any] = {
    "name": QUERY_TOOL,
    "description": "Record one or two shopping search queries.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
        "required": ["queries"],
        "additionalProperties": False,
    },
}
_URLISH = re.compile(r"https?:|www\.|\.com\b|\.net\b|\.org\b|//|@", flags=re.IGNORECASE)
_PRICEISH = re.compile(r"[$€£]|\b(?:under|below|cheap|cheapest|budget)\b", flags=re.IGNORECASE)


@dataclass
class QueryInput:
    """Everything the writer sees (and the validator needs)."""

    base: ShopQuery
    adjusted: ShopQuery
    converged: dict[str, str] = field(default_factory=dict)
    toward: list[str] = field(default_factory=list)  # "natural wood (frame color)"
    away: list[str] = field(default_factory=list)
    liked_sellers: list[str] = field(default_factory=list)
    liked_titles: list[str] = field(default_factory=list)
    disliked_titles: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)  # queries already planned (normalized)


@dataclass
class QueryResult:
    accepted: list[str] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)  # {"query", "reason"}
    error: str = ""
    usd: float = 0.0
    asked: bool = False


def _singular(word: str) -> str:
    w = word.lower()
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _has(words: set[str], term: str) -> bool:
    parts = [_singular(w) for w in _words(term) if len(w) >= 3]
    return all(p in words for p in parts) if parts else True


def user_text(inp: QueryInput) -> str:
    q = inp.adjusted

    def line(label: str, values: list[str]) -> str:
        return f"{label}: {', '.join(values) if values else 'none'}"

    lines = [
        f"Shopper request: {inp.base.request}",
        f"Category: {q.category or 'unknown'}",
        f"Search words: {q.search_text}",
        line("Must have", q.must_have),
        line("Typed materials", q.material_terms),
        line("Avoid", q.avoid),
        "Settled values: "
        + ("; ".join(f"{d.replace('_', ' ')}: {v}" for d, v in inp.converged.items()) or "none"),
        line("Leaning toward", inp.toward),
        line("Leaning away from", inp.away),
        line("Liked retailers (you may name one)", inp.liked_sellers),
        "Titles the shopper liked:",
        *[f"  * {t[:100]}" for t in inp.liked_titles[:QUERY_LIKED_TITLES]],
        "Titles the shopper disliked:",
        *[f"  * {t[:100]}" for t in inp.disliked_titles[:QUERY_DISLIKED_TITLES]],
        f"Write one or two queries of at most {QUERY_MAX_WORDS} words.",
    ]
    return "\n".join(lines)


def validate_query(text: str, inp: QueryInput) -> tuple[str, str]:
    """(the cleaned query, "") when it passes, else ("", the reason it was dropped)."""
    cleaned = " ".join(text.replace('"', " ").replace("'", "'").split())
    if not cleaned:
        return "", "empty"
    if _URLISH.search(cleaned):
        return "", "contains a URL or website"
    if _PRICEISH.search(cleaned):
        return "", "contains a price or price word"
    words = _words(cleaned)
    if not 2 <= len(words) <= QUERY_MAX_WORDS:
        return "", f"{len(words)} words, needs 2 to {QUERY_MAX_WORDS}"
    if len(cleaned) > MAX_SEARCH_TEXT_CHARS:
        return "", "too long"
    present = {_singular(w) for w in words}
    q = inp.adjusted
    if q.category:
        head = _words(q.category)[-1]
        if _singular(head) not in present:
            return "", f"the category word '{head}' is missing"
    for term in [*q.must_have, *q.material_terms]:
        if not _has(present, term):
            return "", f"the typed constraint '{term}' is missing"
    for term in q.avoid:
        if _has(present, term):
            return "", f"it uses the avoided word '{term}'"
    liked = {seller_key(s) for s in inp.liked_sellers}
    norm = f" {normalize_phrase(cleaned)} "
    for r in RETAILERS:
        names = {normalize_phrase(r.name), normalize_phrase(r.search_name)}
        names |= {normalize_phrase(a) for a in r.aliases}
        if seller_key(r.name) in liked:
            continue
        if any(n and f" {n} " in norm for n in names):
            return "", f"it names {r.name}, which you have not liked"
    return cleaned, ""


def write_queries(inp: QueryInput, budget: Budget, config: Config | None = None) -> QueryResult:
    """Ask the model for queries and validate them. Never raises except on a budget error."""
    config = config or budget.config
    result = QueryResult(asked=True)
    before = budget.cost_usd
    try:
        completion = complete(
            span_name="llm.shop_queries",
            prompt=load_prompt("shop_queries"),
            model=config.rank_model,
            user_text=user_text(inp),
            budget=budget,
            max_tokens=QUERY_MAX_TOKENS,
            tools=[QUERY_TOOL_DEF],
            tool_choice={"type": "tool", "name": QUERY_TOOL},
            stream=True,
            timeout_s=config.shop_tag_call_timeout_s,
            disable_thinking=True,
        )
        raw = (completion.tool_input or {}).get("queries", [])
    except LLMOutputError as exc:
        result.error = str(exc)[:160]
        result.usd = round(budget.cost_usd - before, 6)
        return result
    result.usd = round(budget.cost_usd - before, 6)
    seen = {" ".join(_words(e)) for e in inp.existing}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        cleaned, why = validate_query(item, inp)
        if not why and " ".join(_words(cleaned)) in seen:
            why = "the same as a search already planned"
        if why:
            result.dropped.append({"query": item[:80], "reason": why})
            continue
        if len(result.accepted) >= MODEL_QUERIES:
            result.dropped.append({"query": item[:80], "reason": "more than the limit"})
            continue
        seen.add(" ".join(_words(cleaned)))
        result.accepted.append(cleaned)
    return result
