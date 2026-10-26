"""Shop Again: a fresh set of results shaped by what the visitor rated.

One preference call updates the profile (`app.shop.prefs`), the typed query is adjusted by it, and
the stage 1 pipeline runs again with exclusions: everything already shown in the session (any
search, so every disliked item too), and the same listing under another id. Sellers with at least
two dislikes and no love or like are ranked last, not excluded. Hard filters are the typed query's.
The rank call (prompt shop_rank v4) also sees the profile (converged and open dimensions, price)
and the loved, liked and disliked titles; the final list is then held to the diversity and seller
limits in `search.build_results`.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from typing import Any

from app.config import Config
from app.llm import Budget
from app.shop.candidates import TagBook, Taggable, tag_results
from app.shop.clock import Clock
from app.shop.decide import Exposure, build_exposure, typed_dimensions
from app.shop.evidence import seller_entries
from app.shop.models import MAX_SEARCH_TEXT_CHARS, ShopQuery, StoredProfile
from app.shop.prefs import (
    alignment_counts,
    alignment_detail,
    alignment_signals,
    apply_adjustments,
    build_phrasings,
    build_profile,
    is_removed_in,
    rated_map,
    seller_name,
)
from app.shop.progress import Progress, Timeline
from app.shop.queries import QueryInput, write_queries
from app.shop.refine import (
    RefineSet,
    apply_to_query,
    build_set,
    ranker_lines,
    suppress_inferred,
    writer_lines,
)
from app.shop.retailers import (
    AFFINITY_TIERS,
    Retailer,
    peers_of,
    retailer_of,
    seller_key,
    tier_members,
)
from app.shop.search import (
    AgainContext,
    SearchOutcome,
    _holds,
    _search_page,
    normalized_title_key,
    query_key,
    run_query,
    seen_listing,
    setting_lines,
)
from app.shop.store import Session, current_topic, rated_results, sellers_to_deprioritize
from app.shop.thresholds import (
    BASE_PAGE_MAX,
    CONVERGED_MAX_SHARE,
    DIVERSITY_MAX_SHARE,
    QUERY_DISLIKED_TITLES,
    QUERY_LIKED_TITLES,
    REFINE_FOCUS_MIN_SHARE,
    SELLER_SEARCHES,
)
from app.shop.vocab import DIMENSIONS, title_tags
from app.shopping import ProviderError, SearchCandidate, SearchProvider
from app.thumbnails import Thumbnailer


def base_query(session: Session) -> ShopQuery | None:
    """The last typed search's query: Shop Again always adjusts that, never an earlier Shop Again's,
    so adjustments do not stack on themselves (the profile is already cumulative)."""
    typed = [s for s in session.searches if s.kind == "search"]
    return typed[-1].query if typed else None


def pages_fetched(session: Session) -> dict[str, int]:
    """Per search phrasing, the highest Serper page the session already fetched. A typed search from
    before pages were recorded counts as `serper_calls` pages of its phrasing."""
    out: dict[str, int] = {}
    for rec in session.searches:
        pages = rec.pages or (
            {query_key(rec.query.search_text): rec.serper_calls} if rec.kind == "search" else {}
        )
        for key, page in pages.items():
            out[key] = max(out.get(key, 0), page)
    return out


def peer_plan(
    session: Session, profile: StoredProfile, adjusted: ShopQuery
) -> list[tuple[str, str, str]]:
    """The peer retailer searches for this Shop Again: (seller key, name, search text).

    Only when a seller affinity or a tier affinity was decided. Candidates are the peers of the
    liked seller and of the liked sellers of the tier (most similar first), then the rest of the
    tier, never a retailer the shopper already rated, never one the session deprioritizes. Peers
    not searched yet in this session come first (up to `MAX_PEER_SEARCHES`, so later Shop Agains
    rotate on); if fewer than that remain, a peer that returned something new before is searched
    again (its next page); a peer that returned nothing new is never searched again.
    """
    sellers = [e for e in profile.inferred if e.attr_kind == "seller"]
    tier = next((e for e in profile.inferred if e.attr_kind == "tier"), None)
    if not sellers and tier is None:
        return []
    rows = rated_results(session)
    rated_keys = {seller_key(r["result"].seller) for r in rows if r["result"].seller}
    mass: dict[str, int] = {}
    for r in rows:
        if r["rating"] in ("love", "like") and r["result"].seller:
            weight = 3 if r["rating"] == "love" else 1
            mass[r["result"].seller] = mass.get(r["result"].seller, 0) + weight
    sources: list[str] = []
    for seller in sellers:
        found_seller = retailer_of(seller_name(seller))
        if found_seller is not None and found_seller.tier in AFFINITY_TIERS:
            sources.append(seller_name(seller))  # a marketplace seller has no peers to search
    if tier is not None and tier.values:
        in_tier = [
            name
            for name, _ in sorted(mass.items(), key=lambda kv: (-kv[1], kv[0]))
            if (found := retailer_of(name)) is not None and found.tier == tier.values[0]
        ]
        sources += [n for n in in_tier if seller_key(n) not in {seller_key(x) for x in sources}]
    ordered: list[Retailer] = []
    skip = sellers_to_deprioritize(session)
    for src in sources:
        ordered += peers_of(src)
    if tier is not None and tier.values:
        ordered += tier_members(tier.values[0])
    if not ordered:
        return []
    seen_keys: set[str] = set()
    peers: list[Retailer] = []
    for cand in ordered:
        key = seller_key(cand.name)
        if key in seen_keys or key in rated_keys or key in skip:
            continue
        seen_keys.add(key)
        peers.append(cand)
    done = session.searched_peers
    fresh = [p for p in peers if seller_key(p.name) not in done][:SELLER_SEARCHES]
    again_ok = [
        p
        for p in peers
        if seller_key(p.name) in done
        and done[seller_key(p.name)].get("new", 0) > 0
        and not done[seller_key(p.name)].get("empty")
    ]
    chosen = [*fresh, *again_ok][:SELLER_SEARCHES]
    out: list[tuple[str, str, str]] = []
    for p in chosen:
        text = " ".join(f"{p.search_name} {adjusted.search_text}".split())[:MAX_SEARCH_TEXT_CHARS]
        out.append((seller_key(p.name), p.name, text))
    return out


def seller_plan(
    session: Session, profile: StoredProfile, adjusted: ShopQuery
) -> tuple[list[tuple[str, str, str]], set[str]]:
    """The seller searches of this Shop Again: (seller key, name, search text) and the keys of the
    sellers that were searched because the shopper liked them.

    Up to `SELLER_SEARCHES` sellers chosen by like-rate evidence (highest group like rate first,
    never a marketplace or budget retailer, never one whose chip the shopper deleted, never one
    whose last search found nothing new): "<seller> <request search text>", the next page not yet
    fetched. If fewer qualify, the remaining slots go to peers of the same tier, as before. None
    of this needs a chip to be showing."""
    earned, _ = seller_entries(rated_map(session))
    done = session.searched_peers
    out: list[tuple[str, str, str]] = []
    liked: set[str] = set()
    for entry in earned:
        name = seller_name(entry)
        key = seller_key(name)
        found = retailer_of(name)
        if found is not None and found.tier not in AFFINITY_TIERS:
            continue
        if is_removed_in(entry.text, profile.removed) or done.get(key, {}).get("empty"):
            continue
        text = " ".join(f"{name} {adjusted.search_text}".split())[:MAX_SEARCH_TEXT_CHARS]
        out.append((key, name, text))
        liked.add(key)
        if len(out) >= SELLER_SEARCHES:
            break
    if len(out) < SELLER_SEARCHES:
        for peer in peer_plan(session, profile, adjusted):
            if peer[0] not in liked and all(peer[0] != o[0] for o in out):
                out.append(peer)
            if len(out) >= SELLER_SEARCHES:
                break
    return out, liked


def again_context(
    session: Session,
    profile: StoredProfile,
    config: Config,
    queries: list[str],
    dropped: list[str] | None = None,
    book: TagBook | None = None,
    adjusted: ShopQuery | None = None,
) -> AgainContext:
    """Exclusions, seller rules, the profile (stated, converged, open, guesses), the phrasings.

    Only converged entries press on search and ranking. Open dimensions and a mixed price are
    passed to the ranker as "keep mixed" and "no signal". The final list is held to a 60%
    single-value cap on shape and on every open dimension and 80% on a converged one, except
    dimensions the typed request names.
    """
    seen = session.results
    rows = rated_results(session)
    inferred = profile.inferred
    sellers = [e for e in inferred if e.attr_kind == "seller"]
    price = next((e for e in inferred if e.attr_kind in ("price_band", "price_mixed")), None)
    converged = {
        e.dimension: e.values[0]
        for e in inferred
        if e.polarity == "like"
        and e.status == "converged"
        and e.dimension in DIMENSIONS
        and e.values
    }
    open_dims = {e.dimension: list(e.values) for e in inferred if e.status == "open"}
    dim_trace = profile.trace.get("dimensions", {}) if profile.trace else {}
    open_mass = {
        dim: {
            v: int(m) for v, m in dim_trace.get(dim, {}).get("like_mass", {}).items() if v in vals
        }
        for dim, vals in open_dims.items()
    }
    open_loved = {
        dim: {v for v in dim_trace.get(dim, {}).get("loved", []) if v in vals}
        for dim, vals in open_dims.items()
    }
    base = base_query(session)
    typed_dims = typed_dimensions(base) if base is not None else set()
    # shape is always guarded (60%); an open dimension too; a converged one is capped at 80%
    caps: dict[str, float] = {dim: DIVERSITY_MAX_SHARE for dim in open_dims}
    if "shape" not in converged:
        caps["shape"] = DIVERSITY_MAX_SHARE
    for dim in converged:
        caps[dim] = CONVERGED_MAX_SHARE
    caps = {d: c for d, c in caps.items() if d not in typed_dims and d in DIMENSIONS}
    tier = next((e for e in inferred if e.attr_kind == "tier"), None)
    peers, liked_keys = (
        seller_plan(session, profile, adjusted) if adjusted is not None else ([], set())
    )
    price_line = ""
    if price is not None:
        price_line = f"mixed ({price.detail})" if price.status == "mixed" else price.text
    return AgainContext(
        exclude_ids=set(seen),
        exclude_title_keys={normalized_title_key(r.title, r.seller) for r in seen.values()},
        deprioritize_sellers=sellers_to_deprioritize(session),
        affinity_sellers={seller_key(seller_name(e)) for e in sellers},
        pinned_sellers=set(profile.pinned_sellers),
        guard_caps=caps,
        typed_dims=typed_dims,
        converged=converged,
        open_dims=open_dims,
        price_line=price_line,
        likes=list(profile.likes),
        dislikes=list(profile.dislikes),
        inferred_likes=[
            e.text for e in inferred if e.polarity == "like" and e.status == "converged"
        ],
        inferred_dislikes=[e.text for e in inferred if e.polarity == "dislike"],
        guesses=list(profile.guesses),
        seller_affinity=", ".join(seller_name(e) for e in sellers),
        price_band=price.text if price is not None else "",
        more_like=[(r["result"], r["rating"]) for r in rows if r["rating"] in ("love", "like")],
        less_like=[r["result"] for r in rows if r["rating"] == "dislike"],
        extra_queries=queries[1:],
        dropped_phrasings=list(dropped or []),
        max_calls=config.shop_again_max_serper_calls,
        skip_pages=pages_fetched(session),
        tag_book=book,
        seen=[
            seen_listing(
                r,
                rated=session.ratings.get(rid) is not None
                and session.ratings[rid].rating is not None,
            )
            for rid, r in session.results.items()
        ],
        open_mass=open_mass,
        open_loved=open_loved,
        max_calls_total=config.shop_again_max_serper_calls_total,
        affinity_tier=tier.values[0] if tier is not None and tier.values else "",
        tier_share=(tier.share or 0.0) if tier is not None else 0.0,
        tier_pinned=bool(tier is not None and tier.pinned),
        peer_queries=peers,
        exclude_marketplace=any(e.attr_kind == "exclude_marketplace" for e in inferred),
        lean_toward=[
            (e.dimension, e.values[0])
            for e in inferred
            if e.attr_kind == "leaning_toward" and e.values
        ],
        lean_away=[
            (e.dimension, e.values[0])
            for e in inferred
            if e.attr_kind == "leaning_away" and e.values
        ],
        tag_source=dict(session.tag_source),
        liked_seller_keys=liked_keys,
    )


def refine_trace(
    rs: RefineSet, ctx: AgainContext, outcome: SearchOutcome, suppressed: list[str]
) -> dict[str, Any]:
    """How each refine chip was applied: candidates removed, the share it holds of the batch, a
    hint to the ranker, a constraint for the query writer; and what it suppressed."""
    rows = []
    for c in rs.chips:
        applied: dict[str, Any] = {"query_constraint": True, "rank_hint": True}
        if c.kind.startswith("exclude"):
            applied["removed"] = len(ctx.refine_dropped.get(c.id, ()))
        elif c.kind in ("focus_value", "focus_seller"):
            pair = (
                ("seller", seller_key(c.target))
                if c.kind == "focus_seller"
                else tuple(c.target.split(":", 1))
            )
            held = 0
            for r in outcome.results:
                if pair[0] == "seller":
                    held += seller_key(r.seller) == pair[1]
                else:
                    tags = (ctx.tag_book.get(r.id) if ctx.tag_book else None) or title_tags(r.title)
                    held += _holds(tags, pair[0], pair[1])
            applied["in_batch"] = held
            applied["reserved"] = math.ceil(REFINE_FOCUS_MIN_SHARE * len(outcome.results))
        elif c.kind in ("price_max", "price_min"):
            applied["price"] = c.target
            applied["rank_hint"] = False
        rows.append(
            {
                "id": c.id,
                "label": c.label,
                "kind": c.kind,
                "approximate": c.approximate,
                "applied": applied,
            }  # fmt: skip
        )
    return {"chips": rows, "suppressed": suppressed}


def _query_input(
    session: Session,
    base: ShopQuery,
    adjusted: ShopQuery,
    profile: StoredProfile,
    ctx: AgainContext,
    planned: list[str],
) -> QueryInput | None:
    """What the model query writer sees: the typed constraints, the leanings, the settled values,
    the liked sellers and a few liked and disliked titles. None when nothing was rated up."""
    rows = rated_results(session)
    liked = [r["result"].title for r in rows if r["rating"] in ("love", "like")]
    if not liked:
        return None
    disliked = [r["result"].title for r in rows if r["rating"] == "dislike"]
    names = [seller_name(e) for e in profile.inferred if e.attr_kind == "seller"]
    toward = [
        f"{e.values[0]} ({e.dimension.replace('_', ' ')})"
        for e in profile.inferred
        if e.attr_kind == "leaning_toward" and e.values
    ]
    away = [
        f"{e.values[0]} ({e.dimension.replace('_', ' ')})"
        for e in profile.inferred
        if e.attr_kind == "leaning_away" and e.values
    ]
    return QueryInput(
        base=base, adjusted=adjusted, converged=dict(ctx.converged), toward=toward, away=away,
        liked_sellers=names, liked_titles=liked[:QUERY_LIKED_TITLES],
        disliked_titles=disliked[:QUERY_DISLIKED_TITLES],
        existing=[*planned, *(p[2] for p in ctx.peer_queries)],
        setting=setting_lines(adjusted),
    )  # fmt: skip


def run_shop_again(
    session: Session,
    search: SearchProvider,
    config: Config,
    thumbnailer: Thumbnailer | None = None,
    clock: Clock | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[SearchOutcome, StoredProfile, ShopQuery]:
    """Build the profile and search again. Returns (outcome, new profile, the query used).

    Stage 2g: the preference call, the model's tagging of everything already shown, and the typed
    query's first Serper page all run at the same time (the page only when the provider is thread
    safe); the rest of the searches then run concurrently inside `run_query`. A Shop Again aims for
    `config.shop_target_s` seconds and stops optional work at `config.shop_hard_stop_s`. Phase
    names go to `progress` ("Reading your ratings", then the search phases) so a page can show them.
    Does not change `session`; the caller records the outcome and the profile. Raises
    `ProviderError` when the first Serper call fails and `BudgetExceededError` past a cap.
    """
    clock = clock or Clock()
    say = progress or (lambda _text: None)
    steps = progress if isinstance(progress, Progress) else None
    started = clock.now()
    timeline = Timeline(clock)
    base = base_query(session)
    if base is None:
        raise LookupError("no typed search in this session")
    # a Shop Again makes many small model calls (tagging batches, retries): its own step cap
    budget = Budget(replace(config, max_steps=max(config.max_steps, config.shop_max_steps)))
    warnings: list[str] = []
    book = TagBook(cache=session.tag_cache)
    shown = list(session.results.values())
    skip_pages = pages_fetched(session)
    prefetched: dict[tuple[str, int], list[SearchCandidate] | Exception] = {}
    concurrent = getattr(search, "thread_safe", False) and search.name == "serper"

    def tag_shown() -> float:
        # the model tags everything shown (once per result: cached in the session), so the lift
        # test compares a like share with what the shopper really saw, not what titles say
        t0 = clock.now()
        tag_results(
            [Taggable(r.id, r.title, r.price, r.seller) for r in shown],
            book,
            budget,
            clock,
            workers=config.shop_tag_workers,
            until=started + config.shop_hard_stop_s,
            timeline=timeline,
        )
        return clock.now() - t0

    def prefetch_typed() -> None:
        text, page = base.search_text, skip_pages.get(query_key(base.search_text), 0) + 1
        if page > BASE_PAGE_MAX:
            return  # the base query is not paged past page 2 (page 3 is a last resort)
        try:
            with timeline.span("search", f"{text} (page {page})"):
                prefetched[(text, page)] = _search_page(search, text, page, budget, clock)
        except ProviderError as exc:
            prefetched[(text, page)] = exc

    say("Reading your ratings")
    if steps is not None:
        steps.begin("ratings")
    timeline.phase_start("preferences")
    t_prefs = clock.now()
    if concurrent:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_tag = ex.submit(tag_shown)
            f_search = ex.submit(prefetch_typed)

            def exposure() -> Exposure:
                f_tag.result()
                untagged = sum(1 for r in shown if book.get(r.id) is None)
                return build_exposure(
                    shown,
                    book.merged(),
                    keywords=bool(book.failures) and untagged > 0,
                    tag_source=session.tag_source,
                )

            profile, warning = build_profile(session, base, budget, thumbnailer, exposure=exposure)
            tag_shown_s = f_tag.result()
            f_search.result()
    else:
        tag_shown_s = tag_shown()
        untagged_now = sum(1 for r in shown if book.get(r.id) is None)
        exposure_now = build_exposure(
            shown,
            book.merged(),
            keywords=bool(book.failures) and untagged_now > 0,
            tag_source=session.tag_source,
        )
        profile, warning = build_profile(session, base, budget, thumbnailer, exposure=exposure_now)
    t_prefs_done = clock.now()
    timeline.phase_end("preferences")
    if steps is not None:
        steps.finish("ratings")
    untagged = sum(1 for r in shown if book.get(r.id) is None)
    if warning:
        warnings.append(warning)
    query = apply_adjustments(base, profile)
    topic = current_topic(session)
    rs = build_set(topic.refinements if topic is not None else [])
    profile, query, suppressed = suppress_inferred(profile, query, rs)  # chips outrank ratings
    query = apply_to_query(query, rs)
    queries, dropped = build_phrasings(
        base, profile, query, config.shop_again_max_serper_calls, with_seller=False
    )  # the seller searches are planned by the evidence, not by a chip's phrasing
    ctx = again_context(session, profile, config, queries, dropped, book, query)
    ctx.refine, ctx.refine_lines = rs, ranker_lines(rs)
    for name in rs.focus_sellers:  # a focus on a seller searches that seller by name
        key = seller_key(name)
        ctx.affinity_sellers.add(key)
        ctx.liked_seller_keys.add(key)
        text = " ".join(f"{name} {query.search_text}".split())[:MAX_SEARCH_TEXT_CHARS]
        ctx.peer_queries = [(key, name, text), *[p for p in ctx.peer_queries if p[0] != key]][
            :SELLER_SEARCHES
        ]
    qinput = _query_input(session, base, query, profile, ctx, queries)
    if qinput is not None:
        qinput.setting += writer_lines(rs)
    if config.shop_model_queries and qinput is not None:
        ctx.query_writer = partial(write_queries, qinput, budget, config)
    ctx.clock, ctx.progress, ctx.hard_stop_at = clock, progress, started + config.shop_hard_stop_s
    ctx.prefetched = prefetched
    ctx.timeline = timeline
    outcome = run_query(query, search, config, budget, warnings, started, ctx, clock, progress)
    # the request chips keep showing only what the parser read, and a refined price
    outcome.display_query = (
        base.model_copy(update={"price_min": query.price_min, "price_max": query.price_max})
        if (rs.price_min is not None or rs.price_max is not None)
        else base
    )
    if book.failed:
        outcome.warnings.append(
            f"Candidate tagging failed: {book.reason}. No attribute preference was applied "
            "(only seller, retailer, price and what you said)."
        )
    pool = outcome.composition.get("pool_after") or outcome.composition.get("pool_before") or {}
    timings = outcome.composition.setdefault("timings", {})
    timings["preferences"] = round(t_prefs_done - t_prefs, 3)
    timings["tagging_shown"] = round(tag_shown_s, 3)
    timings["total"] = round(clock.now() - started, 3)
    outcome.seconds = {
        **outcome.seconds,
        "preferences": timings["preferences"],
        "total": timings["total"],
    }
    ctx_counts = alignment_detail(profile)
    outcome.trace = {
        **profile.trace,
        "batch": outcome.composition,
        "tag_calls": book.log,
        "alignment_counted": ctx_counts,
    }
    if rs.active:
        outcome.trace["refine"] = refine_trace(rs, ctx, outcome, suppressed)
    if ctx.query_result is not None:
        outcome.trace["model_queries"] = outcome.composition.get("model_queries", {})
    outcome.tagging = {
        **profile.tagging,
        "candidates_total": pool.get("size", 0),
        "candidates_tagged": pool.get("tagged", 0),
        "candidates_keyword": pool.get("keyword_tagged", 0),
        "tag_top_n": config.shop_tag_top_n,
        "shown_total": len(shown),
        "shown_tagged": len(shown) - untagged,
        "tag_calls": book.calls,
        "tag_usd": round(book.usd, 6),
        "tag_failed_usd": round(book.failed_usd, 6),
        "tag_estimated_usd": round(book.estimated_usd, 6),
        "tag_skipped": book.skipped,
        "tag_failures": book.failures[:3],
        "tag_reason": book.reason if book.failed else "",
        "tag_call_log": book.log,
    }
    outcome.alignment = alignment_counts(outcome.results, profile, book.merged())
    outcome.alignment_signals = alignment_signals(profile)
    return outcome, profile, query
