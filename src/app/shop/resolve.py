"""Lazy link resolve: one SerpApi `google_product` lookup for one result, on demand.

Searching never looks anything up. A lookup runs only when the visitor opens a result (loving one
looks nothing up), for a result that was actually shown in the session, at most
`Config.shop_lookups_per_session` times per session, and never twice for the same result (the
outcome, including "no retailer link found", is cached in the session). Past the cap the Google
product link is returned, marked as such.

Link hygiene is the room pipeline's (`app.shopping_retrieval._lookup_one`): tracking parameters
stripped, the store matched to the search seller, a store URL that is only a listing or search page
rejected, shipping known / free / unknown kept apart.
"""

from __future__ import annotations

from app.config import Config
from app.llm import Budget
from app.shop.models import ShopResult
from app.shop.store import ResolvedLink, Session, now_iso
from app.shopping import ProductLookup, SearchCandidate
from app.shopping_retrieval import _lookup_one

CAP_NOTE = "Lookup limit for this session reached; this is the Google Shopping link."
NO_LINK_NOTE = "No direct retailer link was found; this is the Google Shopping link."


class UnknownResult(LookupError):
    """The id is not a result shown in this session."""


def _fallback(result: ShopResult, note: str, status: str, cost: float = 0.0) -> ResolvedLink:
    return ResolvedLink(
        result_id=result.id,
        status=status,
        link=result.link,
        link_kind=result.link_kind,
        seller=result.seller,
        price=result.price,
        note=note,
        cost_usd=cost,
        resolved_at=now_iso(),
    )


def resolve(
    session: Session, result_id: str, lookup: ProductLookup, config: Config
) -> ResolvedLink:
    """Resolve one shown result. Mutates `session` (cache, counters); the caller saves it.

    A 401/403 from the provider (a bad key or plan) propagates as `ProviderError` and is not
    cached; any other provider failure is cached as a fallback to the Google link.
    """
    result = session.results.get(result_id)
    if result is None:
        raise UnknownResult(result_id)
    cached = session.resolved.get(result_id)
    if cached is not None:
        return cached
    if session.lookups_used >= config.shop_lookups_per_session:
        return _fallback(result, CAP_NOTE, "cap_reached")  # not cached: no lookup was made

    budget = Budget(config)
    candidate = SearchCandidate(
        product_id=result.product_id,
        title=result.title,
        seller=result.seller,
        price=result.price,
        currency=result.currency,
        rating=None,
        rating_count=None,
        google_panel_url=result.link if result.link_kind == "google_panel" else None,
        thumbnail_url=result.thumbnail_url or None,
    )
    index = session.first_seen.get(result_id, 0)
    query = session.searches[index - 1].query if 0 < index <= len(session.searches) else None
    category = (query.category_key or query.category) if query else ""
    product, _, _, issue = _lookup_one(candidate, "", lookup, budget, category)
    session.lookups_used += 1
    session.lookup_usd = round(session.lookup_usd + budget.provider_cost_usd, 6)
    if product.url_kind == "retailer_product":
        resolved = ResolvedLink(
            result_id=result.id,
            status="resolved",
            link=product.url,
            link_kind="retailer_product",
            seller=product.retailer or result.seller,
            price=product.price,
            shipping=product.shipping,
            shipping_status=product.shipping_status,
            total=product.total,
            cost_usd=round(budget.provider_cost_usd, 6),
            resolved_at=now_iso(),
        )
    else:
        note = NO_LINK_NOTE if issue is None else f"{NO_LINK_NOTE} ({issue})"
        resolved = _fallback(result, note, "fallback", round(budget.provider_cost_usd, 6))
    session.resolved[result_id] = resolved
    return resolved
