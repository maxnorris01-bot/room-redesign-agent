"""Shop Again: a fresh set of results shaped by what the visitor rated.

One preference call updates the profile (`app.shop.prefs`), the typed query is adjusted by it, and
the stage 1 pipeline runs again with exclusions: everything already shown in the session (any
search, so every disliked item too), and the same listing under another id. Sellers with at least
two dislikes and no love or like are ranked last, not excluded. Hard filters are the typed query's.
The rank call (prompt shop_rank v2) also sees the profile and the loved, liked and disliked titles.
"""

from __future__ import annotations

import time

from app.config import Config
from app.llm import Budget
from app.shop.models import ShopQuery, StoredProfile
from app.shop.prefs import apply_adjustments, build_profile
from app.shop.search import AgainContext, SearchOutcome, normalized_title_key, run_query
from app.shop.store import Session, rated_results, sellers_to_deprioritize
from app.shopping import SearchProvider


def base_query(session: Session) -> ShopQuery | None:
    """The last typed search's query: Shop Again always adjusts that, never an earlier Shop Again's,
    so adjustments do not stack on themselves (the profile is already cumulative)."""
    typed = [s for s in session.searches if s.kind == "search"]
    return typed[-1].query if typed else None


def again_context(session: Session, profile: StoredProfile, config: Config) -> AgainContext:
    seen = session.results
    rows = rated_results(session)
    return AgainContext(
        exclude_ids=set(seen),
        exclude_title_keys={normalized_title_key(r.title, r.seller) for r in seen.values()},
        deprioritize_sellers=sellers_to_deprioritize(session),
        likes=list(profile.likes),
        dislikes=list(profile.dislikes),
        more_like=[(r["result"], r["rating"]) for r in rows if r["rating"] in ("love", "like")],
        less_like=[r["result"] for r in rows if r["rating"] == "dislike"],
    )


def run_shop_again(
    session: Session, search: SearchProvider, config: Config
) -> tuple[SearchOutcome, StoredProfile, ShopQuery]:
    """Build the profile and search again. Returns (outcome, new profile, the query used).

    Does not change `session`; the caller records the outcome and the profile. Raises
    `ProviderError` when page 1 fails and `BudgetExceededError` past a cap.
    """
    started = time.perf_counter()
    base = base_query(session)
    if base is None:
        raise LookupError("no typed search in this session")
    budget = Budget(config)
    warnings: list[str] = []
    profile, warning = build_profile(session, base, budget)
    if warning:
        warnings.append(warning)
    query = apply_adjustments(base, profile)
    outcome = run_query(
        query, search, config, budget, warnings, started, again_context(session, profile, config)
    )
    return outcome, profile, query
