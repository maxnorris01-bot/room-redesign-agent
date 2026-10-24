"""Request parsing: one small-model call turns free text into a `ShopQuery` (prompt shop_parse).

The model proposes; code decides what is kept. Prices must be sane numbers, term lists are
lowercased, deduplicated and bounded, price words are stripped from the search text, and the known
category key comes from `categories.py`, never from the model.
"""

from __future__ import annotations

import re

from app.categories import known_category
from app.llm import Budget, Completion, LLMOutputError, complete
from app.prompts import load_prompt
from app.schemas import SchemaError, parse_model, to_output_schema
from app.shop.models import (
    MAX_SEARCH_TEXT_CHARS,
    MAX_TERM_CHARS,
    MAX_TERMS,
    ShopParse,
    ShopQuery,
    clean_request,
)

PARSE_MAX_TOKENS = 600
MAX_PRICE_USD = 1_000_000.0
_MONEY = r"\$?\s*\d[\d,]*(?:\.\d+)?\s*(?:k\b|dollars?\b|bucks\b|usd\b)?"
_PRICE_PHRASE = re.compile(
    rf"\b(?:under|below|over|above|less than|more than|at most|at least|up to|around|about"
    rf"|between|from|max|min)\s+{_MONEY}(?:\s*(?:and|to|-)\s*{_MONEY})?",
    re.IGNORECASE,
)
_BARE_MONEY = re.compile(r"\$\s*\d[\d,]*(?:\.\d+)?", re.IGNORECASE)
_PRICE_WORDS = re.compile(
    r"\b(?:cheap|cheapest|cheaply|affordable|inexpensive|budget|expensive|luxury)\b", re.IGNORECASE
)


def strip_price_words(text: str) -> str:
    """Remove "under $1000", "between 300 and 600", "$50" and "cheap" from a search phrase."""
    out = _PRICE_PHRASE.sub(" ", text)
    out = _BARE_MONEY.sub(" ", out)
    out = _PRICE_WORDS.sub(" ", out)
    return " ".join(out.split())


def _price(value: float | None) -> float | None:
    if value is None or value != value or value <= 0 or value > MAX_PRICE_USD:
        return None
    return round(float(value), 2)


def _terms(values: list[str]) -> list[str]:
    out: list[str] = []
    for raw in values:
        term = " ".join(raw.lower().split())[:MAX_TERM_CHARS].strip(" .,;:")
        if term and term not in out:
            out.append(term)
    return out[:MAX_TERMS]


def build_query(request: str, parsed: ShopParse) -> ShopQuery:
    """The `ShopQuery` for a model's parse of `request`, with every field sanitized."""
    price_min, price_max = _price(parsed.price_min), _price(parsed.price_max)
    if price_min is not None and price_max is not None and price_min > price_max:
        price_min, price_max = price_max, price_min
    category = " ".join(parsed.category.lower().split())[:MAX_TERM_CHARS]
    search_text = strip_price_words(" ".join(parsed.search_text.split()))[:MAX_SEARCH_TEXT_CHARS]
    search_text = search_text or strip_price_words(request)[:MAX_SEARCH_TEXT_CHARS] or category
    return ShopQuery(
        request=request,
        search_text=search_text.strip(),
        category=category,
        category_key=known_category(category) if category else None,
        price_min=price_min,
        price_max=price_max,
        style_terms=_terms(parsed.style_terms),
        material_terms=_terms(parsed.material_terms),
        must_have=_terms(parsed.must_have),
        avoid=_terms(parsed.avoid),
    )


def fallback_query(request: str) -> ShopQuery:
    """The request as typed, with no filters: used when the parse call's output is unusable."""
    text = strip_price_words(request)[:MAX_SEARCH_TEXT_CHARS] or request[:MAX_SEARCH_TEXT_CHARS]
    return ShopQuery(request=request, search_text=text, category="")


def parse_request(
    request: str, budget: Budget, version: str | None = None
) -> tuple[ShopQuery, Completion | None, str]:
    """Parse `request`. Returns (query, completion, warning); the warning is empty when all is well.

    An unusable model output degrades to `fallback_query` with a warning. A budget error propagates.
    """
    cleaned = clean_request(request)
    config = budget.config
    try:
        completion = complete(
            span_name="llm.shop_parse",
            prompt=load_prompt("shop_parse", version),
            model=config.model_retrieval,
            user_text=f"<request>{cleaned}</request>",
            budget=budget,
            max_tokens=PARSE_MAX_TOKENS,
            schema=to_output_schema(ShopParse),
            effort=config.effort_retrieval,
        )
        parsed = parse_model(ShopParse, completion.text, what="shop_parse")
    except (LLMOutputError, SchemaError) as exc:
        return (
            fallback_query(cleaned),
            None,
            f"Could not interpret the request ({exc}); searched your words as typed.",
        )
    return build_query(cleaned, parsed), completion, ""
