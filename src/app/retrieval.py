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

from app.categories import already_covered, norm
from app.categories import canonical_category as canonical_category  # re-exported
from app.config import Config
from app.llm import Budget, Completion, complete, web_search_tool
from app.preferences import Preferences, resolve_preferences
from app.prompts import Prompt, load_prompt
from app.schemas import (
    Candidate,
    FurnitureItem,
    OverBudgetOption,
    ProductCandidate,
    RoomPerception,
    SlotCandidates,
    parse_model,
    to_output_schema,
)
from app.shopping import ProductLookup, SearchProvider
from app.tracing import span

RETRIEVAL_MAX_TOKENS = 3000

# Accent slots: common pieces per room type, most impactful first, used to fill the slot list when
# there are few gaps or replacements. Categories already covered by something that stays are
# skipped (see `plan_slots`).
_ACCENT_SLOTS: dict[str, list[str]] = {
    "living": ["sofa", "coffee table", "area rug", "floor lamp", "bookshelf", "media unit"],
    "bed": ["bed frame", "nightstand", "dresser", "area rug", "reading lamp"],
    "office": ["desk", "office chair", "bookshelf", "desk lamp", "area rug"],
    "dining": ["dining table", "dining chairs", "sideboard", "pendant light", "area rug"],
}
_GENERIC_SLOTS = ["seating", "side table", "area rug", "lamp", "storage"]


@dataclass(frozen=True)
class Slot:
    category: str
    reason: str
    source: str  # "replace_requested" | "gap" | "replace_suggested" | "accent"
    budget_usd: float  # per-item PRICE ceiling for search (same for every slot), not a share
    size_hint: str = ""
    replaces_id: str | None = None  # the existing item (perception id) this slot would replace
    total_cap_usd: float | None = None  # items over this total (price + shipping) are dropped
    total_budget_usd: float | None = None  # the whole budget (flags over-limit options)


@dataclass
class CallRecord:
    """One external call inside a slot: a search, a rank call, a lookup or a web-search call."""

    kind: str  # "search" | "rank" | "lookup" | "web_search"
    label: str
    provider: str  # provider name, or the model for LLM calls (shown in the table's model column)
    input_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    time_to_first_token_s: float | None = None
    estimated: bool = False  # True for provider calls: dollars come from the APP_*_USD settings
    ok: bool = True
    detail: str = ""
    phase: str = ""  # e.g. "over limit": a second rank/lookup round for over-budget options
    # Calls that share a non-None group ran in parallel (proposal self-check: "selfcheck",
    # "recheck"); only the slowest of a group is on the critical path. See SpikeResult.
    group: str | None = None


@dataclass
class SlotResult:
    slot: Slot
    candidates: list[ProductCandidate]
    dropped: list[dict[str, Any]]
    note: str
    completion: Completion
    seconds: float = 0.0  # measured wall time of the whole slot chain
    calls: list[CallRecord] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    # Shown to the buyer for an empty slot, never picked and never counted (ADR 0007).
    over_budget_options: list[OverBudgetOption] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    @property
    def chain_seconds(self) -> float:
        """Critical path inside the slot: search, rank, then the slowest lookup (they overlap)."""
        sequential = sum(c.seconds for c in self.calls if c.kind != "lookup")
        return sequential + max((c.seconds for c in self.calls if c.kind == "lookup"), default=0.0)


@dataclass
class RetrievalResult:
    slots: list[SlotResult] = field(default_factory=list)
    prompt_version: str = ""
    mode: str = "web_search"
    plan: SlotPlan | None = None


@dataclass
class SlotPlan:
    """The slot list plus what the policy left out, so nothing disappears silently."""

    slots: list[Slot]
    dropped_by_cap: list[str] = field(default_factory=list)  # wanted but over max_slots
    skipped: list[str] = field(default_factory=list)  # not wanted after all (already covered, ...)

    def to_json(self) -> dict[str, object]:
        return {
            "slots": [s.category for s in self.slots],
            "dropped_by_cap": self.dropped_by_cap,
            "skipped": self.skipped,
        }


def item_ceiling_usd(total_budget_usd: float, config: Config) -> float:
    """Per-item price ceiling for search: budget x item cap / (1 + shipping buffer), every slot."""
    return round(total_budget_usd * config.item_cap_fraction / (1 + config.shipping_buffer), 2)


def plan_slots(
    perception: RoomPerception,
    total_budget_usd: float,
    max_slots: int,
    preferences: Preferences | None = None,
    config: Config | None = None,
) -> SlotPlan:
    """Build the slot list (ADR 0007). Order, stopping at `max_slots`:

    1. requested replacements (`--replace`), 2. gaps, 3. suggested replacements (perception said
    "unlikely"), 4. accent slots from the room-type default list.

    Accents skip any category already covered by kept furniture, fixed items or decor, and a
    replaced item's category is never an accent. A gap already covered by something that stays is
    skipped too. Every slot gets the same per-item ceiling (`item_ceiling_usd`), not a share.
    """
    config = config or Config()
    if max_slots < 1:
        return SlotPlan([])
    prefs = preferences or resolve_preferences(perception)
    ceiling = item_ceiling_usd(total_budget_usd, config)
    cap = round(total_budget_usd * config.item_cap_fraction, 2)
    by_id = {f.id: f for f in perception.furniture}
    kept_categories = [norm(by_id[r.id].category) for r in prefs.decided("keep") if r.id in by_id]
    owned = [
        *kept_categories,
        *(norm(d.kind) for d in perception.art_and_decor),
        *(norm(x.category) for x in perception.fixed_items),
    ]
    replaced_categories = [norm(r.category) for r in prefs.decided("replace")]

    wanted: list[Slot] = []

    def slot(category: str, reason: str, source: str, item: FurnitureItem | None = None) -> Slot:
        hint = ""
        if item is not None:
            hint = (
                f"replaces your {item.category} ({item.id}), about "
                f"{item.size.width_m:.1f} m x {item.size.depth_m:.1f} m"
            )
        return Slot(
            category, reason, source, ceiling, hint,
            replaces_id=item.id if item else None, total_cap_usd=cap,
            total_budget_usd=total_budget_usd,
        )  # fmt: skip

    for r in prefs.decided("replace", "requested"):
        item = by_id[r.id]
        wanted.append(
            slot(item.category, f"replace existing {item.category} ({item.id}) as requested",
                 "replace_requested", item)
        )  # fmt: skip
    skipped: list[str] = []
    for gap in perception.furniture_gaps:
        if already_covered(gap.category, owned):
            skipped.append(f"gap {gap.category!r}: already covered by something that stays")
            continue
        wanted.append(slot(gap.category, gap.reason, "gap"))
    for r in prefs.decided("replace", "suggested"):
        item = by_id[r.id]
        wanted.append(
            slot(item.category, f"replace existing {item.category} ({item.id}): {item.note}",
                 "replace_suggested", item)
        )  # fmt: skip
    room = perception.room_type.lower()
    accents = next((v for k, v in _ACCENT_SLOTS.items() if k in room), _GENERIC_SLOTS)
    planned = [norm(w.category) for w in wanted]  # slots already planned (for example a gap)
    for category in accents:
        if (
            already_covered(category, owned)
            or already_covered(category, replaced_categories)
            or already_covered(category, planned)
        ):
            continue
        wanted.append(slot(category, "accent piece for this room type", "accent"))

    chosen: list[Slot] = []
    taken: list[str] = []
    dropped: list[str] = []
    for s in wanted:
        if already_covered(s.category, taken):
            if s.source != "accent":  # accents are filler, not wishes: no need to report them
                skipped.append(f"{s.source} {s.category!r}: same kind as an earlier slot")
        elif len(chosen) >= max_slots:
            dropped.append(f"{s.source}: {s.category}")
        else:
            chosen.append(s)
            taken.append(norm(s.category))
    # Accents are a filler, not a wish: only report dropped requests, gaps and suggestions.
    dropped = [d for d in dropped if not d.startswith("accent:")]
    return SlotPlan(chosen, dropped, skipped)


def derive_slots(
    perception: RoomPerception,
    total_budget_usd: float,
    max_slots: int,
    preferences: Preferences | None = None,
    config: Config | None = None,
) -> list[Slot]:
    """The slots of `plan_slots` (use that when you also need what was dropped)."""
    return plan_slots(perception, total_budget_usd, max_slots, preferences, config).slots


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
        f"Per-item price ceiling (soft): {slot.budget_usd:.0f} USD",
        f"Search limit: at most {max_uses} searches.",
        f"Room: {perception.room_type}, about {d.width_m:.1f} m x {d.length_m:.1f} m "
        f"(confidence {d.confidence:.1f}); current style: {styles}",
    ]
    if slot.size_hint:
        lines.append(f"Size hint: {slot.size_hint}")
    return "\n".join(lines)


def _to_product(c: Candidate) -> ProductCandidate:
    """A model-supplied (web_search mode) candidate, marked as such: its price came from a model."""
    return ProductCandidate(**c.model_dump(), price_source="model")


def _run_slot(
    slot: Slot, perception: RoomPerception, style: str, budget: Budget, prompt: Prompt
) -> SlotResult:
    """web_search mode: one search-tool call for the slot, run in a worker thread."""
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
    seconds = round(time.perf_counter() - started, 3)
    summary = {
        "slot": slot.category,
        "mode": "web_search",
        "queries": [s["query"] for s in completion.searches if "query" in s],
        "result_counts": [s["results"] for s in completion.searches if "results" in s],
        "search_errors": [s["error"] for s in completion.searches if "error" in s],
        "candidates_returned": len(parsed.candidates),
        "candidates_kept": len(kept),
        "dropped_unverified_urls": len(dropped),
        "dropped_reasons": [d["reason"] for d in dropped],
        "seconds": seconds,
    }
    call = CallRecord(
        kind="web_search",
        label=slot.category,
        provider=completion.model,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        web_searches=completion.web_searches,
        cost_usd=completion.cost_usd,
        seconds=seconds,
        time_to_first_token_s=completion.time_to_first_token_s,
    )
    return SlotResult(
        slot,
        [_to_product(c) for c in kept],
        dropped,
        parsed.note,
        completion,
        seconds,
        [call],
        summary,
    )


def retrieve(
    perception: RoomPerception,
    *,
    budget_usd: float,
    style: str,
    max_slots: int,
    budget: Budget,
    run_id: str | None = None,
    providers: tuple[SearchProvider, ProductLookup] | None = None,
    preferences: Preferences | None = None,
) -> RetrievalResult:
    """Retrieve candidates for each derived slot concurrently, under `budget`'s caps.

    `config.retrieval_mode` picks the pipeline: "shopping" (default; `app.shopping_retrieval`) or
    "web_search" (Claude's search tool). One worker thread per slot. Results are returned in slot
    order regardless of which slot finishes first. If any slot raises (for example
    `BudgetExceededError`), the first failure in slot order is re-raised once the slots already in
    flight have finished; they cannot be cancelled, and they bill. `providers` overrides the
    (search, lookup) pair in shopping mode (tests). `preferences` (keep or replace) shapes the
    slot list; without it perception's `keepable` flags decide.
    """
    from app import shopping_retrieval  # local import: that module imports this one

    config = budget.config
    shopping = config.retrieval_mode == "shopping"
    prompt = (
        load_prompt("rank", config.rank_prompt_version) if shopping else load_prompt("retrieval")
    )
    plan = plan_slots(perception, budget_usd, max_slots, preferences, config)
    slots = plan.slots
    result = RetrievalResult(prompt_version=prompt.version, mode=config.retrieval_mode, plan=plan)
    pair = (providers or shopping_retrieval.build_providers(config)) if shopping else None
    with span(
        "stage.retrieval",
        config=config,
        prompt_version=prompt.version,
        prompt_name=prompt.name,
        model=config.rank_model if shopping else config.model_retrieval,
        run_id=run_id,
        retrieval_mode=config.retrieval_mode,
        search_provider=config.search_provider if shopping else None,
        slots=[s.category for s in slots],
        concurrent=True,
        web_search_tool=None if shopping else config.web_search_tool,
        max_searches_per_slot=None if shopping else config.max_searches_per_slot,
    ) as rec:
        if slots:
            with ThreadPoolExecutor(max_workers=len(slots), thread_name_prefix="slot") as pool:
                futures: list[Future[SlotResult]] = []
                for slot in slots:
                    if pair is not None:
                        futures.append(
                            pool.submit(
                                shopping_retrieval.run_slot,
                                slot,
                                perception,
                                style,
                                budget,
                                prompt,
                                pair[0],
                                pair[1],
                            )  # fmt: skip
                        )
                    else:
                        futures.append(
                            pool.submit(_run_slot, slot, perception, style, budget, prompt)
                        )
                outcomes = [f.exception() for f in futures]  # waits for every slot to finish
            for future, error in zip(futures, outcomes, strict=True):
                if error is not None:
                    raise error
                result.slots.append(future.result())
        rec.update(
            per_slot=[r.summary for r in result.slots],
            cost_usd=round(sum(r.cost_usd for r in result.slots), 5),
            slowest_slot_seconds=max((r.seconds for r in result.slots), default=0.0),
        )
    return result
