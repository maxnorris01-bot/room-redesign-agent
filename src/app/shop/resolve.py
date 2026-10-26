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

from dataclasses import asdict
from typing import Any

from app.config import Config
from app.llm import Budget
from app.shop.models import ShopResult
from app.shop.store import ResolvedLink, Session, now_iso
from app.shopping import (
    Dimensions,
    ProductDetails,
    ProductLookup,
    ProviderError,
    SearchCandidate,
    Store,
)
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


class _Fixed:
    """A lookup that has already been made: `lookup` hands back the cached outcome (details, or the
    provider error), so `_lookup_one` does its link hygiene without calling the provider again."""

    def __init__(
        self, name: str, details: ProductDetails | None, error: ProviderError | None
    ) -> None:
        self.name = name
        self._details = details
        self._error = error

    def lookup(self, product_id: str, gl: str = "us", hl: str = "en") -> ProductDetails:
        if self._error is not None:
            raise self._error
        return self._details or ProductDetails(note="no product_results")


def details_to_json(details: ProductDetails) -> dict[str, Any]:
    return asdict(details)


def details_from_json(raw: dict[str, Any]) -> ProductDetails:
    dims = raw.get("dimensions")
    return ProductDetails(
        stores=[Store(**s) for s in raw.get("stores", [])],
        attributes=list(raw.get("attributes", [])),
        dimensions=Dimensions(**dims) if isinstance(dims, dict) else None,
        note=str(raw.get("note", "")),
        images=list(raw.get("images", [])),
        rating=raw.get("rating"),
        rating_count=raw.get("rating_count"),
    )


def product_lookup(
    session: Session, result: ShopResult, lookup: ProductLookup, config: Config
) -> tuple[ProductDetails | None, ProviderError | None, bool]:
    """The one SerpApi product lookup of a result, shared by Open and "More photos": (details, the
    provider error if it failed, whether a lookup was made just now).

    The outcome, including a failure, is cached in the session, so a result is looked up once and
    counted once against `shop_lookups_per_session`; a later call is free. A 401 or 403 (a bad key
    or plan) propagates and is not cached. The caller checks the cap before a fresh lookup."""
    cached = session.product_cache.get(result.id)
    if cached is not None:
        if cached.get("error"):
            return (
                None,
                ProviderError(lookup.name, "request failed", status=cached.get("status")),
                False,
            )
        return details_from_json(cached["details"]), None, False
    try:
        details = lookup.lookup(result.product_id)
    except ProviderError as exc:
        if exc.status in (401, 403):
            raise
        session.product_cache[result.id] = {"error": True, "status": exc.status}
        session.lookups_used += 1
        return None, exc, True
    session.product_cache[result.id] = {"error": False, "details": details_to_json(details)}
    session.lookups_used += 1
    return details, None, True


def fill_rating(session: Session, result: ShopResult, details: ProductDetails | None) -> None:
    """A lookup's star rating and review count fill in what the search did not give (never
    overwrite what it did, never estimate)."""
    if details is None or result.id not in session.results:
        return
    update: dict[str, Any] = {}
    if result.rating is None and details.rating is not None:
        update["rating"] = details.rating
    if result.rating_count is None and details.rating_count is not None:
        update["rating_count"] = details.rating_count
    if update:
        session.results[result.id] = result.model_copy(update=update)


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
    if (
        result_id not in session.product_cache
        and session.lookups_used >= config.shop_lookups_per_session
    ):
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
    details, error, fresh = product_lookup(session, result, lookup, config)
    fill_rating(session, result, details)
    index = session.first_seen.get(result_id, 0)
    query = session.searches[index - 1].query if 0 < index <= len(session.searches) else None
    category = (query.category_key or query.category) if query else ""
    product, _, _, issue = _lookup_one(
        candidate, "", _Fixed(lookup.name, details, error), budget, category
    )
    cost = budget.provider_cost_usd if fresh else 0.0  # a lookup made before is not paid for twice
    session.lookup_usd = round(session.lookup_usd + cost, 6)
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
            cost_usd=round(cost, 6),
            resolved_at=now_iso(),
        )
    else:
        note = NO_LINK_NOTE if issue is None else f"{NO_LINK_NOTE} ({issue})"
        resolved = _fallback(result, note, "fallback", round(cost, 6))
    session.resolved[result_id] = resolved
    return resolved
