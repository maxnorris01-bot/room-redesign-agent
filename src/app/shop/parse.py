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
    CONTEXT_KINDS,
    MAX_CONTEXT,
    MAX_SEARCH_TEXT_CHARS,
    MAX_TERM_CHARS,
    MAX_TERMS,
    ContextItem,
    ShopParse,
    ShopQuery,
    clean_request,
)
from app.shop.vocab import COLOR_LABELS, OTHER, normalize

PARSE_MAX_TOKENS = 900  # stage 2k: 600, plus the context list and the colour suggestions
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


MAX_SUGGESTED = 4
MAX_AVOID = 3
COLOR_CONTEXT_KINDS = ("room_color", "wall_color", "existing_item")  # these can suggest colours
_REASON_CHARS = 140


def color_ids(values: list[str]) -> tuple[list[str], list[str]]:
    """(colour ids in the fixed vocabulary, the words that were not colours we know). Each value is
    folded through `app.shop.vocab`; `other` and anything unknown is returned as a leftover."""
    ids: list[str] = []
    left: list[str] = []
    for raw in values:
        text = " ".join(raw.lower().replace("_", " ").split())[:MAX_TERM_CHARS]
        if not text:
            continue
        value = normalize("color", text)
        if value is None or value == OTHER:
            left.append(text)
        elif value not in ids:
            ids.append(value)
    return ids, left


def _context(parsed: ShopParse) -> tuple[list[ContextItem], list[str]]:
    """The context items, kinds checked against the fixed list (an unknown kind is `other` and
    approximate), values bounded. Also returns the colour words found in colour-bearing items, so
    the search text can be cleaned of them."""
    items: list[ContextItem] = []
    for raw in parsed.context:
        value = " ".join(raw.value.lower().split())[:MAX_TERM_CHARS].strip(" .,;:")
        if not value or any(i.kind == raw.kind and i.value == value for i in items):
            continue
        kind = raw.kind if raw.kind in CONTEXT_KINDS else "other"
        items.append(ContextItem(kind=kind, value=value, approximate=kind == "other"))
    return items[:MAX_CONTEXT], [i.value for i in items if i.kind in COLOR_CONTEXT_KINDS]


def _strip_words(text: str, phrases: list[str]) -> str:
    """Remove the context phrases (and the colour words of a room colour) from a search text: a
    colour that describes the room must not become part of the product search."""
    out = text
    for phrase in sorted(phrases, key=len, reverse=True):
        out = re.sub(rf"(?i)\b{re.escape(phrase)}\b", " ", out)
    return " ".join(out.split())


def build_query(request: str, parsed: ShopParse) -> ShopQuery:
    """The `ShopQuery` for a model's parse of `request`, with every field sanitized."""
    price_min, price_max = _price(parsed.price_min), _price(parsed.price_max)
    if price_min is not None and price_max is not None and price_min > price_max:
        price_min, price_max = price_max, price_min
    category = " ".join(parsed.category.lower().split())[:MAX_TERM_CHARS]
    context, context_phrases = _context(parsed)
    own_colors, _ = color_ids(parsed.color_terms)
    text = strip_price_words(" ".join(parsed.search_text.split()))
    if context_phrases and not own_colors:
        # the colour is the room's, not the product's: it never belongs in the search text
        cleaned = _strip_words(text, context_phrases)
        text = cleaned or text
    search_text = text[:MAX_SEARCH_TEXT_CHARS]
    search_text = search_text or strip_price_words(request)[:MAX_SEARCH_TEXT_CHARS] or category
    suggested: list[str] = []
    avoid_colors: list[str] = []
    reason = ""
    if any(i.kind in COLOR_CONTEXT_KINDS for i in context) and not own_colors:
        suggested, left_s = color_ids(parsed.suggested_colors)
        avoid_colors, left_a = color_ids(parsed.avoid_colors)
        suggested = [c for c in suggested if c not in avoid_colors][:MAX_SUGGESTED]
        avoid_colors = avoid_colors[:MAX_AVOID]
        reason = " ".join(parsed.color_reason.split())[:_REASON_CHARS]
        for word in [*left_s, *left_a][:2]:  # colours we do not know: a soft, approximate hint
            if len(context) < MAX_CONTEXT:
                context.append(ContextItem(kind="other", value=word, approximate=True))
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
        color_terms=own_colors[:MAX_SUGGESTED],
        context=context,
        suggested_colors=suggested,
        avoid_colors=avoid_colors,
        color_reason=reason,
    )


def color_word(color: str) -> str:
    """The word a search carries for a colour id ("navy_dark_blue" is "navy")."""
    return COLOR_LABELS.get(color, color)


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
            disable_thinking=True,
        )
        parsed = parse_model(ShopParse, completion.text, what="shop_parse")
    except (LLMOutputError, SchemaError) as exc:
        return (
            fallback_query(cleaned),
            None,
            f"Could not interpret the request ({exc}); searched your words as typed.",
        )
    return build_query(cleaned, parsed), completion, ""
