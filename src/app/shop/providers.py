"""Providers for Shop Mode: the room pipeline's live providers, or fixture fakes in mock mode.

Live: `app.shopping.build_providers` (Serper or SerpApi search, SerpApi lookup). Mock: synthetic
results shaped like a Serper response (`tests/fixtures/shop/`), chosen by words in the query, split
over two pages so the page-2 path runs; the lookup serves the trimmed real Article product fixture
and finds nothing for ids ending in 9, so the "no retailer link" fallback is visible too. Every
mock title carries the "[FIXTURE]" prefix.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.config import Config
from app.shopping import (
    FIXTURE_TITLE_PREFIX,
    ProductDetails,
    ProductLookup,
    SearchCandidate,
    SearchProvider,
    _load_fixture,
    build_providers,
    parse_product_details,
    parse_serper_search,
)

SHOP_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "shop"
PAGE_SIZE = 20
# first matching keyword wins; the last entry is the default
FIXTURE_BY_KEYWORD: list[tuple[tuple[str, ...], str]] = [
    (("couch", "sofa", "sectional", "loveseat"), "serper_couches.json"),
    (("coffee table", "cocktail table"), "serper_coffee_tables.json"),
    (("curtain", "drape"), "serper_curtains.json"),
    ((), "serper_couches.json"),
]


def _shop_fixture(name: str) -> list[SearchCandidate]:
    import json

    data = json.loads((SHOP_FIXTURE_DIR / name).read_text())
    return parse_serper_search(data)


PREMIUM_SELLER = "Hartwell & Co"  # the invented seller in the fixtures (see their README)
BOOST_WORDS = ("hartwell", "brass", "brushed", "walnut", "solid")


class ShopFixtureSearch:
    """Fake Serper for Shop Mode: pages of 20 synthetic results per topic.

    A query that names the invented premium seller or one of its materials (`BOOST_WORDS`) gets that
    seller's results first, the way a more specific real query surfaces more specific products;
    any other query sees the fixture in its own order. Results are never fetched or real.
    """

    name = "serper"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search(
        self, query: str, gl: str = "us", hl: str = "en", page: int = 1
    ) -> list[SearchCandidate]:
        self.calls.append((query, page))
        lowered = query.lower()
        fixture = next(
            f for words, f in FIXTURE_BY_KEYWORD if not words or any(w in lowered for w in words)
        )
        everything = _shop_fixture(fixture)
        if any(w in lowered for w in BOOST_WORDS):
            everything = sorted(everything, key=lambda c: c.seller != PREMIUM_SELLER)  # stable
        window = everything[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        return [replace(c, title=FIXTURE_TITLE_PREFIX + c.title) for c in window]


class ShopFixtureLookup:
    """Fake SerpApi lookup: the Article fixture's stores, nothing for ids ending in 9."""

    name = "serpapi"

    def __init__(self) -> None:
        self.product_ids: list[str] = []

    def lookup(self, product_id: str, gl: str = "us", hl: str = "en") -> ProductDetails:
        self.product_ids.append(product_id)
        if product_id.endswith("9"):
            return ProductDetails(note="no product_results")
        return parse_product_details(_load_fixture("serpapi_product_article.json"))


def build_shop_providers(config: Config) -> tuple[SearchProvider, ProductLookup]:
    if config.llm_mode == "mock":
        return ShopFixtureSearch(), ShopFixtureLookup()
    return build_providers(config)
