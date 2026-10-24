"""Shop Mode search: filter, dedupe, rank, cap per seller, return the top results.

  1. Serper page 1 (page 2 only when fewer than `RESULT_COUNT` results could be shown).
  2. Hard filters from the query: price range (a result with no price goes only when a price filter
     is set; a non-USD price cannot be compared, so it goes too), and category plausibility
     (a result that is plainly another known kind of product goes, see `plausible`).
  3. Dedupe by product id, then by normalized title + seller.
  4. One small-model call orders the pool against the soft preferences (ids only: the model never
     supplies a price, seller or link). Results it leaves out follow in pool order.
  5. At most `SELLER_CAP` results per seller are kept, walking that order, up to `RESULT_COUNT`.

No SerpApi lookup happens here; links stay the Google product links until the visitor opens or
loves a result (`app.shop.resolve`).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from app.categories import category_hits
from app.config import Config
from app.llm import Budget, LLMOutputError, complete
from app.prompts import load_prompt
from app.schemas import DimensionsM, RankedFinalists, SchemaError, parse_model, to_output_schema
from app.shop.models import (
    FilterCounts,
    LinkKind,
    ShopQuery,
    ShopResult,
    clean_request,
    is_https_url,
    result_id,
)
from app.shop.parse import parse_request
from app.shopping import ProviderError, SearchCandidate, SearchProvider
from app.shopping_retrieval import _record_provider_call, cap_per_seller, seller_key
from app.sizes import IN_M, resolve_size

MAX_PAGES_AGAIN = 3  # Serper pages a Shop Again may fetch
MAX_EXAMPLES = 8  # loved/liked and disliked titles shown to the ranker, each
RANK_VERSION = "1"  # typed searches
RANK_VERSION_AGAIN = "2"  # Shop Again: also sees the profile and the examples
RESULT_COUNT = 24  # results returned, and the fewest that avoid a second Serper page
SELLER_CAP = 4  # most results one seller may have in the returned list. UNTUNED GUESS.
MAX_POOL = 60  # candidates shown to the ranker (keeps the prompt small)
RANK_MAX_TOKENS = 2500
MAX_TITLE_CHARS = 100
# Titles with these words are parts or covers, not the product (applied only for a known category).
# UNTUNED GUESS: short on purpose, a longer list would drop real products.
ACCESSORY_PHRASES = ("slipcover", "slip cover", "replacement", "furniture leg", "cover for")


@dataclass
class AgainContext:
    """What a Shop Again adds to the search pipeline (see `app.shop.again`)."""

    exclude_ids: set[str] = field(default_factory=set)  # result ids shown earlier in the session
    exclude_title_keys: set[str] = field(default_factory=set)  # same listing under another id
    deprioritize_sellers: set[str] = field(default_factory=set)  # seller keys, ranked last
    likes: list[str] = field(default_factory=list)  # profile phrases
    dislikes: list[str] = field(default_factory=list)
    more_like: list[tuple[ShopResult, str]] = field(default_factory=list)  # (result, love|like)
    less_like: list[ShopResult] = field(default_factory=list)
    max_pages: int = MAX_PAGES_AGAIN


@dataclass
class SearchOutcome:
    query: ShopQuery
    results: list[ShopResult]
    counts: FilterCounts
    serper_calls: int
    seconds: dict[str, float]  # parse, search, rank, total
    anthropic_usd: float
    provider_usd: float
    ranked: bool
    warnings: list[str] = field(default_factory=list)
    kind: str = "search"  # "search" or "shop_again"
    serper_credits: int = 0
    note: str = ""

    @property
    def total_usd(self) -> float:
        return round(self.anthropic_usd + self.provider_usd, 6)


def normalized_title_key(title: str, seller: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower()) + "|" + seller_key(seller)


def plausible(title: str, category_key: str | None, search_text: str = "") -> bool:
    """Whether a result could be a `category_key` product, judged from its title alone.

    False when the title names another known kind of product and not the requested one (a "side
    table" for a coffee-table search), or is plainly a part or cover. Titles that name no known
    category pass: they cannot be judged. Always True for an unknown category (None).
    """
    if category_key is None:
        return True
    lowered = title.lower()
    if any(p in lowered and p not in search_text.lower() for p in ACCESSORY_PHRASES):
        return False
    hits = category_hits(title)
    return not hits or category_key in hits


def apply_filters(
    candidates: list[SearchCandidate], query: ShopQuery
) -> tuple[list[SearchCandidate], FilterCounts]:
    """Hard filters then dedupe, in search order. Returns the pool and what each step removed."""
    counts = FilterCounts(returned=len(candidates))
    priced_filter = query.price_min is not None or query.price_max is not None
    kept: list[SearchCandidate] = []
    for c in candidates:
        if priced_filter:
            if c.price is None or c.currency != "USD":
                counts.no_price += 1
                continue
            if (query.price_min is not None and c.price < query.price_min) or (
                query.price_max is not None and c.price > query.price_max
            ):
                counts.out_of_range += 1
                continue
        if not plausible(c.title, query.category_key, query.search_text):
            counts.wrong_category += 1
            continue
        kept.append(c)
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    pool: list[SearchCandidate] = []
    for c in kept:
        title_key = normalized_title_key(c.title, c.seller)
        if c.product_id in seen_ids or title_key in seen_titles:
            counts.duplicates += 1
            continue
        seen_ids.add(c.product_id)
        seen_titles.add(title_key)
        pool.append(c)
    counts.kept = len(pool)
    return pool, counts


def format_size_hint(dims: DimensionsM) -> str | None:
    """'79" W × 36" D', '48" W × 72" L' or '58" long' from sizes in metres; None when empty."""

    def inches(value: float) -> str:
        n = value / IN_M
        return f"{n:.1f}".removesuffix(".0") if n < 10 else f"{n:.0f}"

    parts = [
        f'{inches(v)}" {label}'
        for v, label in (
            (dims.width_m, "W"),
            (dims.depth_m, "D"),
            (dims.height_m, "H"),
            (dims.length_m, "L"),
        )
        if v
    ]
    if len(parts) == 1 and dims.length_m:
        return f'{inches(dims.length_m)}" long'
    return " × ".join(parts) or None


def size_hint_for(title: str, query: ShopQuery) -> str | None:
    size = resolve_size(query.category_key or query.category, None, [title], "")
    return format_size_hint(size.dims) if size.dims is not None else None


def _price(value: float | None) -> str:
    return "no price" if value is None else f"${value:,.0f}"


def rank_user_text(
    query: ShopQuery, pool: list[SearchCandidate], n: int, again: AgainContext | None = None
) -> str:
    def line(label: str, values: list[str]) -> str:
        return f"{label}: {', '.join(values) if values else 'none'}"

    if query.price_min is None and query.price_max is None:
        price = "no limit"
    else:
        low = f"from ${query.price_min:,.0f}" if query.price_min is not None else ""
        high = f"up to ${query.price_max:,.0f}" if query.price_max is not None else ""
        price = f"{low} {high}".strip() + " (already filtered; do not rank by price)"
    lines = [
        f"Shopper request: {query.request}",
        f"Category: {query.category or 'unknown'}",
        f"Price: {price}",
        line("Style", query.style_terms),
        line("Materials", query.material_terms),
        line("Must have", query.must_have),
        line("Avoid", query.avoid),
    ]
    if again is not None:
        lines += [
            line("Profile likes", again.likes),
            line("Profile dislikes", again.dislikes),
            "More like these (the shopper loved or liked them):",
            *[
                f"  * {r.title[:MAX_TITLE_CHARS]} | {_price(r.price)} | {r.seller or '?'} | {how}"
                for r, how in again.more_like[:MAX_EXAMPLES]
            ],
            "Less like these (the shopper disliked them):",
            *[
                f"  * {r.title[:MAX_TITLE_CHARS]} | {_price(r.price)} | {r.seller or '?'}"
                for r in again.less_like[:MAX_EXAMPLES]
            ],
        ]
    lines += [f"Pick at most {n} finalists, best first.", "", "Candidates:"]
    for c in pool:
        rating = f"rating {c.rating:g} ({c.rating_count})" if c.rating is not None else "no rating"
        price_text = f"{c.currency} {c.price:.2f}" if c.price is not None else "no price"
        lines.append(
            f"- id: {c.product_id} | {c.title[:MAX_TITLE_CHARS]} | seller: {c.seller or '?'} "
            f"| {price_text} | {rating}"
        )
    return "\n".join(lines)


def order_pool(
    query: ShopQuery,
    pool: list[SearchCandidate],
    budget: Budget,
    again: AgainContext | None = None,
) -> tuple[list[tuple[SearchCandidate, str]], str]:
    """The pool in the ranker's order, each with its reason; unranked results follow in pool order.

    Returns (ordered, warning). A ranking the model cannot give (unusable output) leaves the pool
    in search order with a warning; a budget error propagates.
    """
    config = budget.config
    by_id = {c.product_id: c for c in pool}
    try:
        completion = complete(
            span_name="llm.shop_rank",
            prompt=load_prompt("shop_rank", RANK_VERSION if again is None else RANK_VERSION_AGAIN),
            model=config.rank_model,
            user_text=rank_user_text(query, pool, RESULT_COUNT, again),
            budget=budget,
            max_tokens=RANK_MAX_TOKENS,
            schema=to_output_schema(RankedFinalists),
            effort=config.effort_retrieval,
            stream=True,
        )
        ranked = parse_model(RankedFinalists, completion.text, what="shop_rank")
    except (LLMOutputError, SchemaError) as exc:
        return [(c, "") for c in pool], f"Ranking unavailable ({exc}); showing search order."
    ordered: list[tuple[SearchCandidate, str]] = []
    used: set[str] = set()
    for f in ranked.finalists:
        if f.id in by_id and f.id not in used:  # an invented id is dropped, never shown
            used.add(f.id)
            ordered.append((by_id[f.id], f.reason.strip()))
    ordered.extend((c, "") for c in pool if c.product_id not in used)
    return ordered, ""


def _link(candidate: SearchCandidate) -> tuple[str, LinkKind]:
    url = candidate.google_panel_url or ""
    return (url, "google_panel") if is_https_url(url) else ("", "none")


def build_results(
    ordered: list[tuple[SearchCandidate, str]], query: ShopQuery, pool_index: dict[str, int]
) -> tuple[list[ShopResult], int]:
    """Walk the ranked order keeping at most `SELLER_CAP` per seller, up to `RESULT_COUNT`."""
    per_seller: dict[str, int] = {}
    results: list[ShopResult] = []
    capped = 0
    for candidate, reason in ordered:
        if len(results) >= RESULT_COUNT:
            break
        key = seller_key(candidate.seller)
        if per_seller.get(key, 0) >= SELLER_CAP:
            capped += 1
            continue
        per_seller[key] = per_seller.get(key, 0) + 1
        link, kind = _link(candidate)
        results.append(
            ShopResult(
                id=result_id(candidate.product_id),
                title=candidate.title,
                price=candidate.price,
                currency=candidate.currency,
                seller=candidate.seller,
                thumbnail_url=(
                    candidate.thumbnail_url
                    if candidate.thumbnail_url and is_https_url(candidate.thumbnail_url)
                    else ""
                ),
                link=link,
                link_kind=kind,
                size_hint=size_hint_for(candidate.title, query),
                product_id=candidate.product_id,
                rank=len(results) + 1,
                pool_position=pool_index[candidate.product_id],
                rank_reason=reason,
            )
        )
    return results, capped


def _search_page(
    search: SearchProvider, query: ShopQuery, page: int, budget: Budget
) -> list[SearchCandidate]:
    config: Config = budget.config
    usd = config.serper_usd_per_call if search.name == "serper" else config.serpapi_usd_per_search
    started = time.perf_counter()
    try:
        found = search.search(query.search_text, page=page)
    except ProviderError:
        _record_provider_call(
            budget, "search", f"shop p{page}", search.name, usd,
            round(time.perf_counter() - started, 3), ok=False,
        )  # fmt: skip
        raise
    _record_provider_call(
        budget, "search", f"shop p{page}", search.name, usd,
        round(time.perf_counter() - started, 3), ok=True,
    )  # fmt: skip
    return found


def _exclusions(
    pool: list[SearchCandidate], again: AgainContext | None
) -> tuple[list[SearchCandidate], int]:
    """Drop what the session already showed (by result id, or by the same title and seller)."""
    if again is None:
        return pool, 0
    kept = [
        c
        for c in pool
        if result_id(c.product_id) not in again.exclude_ids
        and normalized_title_key(c.title, c.seller) not in again.exclude_title_keys
    ]
    return kept, len(pool) - len(kept)


def _ordinal(page: int) -> str:
    return {2: "Second", 3: "Third"}.get(page, f"Page {page}")


def run_query(
    query: ShopQuery,
    search: SearchProvider,
    config: Config,
    budget: Budget,
    warnings: list[str],
    started: float,
    again: AgainContext | None = None,
) -> SearchOutcome:
    """Search, filter, (exclude), rank and cap for an already parsed `query`.

    Typed searches fetch page 2 only when fewer than `RESULT_COUNT` results could be shown; a Shop
    Again keeps going to `again.max_pages` pages while fewer than that many unseen results survive
    (it also stops at a page that returns nothing). Page 1 failing raises `ProviderError`; a later
    page failing, or ranking failing, degrades with a warning.
    """
    t_start = time.perf_counter()
    max_pages = again.max_pages if again is not None else 2

    def filtered(raw: list[SearchCandidate]) -> tuple[list[SearchCandidate], FilterCounts]:
        pool, counts = apply_filters(raw, query)
        pool, counts.seen_excluded = _exclusions(pool, again)
        counts.kept = len(pool)
        return pool, counts

    calls = 1
    raw = _search_page(search, query, 1, budget)
    pool, counts = filtered(raw)
    page = 1
    # Only Serper pages (see SerpApiSearch.search); count what could be shown after the seller cap.
    while (
        search.name == "serper"
        and page < max_pages
        and len(cap_per_seller(pool, SELLER_CAP)[0]) < RESULT_COUNT
    ):
        page += 1
        calls += 1
        try:
            more = _search_page(search, query, page, budget)
        except ProviderError as exc:
            warnings.append(f"{_ordinal(page)} page unavailable ({exc}); showing what was found.")
            break
        raw = raw + more
        pool, counts = filtered(raw)
        if not more:
            break
    t_search = time.perf_counter()

    pool = pool[:MAX_POOL]
    pool_index = {c.product_id: i + 1 for i, c in enumerate(pool)}
    ranked = False
    ordered: list[tuple[SearchCandidate, str]] = []
    if pool:
        ordered, rank_warning = order_pool(query, pool, budget, again)
        ranked = not rank_warning
        if rank_warning:
            warnings.append(rank_warning)
    if again is not None and again.deprioritize_sellers:  # not excluded: ranked after the rest
        keep = [x for x in ordered if seller_key(x[0].seller) not in again.deprioritize_sellers]
        later = [x for x in ordered if seller_key(x[0].seller) in again.deprioritize_sellers]
        ordered = keep + later
    results, capped = build_results(ordered, query, pool_index)
    counts.seller_capped = capped
    t_end = time.perf_counter()
    if again is not None and len(results) < RESULT_COUNT:
        warnings.append(
            f"Only {len(results)} new results found. Try changing the request."
            if results
            else "No new results found. Try changing the request."
        )
    elif not results:
        warnings.append("No results matched the filters. Try loosening the price or the wording.")
    return SearchOutcome(
        query=query,
        results=results,
        counts=counts,
        serper_calls=calls,
        seconds={
            "parse": round(t_start - started, 3),
            "search": round(t_search - t_start, 3),
            "rank": round(t_end - t_search, 3),
            "total": round(t_end - started, 3),
        },
        anthropic_usd=round(budget.cost_usd, 6),
        provider_usd=round(budget.provider_cost_usd, 6),
        ranked=ranked,
        warnings=warnings,
        kind="shop_again" if again is not None else "search",
        serper_credits=calls * config.serper_credits_per_call if search.name == "serper" else 0,
    )


def run_search(request: str, search: SearchProvider, config: Config) -> SearchOutcome:
    """Parse `request`, search, filter, rank and cap. Raises `ProviderError` when page 1 fails
    (nothing to show) and `BudgetExceededError` past a cap; page 2 and ranking degrade."""
    t0 = time.perf_counter()
    budget = Budget(config)
    warnings: list[str] = []
    query, _, parse_warning = parse_request(clean_request(request), budget)
    if parse_warning:
        warnings.append(parse_warning)
    return run_query(query, search, config, budget, warnings, t0)
