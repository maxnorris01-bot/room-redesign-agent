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

import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.categories import category_hits
from app.config import Config
from app.llm import Budget, LLMOutputError, complete
from app.prompts import load_prompt
from app.schemas import DimensionsM, RankedFinalists, SchemaError, parse_model, to_output_schema
from app.shop.candidates import TagBook, Taggable, tag_results
from app.shop.decide import composition, top_share
from app.shop.models import (
    MAX_SEARCH_TEXT_CHARS,
    FilterCounts,
    LinkKind,
    ShopQuery,
    ShopResult,
    clean_request,
    is_https_url,
    result_id,
)
from app.shop.parse import parse_request
from app.shop.retailers import seller_key, tier_of
from app.shop.thresholds import (
    DIVERSITY_MAX_SHARE,
    MAX_PEER_SEARCHES,
    MINORITY_POOL_SHARE,
    OPEN_STRATUM_FLOOR,
    SELLER_MAX_SHARE,
    SELLER_PINNED_SHARE,
    TIER_BATCH_SHARE,
)
from app.shop.vocab import OTHER, PHRASE_DIMS, phrase_word, tag_values, title_tags
from app.shopping import ProviderError, SearchCandidate, SearchProvider
from app.shopping_retrieval import _record_provider_call, cap_per_seller
from app.sizes import IN_M, resolve_size

MAX_EXAMPLES = 8  # loved/liked and disliked titles shown to the ranker, each
RANK_VERSION = "1"  # typed searches
RANK_VERSION_AGAIN = "4"  # Shop Again: converged and open dimensions, examples (v2, v3 kept)
RESULT_COUNT = 24  # results returned, and the fewest that avoid a second Serper page
SELLER_CAP = 4  # most results one seller may have in the returned list. UNTUNED GUESS.
MAX_POOL = 60  # candidates shown to the ranker (keeps the prompt small)
MAX_POOL_AGAIN = 80  # a Shop Again pools several phrasings, so the ranker sees more
POOL_TARGET_AGAIN = 60  # extra pages are fetched only while the pool is smaller than this
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
    affinity_sellers: set[str] = field(default_factory=set)  # seller keys: up to 50% of a batch
    pinned_sellers: set[str] = field(default_factory=set)  # seller keys the visitor pinned: 80%
    guard_caps: dict[str, float] = field(
        default_factory=dict
    )  # dimension -> most one value may take
    typed_dims: set[str] = field(
        default_factory=set
    )  # dimensions the typed request names: no warning
    converged: dict[str, str] = field(default_factory=dict)  # dimension -> the preferred value
    open_dims: dict[str, list[str]] = field(default_factory=dict)  # dimension -> liked values
    price_line: str = ""  # "mixed (...)" or a band; empty: no price signal
    dropped_phrasings: list[str] = field(default_factory=list)  # left out to fit the call cap
    likes: list[str] = field(default_factory=list)  # stated profile phrases (from comments)
    dislikes: list[str] = field(default_factory=list)
    inferred_likes: list[str] = field(default_factory=list)  # learned from the rated items
    inferred_dislikes: list[str] = field(default_factory=list)
    guesses: list[str] = field(default_factory=list)  # parser-guessed styles: not likes
    seller_affinity: str = ""
    price_band: str = ""
    more_like: list[tuple[ShopResult, str]] = field(default_factory=list)  # (result, love|like)
    less_like: list[ShopResult] = field(default_factory=list)
    extra_queries: list[str] = field(default_factory=list)  # phrasings after the adjusted query
    max_calls: int = 4  # Serper calls in all
    pool_target: int = POOL_TARGET_AGAIN
    # phrasing key (`query_key`) -> pages already fetched this session; a repeat starts after them
    skip_pages: dict[str, int] = field(default_factory=dict)
    # stage 2e: the model's tags of the pool (cached by result id), the like mass of each open
    # dimension's values (and the values with a loved item) for the stratified selection, and the
    # Serper call cap when an open dimension's minority value needs targeted searches
    tag_book: TagBook | None = None
    open_mass: dict[str, dict[str, int]] = field(default_factory=dict)
    open_loved: dict[str, set[str]] = field(default_factory=dict)
    max_calls_total: int = 9
    # stage 2f: the retailer tier the shopper likes (a tier chip), its share of the like mass,
    # whether it is pinned, and the peer retailer searches to run: (seller key, name, search text)
    affinity_tier: str = ""
    tier_share: float = 0.0
    tier_pinned: bool = False
    peer_queries: list[tuple[str, str, str]] = field(default_factory=list)


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
    queries: list[str] = field(default_factory=list)  # Serper phrasings used
    dropped_phrasings: list[str] = field(default_factory=list)  # phrasings cut to fit the call cap
    diversity: dict[str, tuple[str, float]] = field(default_factory=dict)  # dim -> (value, share)
    seller_share: tuple[str, float] | None = None  # the largest seller and its share of the batch
    display_query: ShopQuery | None = None  # what the request chips show (the typed parse)
    trace: dict[str, Any] = field(default_factory=dict)  # how the preferences were decided
    tagging: dict[str, Any] = field(default_factory=dict)  # items tagged, images, retry, fallback
    pages: dict[str, int] = field(default_factory=dict)  # phrasing key -> highest page fetched
    composition: dict[str, Any] = field(default_factory=dict)  # pool and batch makeup (stage 2e)
    new_tags: dict[str, dict[str, str | None]] = field(
        default_factory=dict
    )  # for the session cache
    peer_log: list[dict[str, Any]] = field(default_factory=list)  # peer searches and their yield
    alignment_signals: int = 0  # decided signals the match was measured against
    alignment: tuple[int, int] | None = None  # (matching, total) for a Shop Again

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
        styles = [t for t in query.style_terms if t not in again.guesses]
        lines[3] = line("Style", styles)
        lines += [
            line(
                "Style guessed by the request parser (not the shopper's, do not treat as likes)",
                again.guesses,
            ),
            line("Profile likes (the shopper's own words)", again.likes),
            line("Profile dislikes (the shopper's own words)", again.dislikes),
            "Converged preferences (learned from the rated items; apply them): "
            + (
                "; ".join(f"{d.replace('_', ' ')}: {v}" for d, v in again.converged.items())
                or "none"
            ),
            "Open dimensions (the shopper likes several values; keep them mixed, no pressure): "
            + (
                "; ".join(
                    f"{d.replace('_', ' ')}: {', '.join(v)}" for d, v in again.open_dims.items()
                )
                or "none"
            ),
            line("Inferred dislikes", again.inferred_dislikes),
            f"Seller the shopper likes: {again.seller_affinity or 'none'}",
            f"Price: {again.price_line or 'no signal'}"
            " (a hint about taste, never a limit; the price filter above is unchanged)",
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


def strata_quotas(
    values: list[str], mass: Mapping[str, int], loved: set[str], n: int = RESULT_COUNT
) -> dict[str, int]:
    """Batch slots per open value, in proportion to its like mass, within the open cap
    (`DIVERSITY_MAX_SHARE` of `n`) and with a floor of `OPEN_STRATUM_FLOOR` of `n` for a value
    with a loved item. Always adds up to `n`."""
    cap = int(DIVERSITY_MAX_SHARE * n)
    floor = math.ceil(OPEN_STRATUM_FLOOR * n)
    total = sum(mass.get(v, 0) for v in values) or len(values)
    weight = {v: (mass.get(v, 0) or (1 if total == len(values) else 0)) for v in values}
    quota = {v: round(n * weight[v] / total) for v in values}
    for v in values:
        if v in loved:
            quota[v] = max(quota[v], floor)
        quota[v] = min(quota[v], cap)
    while sum(quota.values()) > n:  # take from the value furthest above its floor
        v = max(
            (v for v in values if quota[v] > (floor if v in loved else 0)),
            key=lambda v: (quota[v], v),
        )
        quota[v] -= 1
    while sum(quota.values()) < n and any(quota[v] < cap for v in values):
        v = max((v for v in values if quota[v] < cap), key=lambda v: (weight[v], v))
        quota[v] += 1
    return quota


def build_results(
    ordered: list[tuple[SearchCandidate, str]],
    query: ShopQuery,
    pool_index: dict[str, int],
    affinity_sellers: set[str] | None = None,
    pinned_sellers: set[str] | None = None,
    guard_dims: list[str] | None = None,
    guard_caps: dict[str, float] | None = None,
    tags: Callable[[SearchCandidate], Mapping[str, str | None]] | None = None,
    strata: tuple[str, dict[str, int]] | None = None,
    tier_limit: tuple[str, int, int] | None = None,
) -> tuple[list[ShopResult], int, int]:
    """Walk the ranked order, up to `RESULT_COUNT`, keeping:

      * at most `SELLER_CAP` results per seller, except a seller the shopper has an affinity for
        (up to `SELLER_MAX_SHARE` of a batch) and a pinned seller (`SELLER_PINNED_SHARE`);
      * for each guarded dimension, at most its share of a batch (`guard_caps`:
        `DIVERSITY_MAX_SHARE` for an open dimension or shape, `CONVERGED_MAX_SHARE` for a converged
        one; `guard_dims` alone means the former) from any one known value (a result with no tag
        for the dimension is unknown and never counted); an item over that is skipped and the next
        best ranked item of another value takes its place.

    `tags` gives a candidate's values by dimension (the model's tags in a Shop Again; title
    keywords when omitted). With `strata=(dimension, quota per value)` the batch is selected
    stratified: first each value's best ranked items up to its quota, then the rest in ranked
    order, and the batch is returned in ranked order. Every cap holds in both passes. With
    `tier_limit=(tier, cap, quota)` (stage 2f) the best ranked results from that retailer tier go
    first, up to `quota`, and the tier never takes more than `cap` of the batch. Returns
    (results, skipped by the seller cap, skipped by the diversity guard).
    """
    affinity = affinity_sellers or set()
    pinned = pinned_sellers or set()
    caps = dict(guard_caps) if guard_caps else {d: DIVERSITY_MAX_SHARE for d in (guard_dims or [])}
    guard = list(caps)
    affinity_cap = int(SELLER_MAX_SHARE * RESULT_COUNT)
    pinned_cap = int(SELLER_PINNED_SHARE * RESULT_COUNT)
    value_caps = {d: int(share * RESULT_COUNT) for d, share in caps.items()}
    value_counts: dict[tuple[str, str], int] = {}
    per_seller: dict[str, int] = {}
    accepted: list[int] = []
    taken: set[int] = set()
    seller_skipped: set[int] = set()
    diversity_skipped: set[int] = set()

    def tag_of(candidate: SearchCandidate) -> Mapping[str, str | None]:
        return tags(candidate) if tags is not None else title_tags(candidate.title)

    tier_count = 0

    def try_take(index: int, candidate: SearchCandidate) -> bool:
        nonlocal tier_count
        in_tier = tier_limit is not None and tier_of(candidate.seller) == tier_limit[0]
        if in_tier and tier_limit is not None and tier_count >= tier_limit[1]:
            seller_skipped.add(index)
            return False
        key = seller_key(candidate.seller)
        cap = pinned_cap if key in pinned else affinity_cap if key in affinity else SELLER_CAP
        if per_seller.get(key, 0) >= cap:
            seller_skipped.add(index)
            return False
        got = tag_of(candidate) if guard else {}
        held = {dim: [v for v in tag_values(dim, got.get(dim)) if v != OTHER] for dim in guard}
        if any(
            value_counts.get((dim, v), 0) >= value_caps[dim] for dim in guard for v in held[dim]
        ):
            diversity_skipped.add(index)
            return False
        for dim in guard:
            for v in held[dim]:
                value_counts[(dim, v)] = value_counts.get((dim, v), 0) + 1
        per_seller[key] = per_seller.get(key, 0) + 1
        if in_tier:
            tier_count += 1
        accepted.append(index)
        taken.add(index)
        return True

    if tier_limit is not None:
        for index, (candidate, _) in enumerate(ordered):
            if tier_count >= min(tier_limit[2], tier_limit[1]) or len(accepted) >= RESULT_COUNT:
                break
            if tier_of(candidate.seller) == tier_limit[0]:
                if strata is not None:  # the tier pass leaves each shape its planned slots
                    shape_value = tag_of(candidate).get(strata[0])
                    planned = strata[1].get(shape_value) if shape_value else None
                    taken_shape = sum(
                        1 for i in accepted if tag_of(ordered[i][0]).get(strata[0]) == shape_value
                    )
                    if planned is None or taken_shape >= planned:
                        continue  # a shape outside the plan waits for the later passes
                try_take(index, candidate)
    if strata is not None:
        dim, quota = strata
        remaining = dict(quota)
        for index in accepted:  # slots the tier pass already used count toward each value
            used_value = tag_of(ordered[index][0]).get(dim)
            if used_value in remaining:
                remaining[used_value] = max(0, remaining[used_value] - 1)
        for index, (candidate, _) in enumerate(ordered):
            if len(accepted) >= RESULT_COUNT or not any(remaining.values()):
                break
            value = tag_of(candidate).get(dim)
            if value in remaining and remaining[value] > 0 and try_take(index, candidate):
                remaining[value] -= 1
    for index, (candidate, _) in enumerate(ordered):
        if len(accepted) >= RESULT_COUNT:
            break
        if index not in taken:
            try_take(index, candidate)
    results: list[ShopResult] = []
    for index in sorted(accepted):
        candidate, reason = ordered[index]
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
    return results, len(seller_skipped - taken), len(diversity_skipped - taken)


def _search_page(
    search: SearchProvider, text: str, page: int, budget: Budget
) -> list[SearchCandidate]:
    config: Config = budget.config
    usd = config.serper_usd_per_call if search.name == "serper" else config.serpapi_usd_per_search
    started = time.perf_counter()
    try:
        found = search.search(text, page=page)
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


def query_key(text: str) -> str:
    """A search phrasing normalized for comparison (case, punctuation and spacing ignored)."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


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

    A typed search fetches page 2 only when fewer than `RESULT_COUNT` results could be shown. A Shop
    Again runs page 1 of each of its phrasings (the adjusted query, then up to two built from the
    profile: `again.extra_queries`), and further pages while fewer than `again.pool_target`
    candidates survive the filters and exclusions, at most `again.max_calls` Serper calls in all; a
    phrasing that returns nothing is not paged further. The first call failing raises
    `ProviderError`; a later call failing, or ranking failing, degrades with a warning.
    """
    t_start = time.perf_counter()

    def filtered(raw: list[SearchCandidate]) -> tuple[list[SearchCandidate], FilterCounts]:
        pool, counts = apply_filters(raw, query)
        pool, counts.seen_excluded = _exclusions(pool, again)
        counts.kept = len(pool)
        return pool, counts

    texts = [query.search_text, *(again.extra_queries if again is not None else [])]
    max_calls = again.max_calls if again is not None else 2
    if search.name != "serper":  # only Serper pages; other providers get one call
        texts, max_calls = texts[:1], 1
    # the first page of every phrasing (after any pages the session already fetched for the same
    # words), then further pages of each while the pool is thin
    skip = again.skip_pages if again is not None else {}
    first = [skip.get(query_key(t), 0) + 1 for t in texts]
    plan = [(i, first[i]) for i in range(len(texts))]
    plan += [(i, first[i] + k) for k in range(1, max_calls) for i in range(len(texts))]

    def enough(pool: list[SearchCandidate]) -> bool:
        if again is not None:
            return len(pool) >= again.pool_target
        return len(cap_per_seller(pool, SELLER_CAP)[0]) >= RESULT_COUNT

    calls = 0
    raw: list[SearchCandidate] = []
    pool: list[SearchCandidate] = []
    counts = FilterCounts()
    finished: set[int] = set()  # phrasings that ran out of results
    used: list[str] = []
    pages: dict[str, int] = {}
    for i, page in plan:
        if calls >= max_calls:
            break
        if page > first[i] and (enough(pool) or i in finished):
            continue
        calls += 1
        try:
            more = _search_page(search, texts[i], page, budget)
        except ProviderError as exc:
            if calls == 1:
                raise  # nothing to show without the first page
            warnings.append(f"Search page {page} unavailable ({exc}); showing what was found.")
            finished.add(i)
            continue
        if texts[i] not in used:
            used.append(texts[i])
        pages[query_key(texts[i])] = max(pages.get(query_key(texts[i]), 0), page)
        if not more:
            finished.add(i)
        raw = raw + more
        pool, counts = filtered(raw)

    # ---- stage 2e: tag the pool, then (open dimensions) go and find the minority value ----
    book = again.tag_book if again is not None else None
    composition_info: dict[str, Any] = {}
    supplied: set[str] = set()
    if again is not None and book is not None:

        def tag_pool() -> None:
            assert book is not None
            tag_results(
                [Taggable(result_id(c.product_id), c.title, c.price, c.seller) for c in pool],
                book,
                budget,
            )

        cap = max(max_calls, again.max_calls_total) if search.name == "serper" else max_calls
        # peer retailers: when the shopper likes a seller or a tier, search similar retailers too
        # (one page each, at most `MAX_PEER_SEARCHES`, inside the total call cap)
        peer_log: list[dict[str, Any]] = []
        if search.name == "serper":
            for peer_key, peer_name, peer_text in again.peer_queries[:MAX_PEER_SEARCHES]:
                if calls >= cap:
                    break
                qkey = query_key(peer_text)
                peer_page = max(again.skip_pages.get(qkey, 0), pages.get(qkey, 0)) + 1
                calls += 1
                entry: dict[str, Any] = {
                    "peer": peer_name, "key": peer_key, "search": peer_text, "page": peer_page,
                    "found": 0, "new_candidates": 0, "error": "",
                }  # fmt: skip
                try:
                    more = _search_page(search, peer_text, peer_page, budget)
                except ProviderError as exc:
                    warnings.append(f"Search for {peer_name} unavailable ({exc}).")
                    entry["error"] = "search failed"
                    peer_log.append(entry)
                    continue
                if peer_text not in used:
                    used.append(peer_text)
                pages[qkey] = max(pages.get(qkey, 0), peer_page)
                before_ids = {c.product_id for c in pool}
                raw = raw + more
                pool, counts = filtered(raw)
                added_peer = [c for c in pool if c.product_id not in before_ids]
                supplied.update(c.product_id for c in added_peer)
                entry["found"], entry["new_candidates"] = len(more), len(added_peer)
                peer_log.append(entry)
        composition_info["peers"] = peer_log
        tag_pool()
        composition_info["pool_before"] = pool_composition(pool, again)
        supply_log: list[dict[str, Any]] = []
        extra_done: set[str] = set()
        while calls < cap:
            need = minority_values(pool, again)
            step = next(((d, v, c, k) for d, v, c, k in need if f"{d}:{v}" not in extra_done), None)
            if step is None:
                break
            dim, value, count, known = step
            text = " ".join(f"{phrase_word(dim, value)} {query.search_text}".split())[
                :MAX_SEARCH_TEXT_CHARS
            ]
            key = query_key(text)
            page = max(again.skip_pages.get(key, 0), pages.get(key, 0)) + 1
            calls += 1
            try:
                more = _search_page(search, text, page, budget)
            except ProviderError as exc:
                warnings.append(
                    f"Search for {value} {dim} unavailable ({exc}); showing what was found."
                )
                extra_done.add(f"{dim}:{value}")
                continue
            if text not in used:
                used.append(text)
            pages[key] = max(pages.get(key, 0), page)
            before = {c.product_id for c in pool}
            raw = raw + more
            pool, counts = filtered(raw)
            added = [c for c in pool if c.product_id not in before]
            supplied.update(c.product_id for c in added)
            if not more or not added:
                extra_done.add(f"{dim}:{value}")  # nothing new from this phrasing: stop asking it
            tag_pool()
            after = next(
                (
                    n
                    for d, v, n, _ in minority_values(pool, again, all_values=True)
                    if (d, v) == (dim, value)
                ),
                None,
            )
            supply_log.append(
                {
                    "dimension": dim, "value": value, "search": text, "page": page,
                    "found": len(more), "new_candidates": len(added),
                    "value_before": f"{count} of {known}", "value_after": after,
                }
            )  # fmt: skip
        composition_info["supply"] = supply_log
        composition_info["pool_after"] = pool_composition(pool, again)
    t_search = time.perf_counter()

    limit = MAX_POOL_AGAIN if again is not None else MAX_POOL
    if len(pool) > limit:  # supplied candidates are what the pool lacked: they are kept first
        keep = [c for c in pool if c.product_id in supplied][:limit]
        kept = {c.product_id for c in keep}
        rest = [c for c in pool if c.product_id not in kept][: limit - len(keep)]
        chosen = {c.product_id for c in [*keep, *rest]}
        pool = [c for c in pool if c.product_id in chosen]
    pool_index = {c.product_id: i + 1 for i, c in enumerate(pool)}
    ranked = False
    ordered: list[tuple[SearchCandidate, str]] = []
    if pool:
        ordered, rank_warning = order_pool(query, pool, budget, again)
        ranked = not rank_warning
        if rank_warning:
            warnings.append(rank_warning)
    if again is not None and again.deprioritize_sellers:  # not excluded: ranked after the rest
        keep_first = [
            x for x in ordered if seller_key(x[0].seller) not in again.deprioritize_sellers
        ]
        later = [x for x in ordered if seller_key(x[0].seller) in again.deprioritize_sellers]
        ordered = keep_first + later

    tag_of: Callable[[SearchCandidate], Mapping[str, str | None]] | None = None
    strata: tuple[str, dict[str, int]] | None = None
    if again is not None:

        def _tag_of(candidate: SearchCandidate) -> Mapping[str, str | None]:
            got = book.get(result_id(candidate.product_id)) if book is not None else None
            if got is not None:
                return got
            return title_tags(candidate.title) if book is None or book.failed else {}

        tag_of = _tag_of

        shape_values = [v for v in again.open_dims.get("shape", []) if v != OTHER]
        if len(shape_values) >= 2:
            strata = (
                "shape",
                strata_quotas(
                    shape_values,
                    again.open_mass.get("shape", {}),
                    again.open_loved.get("shape", set()),
                ),
            )
            composition_info["strata"] = {"dimension": "shape", "quota": strata[1]}
    tier_limit: tuple[str, int, int] | None = None
    if again is not None and again.affinity_tier:
        share = 0.80 if again.tier_pinned else TIER_BATCH_SHARE
        cap_n = int(share * RESULT_COUNT)
        tier_limit = (
            again.affinity_tier,
            cap_n,
            min(cap_n, round(again.tier_share * RESULT_COUNT)),
        )
        composition_info["tier"] = {
            "tier": again.affinity_tier, "cap": cap_n, "quota": tier_limit[2],
            "pinned": again.tier_pinned,
        }  # fmt: skip
    results, capped, diversity_skipped = build_results(
        ordered,
        query,
        pool_index,
        again.affinity_sellers if again is not None else None,
        again.pinned_sellers if again is not None else None,
        None,
        again.guard_caps if again is not None else None,
        tag_of,
        strata,
        tier_limit,
    )
    counts.seller_capped = capped
    diversity: dict[str, tuple[str, float]] = {}
    seller_share: tuple[str, float] | None = None
    if again is not None and results:
        dims = list(dict.fromkeys(["shape", *again.guard_caps]))
        tagged_batch: dict[str, Mapping[str, str | None]] = {
            r.id: tag_of(_candidate_of(r, ordered)) if tag_of else {} for r in results
        }
        batch_comp = composition(
            results, tagged_batch, list(dict.fromkeys([*dims, "color_finish"]))
        )
        for dim in dims:
            top = top_share(batch_comp.get(dim, {}))
            if top is not None:
                diversity[dim] = top
        by_seller: dict[str, int] = {}
        for r in results:
            by_seller[r.seller] = by_seller.get(r.seller, 0) + 1
        name = max(sorted(by_seller), key=lambda k: by_seller[k])
        seller_share = (name, round(by_seller[name] / len(results), 4))
        tier_counts: dict[str, int] = {}
        for r in results:
            label = tier_of(r.seller) or "(none)"
            tier_counts[label] = tier_counts.get(label, 0) + 1
        composition_info["batch"] = {
            "size": len(results),
            "distinct_sellers": len(by_seller),
            "tier": dict(sorted(tier_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
            **batch_comp,
            "seller": dict(sorted(by_seller.items(), key=lambda kv: (-kv[1], kv[0]))[:8]),
        }
        pool_known = composition_info.get("pool_after", {})
        for dim, cap_share in again.guard_caps.items():
            top = diversity.get(dim)
            if dim in again.typed_dims or top is None or top[1] <= cap_share:
                continue  # a dimension the typed request names is not a thing to warn about
            label = dim.replace("_", " ")
            what = "mixed" if cap_share <= DIVERSITY_MAX_SHARE else f"under {cap_share:.0%}"
            pool_counts = pool_known.get(dim, {})
            others = {v: n for v, n in pool_counts.items() if v not in (top[0], "(unknown)")}
            detail = (
                f" The pool of {len(pool)} had {pool_counts.get(top[0], 0)} {top[0]} and "
                + (", ".join(f"{n} {v}" for v, n in others.items()) or "nothing else")
                + f"; {calls} searches were run."
                if pool_counts
                else ""
            )
            warnings.append(
                f"Could not keep {label} {what}: the pool is mostly {top[0]} "
                f"({top[1]:.0%} of these results, limit {cap_share:.0%}).{detail}"
            )
        if again.dropped_phrasings:
            warnings.append(
                f"Left out {len(again.dropped_phrasings)} search phrasing(s) to stay within the "
                f"{again.max_calls}-call cap: {'; '.join(again.dropped_phrasings)}."
            )
    t_end = time.perf_counter()
    if again is not None and len(results) < RESULT_COUNT:
        warnings.append(
            f"Only {len(results)} new results found. Try changing the request."
            if results
            else "No new results found. Try changing the request."
        )
    elif not results:
        warnings.append("No results matched the filters. Try loosening the price or the wording.")
    if composition_info:
        composition_info["serper_calls"] = calls
        composition_info["serper_credits"] = (
            calls * config.serper_credits_per_call if search.name == "serper" else 0
        )
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
        queries=used,
        pages=pages,
        dropped_phrasings=list(again.dropped_phrasings) if again is not None else [],
        diversity=diversity,
        seller_share=seller_share,
        composition=composition_info,
        new_tags=dict(book.new) if book is not None else {},
        peer_log=list(composition_info.get("peers", [])),
    )


def _candidate_of(
    result: ShopResult, ordered: list[tuple[SearchCandidate, str]]
) -> SearchCandidate:
    for candidate, _ in ordered:
        if candidate.product_id == result.product_id:
            return candidate
    raise KeyError(result.product_id)


def pool_composition(pool: list[SearchCandidate], again: AgainContext) -> dict[str, Any]:
    """The pool's makeup for the trace: its size, the tag counts by shape and finish (a result with
    no tag is "(unknown)"), and the seller counts."""
    book = again.tag_book
    tags: dict[str, Mapping[str, str | None]] = {}
    for c in pool:
        rid = result_id(c.product_id)
        got = book.get(rid) if book is not None else None
        tags[rid] = (
            got
            if got is not None
            else ({} if book is not None and not book.failed else title_tags(c.title))
        )
    fake = [
        ShopResult(
            id=result_id(c.product_id), title=c.title, price=c.price, currency=c.currency,
            seller=c.seller, thumbnail_url="", link="", link_kind="none", size_hint=None,
            product_id=c.product_id, rank=0, pool_position=0,
        )
        for c in pool
    ]  # fmt: skip
    comp = composition(fake, tags, ["shape", "color_finish"])
    by_seller: dict[str, int] = {}
    for c in pool:
        by_seller[c.seller] = by_seller.get(c.seller, 0) + 1
    return {
        "size": len(pool),
        "tagged": sum(1 for c in pool if book is not None and book.get(result_id(c.product_id))),
        **comp,
        "seller": dict(sorted(by_seller.items(), key=lambda kv: (-kv[1], kv[0]))[:8]),
    }


def minority_values(
    pool: list[SearchCandidate], again: AgainContext, all_values: bool = False
) -> list[tuple[str, str, int, int]]:
    """(dimension, value, count, known) for each value of an open dimension that has search words
    and holds less than `MINORITY_POOL_SHARE` of the pool's tagged candidates (all of them when
    `all_values`), the rarest first within a dimension, shape first. A pool with fewer than 8
    tagged candidates for a dimension says nothing."""
    book = again.tag_book
    if book is None or book.failed:
        return []
    out: list[tuple[str, str, int, int]] = []
    for dim in PHRASE_DIMS:
        values = [v for v in again.open_dims.get(dim, []) if v != OTHER]
        if len(values) < 2:
            continue
        counts: dict[str, int] = {}
        for c in pool:
            got = book.get(result_id(c.product_id))
            value = got.get(dim) if got is not None else None
            if value:
                counts[value] = counts.get(value, 0) + 1
        known = sum(counts.values())
        if known < 8:
            continue
        rows = [(dim, v, counts.get(v, 0), known) for v in values]
        out += [r for r in rows if all_values or r[2] / known < MINORITY_POOL_SHARE]
    out.sort(key=lambda r: (PHRASE_DIMS.index(r[0]), r[2]))
    return out


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
