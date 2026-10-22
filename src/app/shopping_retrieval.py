"""Retrieval mode "shopping": three stages per slot, with prices and URLs from provider data only.

  1. search  - a shopping provider returns candidate products for "<category> <style>"; code keeps
               the top 25, drops unpriced and duplicate ones and anything priced above the slot's
               per-item ceiling (budget x item cap / (1 + shipping buffer); ADR 0007).
  2. rank    - a small model sees a compact list and returns finalist ids with a reason. Every id
               is validated against the list; the model never supplies a price, seller or URL.
  3. lookup  - each finalist is looked up by product id for its stores: direct retailer URL,
               shipping, total, availability and any stated dimensions. If that fails, the candidate
               is kept with the Google panel URL (or no URL), never an invented one. A lookup that
               fails with 401/403 is a configuration error and is raised, not degraded. Finalists
               whose total (price plus shipping) is over the slot's total cap are dropped.

When a slot ends with no in-budget finalist (every result over the per-item ceiling, or every
finalist over the total cap once shipping is known), up to two over-budget options are built so the
buyer sees what exists: from the cheapest over-ceiling results (ranked, then looked up) or from the
finalists the total cap dropped. Options need a verified retailer URL, are shown separately, and
are never picks, never in the totals and never passed to the proposal (ADR 0007).

Slots run concurrently (see `retrieval.retrieve`) and so do the lookups inside a slot; everything
shares the lock-protected `Budget`. Provider calls add their estimated dollars to the budget's
separate provider total (capped by `max_provider_cost_usd`, not `max_cost_usd`) but do not count
as LLM steps.
"""

from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.llm import Budget, Completion, complete
from app.prompts import Prompt
from app.retrieval import CallRecord, Slot, SlotResult, product_page_rejection
from app.schemas import (
    DimensionsM,
    OverBudgetOption,
    ProductCandidate,
    RankedFinalists,
    RoomPerception,
    parse_model,
    to_output_schema,
)
from app.selfcheck import item_shipping, item_total
from app.shopping import (
    ProductDetails,
    ProductLookup,
    ProviderError,
    SearchCandidate,
    SearchProvider,
    build_providers,
    choose_store,
    strip_tracking,
)
from app.tracing import span

__all__ = ["build_providers", "build_query", "filter_candidates", "price_stats", "run_slot"]

TOP_N = 25
RANK_MAX_TOKENS = 1500
MAX_STYLE_WORDS = 4
MAX_TITLE_CHARS = 100
OVER_POOL = 8  # cheapest over-ceiling results considered for over-budget options
OVER_OPTIONS = 2  # over-budget options shown per empty slot


def build_query(category: str, style: str) -> str:
    """Deterministic search query: the slot category plus up to a few style words."""
    words = [w for w in style.lower().split() if w not in category.lower().split()]
    return " ".join([category.strip().lower(), *words[:MAX_STYLE_WORDS]]).strip()


def filter_candidates(
    results: list[SearchCandidate], ceiling_usd: float
) -> tuple[list[SearchCandidate], dict[str, int]]:
    """Top 25, then drop unpriced, duplicate-id and over-ceiling results (in that order)."""
    top = results[:TOP_N]
    counts = {"returned": len(results), "considered": len(top)}
    priced = [c for c in top if c.price is not None]
    counts["unpriced"] = len(top) - len(priced)
    seen: set[str] = set()
    unique: list[SearchCandidate] = []
    for c in priced:
        if c.product_id not in seen:
            seen.add(c.product_id)
            unique.append(c)
    counts["duplicates"] = len(priced) - len(unique)
    within = [c for c in unique if c.price is not None and c.price <= ceiling_usd]
    counts["over_budget"] = len(unique) - len(within)
    counts["kept"] = len(within)
    return within, counts


def unique_priced(results: list[SearchCandidate]) -> list[SearchCandidate]:
    """The top 25 results that have a price, first occurrence of each product id."""
    seen: set[str] = set()
    out: list[SearchCandidate] = []
    for c in results[:TOP_N]:
        if c.price is not None and c.product_id not in seen:
            seen.add(c.product_id)
            out.append(c)
    return out


def price_stats(
    results: list[SearchCandidate], ceiling_usd: float
) -> dict[str, float | int | None]:
    """What the search saw for a slot, from the parsed numeric prices of the top 25 results."""
    top = results[:TOP_N]
    unique = unique_priced(results)
    prices = sorted(c.price for c in unique if c.price is not None)
    return {
        "considered": len(top),
        "priced": len(prices),
        "unpriced": sum(1 for c in top if c.price is None),
        "over_ceiling": sum(1 for p in prices if p > ceiling_usd),
        "min_usd": prices[0] if prices else None,
        "median_usd": round(statistics.median(prices), 2) if prices else None,
        "max_usd": prices[-1] if prices else None,
    }


def _estimated_cost(budget: Budget, provider: str) -> float:
    config = budget.config
    return config.serper_usd_per_query if provider == "serper" else config.serpapi_usd_per_search


def _room_summary(p: RoomPerception) -> str:
    d = p.dimensions
    furniture = ", ".join(f.category for f in p.furniture) or "none"
    decor = ", ".join(x.kind for x in p.art_and_decor) or "none"
    return (
        f"{p.room_type}, about {d.width_m:.1f} m x {d.length_m:.1f} m "
        f"(confidence {d.confidence:.1f}), ceiling {d.ceiling_height_m:.1f} m\n"
        f"Existing furniture: {furniture}\nExisting decor: {decor}\n"
        f"Current style: {', '.join(p.style_descriptors) or 'unknown'}"
    )


def _rank_user_text(
    slot: Slot,
    perception: RoomPerception,
    style: str,
    candidates: list[SearchCandidate],
    n: int,
    *,
    over_limit: bool = False,
) -> str:
    lines = [
        f"Slot: {slot.category}",
        f"Why this slot: {slot.reason}",
        f"Requested style: {style}",
        (
            f"Per-item price ceiling: {slot.budget_usd:.0f} USD. EVERY candidate below is above "
            "it: pick the closest matches anyway; they are shown to the buyer as over-limit "
            "options."
            if over_limit
            else f"Per-item price ceiling: {slot.budget_usd:.0f} USD (shipping is added later)"
        ),
        f"Room: {_room_summary(perception)}",
    ]
    if slot.size_hint:
        lines.append(f"Size hint: {slot.size_hint}")
    lines += [f"Pick at most {n} finalists.", "", "Candidates:"]
    for c in candidates:
        rating = f"rating {c.rating:g} ({c.rating_count})" if c.rating is not None else "no rating"
        lines.append(
            f"- id: {c.product_id} | {c.title[:MAX_TITLE_CHARS]} | seller: {c.seller or '?'} "
            f"| {c.currency} {c.price:.2f} | {rating}"
        )  # price is never None here: filter_candidates dropped unpriced results
    return "\n".join(lines)


def _record_provider_call(
    budget: Budget, kind: str, label: str, provider: str, usd: float, seconds: float,
    *, ok: bool, detail: str = "",
) -> CallRecord:  # fmt: skip
    """Trace and budget one provider call. Dollars are an estimate from the APP_*_USD settings."""
    with span(
        f"provider.{kind}",
        config=budget.config,
        provider=provider,
        label=label,
        cost_usd=round(usd, 6),
        cost_estimated=True,
        ok=ok,
        detail=detail,
    ):
        pass
    budget.add_provider_cost(usd)
    return CallRecord(
        kind, label, provider, cost_usd=usd, seconds=seconds, estimated=True, ok=ok, detail=detail
    )


def _lookup_one(
    cand: SearchCandidate, reason: str, lookup: ProductLookup, budget: Budget
) -> tuple[ProductCandidate, CallRecord, list[dict[str, Any]], str | None]:
    """Stage 3 for one finalist. Provider failures degrade to a panel-URL candidate.

    The last item is why the lookup gave no retailer link, when it did not: "http error <status>",
    "no product_results", "no stores" or "store url is not a product page" (None otherwise).
    """
    dropped: list[dict[str, Any]] = []
    started = time.perf_counter()
    details = ProductDetails()
    ok, detail = True, ""
    issue: str | None = None
    try:
        details = lookup.lookup(cand.product_id)
    except ProviderError as exc:
        if exc.status in (401, 403):  # bad key or plan: a configuration error, not a bad product
            raise
        ok, detail = False, str(exc)  # provider name + status only, by construction
        issue = f"http error {exc.status}" if exc.status else "http error"
    seconds = round(time.perf_counter() - started, 3)

    dims = None
    if details.dimensions is not None and not details.dimensions.is_empty():
        dims = DimensionsM(
            width_m=details.dimensions.width_m,
            depth_m=details.dimensions.depth_m,
            height_m=details.dimensions.height_m,
            length_m=details.dimensions.length_m,
        )
    base: dict[str, Any] = {
        "name": cand.title,
        "retailer": cand.seller,
        "price": cand.price if cand.price is not None else 0.0,
        "currency": cand.currency,
        "url": "",
        "url_kind": "none",
        "dimensions_m": dims,
        "rating": cand.rating,
        "rating_count": cand.rating_count,
        "reason": reason,
        "product_id": cand.product_id,
        "price_source": "search",
        "store_title": "",
    }
    store = choose_store(details.stores, cand.seller)
    result: ProductCandidate | None = None
    if store is not None:
        url = strip_tracking(store.url)
        rejection = product_page_rejection(url)
        if rejection is None:
            base.update(
                retailer=store.seller or cand.seller,
                url=url,
                url_kind="retailer_product",
                shipping=store.shipping,
                total=store.total,
                price_source="lookup" if store.price is not None else "search",
                store_title=store.title,
            )
            if store.price is not None:
                base["price"] = store.price
            result = ProductCandidate(**base)
        else:
            issue = "store url is not a product page"
            dropped.append(
                {
                    "name": cand.title,
                    "url": url,
                    "reason": f"store_url_not_a_product_page:{rejection}",
                }
            )
    elif issue is None:
        issue = details.note or "no stores"
    if result is None:
        if cand.google_panel_url:
            base.update(url=cand.google_panel_url, url_kind="google_panel")
        result = ProductCandidate(**base)
    record = _record_provider_call(
        budget, "lookup", cand.title[:40], lookup.name,
        budget.config.serpapi_usd_per_search, seconds, ok=ok, detail=detail or (issue or ""),
    )  # fmt: skip
    return result, record, dropped, issue


Looked = tuple[ProductCandidate, CallRecord, list[dict[str, Any]], str | None]


def _lookup_round(
    chosen: list[tuple[SearchCandidate, str]], lookup: ProductLookup, budget: Budget
) -> list[Looked]:
    """Look up the chosen finalists concurrently; results keep rank order. A budget error or a
    401/403 propagates (after the calls in flight finish); other provider errors degrade."""
    with ThreadPoolExecutor(max_workers=len(chosen), thread_name_prefix="lookup") as pool:
        futures = [pool.submit(_lookup_one, c, reason, lookup, budget) for c, reason in chosen]
        outcomes = [f.exception() for f in futures]
    out: list[Looked] = []
    for future, error in zip(futures, outcomes, strict=True):
        if error is not None:
            raise error
        out.append(future.result())
    return out


def _rank_round(
    slot: Slot,
    perception: RoomPerception,
    style: str,
    pool: list[SearchCandidate],
    n: int,
    budget: Budget,
    rank_prompt: Prompt,
    *,
    over_limit: bool = False,
) -> tuple[
    list[tuple[SearchCandidate, str]], Completion, CallRecord, dict[str, int], list[dict[str, Any]]
]:
    """Stage 2: ids only. Returns (picks in rank order, completion, call row, counts, dropped)."""
    config = budget.config
    by_id = {c.product_id: c for c in pool}
    started = time.perf_counter()
    completion = complete(
        span_name="llm.rank",
        prompt=rank_prompt,
        model=config.rank_model,
        user_text=_rank_user_text(slot, perception, style, pool, n, over_limit=over_limit),
        budget=budget,
        max_tokens=RANK_MAX_TOKENS,
        schema=to_output_schema(RankedFinalists),
        effort=config.effort_retrieval,
        stream=True,
    )
    record = CallRecord(
        "rank", slot.category, completion.model, completion.input_tokens,
        completion.output_tokens, completion.web_searches, completion.cost_usd,
        round(time.perf_counter() - started, 3), completion.time_to_first_token_s,
        phase="over limit" if over_limit else "",
    )  # fmt: skip
    ranked = parse_model(RankedFinalists, completion.text, what=f"rank[{slot.category}]")
    dropped: list[dict[str, Any]] = []
    chosen: list[tuple[SearchCandidate, str]] = []
    for f in ranked.finalists:
        if f.id not in by_id:
            dropped.append({"id": f.id, "reason": "id_not_in_candidates"})
        elif len(chosen) < n and all(f.id != c.product_id for c, _ in chosen):
            chosen.append((by_id[f.id], f.reason))
    counts = {"returned": len(ranked.finalists), "valid": len(chosen), "invented": len(dropped)}
    return chosen, completion, record, counts, dropped


def _option(c: ProductCandidate, reason: str, slot: Slot) -> OverBudgetOption:
    total, _ = item_total(c)
    return OverBudgetOption(
        candidate=c,
        reason=reason,  # type: ignore[arg-type]
        item_total=round(total, 2),
        shipping_known=item_shipping(c) is not None,
        also_over_total_budget=slot.total_budget_usd is not None and total > slot.total_budget_usd,
    )


def run_slot(
    slot: Slot,
    perception: RoomPerception,
    style: str,
    budget: Budget,
    rank_prompt: Prompt,
    search: SearchProvider,
    lookup: ProductLookup,
) -> SlotResult:
    """All three stages for one slot (runs in a worker thread)."""
    config = budget.config
    started = time.perf_counter()
    calls: list[CallRecord] = []
    dropped: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []  # lookups that gave no retailer link, and why
    options: list[OverBudgetOption] = []
    query = build_query(slot.category, style)
    summary: dict[str, Any] = {
        "slot": slot.category,
        "mode": "shopping",
        "search_provider": search.name,
        "query": query,
    }

    # Stage 1: search. A provider failure here is loud (bad key, no credits), not "no results".
    t0 = time.perf_counter()
    results = search.search(query)
    calls.append(
        _record_provider_call(
            budget,
            "search",
            slot.category,
            search.name,
            _estimated_cost(budget, search.name),
            round(time.perf_counter() - t0, 3),
            ok=True,
        )  # fmt: skip
    )
    pool_, counts = filter_candidates(results, slot.budget_usd)
    summary["search"] = counts
    summary["price_stats"] = price_stats(results, slot.budget_usd)

    def finish(note: str, candidates: list[ProductCandidate], completion: Completion) -> SlotResult:
        summary.update(
            candidates_kept=len(candidates),
            dropped=[d["reason"] for d in dropped],
            lookup_errors=[c.detail for c in calls if c.kind == "lookup" and not c.ok],
            lookup_issues=issues,
            over_budget_options=len(options),
            seconds=round(time.perf_counter() - started, 3),
        )
        result = SlotResult(
            slot, candidates, dropped, note, completion,
            round(time.perf_counter() - started, 3), calls, summary,
        )  # fmt: skip
        result.over_budget_options = options
        return result

    def collect(looked: list[Looked], phase: str = "") -> list[ProductCandidate]:
        out: list[ProductCandidate] = []
        for candidate, record, drops, issue in looked:
            record.phase = phase
            calls.append(record)
            dropped.extend(drops)
            if issue is not None:
                issues.append({"title": candidate.name, "why": issue})
            out.append(candidate)
        return out

    if not pool_:
        # Nothing at or under the ceiling. Look for the closest over-limit products to show.
        over = sorted(
            (
                c
                for c in unique_priced(results)
                if c.price is not None and c.price > slot.budget_usd
            ),
            key=lambda c: c.price or 0.0,
        )[:OVER_POOL]
        note = (
            f"no shopping results within the ${slot.budget_usd:.0f} per-item ceiling "
            f"({counts['considered']} considered, {counts['unpriced']} unpriced, "
            f"{counts['over_budget']} over budget)"
        )
        if not over:
            return finish(note, [], Completion(text=""))
        chosen, completion, rank_record, rank_counts, rank_drops = _rank_round(
            slot, perception, style, over, OVER_OPTIONS, budget, rank_prompt, over_limit=True
        )
        calls.append(rank_record)
        dropped.extend(rank_drops)
        summary["over_limit_rank"] = rank_counts
        if chosen:
            for cand in collect(_lookup_round(chosen, lookup, budget), "over limit"):
                if cand.url_kind == "retailer_product":
                    options.append(_option(cand, "over_item_limit", slot))
        return finish(note, [], completion)

    # Stage 2: rank (ids only).
    chosen, completion, rank_record, rank_counts, rank_drops = _rank_round(
        slot, perception, style, pool_, config.finalists_per_slot, budget, rank_prompt
    )
    calls.append(rank_record)
    dropped.extend(rank_drops)
    summary["rank"] = rank_counts
    if not chosen:
        return finish("the ranker returned no valid finalist", [], completion)

    # Stage 3: lookups, concurrently. A BudgetExceededError propagates; provider errors degrade.
    candidates: list[ProductCandidate] = []
    over_cap: list[ProductCandidate] = []
    no_link: list[ProductCandidate] = []
    for candidate in collect(_lookup_round(chosen, lookup, budget)):
        total, _ = item_total(candidate)
        if candidate.url_kind == "none":  # no verified link of any kind: never a finalist
            no_link.append(candidate)
            dropped.append({"name": candidate.name, "reason": "no_link"})
        elif slot.total_cap_usd is not None and total > slot.total_cap_usd:
            over_cap.append(candidate)
            dropped.append(
                {
                    "name": candidate.name,
                    "reason": "total_over_item_cap",
                    "total": f"{total:.2f}",
                    "cap": f"{slot.total_cap_usd:.2f}",
                }
            )
        else:
            candidates.append(candidate)
    summary["over_item_cap_after_lookup"] = len(over_cap)
    if candidates:
        return finish("", candidates, completion)
    # Every finalist was dropped. Finalists the total cap dropped become over-budget options
    # directly (they were already looked up); one with only a Google page link has no verified
    # retailer URL and is not shown.
    cheapest = sorted(
        (c for c in over_cap if c.url_kind == "retailer_product"),
        key=lambda c: item_total(c)[0],
    )[:OVER_OPTIONS]
    options.extend(_option(c, "over_total_cap", slot) for c in cheapest)
    if over_cap:
        note = (
            f"every finalist's total (price plus shipping) is over the ${slot.total_cap_usd:.0f} "
            "per-item cap"
        )
    else:
        titles = "; ".join(c.name for c in no_link)
        note = f"lookup returned no store for {titles}"
    return finish(note, [], completion)
