"""Retrieval stage, configuration A: Claude's web search tool, one call per furniture slot.

Slots are derived in code (not by an LLM call) from the perception result, so the number of
searching calls is bounded and the derivation is inspectable. Every candidate URL must have been
returned by the search tool in that same call; anything else is dropped (the guard from the
Claim Verification repo, ADR 0003 there).
"""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urldefrag, urlsplit

from app.llm import Budget, Completion, complete, web_search_tool
from app.prompts import Prompt, load_prompt
from app.schemas import Candidate, RoomPerception, SlotCandidates, parse_model, to_output_schema
from app.tracing import span

RETRIEVAL_MAX_TOKENS = 3000

# Used only when perception marks nothing as missing or replaceable: common first purchases per
# room type, most impactful first. Style and budget then shape the search, not the category.
_DEFAULT_SLOTS: dict[str, list[str]] = {
    "living": ["sofa", "coffee table", "area rug", "floor lamp", "bookshelf"],
    "bed": ["bed frame", "nightstand", "dresser", "area rug", "reading lamp"],
    "office": ["desk", "office chair", "bookshelf", "desk lamp", "area rug"],
    "dining": ["dining table", "dining chairs", "sideboard", "pendant light", "area rug"],
}
_GENERIC_SLOTS = ["seating", "side table", "area rug", "lamp", "storage"]


@dataclass(frozen=True)
class Slot:
    category: str
    reason: str
    source: str  # "replaceable" | "gap" | "default"
    budget_usd: float
    size_hint: str = ""


@dataclass
class SlotResult:
    slot: Slot
    candidates: list[Candidate]
    dropped: list[dict[str, Any]]
    note: str
    completion: Completion
    seconds: float = 0.0


@dataclass
class RetrievalResult:
    slots: list[SlotResult] = field(default_factory=list)
    prompt_version: str = ""


def _norm(category: str) -> str:
    return " ".join(category.lower().split())


def _already_covered(category: str, taken: list[str]) -> bool:
    c = _norm(category)
    return any(c in t or t in c for t in taken)


def derive_slots(perception: RoomPerception, total_budget_usd: float, max_slots: int) -> list[Slot]:
    """Up to `max_slots` slots: replaceable furniture, then missing categories, then defaults.

    The budget is split equally across the chosen slots (a deliberately simple spike heuristic;
    a smarter split by category is a later concern). Furniture marked keepable is never a slot.
    """
    if max_slots < 1:
        return []
    picks: list[tuple[str, str, str, str]] = []  # category, reason, source, size_hint
    for item in perception.furniture:
        if item.keepable == "unlikely":
            hint = f"current one is about {item.size.width_m:.1f} m x {item.size.depth_m:.1f} m"
            picks.append(
                (
                    item.category,
                    f"replace existing {item.category}: {item.note}",
                    "replaceable",
                    hint,
                )
            )
    for gap in perception.furniture_gaps:
        picks.append((gap.category, gap.reason, "gap", ""))
    if not picks:
        room = perception.room_type.lower()
        defaults = next((v for k, v in _DEFAULT_SLOTS.items() if k in room), _GENERIC_SLOTS)
        owned = [_norm(f.category) for f in perception.furniture]
        picks = [(c, "common first purchase for this room type", "default", "") for c in defaults
                 if not _already_covered(c, owned)]  # fmt: skip

    chosen: list[tuple[str, str, str, str]] = []
    taken: list[str] = []
    for category, reason, source, hint in picks:
        if _already_covered(category, taken):
            continue
        chosen.append((category, reason, source, hint))
        taken.append(_norm(category))
        if len(chosen) == max_slots:
            break
    share = total_budget_usd / len(chosen) if chosen else 0.0
    return [Slot(c, r, s, round(share, 2), h) for c, r, s, h in chosen]


def _norm_url(url: str) -> str:
    return urldefrag(url.strip())[0].rstrip("/")


# Path segments that mark a listing, category or search page rather than one product.
_LISTING_SEGMENTS = {
    "collections", "category", "categories", "search", "browse", "s", "cat",
}  # fmt: skip
# Segments that introduce a single item. A Shopify product reached through a collection
# (/collections/sofas/products/slug) is still a product page, so these override "collections".
_ITEM_SEGMENTS = {"products", "product", "p", "pdp", "dp", "item", "items"}
_SEARCH_QUERY_KEYS = {"keyword", "q", "query", "search", "filter"}


def product_page_rejection(url: str) -> str | None:
    """Why `url` is not a product page (a short rule name), or None if it looks like one.

    This is a deliberately cheap, retailer-agnostic filter on the URL alone: it rejects listing,
    category, collection and search URLs. Passing it does not prove the page is a product page.
    """
    parts = urlsplit(url.strip())
    segments = [s.lower() for s in parts.path.split("/") if s]
    if not segments:
        return "no_path"
    if parts.path.lower().endswith("keyword.php"):
        return "keyword.php"
    query_keys = {k.lower() for k in parse_qs(parts.query, keep_blank_values=True)}
    hit = query_keys & _SEARCH_QUERY_KEYS
    if hit:
        return f"search_query_key:{sorted(hit)[0]}"
    for index, segment in enumerate(segments):
        if segment in _LISTING_SEGMENTS:
            if segment == "collections" and _ITEM_SEGMENTS & set(segments[index + 1 :]):
                continue  # Shopify: /collections/<collection>/products/<handle>
            return f"listing_segment:{segment}"
        if segment == "c" and index == len(segments) - 1:
            return "listing_segment:c"  # /c/ with no item segment after it
    return None


def is_product_page_url(url: str) -> bool:
    return product_page_rejection(url) is None


def verify_candidates(
    candidates: list[Candidate], retrieved_urls: set[str]
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """Keep candidates whose URL the search tool returned AND that looks like a product page.

    The first rule (URL present in the search results, modulo fragment/trailing slash) guards
    against URLs recalled from memory. The second (`product_page_rejection`) guards against
    listing, collection and search-results URLs, which do appear in search results.
    """
    allowed = {_norm_url(u) for u in retrieved_urls}
    kept: list[Candidate] = []
    dropped: list[dict[str, Any]] = []
    for cand in candidates:
        if _norm_url(cand.url) not in allowed:
            reason = "url_not_in_search_results"
        elif (rule := product_page_rejection(cand.url)) is not None:
            reason = f"not_a_product_page:{rule}"
        else:
            kept.append(cand)
            continue
        dropped.append({"name": cand.name, "url": cand.url, "reason": reason})
    return kept, dropped


def _slot_user_text(slot: Slot, perception: RoomPerception, style: str, max_uses: int) -> str:
    d = perception.dimensions
    styles = ", ".join(perception.style_descriptors) or "unknown"
    lines = [
        f"Slot: {slot.category}",
        f"Why this slot: {slot.reason}",
        f"Requested style: {style}",
        f"Slot budget (soft ceiling): {slot.budget_usd:.0f} USD",
        f"Search limit: at most {max_uses} searches.",
        f"Room: {perception.room_type}, about {d.width_m:.1f} m x {d.length_m:.1f} m "
        f"(confidence {d.confidence:.1f}); current style: {styles}",
    ]
    if slot.size_hint:
        lines.append(f"Size hint: {slot.size_hint}")
    return "\n".join(lines)


def _run_slot(
    slot: Slot, perception: RoomPerception, style: str, budget: Budget, prompt: Prompt
) -> tuple[SlotResult, dict[str, Any]]:
    """One slot's search call, run in a worker thread. Shares only the thread-safe `budget`."""
    config = budget.config
    started = time.perf_counter()
    completion = complete(
        span_name="llm.retrieval",
        prompt=prompt,
        model=config.model_retrieval,
        user_text=_slot_user_text(slot, perception, style, config.max_searches_per_slot),
        budget=budget,
        max_tokens=RETRIEVAL_MAX_TOKENS,
        schema=to_output_schema(SlotCandidates),
        tools=[web_search_tool(config.max_searches_per_slot, config.web_search_tool)],
        effort=config.effort_retrieval,
        stream=True,
    )
    parsed = parse_model(SlotCandidates, completion.text, what=f"retrieval[{slot.category}]")
    kept, dropped = verify_candidates(parsed.candidates, completion.retrieved_urls)
    result = SlotResult(
        slot, kept, dropped, parsed.note, completion, round(time.perf_counter() - started, 3)
    )
    summary = {
        "slot": slot.category,
        "queries": [s["query"] for s in completion.searches if "query" in s],
        "result_counts": [s["results"] for s in completion.searches if "results" in s],
        "search_errors": [s["error"] for s in completion.searches if "error" in s],
        "candidates_returned": len(parsed.candidates),
        "candidates_kept": len(kept),
        "dropped_unverified_urls": len(dropped),
        "dropped_reasons": [d["reason"] for d in dropped],
        "seconds": result.seconds,
    }
    return result, summary


def retrieve(
    perception: RoomPerception,
    *,
    budget_usd: float,
    style: str,
    max_slots: int,
    budget: Budget,
    run_id: str | None = None,
) -> RetrievalResult:
    """Run web-search retrieval for each derived slot concurrently, under `budget`'s caps.

    One worker thread per slot (the SDK client is synchronous). Results are returned in slot
    order regardless of which call finishes first. If any slot raises (for example
    `BudgetExceededError`), the first failure in slot order is re-raised once the calls already in
    flight have finished; they cannot be cancelled, and they bill.
    """
    config = budget.config
    prompt = load_prompt("retrieval")
    slots = derive_slots(perception, budget_usd, max_slots)
    result = RetrievalResult(prompt_version=prompt.version)
    with span(
        "stage.retrieval",
        config=config,
        prompt_version=prompt.version,
        prompt_name=prompt.name,
        model=config.model_retrieval,
        run_id=run_id,
        slots=[s.category for s in slots],
        concurrent=True,
        web_search_tool=config.web_search_tool,
        max_searches_per_slot=config.max_searches_per_slot,
    ) as rec:
        per_slot: list[dict[str, Any]] = []
        if slots:
            with ThreadPoolExecutor(max_workers=len(slots), thread_name_prefix="slot") as pool:
                futures: list[Future[tuple[SlotResult, dict[str, Any]]]] = [
                    pool.submit(_run_slot, slot, perception, style, budget, prompt)
                    for slot in slots
                ]
                outcomes = [f.exception() for f in futures]  # waits for every slot to finish
            for future, error in zip(futures, outcomes, strict=True):
                if error is not None:
                    raise error
                slot_result, summary = future.result()
                result.slots.append(slot_result)
                per_slot.append(summary)
        rec.update(
            per_slot=per_slot,
            cost_usd=round(sum(r.completion.cost_usd for r in result.slots), 5),
            slowest_slot_seconds=max((r.seconds for r in result.slots), default=0.0),
        )
    return result
