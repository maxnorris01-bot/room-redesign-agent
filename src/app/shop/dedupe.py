"""One de-duplication for Shop Mode (ADR 0012, stage 2g amendment).

Used for typed searches, Shop Again candidates, the "seen before" exclusion and the final batch.
A candidate is a duplicate of another listing, or of anything shown or rated earlier in the session,
when ANY of these match (checked in this order, and the first that matches is the rule recorded):

  1. `same_product_id`: the same provider product id;
  2. `same_thumbnail`: the same thumbnail URL;
  3. `same_title_and_price`: the same normalized title (lowercase, punctuation, sizes and plain
     colour words removed; the brand prefix is kept) and a price within 1% of the other, whatever
     the seller;
  4. `same_title_and_seller`: the same normalized title from the same (canonical) seller.

Of a group of candidates the BEST listing is kept: one with a retailer link first, then the lowest
price, then the earliest in the search order. Anything shown or rated before always wins, and the
exclusion is counted separately for what was rated and what was only shown. The kept candidates keep
their original order.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from app.shop.retailers import seller_key

RULES = ("same_product_id", "same_thumbnail", "same_title_and_price", "same_title_and_seller")
PRICE_TOLERANCE = 0.01  # prices within this share of the larger one are "the same price"

T = TypeVar("T")

_SIZE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:\"|''|in\b|inch(?:es)?\b|cm\b|ft\b|')\s*(?:x\s*\d+(?:\.\d+)?\s*(?:\"|in\b)?)*",
    flags=re.IGNORECASE,
)
_DIMS = re.compile(r"\b\d+(?:\.\d+)?\s*[x×]\s*\d+(?:\.\d+)?(?:\s*[x×]\s*\d+(?:\.\d+)?)?\b")
_COLORS = frozenset(
    "black white gray grey brown beige cream ivory tan blue green red pink yellow orange purple "
    "silver gold navy".split()
)  # finishes such as walnut or oak are NOT noise: they change the product


def title_key(title: str) -> str:
    """The normalized title two listings of one product share."""
    lowered = title.lower().replace("″", '"').replace("”", '"')
    lowered = _DIMS.sub(" ", lowered)
    lowered = _SIZE.sub(" ", lowered)
    words = [w for w in re.findall(r"[a-z0-9]+", lowered) if w not in _COLORS]
    return " ".join(words)


def prices_close(a: float | None, b: float | None) -> bool:
    if a is None or b is None or a <= 0 or b <= 0:
        return False
    return abs(a - b) <= PRICE_TOLERANCE * max(a, b)


@dataclass(frozen=True)
class Listing:
    """What the rules look at. `has_link` is whether the listing has a usable retailer link."""

    product_id: str
    title: str
    seller: str
    price: float | None
    thumbnail: str = ""
    has_link: bool = False
    rated: bool = False  # for an earlier listing: it was rated (else it was only shown)


@dataclass
class DedupeStats:
    """What each rule removed, and what was excluded because it was seen before."""

    removed: dict[str, int] = field(default_factory=lambda: dict.fromkeys(RULES, 0))
    shown_before: int = 0
    rated_before: int = 0
    kept: int = 0
    total: int = 0

    @property
    def duplicates(self) -> int:
        return sum(self.removed.values())

    def as_dict(self) -> dict[str, object]:
        return {
            "candidates": self.total,
            "kept": self.kept,
            "shown_before": self.shown_before,
            "rated_before": self.rated_before,
            "duplicates": self.duplicates,
            "by_rule": dict(self.removed),
        }


class Index:
    """The listings seen so far, searchable by every rule."""

    def __init__(self) -> None:
        self.by_id: dict[str, Listing] = {}
        self.by_thumb: dict[str, Listing] = {}
        self.by_title: dict[str, list[Listing]] = {}

    def match(self, item: Listing) -> tuple[str, Listing] | None:
        """(rule, the listing it duplicates), or None."""
        hit = self.by_id.get(item.product_id)
        if hit is not None:
            return RULES[0], hit
        if item.thumbnail:
            hit = self.by_thumb.get(item.thumbnail)
            if hit is not None:
                return RULES[1], hit
        key = title_key(item.title)
        if key:
            same = self.by_title.get(key, [])
            for other in same:
                if prices_close(item.price, other.price):
                    return RULES[2], other
            mine = seller_key(item.seller)
            for other in same:
                if seller_key(other.seller) == mine:
                    return RULES[3], other
        return None

    def add(self, item: Listing) -> None:
        self.by_id.setdefault(item.product_id, item)
        if item.thumbnail:
            self.by_thumb.setdefault(item.thumbnail, item)
        key = title_key(item.title)
        if key:
            self.by_title.setdefault(key, []).append(item)


def _quality(item: Listing, order: int) -> tuple[int, float, int]:
    return (
        0 if item.has_link else 1,
        item.price if item.price is not None else float("inf"),
        order,
    )


class Dedupe(Generic[T]):
    """`run` de-duplicates a list of candidates against earlier listings. Reusable for one pass."""

    def __init__(self, listing_of: Callable[[T], Listing]) -> None:
        self.listing_of = listing_of

    def run(
        self, candidates: Sequence[T], seen: Sequence[Listing] = ()
    ) -> tuple[list[T], DedupeStats]:
        stats = DedupeStats(total=len(candidates))
        earlier = Index()
        rated = Index()
        for old in seen:
            earlier.add(old)
            if old.rated:
                rated.add(old)
        kept_index = Index()
        keep: set[int] = set()
        listings = [self.listing_of(c) for c in candidates]
        for i in sorted(range(len(candidates)), key=lambda n: _quality(listings[n], n)):
            item = listings[i]
            if rated.match(item) is not None:
                stats.rated_before += 1
                continue
            if earlier.match(item) is not None:
                stats.shown_before += 1
                continue
            found = kept_index.match(item)
            if found is not None:
                stats.removed[found[0]] += 1
                continue
            kept_index.add(item)
            keep.add(i)
        stats.kept = len(keep)
        return [c for i, c in enumerate(candidates) if i in keep], stats


def merge_stats(*parts: DedupeStats) -> DedupeStats:
    out = DedupeStats()
    for p in parts:
        out.total += p.total
        out.kept += p.kept
        out.shown_before += p.shown_before
        out.rated_before += p.rated_before
        for rule, n in p.removed.items():
            out.removed[rule] += n
    return out
