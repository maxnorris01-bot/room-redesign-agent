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
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from app.categories import category_hits
from app.config import Config
from app.llm import Budget, LLMOutputError, complete
from app.prompts import load_prompt
from app.schemas import DimensionsM, RankedFinalists, SchemaError, parse_model, to_output_schema
from app.shop.candidates import TagBook, Taggable, tag_results
from app.shop.clock import Clock
from app.shop.decide import composition, top_share
from app.shop.dedupe import Dedupe, Index, Listing
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
    # stage 2g: what was shown or rated before (the de-duplication's "seen"), the clock and the
    # phase callback of the job running this Shop Again, the absolute time it must stop at, and
    # searches already made while the preference call ran: (text, page) -> results or the error
    seen: list[Listing] = field(default_factory=list)
    clock: Clock | None = None
    progress: Callable[[str], None] | None = None
    hard_stop_at: float | None = None
    prefetched: dict[tuple[str, int], list[SearchCandidate] | Exception] = field(
        default_factory=dict
    )


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


def listing_of(c: SearchCandidate) -> Listing:
    """What the de-duplication rules look at in a search candidate."""
    return Listing(
        product_id=c.product_id,
        title=c.title,
        seller=c.seller,
        price=c.price,
        thumbnail=c.thumbnail_url or "",
        has_link=is_https_url(c.google_panel_url or ""),
    )


def seen_listing(r: ShopResult, rated: bool) -> Listing:
    return Listing(
        product_id=r.product_id,
        title=r.title,
        seller=r.seller,
        price=r.price,
        thumbnail=r.thumbnail_url,
        has_link=bool(r.link),
        rated=rated,
    )


def apply_filters(
    candidates: list[SearchCandidate], query: ShopQuery, seen: Sequence[Listing] = ()
) -> tuple[list[SearchCandidate], FilterCounts]:
    """Hard filters, then the one de-duplication (`app.shop.dedupe`, against each other and against
    `seen`: what was shown or rated earlier), in search order. Returns the pool and what each step
    removed, with the per-rule numbers in `counts.dedupe`."""
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
    pool, stats = Dedupe(listing_of).run(kept, seen)
    counts.duplicates = stats.duplicates
    counts.seen_excluded = stats.shown_before + stats.rated_before
    counts.rated_excluded = stats.rated_before
    counts.dedupe = stats.as_dict()
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
            disable_thinking=True,
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
    shown_here = Index()  # the batch so far: the same product never appears twice in it

    def try_take(index: int, candidate: SearchCandidate) -> bool:
        nonlocal tier_count
        listing = listing_of(candidate)
        if shown_here.match(listing) is not None:
            return False
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
        shown_here.add(listing)
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
    search: SearchProvider, text: str, page: int, budget: Budget, clock: Clock | None = None
) -> list[SearchCandidate]:
    config: Config = budget.config
    clock = clock or Clock()
    usd = config.serper_usd_per_call if search.name == "serper" else config.serpapi_usd_per_search
    started = clock.now()
    try:
        found = search.search(text, page=page)
    except ProviderError:
        _record_provider_call(
            budget, "search", f"shop p{page}", search.name, usd,
            round(clock.now() - started, 3), ok=False,
        )  # fmt: skip
        raise
    _record_provider_call(
        budget, "search", f"shop p{page}", search.name, usd,
        round(clock.now() - started, 3), ok=True,
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
    clock: Clock | None = None,
    progress: Callable[[str], None] | None = None,
) -> SearchOutcome:
    """Search, filter, de-duplicate, rank, (tag), cap and return the results for a parsed `query`.

    A typed search fetches page 2 only when fewer than `RESULT_COUNT` results could be shown. A Shop
    Again runs page 1 of each of its phrasings (the adjusted query, then up to two built from the
    profile) and of up to 3 peer retailers, concurrently when the provider is thread safe
    (`thread_safe`) and else one after the other; then further pages while the pool is thin. All of
    it stays inside `again.max_calls_total` Serper calls. The first phrasing's page failing raises
    `ProviderError`; a later call failing, or ranking failing, degrades with a warning.

    Stage 2g order for a Shop Again: searches, hard filters and de-duplication (against each
    other and against everything shown or rated earlier), the pool cut to 80, ONE rank call, then
    the model tags only the top `config.shop_tag_top_n` untagged candidates by rank (title
    keywords stand in for the rest), then the supply step for a thin open value, the batch
    selection, and one refill search when fewer than 24 results survive. Past
    `again.hard_stop_at` every remaining optional step is skipped and the response says so.
    """
    clock = clock or (again.clock if again is not None else None) or Clock()
    say = progress or (again.progress if again is not None else None) or (lambda _text: None)
    t_start = clock.now()
    hard_stop_at = (
        again.hard_stop_at
        if again is not None and again.hard_stop_at is not None
        else t_start + config.shop_hard_stop_s
    )
    stopped: list[str] = []  # the step the hard stop skipped, once

    def expired(step: str) -> bool:
        if clock.now() < hard_stop_at:
            return False
        if not stopped:
            stopped.append(step)
            warnings.append(
                f"Stopped at the {config.shop_hard_stop_s:.0f}-second limit before {step}; "
                "showing what was found."
            )
        return True

    seen = again.seen if again is not None else []

    def filtered(raw: list[SearchCandidate]) -> tuple[list[SearchCandidate], FilterCounts]:
        pool, counts = apply_filters(raw, query, seen)
        pool, extra = _exclusions(pool, again)
        counts.seen_excluded += extra
        counts.kept = len(pool)
        return pool, counts

    texts = [query.search_text, *(again.extra_queries if again is not None else [])]
    max_calls = again.max_calls if again is not None else 2
    if search.name != "serper":  # only Serper pages; other providers get one call
        texts, max_calls = texts[:1], 1
    cap = (
        max(max_calls, again.max_calls_total)
        if again is not None and search.name == "serper"
        else max_calls
    )
    # the first page of every phrasing (after any pages the session already fetched for the same
    # words), then further pages of each while the pool is thin
    skip = again.skip_pages if again is not None else {}
    first = [skip.get(query_key(t), 0) + 1 for t in texts]
    plan = [(i, first[i]) for i in range(len(texts))]
    plan += [(i, first[i] + k) for k in range(1, max_calls) for i in range(len(texts))]
    parallel = getattr(search, "thread_safe", False) and config.shop_search_workers > 1

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
    supplied: set[str] = set()

    consumed: set[tuple[str, int]] = set()

    def fetch(text: str, page: int) -> list[SearchCandidate] | Exception:
        """One search page (or the one already made while the preference call ran); a provider
        error is returned, not raised, so a concurrent batch can report every outcome."""
        if again is not None and (text, page) in again.prefetched:
            consumed.add((text, page))
            return again.prefetched[(text, page)]
        try:
            return _search_page(search, text, page, budget, clock)
        except ProviderError as exc:
            return exc

    def absorb(text: str, page: int, more: list[SearchCandidate]) -> list[SearchCandidate]:
        """Merge a page into the pool; returns the candidates it newly added."""
        nonlocal raw, pool, counts
        if text not in used:
            used.append(text)
        pages[query_key(text)] = max(pages.get(query_key(text), 0), page)
        before = {c.product_id for c in pool}
        raw = raw + more
        pool, counts = filtered(raw)
        return [c for c in pool if c.product_id not in before]

    peer_log: list[dict[str, Any]] = []
    peers = (
        again.peer_queries[:MAX_PEER_SEARCHES]
        if again is not None and search.name == "serper"
        else []
    )

    def peer_entry(name: str, key: str, text: str, page: int) -> dict[str, Any]:
        return {
            "peer": name, "key": key, "search": text, "page": page,
            "found": 0, "new_candidates": 0, "error": "",
        }  # fmt: skip

    say(f"Searching {len(texts) + len(peers)} retailers" if peers else "Searching")
    t_searches = clock.now()
    if parallel:
        # wave one: the first page of every phrasing and of every peer, at the same time
        jobs: list[tuple[str, int, tuple[str, str] | None]] = [
            (texts[i], first[i], None) for i in range(len(texts))
        ]
        room = max(0, cap - len(jobs))
        for peer_key, peer_name, peer_text in peers[:room]:
            qkey = query_key(peer_text)
            jobs.append((peer_text, skip.get(qkey, 0) + 1, (peer_key, peer_name)))
        jobs = jobs[:cap]
        with ThreadPoolExecutor(max_workers=min(config.shop_search_workers, len(jobs))) as ex:
            outcomes = list(ex.map(lambda j: fetch(j[0], j[1]), jobs))
        calls += len(jobs)
        for n, ((text, page, peer), got) in enumerate(zip(jobs, outcomes, strict=True)):
            entry = peer_entry(peer[1], peer[0], text, page) if peer else None
            if isinstance(got, Exception):
                if n == 0:
                    raise got  # nothing to show without the first phrasing's first page
                if peer is None:
                    warnings.append(
                        f"Search page {page} unavailable ({got}); showing what was found."
                    )
                    finished.add(n)
                else:
                    warnings.append(f"Search for {peer[1]} unavailable ({got}).")
                    assert entry is not None
                    entry["error"] = "search failed"
                    peer_log.append(entry)
                continue
            added = absorb(text, page, got)
            if not got and peer is None:
                finished.add(n)
            if peer is not None:
                supplied.update(c.product_id for c in added)
                assert entry is not None
                entry["found"], entry["new_candidates"] = len(got), len(added)
                peer_log.append(entry)
        extra_plan = [(i, pg) for i, pg in plan if pg > first[i]]
    else:
        extra_plan = list(plan)
    # sequential pages: all of `plan` when not parallel, the further pages when it is
    for i, page in extra_plan:
        if calls >= max_calls + (len(peers) if parallel else 0) and calls >= cap:
            break
        if calls >= (cap if parallel else max_calls):
            break
        if (page > first[i] or parallel) and (enough(pool) or i in finished):
            continue
        calls += 1
        got = fetch(texts[i], page)
        if isinstance(got, Exception):
            if calls == 1:
                raise got  # nothing to show without the first page
            warnings.append(f"Search page {page} unavailable ({got}); showing what was found.")
            finished.add(i)
            continue
        if not got:
            finished.add(i)
        absorb(texts[i], page, got)
    if not parallel:  # peers after the phrasings, one after the other
        for peer_key, peer_name, peer_text in peers:
            if calls >= cap:
                break
            qkey = query_key(peer_text)
            peer_page = max(skip.get(qkey, 0), pages.get(qkey, 0)) + 1
            calls += 1
            entry = peer_entry(peer_name, peer_key, peer_text, peer_page)
            got = fetch(peer_text, peer_page)
            if isinstance(got, Exception):
                warnings.append(f"Search for {peer_name} unavailable ({got}).")
                entry["error"] = "search failed"
                peer_log.append(entry)
                continue
            added = absorb(peer_text, peer_page, got)
            supplied.update(c.product_id for c in added)
            entry["found"], entry["new_candidates"] = len(got), len(added)
            peer_log.append(entry)
    if again is not None:  # a search made while the preference call ran but never used still billed
        calls += sum(1 for key in again.prefetched if key not in consumed)
    t_search = clock.now()

    # ---- cut the pool, rank it once ----
    limit = MAX_POOL_AGAIN if again is not None else MAX_POOL
    if len(pool) > limit:  # peer candidates are what the pool lacked: they are kept first
        keep = [c for c in pool if c.product_id in supplied][:limit]
        kept = {c.product_id for c in keep}
        rest = [c for c in pool if c.product_id not in kept][: limit - len(keep)]
        chosen = {c.product_id for c in [*keep, *rest]}
        pool = [c for c in pool if c.product_id in chosen]
    pool_index = {c.product_id: i + 1 for i, c in enumerate(pool)}
    ranked = False
    ordered: list[tuple[SearchCandidate, str]] = []
    t_rank = clock.now()
    if pool:
        say("Ranking")
        if expired("ranking"):
            ordered = [(c, "") for c in pool]
        else:
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
    t_ranked = clock.now()

    # ---- stage 2e/2g: tag the top candidates by rank, then find the minority value ----
    book = again.tag_book if again is not None else None
    composition_info: dict[str, Any] = {}
    supply_log: list[dict[str, Any]] = []
    refill_log: list[dict[str, Any]] = []
    tag_seconds = 0.0

    def tag_top(candidates: list[tuple[SearchCandidate, str]]) -> None:
        """Tag the first `shop_tag_top_n` candidates (in this order) that carry no model tag yet."""
        nonlocal tag_seconds
        if book is None or again is None:
            return
        todo = [
            Taggable(result_id(c.product_id), c.title, c.price, c.seller)
            for c, _ in candidates
            if book.get(result_id(c.product_id)) is None
        ][: config.shop_tag_top_n]
        if not todo or expired("tagging"):
            return
        say("Tagging results")
        t0 = clock.now()
        tag_results(
            todo,
            book,
            budget,
            clock,
            workers=config.shop_search_workers,
            until=hard_stop_at,
        )
        tag_seconds += clock.now() - t0

    if again is not None and book is not None:
        composition_info["peers"] = peer_log
        tag_top(ordered)
        composition_info["pool_before"] = pool_composition(
            [c for c, _ in ordered], again, config.shop_tag_top_n
        )
        extra_done: set[str] = set()
        while calls < cap and not expired("the supply search"):
            need = minority_values([c for c, _ in ordered], again)
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
            got = fetch(text, page)
            if isinstance(got, Exception):
                warnings.append(
                    f"Search for {value} {dim} unavailable ({got}); showing what was found."
                )
                extra_done.add(f"{dim}:{value}")
                continue
            added = absorb(text, page, got)
            supplied.update(c.product_id for c in added)
            if not got or not added:
                extra_done.add(f"{dim}:{value}")  # nothing new from this phrasing: stop asking it
            new_pairs = [(c, "") for c in added]  # appended in search order: no extra rank call
            for c, _r in new_pairs:
                pool_index[c.product_id] = len(pool_index) + 1
            ordered = ordered + new_pairs
            tag_top(new_pairs)
            after = next(
                (
                    n
                    for d, v, n, _ in minority_values([c for c, _ in ordered], again, True)
                    if (d, v) == (dim, value)
                ),
                None,
            )
            supply_log.append(
                {
                    "dimension": dim, "value": value, "search": text, "page": page,
                    "found": len(got), "new_candidates": len(added),
                    "value_before": f"{count} of {known}", "value_after": after,
                }
            )  # fmt: skip
        composition_info["supply"] = supply_log
        composition_info["pool_after"] = pool_composition(
            [c for c, _ in ordered], again, config.shop_tag_top_n
        )

    # ---- select the batch (and, when it is short, one refill search) ----
    tag_of: Callable[[SearchCandidate], Mapping[str, str | None]] | None = None
    strata: tuple[str, dict[str, int]] | None = None
    if again is not None:

        def _tag_of(candidate: SearchCandidate) -> Mapping[str, str | None]:
            got = book.get(result_id(candidate.product_id)) if book is not None else None
            if got is not None:
                return got
            return title_tags(candidate.title)  # the keyword reader stands in for untagged ones

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

    def select() -> tuple[list[ShopResult], int, int]:
        return build_results(
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

    results, capped, diversity_skipped = select()
    if (
        again is not None
        and len(results) < RESULT_COUNT
        and calls < cap
        and not expired("the refill search")
    ):
        word = _strongest_value(again)
        if word is not None:
            text = " ".join(f"{word[1]} {query.search_text}".split())[:MAX_SEARCH_TEXT_CHARS]
            key = query_key(text)
            page = max(again.skip_pages.get(key, 0), pages.get(key, 0)) + 1
            calls += 1
            got = fetch(text, page)
            if isinstance(got, Exception):
                warnings.append(f"Refill search unavailable ({got}).")
            else:
                added = absorb(text, page, got)
                supplied.update(c.product_id for c in added)
                new_pairs = [(c, "") for c in added]
                for c, _r in new_pairs:
                    pool_index[c.product_id] = len(pool_index) + 1
                ordered = ordered + new_pairs
                tag_top(new_pairs)
                refill_log.append(
                    {
                        "dimension": word[0], "value": word[1], "search": text, "page": page,
                        "found": len(got), "new_candidates": len(added),
                        "before": len(results),
                    }
                )  # fmt: skip
                results, capped, diversity_skipped = select()
                refill_log[-1]["after"] = len(results)
    counts.seller_capped = capped
    diversity: dict[str, tuple[str, float]] = {}
    seller_share: tuple[str, float] | None = None
    if again is not None and results:
        dims = list(dict.fromkeys(["shape", *again.guard_caps]))
        by_id = {result_id(c.product_id): c for c, _ in ordered}
        tagged_batch: dict[str, Mapping[str, str | None]] = {
            r.id: tag_of(by_id[r.id]) if tag_of and r.id in by_id else {} for r in results
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
        model_tagged = sum(1 for r in results if book is not None and book.get(r.id) is not None)
        composition_info["batch"] = {
            "size": len(results),
            "distinct_sellers": len(by_seller),
            "tier": dict(sorted(tier_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
            "tag_sources": {"model": model_tagged, "keyword": len(results) - model_tagged},
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
                f" The pool of {len(ordered)} had {pool_counts.get(top[0], 0)} {top[0]} and "
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
    t_end = clock.now()
    dd = counts.dedupe or {}
    filtered_out = counts.no_price + counts.out_of_range + counts.wrong_category
    explanation = (
        f"{counts.seen_excluded} seen or rated, {counts.duplicates} duplicates, "
        f"{filtered_out} filtered out"
    )
    if again is not None and len(results) < RESULT_COUNT:
        warnings.append(
            f"Only {len(results)} new results found: {explanation}. Try changing the request."
            if results
            else f"No new results found: {explanation}. Try changing the request."
        )
    elif not results:
        warnings.append("No results matched the filters. Try loosening the price or the wording.")
    if again is not None:
        composition_info["shortfall"] = {
            "wanted": RESULT_COUNT, "got": len(results), "seen_or_rated": counts.seen_excluded,
            "rated": counts.rated_excluded, "duplicates": counts.duplicates,
            "filtered_out": filtered_out, "seller_capped": capped, "guarded": diversity_skipped,
            "explanation": explanation,
        }  # fmt: skip
        composition_info["refill"] = refill_log
        composition_info["dedupe"] = dd
    if composition_info or again is not None:
        composition_info["serper_calls"] = calls
        composition_info["serper_credits"] = (
            calls * config.serper_credits_per_call if search.name == "serper" else 0
        )
        composition_info["timings"] = {
            "target_s": config.shop_target_s,
            "hard_stop_s": config.shop_hard_stop_s,
            "searches": round(t_search - t_searches, 3),
            "rank": round(t_ranked - t_rank, 3),
            "tagging": round(tag_seconds, 3),
            "total_in_search": round(t_end - t_start, 3),
            "stopped_before": stopped[0] if stopped else "",
        }
    return SearchOutcome(
        query=query,
        results=results,
        counts=counts,
        serper_calls=calls,
        seconds={
            "parse": round(max(0.0, t_start - started) if started < t_start else 0.0, 3),
            "search": round(t_search - t_start, 3),
            "rank": round(t_ranked - t_rank, 3),
            "tagging": round(tag_seconds, 3),
            "total": round(t_end - t_start, 3),
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
        peer_log=list(peer_log),
    )


def _strongest_value(again: AgainContext) -> tuple[str, str] | None:
    """The converged value (else the first open value) a refill search can name: (dimension,
    search words). Only dimensions that have search words (`PHRASE_DIMS`)."""
    for dim in PHRASE_DIMS:
        if dim in again.converged and again.converged[dim] != OTHER:
            return dim, phrase_word(dim, again.converged[dim])
    for dim in PHRASE_DIMS:
        values = [v for v in again.open_dims.get(dim, []) if v != OTHER]
        if values:
            return dim, phrase_word(dim, values[0])
    return None


def _candidate_of(
    result: ShopResult, ordered: list[tuple[SearchCandidate, str]]
) -> SearchCandidate:
    for candidate, _ in ordered:
        if candidate.product_id == result.product_id:
            return candidate
    raise KeyError(result.product_id)


def _merged_tags(c: SearchCandidate, again: AgainContext) -> tuple[Mapping[str, str | None], str]:
    """A candidate's tags and where they came from: the model's (cached by result id), else the
    title keyword reader's (`keyword`; empty when the title says nothing)."""
    book = again.tag_book
    got = book.get(result_id(c.product_id)) if book is not None else None
    if got is not None:
        return got, "model"
    return title_tags(c.title), "keyword"


def pool_composition(
    pool: list[SearchCandidate], again: AgainContext, tag_top_n: int = 0
) -> dict[str, Any]:
    """The pool's makeup for the trace: its size, how many candidates carry a model tag and how
    many only a keyword reading (the guards and the supply step act on both), the tag counts by
    shape and finish (a result with no tag is "(unknown)"), and the seller counts."""
    tags: dict[str, Mapping[str, str | None]] = {}
    sources = {"model": 0, "keyword": 0}
    for c in pool:
        got, source = _merged_tags(c, again)
        tags[result_id(c.product_id)] = got
        sources[source] += 1
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
        "tagged": sources["model"],
        "keyword_tagged": sources["keyword"],
        "tag_top_n": tag_top_n,
        **comp,
        "seller": dict(sorted(by_seller.items(), key=lambda kv: (-kv[1], kv[0]))[:8]),
    }


def minority_values(
    pool: list[SearchCandidate], again: AgainContext, all_values: bool = False
) -> list[tuple[str, str, int, int]]:
    """(dimension, value, count, known) for each value of an open dimension that has search words
    and holds less than `MINORITY_POOL_SHARE` of the pool's candidates that carry a tag (the
    model's, else the keyword reader's: a guard or a search can only act on what is tagged), the
    rarest first within a dimension, shape first. A pool with fewer than 8 tagged candidates for a
    dimension says nothing. No supply when the model's tagging failed."""
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
            got, _ = _merged_tags(c, again)
            value = got.get(dim)
            if value:
                counts[value] = counts.get(value, 0) + 1
        known = sum(counts.values())
        if known < 8:
            continue
        rows = [(dim, v, counts.get(v, 0), known) for v in values]
        out += [r for r in rows if all_values or r[2] / known < MINORITY_POOL_SHARE]
    out.sort(key=lambda r: (PHRASE_DIMS.index(r[0]), r[2]))
    return out


def run_search(
    request: str,
    search: SearchProvider,
    config: Config,
    progress: Callable[[str], None] | None = None,
    clock: Clock | None = None,
) -> SearchOutcome:
    """Parse `request`, search, filter, rank and cap. Raises `ProviderError` when page 1 fails
    (nothing to show) and `BudgetExceededError` past a cap; page 2 and ranking degrade."""
    clock = clock or Clock()
    t0 = clock.now()
    budget = Budget(config)
    warnings: list[str] = []
    if progress is not None:
        progress("Reading your request")
    query, _, parse_warning = parse_request(clean_request(request), budget)
    if parse_warning:
        warnings.append(parse_warning)
    return run_query(query, search, config, budget, warnings, t0, None, clock, progress)
