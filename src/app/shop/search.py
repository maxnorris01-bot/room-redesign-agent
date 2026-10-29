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
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

from app.categories import canonical_category, category_hits
from app.config import Config
from app.llm import Budget, LLMOutputError, complete
from app.prompts import load_prompt
from app.schemas import DimensionsM, RankedFinalists, SchemaError, parse_model, to_output_schema
from app.shop import typed as typed_mod
from app.shop.candidates import TagBook, Taggable, note_key, tag_results
from app.shop.clock import Clock
from app.shop.decide import composition, top_share
from app.shop.dedupe import Dedupe, Index, Listing
from app.shop.models import (
    MAX_SEARCH_TEXT_CHARS,
    AlsoAt,
    FilterCounts,
    LinkKind,
    ResultSource,
    ShopQuery,
    ShopResult,
    clean_request,
    is_https_url,
    result_id,
)
from app.shop.nearby import (
    Fingerprinter,
    NearItem,
    collapse,
    fingerprint_many,
    preferred_seller,
    seen_items,
)
from app.shop.parse import color_word, parse_request
from app.shop.phototags import ThumbCache, photo_tag_top
from app.shop.pricecheck import split_price_outliers
from app.shop.progress import Progress, Timeline
from app.shop.queries import QueryInput, QueryResult, extra_fillers, variant_queries
from app.shop.refine import RefineSet
from app.shop.retailers import (
    MARKETPLACE_BUDGET,
    canonical_name,
    count_sellers,
    seller_key,
    tier_of,
)
from app.shop.store import Session
from app.shop.thresholds import (
    BASE_PAGE_MAX,
    CANDIDATE_TAG_BATCH,
    COLOR_AVOID_MAX_SHARE,
    COLOR_SUGGESTED_MIN_SHARE,
    DIVERSITY_MAX_SHARE,
    FILL_EMPTY_STREAK_MAX,
    FIRST_SEARCH_MAX_CALLS,
    GUARD_ALT_SUPPLY_MIN,
    LEAN_AWAY_MAX_SHARE,
    LEAN_TOWARD_MIN_SHARE,
    MARKET_MIN_RESULTS,
    MINORITY_POOL_SHARE,
    NEAR_FP_BUDGET_S,
    NEAR_FP_TOP,
    NEAR_FP_WORKERS,
    OPEN_STRATUM_FLOOR,
    PLENTIFUL_UNSEEN,
    REFINE_FOCUS_MIN_SHARE,
    SELLER_CAP_MAX,
    SELLER_CAP_SHARE,
    SELLER_MAX_SHARE,
    SELLER_PINNED_SHARE,
    SELLER_SEARCHES,
    TIER_BATCH_SHARE,
    TYPED_MATCH_MIN_SHARE,
    seller_cap,
    seller_cap_ceiling,
)
from app.shop.typed import TypedAttr, typed_attributes
from app.shop.vocab import OTHER, PHRASE_DIMS, phrase_word, style_leaves, tag_values, title_tags
from app.shopping import ProviderError, SearchCandidate, SearchProvider
from app.shopping_retrieval import _record_provider_call, cap_per_seller
from app.sizes import FT_M, IN_M, resolve_size

MAX_EXAMPLES = 8  # loved/liked and disliked titles shown to the ranker, each
RANK_VERSION = "1"  # typed searches
RANK_VERSION_AGAIN = "6"  # Shop Again: converged, open, leanings, setting, examples (v2-v5 kept)
RANK_VERSION_CONTEXT = "6"  # a typed search whose request has a setting or colour suggestions
RESULT_COUNT = 24  # results returned, and the fewest that avoid a second Serper page
EXHAUSTED_MESSAGE = (
    "I ran out of new results for this search. Try changing the request or the refine chips."
)
SELLER_CAP = SELLER_CAP_MAX  # most results one seller may have in a full batch (see `seller_cap`)
MAX_POOL = 60  # candidates shown to the ranker (keeps the prompt small)
MAX_POOL_AGAIN = 80  # a Shop Again pools several phrasings, so the ranker sees more
POOL_TARGET_AGAIN = 36  # a Shop Again pages deeper only while the pool is smaller than a batch (24)
# plus the tagging reserve (12). Page 3 of a phrasing is a last resort; page 4 is never fetched.
LAST_RESORT_PAGE = 3
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
    timeline: Timeline | None = None
    prefetched: dict[tuple[str, int], list[SearchCandidate] | Exception] = field(
        default_factory=dict
    )
    # stage 2i: the exclude-marketplace chip (a hard filter before ranking and the seller caps)
    # and the candidates it dropped; leanings (dimension, value) toward and away from, which the
    # ranker sees and the selection holds to a soft share; the model query writer and its result;
    # where each session tag came from; the seller keys searched because the shopper liked them
    exclude_marketplace: bool = False
    marketplace_dropped: set[str] = field(default_factory=set)
    lean_toward: list[tuple[str, str]] = field(default_factory=list)
    lean_away: list[tuple[str, str]] = field(default_factory=list)
    query_writer: Callable[[], QueryResult] | None = None
    query_input: QueryInput | None = None  # stage 2r: what the writer saw, reused by the fill loop
    query_result: QueryResult | None = None
    typed: list[TypedAttr] = field(default_factory=list)  # stage 2p: typed attributes as quotas
    tag_source: dict[str, str] = field(default_factory=dict)
    liked_seller_keys: set[str] = field(default_factory=set)
    # stage 2l: the shopper's refine chips (they outrank everything inferred), what each removed
    # from the pool, and the lines the ranker is given
    refine: RefineSet | None = None
    refine_dropped: dict[str, set[str]] = field(default_factory=dict)
    refine_lines: list[str] = field(default_factory=list)


@dataclass
class NearContext:
    """What the near-duplicate step needs (stage 2n): everything already shown, rated or hidden as a
    duplicate (the seen memory), the fingerprints known so far, the fingerprint fetcher, and which
    sellers are preferred as the one to show (chip sellers and the chip tier)."""

    seen: list[NearItem] = field(default_factory=list)
    fingerprinter: Fingerprinter | None = None
    fingerprints: dict[str, int | None] = field(default_factory=dict)
    chip_sellers: set[str] = field(default_factory=set)
    chip_tier: str = ""
    top: int = NEAR_FP_TOP
    workers: int = NEAR_FP_WORKERS
    budget_s: float = NEAR_FP_BUDGET_S


def near_context(
    session: Session | None, fingerprinter: Fingerprinter | None, config: Config
) -> NearContext | None:
    """The near-duplicate step's inputs for a session (None when the feature is off)."""
    if not config.shop_near_duplicates:
        return None
    if session is None:
        return NearContext(fingerprinter=fingerprinter)
    return NearContext(
        seen=seen_items(session), fingerprinter=fingerprinter,
        fingerprints=dict(session.fingerprints),
    )  # fmt: skip


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
    pool_snapshot: list[dict[str, Any]] = field(default_factory=list)  # compact pool, for replays
    request_trace: dict[str, Any] = field(
        default_factory=dict
    )  # stage 2k: how the request was split
    duplicate_trace: dict[str, Any] = field(default_factory=dict)  # stage 2n: near-duplicates
    hidden: list[dict[str, Any]] = field(default_factory=list)  # stage 2n: hidden as duplicates
    fingerprints: dict[str, int | None] = field(default_factory=dict)  # stage 2n: by result id
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
    kept, outliers = split_price_outliers(kept, query.category_key)
    counts.price_outliers = len(outliers)
    counts.price_outlier_rows = outliers[:10]
    pool, stats = Dedupe(listing_of).run(kept, seen)
    counts.duplicates = stats.duplicates
    counts.seen_excluded = stats.shown_before + stats.rated_before
    counts.rated_excluded = stats.rated_before
    counts.dedupe = stats.as_dict()
    counts.kept = len(pool)
    return pool, counts


def format_size_hint(dims: DimensionsM, category: str | None = None) -> str | None:
    """'79" W × 36" D', '48" W × 72" L' or '58" long' from sizes in metres; None when empty. A rug
    with a width and a length reads in feet, the way rugs are sold: "8' × 10'"."""

    def inches(value: float) -> str:
        n = value / IN_M
        return f"{n:.1f}".removesuffix(".0") if n < 10 else f"{n:.0f}"

    if (
        category is not None
        and canonical_category(category) == "rug"
        and dims.width_m
        and dims.length_m
    ):

        def feet(value: float) -> str:
            n = value / FT_M
            return f"{n:.1f}".removesuffix(".0")

        return (
            f"{feet(min(dims.width_m, dims.length_m))}' × {feet(max(dims.width_m, dims.length_m))}'"
        )
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
    category = query.category_key or query.category
    size = resolve_size(category, None, [title], "")
    return format_size_hint(size.dims, category) if size.dims is not None else None


def _price(value: float | None) -> str:
    return "no price" if value is None else f"${value:,.0f}"


def has_setting(query: ShopQuery) -> bool:
    """Whether the request carries a setting or colour guidance the ranker should be told about."""
    return bool(query.context or query.suggested_colors or query.avoid_colors or query.color_terms)


def rank_version(query: ShopQuery, again: AgainContext | None) -> str:
    if again is not None:
        return RANK_VERSION_AGAIN
    return RANK_VERSION_CONTEXT if has_setting(query) else RANK_VERSION


def setting_lines(query: ShopQuery) -> list[str]:
    """The setting and colour guidance for the ranker: soft, and never something the product must
    literally contain (`shop_rank` v6)."""
    if not has_setting(query):
        return []
    kinds: dict[str, list[str]] = {}
    for item in query.context:
        kinds.setdefault(item.kind, []).append(
            item.value + (" (approximate)" if item.approximate else "")
        )
    lines: list[str] = []
    setting = [
        f"{k.replace('_', ' ')}: {', '.join(v)}"
        for k, v in kinds.items()
        if k not in ("room_size", "mood")
    ]
    if setting:
        lines.append(
            "Setting (where the product goes; NOT what it is, never require it): "
            + "; ".join(setting)
        )
    if query.color_terms:
        names = ", ".join(color_word(c) for c in query.color_terms)
        lines.append(f"Colors the product itself must be: {names}")
    if query.suggested_colors:
        names = ", ".join(color_word(c) for c in query.suggested_colors)
        lines.append(f"Suggested product colors (prefer): {names}")
    if query.avoid_colors:
        names = ", ".join(color_word(c) for c in query.avoid_colors)
        lines.append(f"Colors to avoid (demote; the product would blend in or clash): {names}")
    if "room_size" in kinds:
        lines.append(f"Size preference: compact ({', '.join(kinds['room_size'])})")
    if "mood" in kinds:
        lines.append(f"Mood (a soft style hint): {', '.join(kinds['mood'])}")
    return lines


def typed_lines(attrs: list[TypedAttr]) -> list[str]:
    """What the ranker is told about typed attributes (stage 2p): they are quotas, so favor them."""
    if not attrs:
        return []
    named = "; ".join(f"{a.dim.replace('_', ' ')}: {a.typed}" for a in attrs)
    return [
        "Typed product attributes (must favor: at least "
        f"{TYPED_MATCH_MIN_SHARE:.0%} of the final list will have them): {named}"
    ]


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
    lines += setting_lines(query)
    lines += typed_lines(again.typed if again is not None else typed_attributes(query))
    if again is not None:
        lines += again.refine_lines
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
            "Leaning toward (soft preference; rank a little higher): "
            + ("; ".join(f"{v} ({d.replace('_', ' ')})" for d, v in again.lean_toward) or "none"),
            "Leaning away from (soft avoid; rank a little lower): "
            + ("; ".join(f"{v} ({d.replace('_', ' ')})" for d, v in again.lean_away) or "none"),
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
            prompt=load_prompt("shop_rank", rank_version(query, again)),
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


def _also_at(members: list[SearchCandidate], shown_seller: str = "") -> list[AlsoAt]:
    """The other sellers of a collapsed product, one entry per seller, cheapest first (never the
    seller of the card itself)."""
    seen: set[str] = {seller_key(shown_seller)} if shown_seller else set()
    out: list[AlsoAt] = []
    for m in sorted(members, key=lambda c: (c.price is None, c.price or 0.0)):
        key = seller_key(m.seller)
        if key in seen:
            continue
        seen.add(key)
        name = canonical_name(m.seller) if m.seller else "Seller not listed"
        out.append(AlsoAt(seller=name, price=m.price))
    return out


def _valid_rating(value: float | None) -> float | None:
    return round(value, 2) if value is not None and 0 < value <= 5 else None


def _valid_count(value: int | None) -> int | None:
    return value if value is not None and value >= 0 else None


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


def filter_breakdown(
    counts: FilterCounts, shown: int, seller_capped: int, guarded: int
) -> dict[str, int]:
    """Where every candidate the searches returned went (stage 2r): shown, or removed by price,
    category or size, an implausible price, being seen or rated, being a duplicate, the seller cap,
    a diversity guard, or something else (an excluded seller or marketplace, the cut of the pool, a
    refine chip). The parts sum to `returned` by construction (`other` is the remainder)."""
    kept_unshown = max(0, counts.kept - shown)
    capped = min(seller_capped, kept_unshown)
    guard = min(guarded, kept_unshown - capped)
    parts = {
        "price": counts.no_price + counts.out_of_range,
        "category_or_size": counts.wrong_category,
        "price_outlier": counts.price_outliers,
        "seen_or_rated": counts.seen_excluded,
        "duplicate": counts.duplicates,
        "seller_cap": capped,
        "diversity_guard": guard,
    }
    other = max(0, counts.returned - shown - sum(parts.values()))
    return {"returned": counts.returned, "shown": shown, **parts, "other": other}


def explain_breakdown(counts: FilterCounts, filtered_out: int, parts: dict[str, int]) -> str:
    """The sentence a short batch shows: the old three numbers, then what made up "filtered
    out" and what the seller cap, the guards and everything else held back (zeros left out)."""
    text = (
        f"{counts.seen_excluded} seen or rated, {counts.duplicates} duplicates, "
        f"{filtered_out} filtered out"
    )
    inside = [
        f"{parts[k]} {what}"
        for k, what in (
            ("price", "outside your price"),
            ("category_or_size", "the wrong category or size"),
            ("price_outlier", "priced implausibly low"),
        )
        if parts[k]
    ]
    if inside:
        text += f" ({', '.join(inside)})"
    after = [
        f"{parts[k]} {what}"
        for k, what in (
            ("seller_cap", "held back by the seller cap"),
            ("diversity_guard", "by diversity guards"),
            ("other", "removed for other reasons"),
        )
        if parts[k]
    ]
    return text + ("; " + ", ".join(after) if after else "")


def fill_phrase(fill: dict[str, Any]) -> str:
    """What the fill loop did, for the page ("" when it did not run)."""
    steps = fill.get("steps") or []
    if not steps:
        return ""
    return (
        f"searched {len(steps)} more variant{'s' if len(steps) != 1 else ''} "
        f"(stopped: {fill.get('stop', '')})"
    )


def seller_cap_info(log: list[dict[str, Any]], pool_size: int) -> dict[str, Any]:
    """The trace of a seller-cap relaxation (empty when the first walk filled the batch): the cap
    it began with, every step tried, and the cap it settled on with the sentence the page shows."""
    if not log:
        return {}
    kept = [x for x in log if x["kept"]]
    info: dict[str, Any] = {
        "base": seller_cap(min(RESULT_COUNT, pool_size)), "steps": list(log),
        "final": kept[-1]["cap"] if kept else None,
    }  # fmt: skip
    if kept:
        info["message"] = (
            "A few sellers dominate this category; allowed up to "
            f"{kept[-1]['cap']} per seller to fill the batch."
        )
    return info


def top_seller_share(results: list[ShopResult]) -> float:
    """The largest share of `results` held by one seller (0 for none)."""
    if not results:
        return 0.0
    held: dict[str, int] = {}
    for r in results:
        key = seller_key(r.seller)
        held[key] = held.get(key, 0) + 1
    return max(held.values()) / len(results)


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
    leanings: tuple[list[tuple[str, str]], list[tuple[str, str]]] | None = None,
    colors: tuple[list[str], list[str]] | None = None,
    focus: list[tuple[str, str]] | None = None,
    groups: dict[str, list[SearchCandidate]] | None = None,
    cap_override: int | None = None,
    typed: list[TypedAttr] | None = None,
    sources: dict[str, ResultSource] | None = None,
    relax_log: list[dict[str, Any]] | None = None,
    relaxing: bool = False,
) -> tuple[list[ShopResult], int, int]:
    """Walk the ranked order, up to `RESULT_COUNT`, keeping:

      * at most `seller_cap(batch size)` results per seller (clamp(floor(0.30 x size), 2, 4);
        stage 2p: the cap follows the batch, so a short one cannot give a third to one seller;
        the walk assumes the size the pool can fill and walks again with the smaller cap if the
        batch comes out shorter), except a seller the shopper has an affinity for
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

    Stage 2q: when the first walk (the cap above) fills fewer than `RESULT_COUNT` from a pool that
    holds at least that many, the per-seller cap is raised one step at a time (5, 6, 7: up to
    `seller_cap_ceiling(RESULT_COUNT)`) until the batch is full; a relaxed batch is kept only
    when it is longer and no seller holds more than `SELLER_CAP_SHARE` of the batch actually
    shown. Each step is appended to `relax_log`. If none qualifies the shorter batch is
    tightened as before.

    Stage 2i: `leanings=(toward, away)` lists (dimension, value) pairs. Each value leaned toward
    gets at least `LEAN_TOWARD_MIN_SHARE` of the batch where the pool has it (a pass after the
    tier and the strata, never over a guard cap), and each value leaned away from at most
    `LEAN_AWAY_MAX_SHARE`. A leaning never overrides a hard filter or a cap.

    Stage 2k: `colors=(suggested, avoid)` are colour ids from the request's setting. The batch takes
    at least `COLOR_SUGGESTED_MIN_SHARE` of its results from the suggested colours where the pool
    has them (a pass before the leanings) and at most `COLOR_AVOID_MAX_SHARE` from the colours to
    avoid, under every cap and filter. A result with no known colour counts for neither.

    Stage 2l: `focus` lists the shopper's "focus on" chips as (dimension, value) pairs ("seller"
    and "tier" are pseudo-dimensions). Each takes at least `REFINE_FOCUS_MIN_SHARE` of the batch
    where the pool has it, in a pass before everything else the ratings inferred.
    """
    affinity = affinity_sellers or set()
    pinned = pinned_sellers or set()
    base_cap = cap_override or seller_cap(min(RESULT_COUNT, len(ordered)))
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
    toward, away = leanings or ([], [])
    away_cap = int(LEAN_AWAY_MAX_SHARE * RESULT_COUNT)
    toward_min = math.ceil(LEAN_TOWARD_MIN_SHARE * RESULT_COUNT)
    away_counts: dict[tuple[str, str], int] = {}
    suggested_colors = set(colors[0]) if colors else set()
    avoid_colors = set(colors[1]) if colors else set()
    avoid_color_cap = int(COLOR_AVOID_MAX_SHARE * RESULT_COUNT)
    suggested_min = math.ceil(COLOR_SUGGESTED_MIN_SHARE * RESULT_COUNT)
    avoid_color_count = 0

    def holds(candidate: SearchCandidate, dim: str, value: str) -> bool:
        got = tag_of(candidate).get(dim)
        return value in (style_leaves(got) if dim == "style" else tag_values(dim, got))

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
        cap = pinned_cap if key in pinned else affinity_cap if key in affinity else base_cap
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
        nonlocal avoid_color_count
        is_avoid = bool(avoid_colors) and tag_of(candidate).get("color") in avoid_colors
        if is_avoid and avoid_color_count >= avoid_color_cap:
            diversity_skipped.add(index)
            return False
        away_held = [(d, v) for d, v in away if holds(candidate, d, v)]
        if any(away_counts.get(pair, 0) >= away_cap for pair in away_held):
            diversity_skipped.add(index)
            return False
        for pair in away_held:
            away_counts[pair] = away_counts.get(pair, 0) + 1
        if is_avoid:
            avoid_color_count += 1
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

    refine_min = math.ceil(REFINE_FOCUS_MIN_SHARE * RESULT_COUNT)
    for f_dim, f_value in focus or []:  # the shopper's own focus chips come first
        have = sum(1 for i in accepted if _focus_holds(ordered[i][0], f_dim, f_value, tag_of))
        for index, (candidate, _) in enumerate(ordered):
            if have >= refine_min or len(accepted) >= RESULT_COUNT:
                break
            if (
                index not in taken
                and _focus_holds(candidate, f_dim, f_value, tag_of)
                and try_take(index, candidate)
            ):
                have += 1
    if (
        typed
    ):  # stage 2p: typed colour, material and finish are quotas (after the shopper's focus chips)
        want = typed_mod.target(RESULT_COUNT)
        every = typed_mod.match_filter(typed)
        have_all = sum(1 for i in accepted if every(tag_of(ordered[i][0])))
        for index, (candidate, _) in enumerate(ordered):  # items that match every typed attribute
            if have_all >= want or len(accepted) >= RESULT_COUNT:
                break
            if index not in taken and every(tag_of(candidate)) and try_take(index, candidate):
                have_all += 1
        for attr in typed:  # then, per attribute, whatever else matches it
            have = sum(1 for i in accepted if typed_mod.matches(tag_of(ordered[i][0]), attr))
            for index, (candidate, _) in enumerate(ordered):
                if have >= want or len(accepted) >= RESULT_COUNT:
                    break
                if (
                    index not in taken
                    and typed_mod.matches(tag_of(candidate), attr)
                    and try_take(index, candidate)
                ):
                    have += 1
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
    if suggested_colors:  # the suggested colours first, where the pool has them
        have_colors = sum(
            1 for i in accepted if tag_of(ordered[i][0]).get("color") in suggested_colors
        )
        for index, (candidate, _) in enumerate(ordered):
            if have_colors >= suggested_min or len(accepted) >= RESULT_COUNT:
                break
            if (
                index not in taken
                and tag_of(candidate).get("color") in suggested_colors
                and try_take(index, candidate)
            ):
                have_colors += 1
    strata_dim = strata[0] if strata is not None else None
    for dim, value in toward:  # leanings toward: their soft share, where the pool has them
        if dim == strata_dim:
            continue  # an open dimension keeps its planned slots
        have = sum(1 for i in accepted if holds(ordered[i][0], dim, value))
        for index, (candidate, _) in enumerate(ordered):
            if have >= toward_min or len(accepted) >= RESULT_COUNT:
                break
            if index not in taken and holds(candidate, dim, value) and try_take(index, candidate):
                have += 1
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
                seller=canonical_name(candidate.seller) if candidate.seller else "",
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
                rating=_valid_rating(candidate.rating),
                rating_count=_valid_count(candidate.rating_count),
                also_at=_also_at(
                    groups.get(candidate.product_id, []) if groups else [], candidate.seller
                ),
                source=sources.get(candidate.product_id) if sources else None,
            )
        )

    def rerun(cap: int, relax: bool = False) -> tuple[list[ShopResult], int, int]:
        return build_results(
            ordered, query, pool_index, affinity_sellers, pinned_sellers, guard_dims, guard_caps,
            tags, strata, tier_limit, leanings, colors, focus, groups, cap_override=cap,
            typed=typed, sources=sources, relaxing=relax,
        )  # fmt: skip

    if relaxing:  # a relaxed walk is judged by the caller; it is never tightened again
        return results, len(seller_skipped - taken), len(diversity_skipped - taken)

    if cap_override is None and len(results) < RESULT_COUNT and len(ordered) >= RESULT_COUNT:
        best: tuple[list[ShopResult], int, int] | None = None
        for relaxed_cap in range(base_cap + 1, seller_cap_ceiling(RESULT_COUNT) + 1):
            got = rerun(relaxed_cap, relax=True)
            share = top_seller_share(got[0])
            better = len(got[0]) > len(best[0] if best else results)
            kept = better and share <= SELLER_CAP_SHARE
            if relax_log is not None:
                relax_log.append(
                    {
                        "cap": relaxed_cap,
                        "size": len(got[0]),
                        "top_share": round(share, 4),
                        "kept": kept,
                    }  # fmt: skip
                )
            if kept:
                best = got
            if len(got[0]) >= RESULT_COUNT:
                break
        if best is not None:
            return best
    shorter = seller_cap(len(results))
    if len(results) < RESULT_COUNT and shorter < base_cap:  # the batch came out short: tighten
        return rerun(shorter)
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


def drop_refined(pool: list[SearchCandidate], again: AgainContext | None) -> list[SearchCandidate]:
    """The shopper's exclusions as hard filters on the candidate pool: a seller, a kind of retailer,
    or a value of a dimension (read from the model's tags where the candidate has them, else from
    its title). What each chip removes is counted by chip; nothing is ever re-added."""
    rs = again.refine if again is not None else None
    if (
        again is None
        or rs is None
        or not (rs.exclude_sellers or rs.exclude_tiers or rs.exclude_values)
    ):
        return pool
    kept = []
    for c in pool:
        chip = _excluded_by(c, again, rs)
        if chip:
            again.refine_dropped.setdefault(chip, set()).add(c.product_id)
        else:
            kept.append(c)
    return kept


def _excluded_by(c: SearchCandidate, again: AgainContext, rs: RefineSet) -> str:
    """The id of the refine chip that excludes `c`, or ''."""
    if c.seller:
        if seller_key(c.seller) in rs.exclude_sellers:
            return rs.exclude_sellers[seller_key(c.seller)]
        tier = tier_of(c.seller)
        if tier and tier in rs.exclude_tiers:
            return rs.exclude_tiers[tier]
    if rs.exclude_values:
        book = again.tag_book
        cached = (book.get(result_id(c.product_id)) if book else None) or {}
        tags = {**title_tags(c.title), **{k: v for k, v in cached.items() if v}}
        for (dim, value), chip in rs.exclude_values.items():
            if _holds(tags, dim, value):
                return chip
    return ""


def drop_marketplace(
    pool: list[SearchCandidate], again: AgainContext | None
) -> list[SearchCandidate]:
    """The exclude-marketplace chip as a hard filter: marketplace and budget sellers leave the pool
    (counted, never silently re-added)."""
    if again is None or not again.exclude_marketplace:
        return pool
    kept = []
    for c in pool:
        if c.seller and tier_of(c.seller) == MARKETPLACE_BUDGET:
            again.marketplace_dropped.add(c.product_id)
        else:
            kept.append(c)
    return kept


def query_key(text: str) -> str:
    """A search phrasing normalized for comparison (case, punctuation and spacing ignored)."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


SUPPLY_TAG_RESERVE = 12  # of the tag cap, kept for the supply and refill candidates


class TagPipeline:
    """Candidate tagging that starts while the searches are still running.

    Each search's candidates (after the hard filters and de-duplication) are queued for tagging the
    moment that search returns, in batches of at most `CANDIDATE_TAG_BATCH` titles that run at the
    same time (`shop_tag_workers`); after the rank call the top-ranked candidates still untagged
    fill what is left. In all a Shop Again queues at most `cap` titles (`APP_SHOP_TAG_TOP_N`): the
    initial work may use `cap - SUPPLY_TAG_RESERVE` of them and the supply and refill candidates
    the rest, so a title is never tagged twice and the cap holds across every step. With an
    inline pipeline (a provider that is not thread safe) each batch runs at once, in the caller.
    """

    def __init__(
        self,
        book: TagBook,
        budget: Budget,
        clock: Clock,
        config: Config,
        timeline: Timeline,
        steps: Progress | None,
        until: float,
        inline: bool,
    ) -> None:
        self.book, self.budget, self.clock, self.config = book, budget, clock, config
        self.timeline, self.steps, self.until = timeline, steps, until
        self.cap = config.shop_tag_top_n
        self.reserve = min(SUPPLY_TAG_RESERVE, self.cap // 5)
        self.queued: set[str] = set()
        self.titles_queued = 0
        self.seconds = 0.0
        self._futures: list[Future[None]] = []
        self._lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = (
            None if inline else ThreadPoolExecutor(max_workers=config.shop_tag_workers)
        )
        self._started: float | None = None
        self.deadline = until

    def room(self, supply: bool) -> int:
        left = self.cap - self.titles_queued
        return max(0, left if supply else left - self.reserve)

    def submit(self, candidates: list[SearchCandidate], supply: bool = False) -> int:
        """Queue the untagged, not yet queued candidates, in this order, within the cap. Returns how
        many titles were queued."""
        with self._lock:
            fresh: list[Taggable] = []
            for c in candidates:
                rid = result_id(c.product_id)
                if rid in self.queued or self.book.get(rid) is not None:
                    continue
                if len(fresh) >= self.room(supply):
                    break
                self.queued.add(rid)
                fresh.append(Taggable(rid, c.title, c.price, c.seller))
            self.titles_queued += len(fresh)
            if fresh and self._started is None:
                self._started = self.clock.now()
                self.deadline = min(self.until, self._started + self.config.shop_tag_budget_s)
        if not fresh:
            return 0
        if self.steps is not None:
            self.steps.begin("tagging", total=None)
            self.steps.set_total("tagging", self.titles_queued)
        size = CANDIDATE_TAG_BATCH
        for start in range(0, len(fresh), size):
            chunk = fresh[start : start + size]
            queued_at = self.clock.now()
            if self._pool is None:
                self._run(chunk, queued_at)
            else:
                self._futures.append(self._pool.submit(self._run, chunk, queued_at))
        return len(fresh)

    def _run(self, chunk: list[Taggable], queued_at: float) -> None:
        t0 = self.clock.now()
        tag_results(
            chunk, self.book, self.budget, self.clock, workers=1, until=self.deadline,
            timeline=self.timeline,
        )  # fmt: skip
        with self._lock:
            self.seconds += self.clock.now() - t0
        if self.steps is not None:
            self.steps.advance("tagging", len(chunk))

    def wait(self) -> None:
        """Wait for every queued batch (each is bounded by the tagging deadline); a budget error
        raised in a worker surfaces here."""
        futures, self._futures = self._futures, []
        for fut in futures:
            fut.result()
        if self.steps is not None and self.titles_queued:
            self.steps.finish("tagging")

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)


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
    near: NearContext | None = None,
    photos: ThumbCache | None = None,
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
        pool = drop_refined(drop_marketplace(pool, again), again)
        counts.seen_excluded += extra
        counts.kept = len(pool)
        return pool, counts

    texts = [query.search_text, *(again.extra_queries if again is not None else [])]
    max_calls = again.max_calls if again is not None else 2
    color_query = ""
    if again is None and query.suggested_colors and search.name == "serper":
        # stage 2k: page 1 of the plain query and page 1 of one that adds the top two suggested
        # colours, at the same time, instead of page 2 of the plain query (which stays the fallback)
        words = " ".join(color_word(c) for c in query.suggested_colors[:2])
        color_query = f"{query.search_text} {words}"[:MAX_SEARCH_TEXT_CHARS]
        if query_key(color_query) != query_key(query.search_text):
            texts.append(color_query)
            max_calls = 3
        else:
            color_query = ""
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
    if again is not None:  # page 4 is never fetched; page 3 only as a last resort (below)
        plan = [(i, pg) for i, pg in plan if pg <= LAST_RESORT_PAGE]
    if color_query:  # both first pages, then page 2 of the plain query only if the pool is thin
        plan = [(0, first[0]), (1, first[1]), (0, first[0] + 1)]
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
    item_sources: dict[str, ResultSource] = {}  # product id -> the search that first found it

    consumed: set[tuple[str, int]] = set()
    book = again.tag_book if again is not None else None
    timeline = (again.timeline if again is not None else None) or Timeline(clock)
    prog = progress or (again.progress if again is not None else None)
    steps = prog if isinstance(prog, Progress) else None
    if steps is not None and again is None:
        steps.skip("tagging")  # a typed search tags nothing: the step is already satisfied
    pipeline: TagPipeline | None = None
    if again is not None and book is not None and config.shop_tag_top_n > 0:
        pipeline = TagPipeline(
            book, budget, clock, config, timeline, steps, hard_stop_at, inline=not parallel
        )

    def fetch(
        text: str, page: int, queued_at: float | None = None
    ) -> list[SearchCandidate] | Exception:
        """One search page (or the one already made while the preference call ran); a provider
        error is returned, not raised, so a concurrent batch can report every outcome."""
        if again is not None and (text, page) in again.prefetched:
            consumed.add((text, page))
            return again.prefetched[(text, page)]
        try:
            with timeline.span("search", f"{text} (page {page})", queued_at):
                return _search_page(search, text, page, budget, clock)
        except ProviderError as exc:
            return exc

    def queue_tags(found: list[SearchCandidate]) -> None:
        """Start tagging one search's candidates (hard filters and de-duplication applied to that
        search alone) without waiting for the other searches."""
        if pipeline is None or again is None:
            return
        own, _ = apply_filters(found, query, seen)
        own, _extra = _exclusions(own, again)
        pipeline.submit(drop_refined(drop_marketplace(own, again), again))

    def absorb(
        text: str, page: int, more: list[SearchCandidate], kind: str = "base"
    ) -> list[SearchCandidate]:
        """Merge a page into the pool; returns the candidates it newly added. The first search that
        found a product is where it came from (`item_sources`, stage 2q)."""
        nonlocal raw, pool, counts
        for c in more:
            item_sources.setdefault(c.product_id, ResultSource(kind=kind, query=text))
        if text not in used:
            used.append(text)
        pages[query_key(text)] = max(pages.get(query_key(text), 0), page)
        before = {c.product_id for c in pool}
        raw = raw + more
        pool, counts = filtered(raw)
        return [c for c in pool if c.product_id not in before]

    peer_log: list[dict[str, Any]] = []
    peers = (
        again.peer_queries[:SELLER_SEARCHES]
        if again is not None and search.name == "serper"
        else []
    )
    model_log: list[dict[str, Any]] = []

    def peer_entry(name: str, key: str, text: str, page: int) -> dict[str, Any]:
        kind = "seller" if again is not None and key in again.liked_seller_keys else "peer"
        return {
            "peer": name, "key": key, "search": text, "page": page, "kind": kind,
            "found": 0, "new_candidates": 0, "error": "",
        }  # fmt: skip

    n_searches = len(texts) + len(peers)
    say(f"Searching {n_searches} retailers" if peers else "Searching")
    if steps is not None:
        steps.begin("searching", total=n_searches)
    timeline.phase_start("searching")
    t_searches = clock.now()
    writer = again.query_writer if again is not None and search.name == "serper" else None
    query_result: QueryResult | None = None
    # a phrasing whose next page is already past page 2 waits: page 3 is a last resort (below)
    eligible = [i for i in range(len(texts)) if again is None or first[i] <= BASE_PAGE_MAX]

    def timed_writer() -> QueryResult | None:
        if writer is None:
            return None
        with timeline.span("model", "write search queries"):
            return writer()

    def model_query_jobs(res: QueryResult | None) -> list[tuple[str, int]]:
        """The accepted model queries that fit the call cap (one call stays for the supply
        search); the rest are dropped with the reason."""
        if res is None:
            return []
        room = max(0, cap - calls - 1)
        for q in res.accepted[room:]:
            res.dropped.append({"query": q, "reason": "no search call left in the cap"})
        res.accepted = res.accepted[:room]
        return [(q, skip.get(query_key(q), 0) + 1) for q in res.accepted]

    def model_entry(text: str, page: int) -> dict[str, Any]:
        return {"query": text, "page": page, "found": 0, "new_candidates": 0, "error": ""}

    early_stops: list[dict[str, Any]] = []
    typed_early: list[TypedAttr] = again.typed if again is not None else []

    def plentiful(before: str) -> bool:
        """Stage 2r: whether the optional searches (`before` names the next one) can be skipped."""
        if (
            again is None or not config.shop_early_stop
        ):  # off in a bare Config, on from the environment
            return False
        ok, why = supply_is_plentiful(pool, again, typed_early)
        early_stops.append({"before": before, "stopped": ok, "why": why, "unseen": len(pool)})
        return ok

    if parallel:
        # wave one: the typed query and the sellers the shopper liked, at the same time; the model
        # query writer runs beside them (nothing here depends on it). Then the writer's variants.
        # The optional searches (the profile's other phrasings, retailers that are not liked
        # sellers) run only if the pool is not plentiful by then (stage 2r).
        every: list[tuple[str, int, tuple[str, str] | None]] = [
            (texts[i], first[i], None) for i in eligible
        ]
        every_index: list[int | None] = list(eligible)
        room = max(0, cap - len(every))
        for peer_key, peer_name, peer_text in peers[:room]:
            qkey = query_key(peer_text)
            every.append((peer_text, skip.get(qkey, 0) + 1, (peer_key, peer_name)))
            every_index.append(None)
        every, every_index = every[:cap], every_index[:cap]
        liked_keys = again.liked_seller_keys if again is not None else set()
        core_ix = [
            n for n, ((_t, _p, peer), idx) in enumerate(zip(every, every_index, strict=True))
            if (idx == 0 or (peer is not None and peer[0] in liked_keys))
        ] or list(range(len(every)))  # fmt: skip
        if again is None or not config.shop_early_stop:  # one wave, as before the flag
            core_ix = list(range(len(every)))
        late_ix = [n for n in range(len(every)) if n not in core_ix]
        jobs = [every[n] for n in core_ix]
        job_index = [every_index[n] for n in core_ix]
        submitted = clock.now()

        def search_and_queue(
            job: tuple[str, int, tuple[str, str] | None],
        ) -> list[SearchCandidate] | Exception:
            got = fetch(job[0], job[1], submitted)
            if not isinstance(got, Exception):
                queue_tags(got)  # tagging starts as this search returns
            if steps is not None:
                steps.advance("searching")
            return got

        def settle(
            wave: list[tuple[str, int, tuple[str, str] | None]],
            index: list[int | None],
            outcomes: list[list[SearchCandidate] | Exception],
            first_wave: bool,
        ) -> None:
            nonlocal calls
            calls += len(wave)
            for n, ((text, page, peer), idx, got) in enumerate(
                zip(wave, index, outcomes, strict=True)
            ):
                entry = peer_entry(peer[1], peer[0], text, page) if peer else None
                if isinstance(got, Exception):
                    if first_wave and n == 0:
                        raise got  # nothing to show without the first search
                    if peer is None:
                        warnings.append(
                            f"Search page {page} unavailable ({got}); showing what was found."
                        )
                        if idx is not None:
                            finished.add(idx)
                    else:
                        warnings.append(f"Search for {peer[1]} unavailable ({got}).")
                        assert entry is not None
                        entry["error"] = "search failed"
                        peer_log.append(entry)
                    continue
                added = absorb(
                    text, page, got,
                    str(entry["kind"]) if peer is not None and entry is not None else "base",
                )  # fmt: skip
                if not got and peer is None and idx is not None:
                    finished.add(idx)
                if peer is not None:
                    supplied.update(c.product_id for c in added)
                    assert entry is not None
                    entry["found"], entry["new_candidates"] = len(got), len(added)
                    peer_log.append(entry)

        workers = min(config.shop_search_workers, max(1, len(jobs))) + (1 if writer else 0)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            writer_future = ex.submit(timed_writer) if writer is not None else None
            outcomes = list(ex.map(search_and_queue, jobs))
            query_result = writer_future.result() if writer_future is not None else None
        settle(jobs, job_index, outcomes, True)
        mq = model_query_jobs(query_result)
        if mq:  # the accepted model queries, page one each, at the same time
            if steps is not None:
                steps.set_total("searching", n_searches + len(mq))
            submitted = clock.now()
            with ThreadPoolExecutor(max_workers=min(config.shop_search_workers, len(mq))) as ex2:
                found_mq = list(ex2.map(search_and_queue, [(t, pg, None) for t, pg in mq]))
            calls += len(mq)
            for (text, page), got in zip(mq, found_mq, strict=True):
                row = model_entry(text, page)
                if isinstance(got, Exception):
                    warnings.append(f"Search for a written query unavailable ({got}).")
                    row["error"] = "search failed"
                else:
                    added = absorb(text, page, got, "writer")
                    supplied.update(c.product_id for c in added)
                    row["found"], row["new_candidates"] = len(got), len(added)
                model_log.append(row)
        if late_ix:  # the optional phrasings and retailers, unless the pool is already plentiful
            late = [every[n] for n in late_ix][: max(0, cap - calls - 1)]
            late_at = [every_index[n] for n in late_ix][: len(late)]
            names = ", ".join(w[2][1] if w[2] else w[0] for w in late)
            if late and plentiful(f"{len(late)} optional search(es): {names}"):
                if steps is not None:
                    steps.set_total("searching", max(1, n_searches - len(late)))
            elif late:
                submitted = clock.now()
                with ThreadPoolExecutor(
                    max_workers=min(config.shop_search_workers, len(late))
                ) as ex3:
                    late_out = list(ex3.map(search_and_queue, late))
                settle(late, late_at, late_out, False)
        extra_plan = [
            (i, pg)
            for i, pg in plan
            if pg > first[i] or (again is not None and first[i] > BASE_PAGE_MAX)
        ]
    else:
        query_result = timed_writer()
        extra_plan = list(plan)
    # sequential pages: all of `plan` when not parallel, the further pages when it is
    for i, page in extra_plan:
        if calls >= max_calls + (len(peers) if parallel else 0) and calls >= cap:
            break
        if calls >= (cap if parallel else max_calls):
            break
        deep = again is not None and page > BASE_PAGE_MAX  # page 3: only when the pool is thin
        if (page > first[i] or parallel or deep) and (enough(pool) or i in finished):
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
        queue_tags_inline = absorb(texts[i], page, got)
        if not parallel:
            if pipeline is not None:
                pipeline.submit(queue_tags_inline)
            if steps is not None:
                steps.advance("searching")
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
            added = absorb(peer_text, peer_page, got, str(entry["kind"]))
            supplied.update(c.product_id for c in added)
            if pipeline is not None:
                pipeline.submit(added)
            if steps is not None:
                steps.advance("searching")
            entry["found"], entry["new_candidates"] = len(got), len(added)
            peer_log.append(entry)
        for text, page in model_query_jobs(query_result):
            calls += 1
            row = model_entry(text, page)
            got = fetch(text, page)
            if isinstance(got, Exception):
                warnings.append(f"Search for a written query unavailable ({got}).")
                row["error"] = "search failed"
            else:
                added = absorb(text, page, got, "writer")
                supplied.update(c.product_id for c in added)
                if pipeline is not None:
                    pipeline.submit(added)
                row["found"], row["new_candidates"] = len(got), len(added)
            model_log.append(row)
    thin_info: dict[str, Any] = {}
    fill_info: dict[str, Any] = {}
    if steps is not None:
        steps.finish("searching")
    timeline.phase_end("searching")
    if again is not None:
        again.query_result = query_result
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
    if steps is not None:
        steps.begin("ranking")
    timeline.phase_start("ranking")
    if pool:
        say("Ranking")
        if expired("ranking"):
            ordered = [(c, "") for c in pool]
        else:
            with timeline.span("model", f"rank {len(pool)} candidates"):
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
    if steps is not None:
        steps.finish("ranking")
    timeline.phase_end("ranking")

    # ---- stage 2n: the same product from other vendors is shown once ----
    groups_by_pid: dict[str, list[SearchCandidate]] = {}
    duplicate_trace: dict[str, Any] = {}
    hidden_out: list[dict[str, Any]] = []
    fingerprints_out: dict[str, int | None] = {}
    if near is not None and ordered:
        ordered, groups_by_pid, duplicate_trace, hidden_out, fingerprints_out = _collapse_near(
            ordered, near, again, hard_stop_at - clock.now()
        )
        duplicate_trace["exact"] = dict(counts.dedupe.get("by_rule", {})) if counts.dedupe else {}

    # ---- stage 2e/2g/2h: tag (the pipeline already did the searches' candidates), then find the
    # minority value ----
    composition_info: dict[str, Any] = {}
    supply_log: list[dict[str, Any]] = []
    refill_log: list[dict[str, Any]] = []
    tag_seconds = 0.0

    def tag_top(candidates: list[tuple[SearchCandidate, str]], supply: bool = False) -> None:
        """Top up the tagging with the first of `candidates` (rank order) still untagged, inside the
        Shop Again's cap, and wait for every queued batch. The supply and refill candidates may use
        the cap's reserve."""
        nonlocal tag_seconds
        if pipeline is None:
            return
        if expired("tagging"):
            return
        say("Tagging results")
        timeline.phase_start("tagging")
        pipeline.submit([c for c, _ in candidates], supply=supply)
        pipeline.wait()
        timeline.phase_end("tagging")
        tag_seconds = pipeline.seconds

    typed_attrs: list[TypedAttr] = again.typed if again is not None else typed_attributes(query)
    photo_book: TagBook | None = None
    photo_info: dict[str, Any] = {}

    def photo_step() -> None:
        """Stage 2p: tag the top candidates from their thumbnail too (a photo tag overrides the
        title tag), inside `PHOTO_TAG_BUDGET_S` and the hard stop."""
        nonlocal photo_info
        notes_on = book is not None and bool(book.notes)
        if photos is None or not ordered or (config.shop_photo_tag_top_n <= 0 and not notes_on):
            return
        if expired("the photo tagging"):
            return
        target_book = book if book is not None else photo_book
        if target_book is None:
            return
        say("Reading photos")
        with timeline.span(
            "model", f"photo tags for {min(len(ordered), config.shop_photo_tag_top_n)}"
        ):
            report = photo_tag_top(
                [
                    Taggable(
                        result_id(c.product_id), c.title, c.price, c.seller, c.thumbnail_url or ""
                    )
                    for c, _ in ordered
                ],
                target_book, budget, clock, config, photos,
                again.tag_source if again is not None else typed_sources,
                timeline, max(0.0, hard_stop_at - clock.now()),
                use_images=config.shop_photo_tag_top_n > 0,
                limit=config.shop_photo_tag_top_n or config.shop_tag_top_n,
            )  # fmt: skip
        photo_info = report.as_dict()

    typed_sources: dict[str, str] = {}
    if again is None and photos is not None and config.shop_photo_tag_top_n > 0:
        photo_book = TagBook()
    if again is not None and book is not None:
        composition_info["peers"] = peer_log
        tag_top(ordered)
        photo_step()
        if not minority_values([c for c, _ in ordered], again):
            tag_top(ordered, supply=True)  # no supply search is needed: the reserve tags more
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
            if plentiful(f"the supply search for {value} {dim}"):
                break
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
            added = absorb(text, page, got, "supply")
            supplied.update(c.product_id for c in added)
            if not got or not added:
                extra_done.add(f"{dim}:{value}")  # nothing new from this phrasing: stop asking it
            new_pairs = [(c, "") for c in added]  # appended in search order: no extra rank call
            for c, _r in new_pairs:
                pool_index[c.product_id] = len(pool_index) + 1
            ordered = ordered + new_pairs
            tag_top(new_pairs, supply=True)
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
        tag_top(ordered, supply=True)  # what the supply did not use of the cap tags the next ranked
        composition_info["pool_after"] = pool_composition(
            [c for c, _ in ordered], again, config.shop_tag_top_n
        )

    if again is None:
        photo_step()
    # ---- select the batch (and, when it is short, one refill search) ----
    tag_of: Callable[[SearchCandidate], Mapping[str, str | None]] | None = None
    strata: tuple[str, dict[str, int]] | None = None
    if again is None and photo_book is not None and photo_book.new:
        typed_book = photo_book

        def _typed_tag_of(candidate: SearchCandidate) -> Mapping[str, str | None]:
            seen_tags = typed_book.get(result_id(candidate.product_id)) or {}
            return {**title_tags(candidate.title), **{k: v for k, v in seen_tags.items() if v}}

        tag_of = _typed_tag_of
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

    seller_cap_log: list[dict[str, Any]] = []

    guard_state: list[dict[str, Any]] = []

    def select() -> tuple[list[ShopResult], int, int]:
        seller_cap_log.clear()
        guard_state.clear()
        caps_now = again.guard_caps if again is not None else None
        if again is not None:
            fallback_tags = tag_of if tag_of is not None else (lambda c: title_tags(c.title))
            caps_now, off = effective_guards(ordered, again.guard_caps, fallback_tags)
            guard_state.extend(off)
        return build_results(
            ordered,
            query,
            pool_index,
            again.affinity_sellers if again is not None else None,
            again.pinned_sellers if again is not None else None,
            None,
            caps_now,
            tag_of,
            strata,
            tier_limit,
            (again.lean_toward, again.lean_away) if again is not None else None,
            (query.suggested_colors, query.avoid_colors),
            again.refine.focus_pairs() if again is not None and again.refine else None,
            groups_by_pid,
            typed=typed_attrs,
            sources=item_sources,
            relax_log=seller_cap_log,
        )

    if (
        again is not None
        and again.refine is not None
        and (again.refine.exclude_values or again.refine.exclude_notes)
    ):
        kept_pairs = []
        for pair in ordered:  # the model's tags (better than the title) decide the exclusions
            chip = ""
            tags_now = tag_of(pair[0]) if tag_of is not None else title_tags(pair[0].title)
            for (dim, value), cid in again.refine.exclude_values.items():
                if _holds(tags_now, dim, value):
                    chip = cid
            for cid in again.refine.exclude_notes:  # an exclude note removes what is flagged true
                if tags_now.get(note_key(cid)) == "true":  # (unknown is never removed)
                    chip = cid
            if chip:
                again.refine_dropped.setdefault(chip, set()).add(pair[0].product_id)
            else:
                kept_pairs.append(pair)
        ordered = kept_pairs
    results, capped, diversity_skipped = select()

    def fill_batch() -> None:
        """Stage 2r: a batch short of RESULT_COUNT after every filter, cap and guard runs the next
        on-request variant search and selects again: the writer's variants (prompt v4, every hard
        word kept), then the neutral fillers, then page 2 of a variant that found something. It
        stops when the batch is full, at the call cap (a Shop Again's 9; a first search's
        FIRST_SEARCH_MAX_CALLS), after FILL_EMPTY_STREAK_MAX searches in a row that add nothing, or
        at the hard stop, and records every step and the reason."""
        nonlocal results, capped, diversity_skipped, ordered, calls, thin_info, fill_info
        if len(results) >= RESULT_COUNT or search.name != "serper":
            return
        if not config.shop_thin_variants:  # off in a bare Config, on from the environment
            return
        limit = cap if again is not None else FIRST_SEARCH_MAX_CALLS
        start = len(results)
        log: list[dict[str, Any]] = []
        info: dict[str, Any] = {
            "target": RESULT_COUNT, "shown_start": start, "calls_at_start": calls,
            "call_limit": limit, "steps": log, "stop": "",
        }  # fmt: skip
        fill_info = info
        if calls >= limit:
            info["stop"] = f"no search call left ({limit} used)"
            return
        if expired("the fill searches"):
            info["stop"] = "the hard stop"
            return
        base_input = again.query_input if again is not None else None
        qi = (
            replace(base_input, used=[*base_input.used, *used], existing=list(used))
            if base_input is not None
            else QueryInput(
                base=query, adjusted=query, used=list(used), existing=list(used),
                setting=setting_lines(query),
            )
        )  # fmt: skip
        with timeline.span("model", "write variant queries"):
            made = variant_queries(qi, budget, config)
        more = extra_fillers(
            replace(qi, existing=[*qi.existing, *made.accepted]),
            limit - calls - len(made.accepted),
        )
        model_n = len(made.accepted) - made.fallback
        queue: list[tuple[str, str]] = []
        for n, text in enumerate([*made.accepted, *more]):
            if again is None:
                kind = "thin_variant"
            else:
                kind = "writer" if n < model_n else "filler"
            queue.append((kind, text))
        info.update(asked_model=config.shop_model_queries, from_fallback=made.fallback + len(more),
                    dropped=list(made.dropped), note=made.note)  # fmt: skip
        paging: list[tuple[str, str]] = []
        empty = 0
        while True:
            if len(results) >= RESULT_COUNT:
                info["stop"] = "the batch is full"
            elif calls >= limit:
                info["stop"] = f"the search cap ({limit} calls) was reached"
            elif empty >= FILL_EMPTY_STREAK_MAX:
                info["stop"] = f"{FILL_EMPTY_STREAK_MAX} searches in a row added nothing new"
            elif expired("the fill searches"):
                info["stop"] = "the hard stop"
            elif not queue and not paging:
                info["stop"] = "no more variants to try"
            if info["stop"]:
                break
            kind, text = queue.pop(0) if queue else paging.pop(0)
            key = query_key(text)
            page = max(skip.get(key, 0), pages.get(key, 0)) + 1
            calls += 1
            row: dict[str, Any] = {
                "kind": kind, "query": text, "page": page, "found": 0, "new_candidates": 0,
                "shown_before": len(results), "shown_after": len(results), "error": "",
            }  # fmt: skip
            got = fetch(text, page)
            log.append(row)
            if isinstance(got, Exception):
                row["error"] = "search failed"
                warnings.append(f"A variant search was unavailable ({got}).")
                empty += 1
                continue
            added = absorb(text, page, got, kind)
            supplied.update(c.product_id for c in added)
            new_pairs = [(c, "") for c in added]
            for c, _r in new_pairs:
                pool_index[c.product_id] = len(pool_index) + 1
            ordered = ordered + new_pairs
            tag_top(new_pairs, supply=True)
            results, capped, diversity_skipped = select()
            row.update(found=len(got), new_candidates=len(added), shown_after=len(results))
            empty = 0 if added else empty + 1
            if added:
                paging.append((kind, text))  # page 2 of a variant that found something, last
        info["shown_end"] = len(results)
        if again is None:
            thin_info = {
                "before": start, "after": len(results), "asked_model": config.shop_model_queries,
                "from_fallback": info["from_fallback"], "dropped": info["dropped"],
                "note": info["note"],
                "variants": [{k: r[k] for k in ("query", "found", "new_candidates", "error")}
                             for r in log],
            }  # fmt: skip

    fill_batch()
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
                added = absorb(text, page, got, "refill")
                supplied.update(c.product_id for c in added)
                new_pairs = [(c, "") for c in added]
                for c, _r in new_pairs:
                    pool_index[c.product_id] = len(pool_index) + 1
                ordered = ordered + new_pairs
                tag_top(new_pairs, supply=True)
                refill_log.append(
                    {
                        "dimension": word[0], "value": word[1], "search": text, "page": page,
                        "found": len(got), "new_candidates": len(added),
                        "before": len(results),
                    }
                )  # fmt: skip
                results, capped, diversity_skipped = select()
                refill_log[-1]["after"] = len(results)
    cap_info = seller_cap_info(seller_cap_log, len(ordered))
    outlier_info: dict[str, Any] = (
        {"count": counts.price_outliers, "dropped": list(counts.price_outlier_rows),
         "message": (f"Left out {counts.price_outliers} listing(s) priced implausibly low for "
                     "this kind of furniture.")}
        if counts.price_outliers else {}
    )  # fmt: skip
    for note in (cap_info.get("message"), outlier_info.get("message")):
        if note:  # stage 2q: said on the page, not only in the trace
            warnings.append(str(note))
    typed_info: dict[str, Any] = {}
    if typed_attrs:
        final_tags = tag_of if tag_of is not None else (lambda c: title_tags(c.title))
        by_pid = {result_id(c.product_id): c for c, _ in ordered}
        batch_tags = [final_tags(by_pid[r.id]) for r in results if r.id in by_pid]
        pool_tags = [final_tags(c) for c, _ in ordered]
        missed = typed_mod.shortfalls(typed_attrs, list(batch_tags), list(pool_tags))
        typed_info = {
            "attributes": [{"label": a.label, "dimension": a.dim, "accepts": sorted(a.values)}
                           for a in typed_attrs],
            "quota_share": TYPED_MATCH_MIN_SHARE, "batch": len(results),
            "matched": {a.label: sum(1 for t in batch_tags if typed_mod.matches(t, a))
                        for a in typed_attrs},
            "shortfalls": missed, "photo": photo_info,
        }  # fmt: skip
        for short in missed:
            warnings.append(typed_mod.banner(short))
    elif photo_info:
        typed_info = {"photo": photo_info}
    counts.seller_capped = capped
    if again is not None:
        counts.marketplace_excluded = len(again.marketplace_dropped)
    by_id_tags: dict[str, Mapping[str, str | None]] = {}
    if again is not None and tag_of is not None:
        by_cand = {result_id(c.product_id): c for c, _ in ordered}
        by_id_tags = {r.id: tag_of(by_cand[r.id]) for r in results if r.id in by_cand}
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
        by_seller = count_sellers(r.seller for r in results)
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
            "seller": by_seller,  # every seller in the batch, by canonical name
        }
        pool_known = composition_info.get("pool_after", {})
        off_dims = {g["dimension"]: g for g in guard_state}
        for dim, cap_share in again.guard_caps.items():
            top = diversity.get(dim)
            if dim in off_dims:
                warnings.append(off_dims[dim]["message"])
                continue
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
    filtered_out = (
        counts.no_price + counts.out_of_range + counts.wrong_category + counts.price_outliers
    )
    breakdown = filter_breakdown(counts, len(results), capped, diversity_skipped)
    explanation = explain_breakdown(counts, filtered_out, breakdown)
    phrase = fill_phrase(fill_info)
    fill_note = (phrase[0].upper() + phrase[1:] + ". ") if phrase else ""
    exhausted: dict[str, Any] = {}
    if again is not None and len(results) < RESULT_COUNT:
        if results:
            warnings.append(
                f"Only {len(results)} new results found: {explanation}. "
                f"{fill_note}Try changing the request."
            )
        else:  # stage 2p: nothing unseen is left; say so plainly and keep the reason
            warnings.append(EXHAUSTED_MESSAGE)
            exhausted = {
                "reason": explanation,
                "seen_or_rated": counts.seen_excluded, "duplicates": counts.duplicates,
                "filtered_out": filtered_out, "serper_calls": calls, "breakdown": breakdown,
                "queries_used_before": len(again.skip_pages),
                "writer_asked": bool(query_result and query_result.asked),
                "writer_accepted": list(query_result.accepted) if query_result else [],
                "writer_dropped": list(query_result.dropped) if query_result else [],
            }  # fmt: skip
    elif again is None and results and len(results) < RESULT_COUNT:
        why = ", ".join(
            f"{n} {what}"
            for n, what in (
                (breakdown["price"], "outside your price"),
                (breakdown["category_or_size"], "the wrong category or size"),
                (breakdown["price_outlier"], "priced implausibly low"),
                (counts.duplicates, "duplicates"),
                (breakdown["seller_cap"], "held back by the per-seller cap"),
                (breakdown["diversity_guard"], "held back by diversity guards"),
            )
            if n
        )
        warnings.append(
            f"Only {len(results)} results matched" + (f" ({why})" if why else "")
            + f"; {phrase + '; ' if phrase else ''}try broadening the request."
        )  # fmt: skip
    elif not results:
        warnings.append("No results matched the filters. Try loosening the price or the wording.")
    if again is not None and again.refine is not None and len(results) < MARKET_MIN_RESULTS:
        responsible = [
            c.label for c in again.refine.chips if again.refine_dropped.get(c.id)
        ]  # a banner only when a refine chip removed something
        if responsible:
            warnings.append(
                f"Only {len(results)} results after your refinements ({'; '.join(responsible)}). "
                "Remove those chips to see more."
            )
    if again is not None and again.exclude_marketplace and len(results) < MARKET_MIN_RESULTS:
        warnings.append(
            f"Only {len(results)} results without marketplace retailers. "
            "Remove the chip to include them."
        )
    if again is not None:
        composition_info["shortfall"] = {
            "wanted": RESULT_COUNT, "got": len(results), "seen_or_rated": counts.seen_excluded,
            "rated": counts.rated_excluded, "duplicates": counts.duplicates,
            "filtered_out": filtered_out, "seller_capped": capped, "guarded": diversity_skipped,
            "explanation": explanation, "breakdown": breakdown,
        }  # fmt: skip
        composition_info["refill"] = refill_log
        if typed_info:
            composition_info["typed"] = typed_info
        if cap_info:
            composition_info["seller_cap"] = cap_info
        if fill_info:
            composition_info["fill"] = fill_info
        if early_stops:
            composition_info["early_stops"] = early_stops
        if guard_state:
            composition_info["guards_off"] = guard_state
        if again.refine is not None and again.refine.focus_pairs():
            composition_info["focus_supply"] = focus_supply_info(ordered, results, again, tag_of)
        if outlier_info:
            composition_info["price_outliers"] = outlier_info
        if exhausted:
            composition_info["exhausted"] = exhausted
        composition_info["dedupe"] = dd
        composition_info["marketplace_excluded"] = len(again.marketplace_dropped)
        composition_info["refine_removed"] = {k: len(v) for k, v in again.refine_dropped.items()}
        composition_info["model_queries"] = {
            "asked": bool(query_result and query_result.asked),
            "accepted": list(query_result.accepted) if query_result else [],
            "dropped": list(query_result.dropped) if query_result else [],
            "error": query_result.error if query_result else "",
            "note": query_result.note if query_result else "",
            "usd": query_result.usd if query_result else 0.0,
            "searches": model_log,
        }
        toward_in = {
            f"{d}:{v}": sum(1 for r in results if _holds(by_id_tags.get(r.id), d, v))
            for d, v in again.lean_toward
        }
        away_in = {
            f"{d}:{v}": sum(1 for r in results if _holds(by_id_tags.get(r.id), d, v))
            for d, v in again.lean_away
        }
        composition_info["leanings"] = {
            "toward": {k: {"in_batch": n, "wanted": math.ceil(LEAN_TOWARD_MIN_SHARE * RESULT_COUNT)}
                       for k, n in toward_in.items()},
            "away": {k: {"in_batch": n, "cap": int(LEAN_AWAY_MAX_SHARE * RESULT_COUNT)}
                     for k, n in away_in.items()},
        }  # fmt: skip
    if composition_info or again is not None:
        composition_info["serper_calls"] = calls
        composition_info["serper_credits"] = (
            calls * config.serper_credits_per_call if search.name == "serper" else 0
        )
        composition_info["timeline"] = timeline.export()
        if pipeline is not None:
            composition_info["tag_budget"] = {
                "cap": pipeline.cap,
                "reserve": pipeline.reserve,
                "queued": pipeline.titles_queued,
            }
        composition_info["timings"] = {
            "target_s": config.shop_target_s,
            "hard_stop_s": config.shop_hard_stop_s,
            "searches": round(t_search - t_searches, 3),
            "rank": round(t_ranked - t_rank, 3),
            "tagging": round(tag_seconds, 3),
            "total_in_search": round(t_end - t_start, 3),
            "stopped_before": stopped[0] if stopped else "",
        }
    if pipeline is not None:
        pipeline.close()
    request_trace: dict[str, Any] = {}
    if has_setting(query):
        by_cand = {result_id(c.product_id): c for c, _ in ordered}
        colors_of = [
            (
                tag_of(by_cand[r.id])
                if tag_of is not None and r.id in by_cand
                else title_tags(r.title)
            ).get("color")
            for r in results
        ]
        request_trace = {
            "search_text": query.search_text,
            "product_colors": [color_word(c) for c in query.color_terms],
            "context": [i.model_dump() for i in query.context],
            "suggested": [color_word(c) for c in query.suggested_colors],
            "avoid": [color_word(c) for c in query.avoid_colors],
            "reason": query.color_reason,
            "second_query": color_query,
            "from_suggested": sum(1 for c in colors_of if c in query.suggested_colors),
            "from_avoid": sum(1 for c in colors_of if c in query.avoid_colors),
            "results": len(results),
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
        trace={
            **({"thin": thin_info} if thin_info else {}),
            **({"fill": fill_info} if fill_info and again is None else {}),
            **({"typed": typed_info} if typed_info and again is None else {}),
            **({"seller_cap": cap_info} if cap_info and again is None else {}),
            **({"price_outliers": outlier_info} if outlier_info and again is None else {}),
        },
        new_tags=dict(book.new) if book is not None else {},
        peer_log=list(peer_log),
        request_trace=request_trace,
        duplicate_trace=duplicate_trace,
        hidden=hidden_out,
        fingerprints=fingerprints_out,
        pool_snapshot=snapshot_of(
            ordered,
            (lambda c: tag_of(c)) if tag_of is not None else (lambda c: title_tags(c.title)),
            again.tag_source if again is not None else {},
        ),
    )


def _collapse_near(
    ordered: list[tuple[SearchCandidate, str]],
    near: NearContext,
    again: AgainContext | None,
    time_left_s: float,
) -> tuple[
    list[tuple[SearchCandidate, str]],
    dict[str, list[SearchCandidate]],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, int | None],
]:
    """Fingerprint the best ranked thumbnails (bounded), then drop what repeats a shown or rated
    item and collapse the rest to one representative per product. Returns the new ordered list, the
    hidden members by representative product id, the trace, what was hidden (for the seen memory)
    and the fingerprints known."""
    items = [
        NearItem(
            key=result_id(c.product_id), title=c.title, seller=c.seller, price=c.price,
            rating=c.rating, thumbnail=c.thumbnail_url or "",
            fingerprint=near.fingerprints.get(result_id(c.product_id)),
        )
        for c, _ in ordered
    ]  # fmt: skip
    report = None
    if near.fingerprinter is not None:
        budget = max(0.0, min(near.budget_s, time_left_s))  # never past the hard stop
        report = fingerprint_many(
            items[: near.top], near.fingerprinter, near.fingerprints, near.workers, budget
        )
    sellers = set(near.chip_sellers) | (again.affinity_sellers if again is not None else set())
    tier = near.chip_tier or (again.affinity_tier if again is not None else "")
    result = collapse(items, near.seen, preferred_seller(sellers, tier))
    kept = [ordered[i] for i in result.kept]
    groups = {
        ordered[k][0].product_id: [ordered[m][0] for m in members]
        for k, members in result.groups.items()
    }
    hidden: list[dict[str, Any]] = []
    for k, members in result.groups.items():
        for m in members:
            hidden.append(_hidden_row(items[m], "group", items[k].key))
    for n, seen_key, reason in result.seen_hits:
        hidden.append(_hidden_row(items[n], reason, seen_key))
    trace: dict[str, Any] = {
        "before": len(items), "after": len(kept), "by_reason": result.by_reason,
        "examples": result.examples, "hidden": len(hidden),
        "fingerprints": report.as_dict() if report is not None else {"wanted": 0, "off": True},
    }  # fmt: skip
    known: dict[str, int | None] = {
        i.key: i.fingerprint for i in items if i.fingerprint is not None
    }
    return kept, groups, trace, hidden, known


def _hidden_row(item: NearItem, reason: str, kept: str) -> dict[str, Any]:
    return {
        "key": item.key, "title": item.title[:140], "seller": canonical_name(item.seller),
        "price": item.price, "thumbnail": item.thumbnail, "fingerprint": item.fingerprint,
        "reason": reason, "kept": kept,
    }  # fmt: skip


def _focus_holds(
    c: SearchCandidate,
    dim: str,
    value: str,
    tag_of: Callable[[SearchCandidate], Mapping[str, str | None]],
) -> bool:
    if dim == "seller":
        return seller_key(c.seller) == value
    if dim == "tier":
        return tier_of(c.seller) == value
    if dim == "note":  # a free note: the tagging flagged this item as matching it
        return tag_of(c).get(note_key(value)) == "true"
    return _holds(tag_of(c), dim, value)


def _holds(tags: Mapping[str, str | None] | None, dim: str, value: str) -> bool:
    got = (tags or {}).get(dim)
    return value in (style_leaves(got) if dim == "style" else tag_values(dim, got))


def snapshot_of(
    ordered: list[tuple[SearchCandidate, str]],
    tags: Callable[[SearchCandidate], Mapping[str, str | None]],
    sources: Mapping[str, str],
    limit: int = 100,
) -> list[dict[str, Any]]:
    """A compact copy of the pool (at most `limit` candidates, in rank order) for the session file:
    title, price, canonical seller, https thumbnail, the tags and where they came from."""
    out: list[dict[str, Any]] = []
    for c, _ in ordered[:limit]:
        rid = result_id(c.product_id)
        got = dict(tags(c))
        out.append(
            {
                "title": c.title[:140], "price": c.price, "seller": canonical_name(c.seller),
                "thumbnail_url": c.thumbnail_url if is_https_url(c.thumbnail_url or "") else "",
                "tags": {k: v for k, v in got.items() if v},
                "source": sources.get(rid, "title" if got else "keyword"),
            }
        )  # fmt: skip
    return out


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


def effective_guards(
    ordered: list[tuple[SearchCandidate, str]],
    guard_caps: dict[str, float],
    tag_of: Callable[[SearchCandidate], Mapping[str, str | None]],
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """The diversity guards that still apply to this pool (stage 2r). A guard caps how much of a
    batch one value of a dimension may take; when fewer than `GUARD_ALT_SUPPLY_MIN` unseen
    candidates carry another value it can only shrink the batch, so it is switched off for that
    dimension and the returned list says so (dimension, top value, how many carry another, the
    sentence). A guard that would not bite (the top value fits its cap anyway) is left alone."""
    caps: dict[str, float] = {}
    off: list[dict[str, Any]] = []
    for dim, share in guard_caps.items():
        held: dict[str, int] = {}
        for c, _ in ordered:
            for v in tag_values(dim, tag_of(c).get(dim)):
                if v != OTHER:
                    held[v] = held.get(v, 0) + 1
        if not held:
            caps[dim] = share
            continue
        top = max(held, key=lambda v: held[v])
        others = sum(held.values()) - held[top]
        limit = int(share * RESULT_COUNT)
        if others < GUARD_ALT_SUPPLY_MIN and held[top] > limit:
            label = dim.replace("_", " ")
            off.append(
                {
                    "dimension": dim, "top": top, "top_count": held[top], "alternatives": others,
                    "cap": limit, "message": (
                        f"Not mixing {label}: only {others} of the {len(ordered)} new candidates "
                        f"are not {top}, so the {label} limit is off for this batch."
                    ),
                }
            )  # fmt: skip
        else:
            caps[dim] = share
    return caps, off


def focus_supply_info(
    ordered: list[tuple[SearchCandidate, str]],
    results: list[ShopResult],
    again: AgainContext,
    tag_of: Callable[[SearchCandidate], Mapping[str, str | None]] | None,
) -> dict[str, Any]:
    """For each focus chip: how many unseen candidates that passed every filter match it (the
    supply the selection could draw on), and how many of the batch do (stage 2r)."""
    tags = tag_of if tag_of is not None else (lambda c: title_tags(c.title))
    ids = {result_id(c.product_id): c for c, _ in ordered}
    pairs = again.refine.focus_pairs() if again.refine is not None else []
    return {
        "unseen_pool": len(ordered),
        "pairs": [
            {
                "dimension": d, "value": v,
                "unseen_flagged": sum(1 for c, _ in ordered if _focus_holds(c, d, v, tags)),
                "in_batch": sum(
                    1 for r in results if r.id in ids and _focus_holds(ids[r.id], d, v, tags)
                ),
            }
            for d, v in pairs
        ],
    }  # fmt: skip


def supply_is_plentiful(
    candidates: list[SearchCandidate], again: AgainContext, typed: list[TypedAttr]
) -> tuple[bool, str]:
    """Whether a Shop Again's optional searches can stop (stage 2r): at least `PLENTIFUL_UNSEEN`
    unseen candidates pass the filters, and from them the typed quotas, the focus shares and the
    diversity guards can still be met (judged on the tags the candidates carry now: the model's when
    tagged, else the title keywords, so a quota the tags cannot show yet keeps the search going).
    Returns (plentiful, the reason in words)."""
    n = len(candidates)
    if n < PLENTIFUL_UNSEEN:
        return False, f"{n} unseen candidates, under {PLENTIFUL_UNSEEN}"

    def tags_of(c: SearchCandidate) -> Mapping[str, str | None]:
        return _merged_tags(c, again)[0]

    tagged = [tags_of(c) for c in candidates]
    want = typed_mod.target(RESULT_COUNT)
    for attr in typed:
        have = sum(1 for t in tagged if typed_mod.matches(t, attr))
        if have < want:
            return False, f"{attr.label}: {have} of the {want} wanted can be met so far"
    if again.refine is not None:
        reserve = math.ceil(REFINE_FOCUS_MIN_SHARE * RESULT_COUNT)
        for dim, value in again.refine.focus_pairs():
            have = sum(1 for c in candidates if _focus_holds(c, dim, value, tags_of))
            if have < reserve:
                return False, f"focus {value}: {have} of the {reserve} wanted can be met so far"
    for dim, share in again.guard_caps.items():
        if dim in again.typed_dims:
            continue
        held: dict[str, int] = {}
        for t in tagged:
            for v in tag_values(dim, t.get(dim)):
                if v != OTHER:
                    held[v] = held.get(v, 0) + 1
        others = n - max(held.values(), default=0)
        if others < RESULT_COUNT - int(share * RESULT_COUNT):
            return False, f"{dim.replace('_', ' ')}: only {others} candidates of another value"
    return True, f"{n} unseen candidates; the quotas, focus and guards can be met"


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
    by_seller = count_sellers(c.seller for c in pool)  # all of them: the batch's sellers are in it
    return {
        "size": len(pool),
        "tagged": sources["model"],
        "keyword_tagged": sources["keyword"],
        "tag_top_n": tag_top_n,
        **comp,
        "seller": by_seller,
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
    near: NearContext | None = None,
    photos: ThumbCache | None = None,
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
    return run_query(
        query, search, config, budget, warnings, t0, None, clock, progress, near, photos
    )
