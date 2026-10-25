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

import time

from app.config import Config
from app.llm import Budget
from app.shop.candidates import TagBook, Taggable, tag_results
from app.shop.decide import build_exposure, typed_dimensions
from app.shop.models import ShopQuery, StoredProfile
from app.shop.prefs import (
    alignment_counts,
    apply_adjustments,
    build_phrasings,
    build_profile,
    seller_name,
)
from app.shop.search import (
    AgainContext,
    SearchOutcome,
    normalized_title_key,
    query_key,
    run_query,
)
from app.shop.store import Session, rated_results, sellers_to_deprioritize
from app.shop.thresholds import CONVERGED_MAX_SHARE, DIVERSITY_MAX_SHARE
from app.shop.vocab import DIMENSIONS
from app.shopping import SearchProvider
from app.shopping_retrieval import seller_key
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


def again_context(
    session: Session,
    profile: StoredProfile,
    config: Config,
    queries: list[str],
    dropped: list[str] | None = None,
    book: TagBook | None = None,
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
    seller = next((e for e in inferred if e.attr_kind == "seller"), None)
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
    price_line = ""
    if price is not None:
        price_line = f"mixed ({price.detail})" if price.status == "mixed" else price.text
    return AgainContext(
        exclude_ids=set(seen),
        exclude_title_keys={normalized_title_key(r.title, r.seller) for r in seen.values()},
        deprioritize_sellers=sellers_to_deprioritize(session),
        affinity_sellers={seller_key(seller_name(seller))} if seller is not None else set(),
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
        seller_affinity=seller_name(seller) if seller is not None else "",
        price_band=price.text if price is not None else "",
        more_like=[(r["result"], r["rating"]) for r in rows if r["rating"] in ("love", "like")],
        less_like=[r["result"] for r in rows if r["rating"] == "dislike"],
        extra_queries=queries[1:],
        dropped_phrasings=list(dropped or []),
        max_calls=config.shop_again_max_serper_calls,
        skip_pages=pages_fetched(session),
        tag_book=book,
        open_mass=open_mass,
        open_loved=open_loved,
        max_calls_open=config.shop_again_max_serper_calls_open,
    )


def run_shop_again(
    session: Session,
    search: SearchProvider,
    config: Config,
    thumbnailer: Thumbnailer | None = None,
) -> tuple[SearchOutcome, StoredProfile, ShopQuery]:
    """Build the profile and search again. Returns (outcome, new profile, the query used).

    One preference call (with the rated items' thumbnails when they can be fetched), up to three
    search phrasings within `shop_again_max_serper_calls` Serper calls, one rank call. Does not
    change `session`; the caller records the outcome and the profile. Raises `ProviderError` when
    the first Serper call fails and `BudgetExceededError` past a cap.
    """
    started = time.perf_counter()
    base = base_query(session)
    if base is None:
        raise LookupError("no typed search in this session")
    budget = Budget(config)
    warnings: list[str] = []
    # the model tags everything shown (once per result: cached in the session), so the lift test
    # compares a like share with what the shopper really saw, not with what titles happen to say
    book = TagBook(cache=session.tag_cache)
    shown = list(session.results.values())
    tag_results([Taggable(r.id, r.title, r.price, r.seller) for r in shown], book, budget)
    untagged = sum(1 for r in shown if book.get(r.id) is None)
    exposure = build_exposure(shown, book.merged(), keywords=bool(book.failures) and untagged > 0)
    profile, warning = build_profile(session, base, budget, thumbnailer, exposure=exposure)
    if warning:
        warnings.append(warning)
    query = apply_adjustments(base, profile)
    queries, dropped = build_phrasings(base, profile, query, config.shop_again_max_serper_calls)
    outcome = run_query(
        query, search, config, budget, warnings, started,
        again_context(session, profile, config, queries, dropped, book),
    )  # fmt: skip
    outcome.display_query = base  # the request chips keep showing only what the parser read
    if book.failed:
        outcome.warnings.append(
            "The shown and candidate results could not be tagged, so no attribute preference was "
            "applied (only seller, price and what you said)."
        )
    pool = outcome.composition.get("pool_after") or outcome.composition.get("pool_before") or {}
    outcome.trace = {**profile.trace, "batch": outcome.composition}
    outcome.tagging = {
        **profile.tagging,
        "candidates_total": pool.get("size", 0),
        "candidates_tagged": pool.get("tagged", 0),
        "shown_total": len(shown),
        "shown_tagged": len(shown) - untagged,
        "tag_calls": book.calls,
        "tag_usd": round(book.usd, 6),
        "tag_failures": book.failures[:3],
    }
    outcome.alignment = alignment_counts(outcome.results, profile, book.merged())
    return outcome, profile, query
