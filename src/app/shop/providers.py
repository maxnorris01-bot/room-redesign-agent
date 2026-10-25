"""Providers for Shop Mode: the room pipeline's live providers, or fixture fakes in mock mode.

Live: `app.shopping.build_providers` (Serper or SerpApi search, SerpApi lookup). Mock: synthetic
results shaped like a Serper response (`tests/fixtures/shop/`), chosen by words in the query, split
over two pages so the page-2 path runs; the lookup serves the trimmed real Article product fixture
and finds nothing for ids ending in 9, so the "no retailer link" fallback is visible too. Every
mock title carries the "[FIXTURE]" prefix.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from pathlib import Path

from app.config import Config
from app.mock_llm import mock_step_delay
from app.shop.retailers import RETAILERS, Retailer, seller_key
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
SHAPE_WORDS = ("round", "oval", "rectangular", "square")


PEER_PAGE_SIZE = 8  # a mock retailer catalogue is small: this many results a page


def _retailer_in(query: str) -> Retailer | None:
    """The table retailer a query begins with ("Article wood coffee table ..."), if any."""
    flat = re.sub(r"[^a-z0-9]+", "", query.lower())
    best: Retailer | None = None
    for r in RETAILERS:
        words = re.sub(r"[^a-z0-9]+", "", r.search_name.lower())
        if flat.startswith(words) and (best is None or len(words) > len(best.search_name)):
            best = r
    return best


class ShopFixtureSearch:
    """Fake Serper for Shop Mode: pages of 20 synthetic results per topic.

    A query that names the invented premium seller or one of its materials (`BOOST_WORDS`) gets that
    seller's results first, and a query that names a shape gets the products with that shape first,
    the way a more specific real query surfaces more specific products; a query that begins
    with the name of a retailer in `app.shop.retailers` gets that retailer's (small, synthetic)
    catalogue, which is what a peer retailer search finds; any other query sees the fixture in its
    own order.
    Results are never fetched or real.
    """

    name = "serper"
    thread_safe = True  # stateless apart from the call log

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search(
        self, query: str, gl: str = "us", hl: str = "en", page: int = 1
    ) -> list[SearchCandidate]:
        mock_step_delay()
        self.calls.append((query, page))
        lowered = query.lower()
        fixture = next(
            f for words, f in FIXTURE_BY_KEYWORD if not words or any(w in lowered for w in words)
        )
        everything = _shop_fixture(fixture)
        boosted = any(w in lowered for w in BOOST_WORDS)
        shapes = [w for w in SHAPE_WORDS if w in lowered]
        if boosted or shapes:  # a more specific query surfaces more specific products (stable sort)
            everything = sorted(
                everything,
                key=lambda c: (
                    -(
                        (2 if boosted and c.seller == PREMIUM_SELLER else 0)
                        + (1 if any(w in c.title.lower() for w in shapes) else 0)
                    )
                ),
            )
        retailer = _retailer_in(query)
        if retailer is not None:  # a query that begins with a retailer: that retailer's catalogue
            start = (page - 1) * PEER_PAGE_SIZE
            tag = seller_key(retailer.name)
            return [
                replace(
                    c,
                    title=f"{FIXTURE_TITLE_PREFIX}{retailer.name} {c.title}",
                    seller=retailer.name,
                    product_id=f"{tag}-{c.product_id}",
                    thumbnail_url=(
                        f"https://mock.example/{tag}/{c.product_id}.jpg"
                        if c.thumbnail_url
                        else None
                    ),  # a re-sellered copy is its own listing: its own image
                )
                for c in everything[start : start + PEER_PAGE_SIZE]
            ]
        window = everything[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        return [replace(c, title=FIXTURE_TITLE_PREFIX + c.title) for c in window]


class ReplayPoolSearch(ShopFixtureSearch):
    """Mock Serper that serves a recorded session's authored candidate pool (`"pool"` in a replay
    file under `tests/fixtures/shop/`) for ordinary queries, so a Shop Again sees a pool that looks
    like the real one. Each distinct query text gets its own slice of the pool (pages of 20), and a
    query that begins with a retailer's name still gets that retailer's catalogue (the peer step).
    """

    def __init__(self, replay_file: str) -> None:
        super().__init__()
        data = json.loads((SHOP_FIXTURE_DIR / replay_file).read_text())
        self.pool: list[SearchCandidate] = [
            SearchCandidate(
                product_id=x["id"],
                title=x["title"],  # as authored: the shown and rated results carry the same title
                seller=x["seller"],
                price=float(x["price"]),
                currency="USD",
                rating=4.4,
                rating_count=120,
                google_panel_url=f"https://www.google.com/shopping/product/{x['id']}",
                thumbnail_url=x.get("thumbnail"),
            )
            for x in data["pool"]
        ]
        self._starts: dict[str, int] = {}

    def search(
        self, query: str, gl: str = "us", hl: str = "en", page: int = 1
    ) -> list[SearchCandidate]:
        if _retailer_in(query) is not None:
            return super().search(query, gl, hl, page)
        mock_step_delay()
        self.calls.append((query, page))
        key = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
        start = self._starts.setdefault(key, (len(self._starts) * 17) % len(self.pool))
        rotated = self.pool[start:] + self.pool[:start]
        return rotated[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]


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
        pool_file = os.environ.get("APP_SHOP_MOCK_POOL")  # mock only: a replay file's pool
        search: ShopFixtureSearch = (
            ReplayPoolSearch(pool_file) if pool_file else ShopFixtureSearch()
        )
        return search, ShopFixtureLookup()
    return build_providers(config)
