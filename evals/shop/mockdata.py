"""Mock catalogues for the Shop eval (mock mode only).

The couch, coffee table and curtain requests run on the existing fixtures under
`tests/fixtures/shop/`. Every other case runs on a generic catalogue generated here: deterministic,
synthetic, titled `[FIXTURE] ...`, with attributes drawn from per-product pools, some accessories
(so the category judge has something to catch), some prices over any typical cap (so price
compliance is exercised), and the retailers the pipeline knows. A query that names a colour,
material, shape or feature surfaces matching listings first, the way a more specific real query
does. Nothing here is real data and nothing is fetched.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass

from app.shop.providers import ShopFixtureSearch
from app.shopping import FIXTURE_TITLE_PREFIX, SearchCandidate

PAGE = 20
POOL_SIZE = 240
SELLERS = (
    "Wayfair", "Amazon", "Walmart", "Target", "IKEA", "West Elm", "Article", "CB2", "Etsy",
    "Overstock", "Home Depot", "Lowe's", "Crate & Barrel", "Pottery Barn", "Room & Board",
    "Hartwell & Co", "World Market", "AllModern",
)  # fmt: skip
COLORS = ("cream", "black", "navy", "walnut", "oak", "brass", "grey", "white", "beige", "rust")
STYLES = ("mid-century", "modern", "industrial", "boho", "farmhouse", "minimalist", "scandinavian")
SHAPES = ("round", "rectangular", "oval", "curved", "square")
ACCESSORIES = ("Replacement Cover for", "Spare Cushion Set for", "Leg Set for", "Slipcover for")


@dataclass(frozen=True)
class Spec:
    keywords: tuple[str, ...]
    nouns: tuple[str, ...]
    materials: tuple[str, ...]
    extras: tuple[str, ...]
    typical_max: float


SPECS: tuple[Spec, ...] = (
    Spec(("dining chair",), ("Dining Chair", "Side Chair"), ("wood", "walnut", "oak", "metal", "plastic", "velvet"),
         ("with Tapered Legs", "Curved Back", "Upholstered Seat"), 160.0),
    Spec(("bookshel", "bookcase"), ("Bookshelf", "Bookcase", "Shelf Unit"), ("wood", "metal", "oak", "laminate"),
         ("Small Open", "Compact", "5-Tier", "Closed Cabinet"), 220.0),
    Spec(("rug",), ("Area Rug", "Rug"), ("wool", "jute", "polyester", "cotton"),
         ("8x10", "5x7", "Low Pile", "Shag High Pile", "Textured"), 450.0),
    Spec(("lamp",), ("Floor Lamp", "Arc Floor Lamp", "Tripod Floor Lamp"), ("brass", "metal", "steel", "plastic"),
         ("Linen Shade", "Brushed Brass", "Arc", "Chrome"), 260.0),
    Spec(("bed frame", "bed"), ("Bed Frame", "Platform Bed"), ("wood", "oak", "walnut", "metal", "upholstered"),
         ("Queen", "with Storage Drawers", "Low Profile", "Tufted Headboard"), 900.0),
    Spec(("side table", "end table"), ("Side Table", "End Table", "Accent Table"), ("metal", "marble", "wood", "glass"),
         ("Round", "Black Legs", "Marble Top"), 180.0),
    Spec(("desk chair", "office chair"), ("Desk Chair", "Office Chair", "Gaming Chair"), ("mesh", "leather", "fabric", "plastic"),
         ("Ergonomic", "Mesh Back", "Lumbar Support", "Racing Stripe"), 340.0),
    Spec(("accent chair", "armchair"), ("Accent Chair", "Armchair", "Barrel Chair"), ("velvet", "boucle", "linen", "leather"),
         ("Tufted", "Brass Legs", "Barrel"), 450.0),
)  # fmt: skip
DEFAULT_SPEC = SPECS[0]


def spec_for(query: str) -> Spec:
    low = query.lower()
    return next((s for s in SPECS if any(k in low for k in s.keywords)), DEFAULT_SPEC)


def _rng(seed: str) -> list[int]:
    """A long deterministic sequence of small ints from a text seed (no randomness)."""
    out: list[int] = []
    state = zlib.crc32(seed.encode()) or 1
    for _ in range(POOL_SIZE * 7 + 7):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        out.append(state >> 8)
    return out


def catalogue(spec: Spec, cap: float | None, favor: str | None = None) -> list[SearchCandidate]:
    """The generated pool for one product type; `cap` is the price the query asked for, if any."""
    r = _rng(spec.keywords[0])
    top = cap or spec.typical_max
    out: list[SearchCandidate] = []
    for i in range(POOL_SIZE):
        n = r[i * 7 : i * 7 + 7]
        noun = spec.nouns[n[0] % len(spec.nouns)]
        material = spec.materials[n[1] % len(spec.materials)]
        color = COLORS[n[2] % len(COLORS)]
        if favor and n[2] % 10 < 7:  # a query that names a colour finds mostly that colour
            color = favor
        style = STYLES[n[3] % len(STYLES)]
        extra = spec.extras[n[4] % len(spec.extras)]
        parts = [color.title(), material.title(), style.title(), noun, extra]
        if n[5] % 5 == 0:
            parts.insert(2, SHAPES[n[5] % len(SHAPES)].title())
        title = " ".join(parts)
        if i % 16 == 5:  # an accessory, not the product
            title = f"{ACCESSORIES[i % len(ACCESSORIES)]} {noun} {color.title()}"
        price = round(top * (0.25 + (n[6] % 1000) / 1000 * 1.35), 2)
        out.append(
            SearchCandidate(
                product_id=f"gen-{spec.keywords[0].replace(' ', '')}-{i:03d}",
                title=f"{FIXTURE_TITLE_PREFIX}{title}",
                seller=SELLERS[(n[0] + n[3]) % len(SELLERS)],
                price=price,
                currency="USD",
                rating=round(3.4 + (n[1] % 16) / 10, 1),
                rating_count=20 + n[2] % 900,
                google_panel_url=f"https://www.google.com/search?ibp=oshop&q={spec.keywords[0]}&prds=catalogid:{i}",
                thumbnail_url=f"https://mock.example/thumb/{i}.jpg",
            )
        )
    return out


class GenericCatalogue:
    """A fake Serper over the generated pools, pages of 20, more specific queries first."""

    name = "serper"
    thread_safe = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search(
        self, query: str, gl: str = "us", hl: str = "en", page: int = 1
    ) -> list[SearchCandidate]:
        self.calls.append((query, page))
        spec = spec_for(query)
        under = re.search(r"(?:under|below|max)\s*\$?(\d+)", query.lower())
        named = [c for c in COLORS if re.search(rf"\b{c}\b", query.lower())]
        everything = catalogue(
            spec, float(under.group(1)) if under else None, named[0] if named else None
        )
        words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
        everything.sort(key=lambda c: -sum(1 for w in words if w in c.title.lower()))
        start = zlib.crc32(" ".join(sorted(words)).encode()) % 24  # a different window per query
        rotated = everything[start:] + everything[:start]
        return rotated[(page - 1) * PAGE : page * PAGE]


class RouterSearch:
    """The existing fixtures for the three fixture cases, the generic catalogue for the rest."""

    name = "serper"
    thread_safe = True

    def __init__(self, fixture_case: bool = False) -> None:
        self.fixtures = ShopFixtureSearch()
        self.generic = GenericCatalogue()
        self.use_fixture = fixture_case

    @property
    def calls(self) -> list[tuple[str, int]]:
        return [*self.fixtures.calls, *self.generic.calls]

    def search(
        self, query: str, gl: str = "us", hl: str = "en", page: int = 1
    ) -> list[SearchCandidate]:
        provider = self.fixtures if self.use_fixture else self.generic
        return provider.search(query, gl, hl, page)
