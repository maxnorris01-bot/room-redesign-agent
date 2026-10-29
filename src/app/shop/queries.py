"""Model-written search queries for a Shop Again (ADR 0012, stage 2i amendment).

After the evidence step, one small forced tool call (`record_queries`, prompt `shop_queries` v1,
strict schema, thinking disabled, about 300 output tokens) writes one or two search queries from
the typed constraints and what the shopper's ratings lean toward and away from. The model
describes; code validates every query and drops the ones that break a rule, noting why in the
trace: every hard word of the request must be present (the category and each typed style, type,
size, colour and material; see `hardwords`) unless a listed synonym replaces it, no style, material
or colour word the request did not contain, no URL, no price, no retailer that is not one the
shopper liked, at most `QUERY_MAX_WORDS` words. Accepted queries run as page-1 searches. A failed
or empty call is recorded and the Shop Again goes on without it; when no variant passes, fewer
searches run and the result says so (`QueryResult.note`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.config import Config
from app.llm import Budget, LLMOutputError, complete
from app.prompts import load_prompt
from app.shop.hardwords import (
    added_attribute_word,
    allowed_words,
    hard_words,
    missing_hard_word,
    tokens,
)
from app.shop.models import MAX_SEARCH_TEXT_CHARS, ShopQuery
from app.shop.retailers import RETAILERS, seller_key
from app.shop.textutil import normalize_phrase
from app.shop.thresholds import (
    MODEL_QUERIES,
    QUERY_DISLIKED_TITLES,
    QUERY_LIKED_TITLES,
    QUERY_MAX_WORDS,
    QUERY_NOVELTY_JACCARD,
)

QUERY_TOOL = "record_queries"
QUERY_USED_SHOWN = 12  # used queries listed to the writer (the most recent)
# stage 2q: neutral fillers only. "modern", "classic" and "contemporary" are style words, so a
# variant that gained one changed what was asked for.
VARIANT_MODIFIERS = (
    "premium", "affordable", "designer", "quality", "stylish", "popular", "best",
)  # fmt: skip
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
    setting: list[str] = field(default_factory=list)  # stage 2k: setting and colour guidance lines
    used: list[str] = field(default_factory=list)  # stage 2p: queries already used in this topic
    seen: int = 0  # results already shown in this topic
    rated: int = 0  # results already rated in this topic


@dataclass
class QueryResult:
    accepted: list[str] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)  # {"query", "reason"}
    error: str = ""
    usd: float = 0.0
    asked: bool = False
    fallback: int = 0  # accepted queries that came from `fallback_variants`, not the model
    note: str = ""  # stage 2q: why fewer variants than wanted ran (empty when all did)


def _singular(word: str) -> str:
    w = word.lower()
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _has(words: set[str], term: str) -> bool:
    parts = [_singular(w) for w in _words(term) if len(w) >= 3]
    return all(p in words for p in parts) if parts else True


def _word_set(text: str) -> frozenset[str]:
    return frozenset(_singular(w) for w in _words(text) if len(w) >= 2)


def too_close_to_used(cleaned: str, used: list[str]) -> str:
    """Why `cleaned` repeats a query already used ("" when it is new): the same words, or at least
    `QUERY_NOVELTY_JACCARD` of its words shared with one (a reordering, one word more or less)."""
    mine = _word_set(cleaned)
    for old in used:
        theirs = _word_set(old)
        if not mine or not theirs:
            continue
        if mine == theirs:
            return "the same as a query already used"
        if len(mine & theirs) / len(mine | theirs) >= QUERY_NOVELTY_JACCARD:
            return "too close to a query already used"
    return ""


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
        *inp.setting,
        line("Leaning toward", inp.toward),
        line("Leaning away from", inp.away),
        f"Results already seen: {inp.seen}; rated: {inp.rated}",
        "Queries already used for this topic (write different ones):",
        *[f"  * {u[:80]}" for u in inp.used[-QUERY_USED_SHOWN:]],
        line("Liked retailers (you may name one)", inp.liked_sellers),
        "Titles the shopper liked:",
        *[f"  * {t[:100]}" for t in inp.liked_titles[:QUERY_LIKED_TITLES]],
        "Titles the shopper disliked:",
        *[f"  * {t[:100]}" for t in inp.disliked_titles[:QUERY_DISLIKED_TITLES]],
        f"Write two NEW queries of at most {QUERY_MAX_WORDS} words, each unlike every used query.",
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
    needed = hard_words(inp.base, q)
    lacking = missing_hard_word(cleaned, needed)
    if lacking is not None:
        word = lacking.missing_word(set(tokens(cleaned)))
        if lacking.kind == "category":
            return "", f"the category word '{word}' is missing"
        if lacking.kind in ("must_have", "material"):
            return "", f"the typed constraint '{lacking.label}' is missing"
        return "", f"the typed {lacking.kind} '{lacking.label}' is missing"
    added = added_attribute_word(cleaned, allowed_words(inp.base, q, needed))
    if added:
        return "", f"it adds '{added}', a word your request did not use"
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
        if not why:
            why = too_close_to_used(cleaned, inp.used)
        if why:
            result.dropped.append({"query": item[:80], "reason": why})
            continue
        if len(result.accepted) >= MODEL_QUERIES:
            result.dropped.append({"query": item[:80], "reason": "more than the limit"})
            continue
        seen.add(" ".join(_words(cleaned)))
        result.accepted.append(cleaned)
    return result


def _fallback(inp: QueryInput, limit: int) -> tuple[list[str], int]:
    """(variants, how many candidates the rules rejected) for `fallback_variants`."""
    out: list[str] = []
    rejected = 0
    base = inp.adjusted.search_text or inp.base.search_text
    needed = hard_words(inp.base, inp.adjusted)
    singles = [(m,) for m in VARIANT_MODIFIERS]
    pairs = [(a, b) for a in VARIANT_MODIFIERS for b in VARIANT_MODIFIERS if a < b]
    for modifiers in [
        *singles,
        *pairs,
    ]:  # one word first; two when one is too close to a used query
        if len(out) >= limit:
            break
        if any(m in _words(base) for m in modifiers):
            continue
        words = [*modifiers, *base.split()]
        for hard in needed:  # a hard word the search words lack is added back, as typed
            if missing_hard_word(" ".join(words), [hard]) is not None:
                words += hard.label.split()
        text = " ".join(words[:QUERY_MAX_WORDS])
        cleaned, why = validate_query(text, inp)
        if why:
            rejected += 1
            continue
        if too_close_to_used(cleaned, [*inp.used, *inp.existing, *out]):
            continue
        out.append(cleaned)
    return out, rejected


def fallback_variants(inp: QueryInput, limit: int = MODEL_QUERIES) -> list[str]:
    """Variants without a model: the request's search words with one neutral filler word in front
    (premium, affordable, designer, ...), every hard word kept, checked by the same rules and the
    same novelty test as a model query. Used when the writer is off, fails or gives nothing new. A
    filler never adds a style, material or colour word (stage 2q); when no variant passes, fewer
    (or none) come back rather than one that drifts."""
    return _fallback(inp, limit)[0]


def variant_queries(inp: QueryInput, budget: Budget, config: Config | None = None) -> QueryResult:
    """Two new queries for a search that came up thin or dry: the model writer first (prompt
    `shop_queries` v3), then the deterministic `fallback_variants` for whatever is still missing.
    The result says which came from where (`fallback` rows in `dropped` never appear: they pass the
    same rules)."""
    config = config or budget.config
    result = write_queries(inp, budget, config) if config.shop_model_queries else QueryResult()
    if len(result.accepted) < MODEL_QUERIES:
        extra, rejected = _fallback(
            QueryInput(**{**inp.__dict__, "existing": [*inp.existing, *result.accepted]}),
            MODEL_QUERIES - len(result.accepted),
        )
        result.accepted.extend(extra)
        result.fallback = len(extra)
        if rejected:
            result.dropped.append(
                {"query": f"{rejected} filler variants", "reason": "dropped: changed your request"}
            )
    if len(result.accepted) < MODEL_QUERIES:
        kept = len(result.accepted)
        result.note = (
            "No variant kept every word of your request, so no variant search was run."
            if not kept
            else f"Only {kept} of {MODEL_QUERIES} variants kept every word of your request, so "
            f"{kept} variant search ran."
        )
    return result
