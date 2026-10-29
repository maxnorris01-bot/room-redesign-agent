"""Code scorers for the Shop eval: no model, no network, deterministic.

Each takes the listings a batch showed (and what the case expects) and returns plain numbers, so a
threshold check is arithmetic on them. A batch's items are `Listing`s built from the service view.
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.shop.nearby import NearItem, match_reason
from app.shop.retailers import seller_key
from app.shop.vocab import title_value


@dataclass
class Listing:
    id: str
    title: str
    price: float | None
    seller: str
    thumbnail_url: str = ""
    fingerprint: int | None = None
    source: dict[str, str] | None = None  # stage 2q: {"kind", "query"} of the search that found it


@dataclass
class Batch:
    """One shown batch: the listings in display order, the search words the pipeline used, the
    price range it parsed, and how long it took."""

    round: int
    kind: str
    listings: list[Listing]
    search_texts: list[str] = field(default_factory=list)
    parsed_min: float | None = None
    parsed_max: float | None = None
    seconds: float = 0.0


def listings_from_view(
    view: dict[str, Any], fingerprints: dict[str, int] | None = None
) -> list[Listing]:
    """The shown results of a service view as listings, in display order."""
    fps = fingerprints or {}
    return [
        Listing(
            id=r["id"], title=r["title"], price=r.get("price"), seller=r.get("seller", ""),
            thumbnail_url=r.get("thumbnail_url", ""), fingerprint=fps.get(r["id"]),
            source=r.get("source") or None,
        )
        for r in view["search"]["results"]
    ]  # fmt: skip


def share(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


# ---- price ----------------------------------------------------------------------------------


def price_compliance(
    listings: Sequence[Listing], lo: float | None, hi: float | None
) -> dict[str, Any]:
    """Share of results with a price inside the parsed range. With no range everything complies;
    with one, a result with no price does not (it cannot be shown to be inside)."""
    if lo is None and hi is None:
        return {"checked": len(listings), "within": len(listings), "share": 1.0, "unpriced": 0}
    within = unpriced = 0
    for item in listings:
        if item.price is None:
            unpriced += 1
        elif (lo is None or item.price >= lo) and (hi is None or item.price <= hi):
            within += 1
    return {
        "checked": len(listings), "within": within, "unpriced": unpriced,
        "share": share(within, len(listings)) if listings else 1.0,
    }  # fmt: skip


# ---- repeats --------------------------------------------------------------------------------


def repeats(batches: Sequence[Batch]) -> dict[str, Any]:
    """Items shown in more than one round of one run."""
    seen: dict[str, int] = {}
    repeated: set[str] = set()
    for batch in batches:
        for item in batch.listings:
            if item.id in seen and seen[item.id] != batch.round:
                repeated.add(item.id)
            seen.setdefault(item.id, batch.round)
    return {"count": len(repeated), "ids": sorted(repeated)}


# ---- sellers --------------------------------------------------------------------------------


def seller_diversity(listings: Sequence[Listing]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in listings:
        counts[seller_key(item.seller)] = counts.get(seller_key(item.seller), 0) + 1
    top = max(counts.values()) if counts else 0
    return {"distinct": len(counts), "top_share": share(top, len(listings)), "top": top}


# ---- near duplicates ------------------------------------------------------------------------


def near_duplicate_groups(listings: Sequence[Listing]) -> dict[str, Any]:
    """Groups of two or more shown listings that `nearby.py` calls the same product (by title and
    price, and by image hash when a fingerprint is known). Reuses the pipeline's own signals."""
    items = [
        NearItem(key=i.id, title=i.title, seller=i.seller, price=i.price, fingerprint=i.fingerprint)
        for i in listings
    ]
    parent = list(range(len(items)))

    def find(n: int) -> int:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for a in range(len(items)):
        for b in range(a + 1, len(items)):
            if match_reason(items[a], items[b]) is not None:
                parent[find(a)] = find(b)
    sizes: dict[int, int] = {}
    for n in range(len(items)):
        sizes[find(n)] = sizes.get(find(n), 0) + 1
    groups = [s for s in sizes.values() if s >= 2]
    return {"groups": len(groups), "sizes": sorted(groups, reverse=True)}


# ---- context and colour ---------------------------------------------------------------------


def context_leakage(search_texts: Sequence[str], context_colors: Sequence[str]) -> dict[str, Any]:
    """Context colours (from the setting, e.g. "dark blue room") found as whole words in any of the
    search texts the pipeline used. Zero is required."""
    found: set[str] = set()
    for text in search_texts:
        low = text.lower()
        for color in context_colors:
            if re.search(rf"(?<![a-z]){re.escape(color.lower())}(?![a-z])", low):
                found.add(color)
    return {"count": len(found), "words": sorted(found)}


def color_of(title: str) -> str | None:
    return title_value("color", title)


def avoid_color_share(listings: Sequence[Listing], avoid: Sequence[str]) -> dict[str, Any]:
    hits = sum(1 for i in listings if color_of(i.title) in set(avoid))
    return {"count": hits, "share": share(hits, len(listings))}


def product_color_share(listings: Sequence[Listing], color: str) -> dict[str, Any]:
    hits = sum(1 for i in listings if color_of(i.title) == color)
    return {"count": hits, "share": share(hits, len(listings))}


def color_match_share(answers: Sequence[str | None]) -> dict[str, Any]:
    """The product-colour share from the judge's `color_match` answers: yes over the items it
    answered yes, no or cannot tell for. `cannot_tell` is not a match; a failed judgement (None) and
    `not_asked` are left out. Share is None when nothing could be judged."""
    scored = [a for a in answers if a in ("yes", "no", "cannot_tell")]
    yes = sum(1 for a in scored if a == "yes")
    return {
        "count": yes, "scored": len(scored), "cannot_tell": sum(1 for a in scored if a == "cannot_tell"),
        "failed": sum(1 for a in answers if a is None),
        "share": yes / len(scored) if scored else None,
    }  # fmt: skip


# ---- refine ---------------------------------------------------------------------------------


def exclusion_respected(listings: Sequence[Listing], sellers: Sequence[str]) -> dict[str, Any]:
    """Share of results not sold by an excluded seller (1.0 means the filter held)."""
    keys = {seller_key(s) for s in sellers}
    bad = [i.id for i in listings if seller_key(i.seller) in keys]
    return {"violations": len(bad), "share": 1.0 - share(len(bad), len(listings)), "ids": bad}


def focus_share(
    listings: Sequence[Listing],
    term: str,
    supply_titles: Sequence[str] = (),
    unseen_supply: int | None = None,
) -> dict[str, Any]:
    """Share of results whose title carries the focus word, and whether supply existed.

    Since stage 2r supply means what the pipeline could still show: `unseen_supply` is the number of
    unseen candidates that passed the filters and were flagged as matching the focus, and supply
    exists when it covers the quota the focus reserves, ceil(0.5 x the batch). Before that (and
    when the pipeline did not report the number: results files from earlier runs) supply was "a
    title in `supply_titles` (everything the run had already shown) or in the batch carries the
    word", which counts items that cannot be shown again; that reading is kept as a fallback and
    marked `measure: seen titles`. With no supply the share is reported but not scored."""
    low = term.lower()
    hits = sum(1 for i in listings if low in i.title.lower())
    reserved = math.ceil(0.5 * len(listings))
    if unseen_supply is not None:
        supply = bool(listings) and unseen_supply >= reserved
        measure = "unseen"
    else:
        supply = hits > 0 or any(low in t.lower() for t in supply_titles)
        measure = "seen titles"
    return {
        "hits": hits, "share": share(hits, len(listings)), "supply": supply,
        "supply_unseen": unseen_supply, "reserved": reserved, "measure": measure,
    }  # fmt: skip


# ---- judge-derived rates --------------------------------------------------------------------


def rate(answers: Sequence[str | None], good: set[str]) -> dict[str, Any]:
    """Share of scored answers in `good`; `None` (judge failed) is left out and counted."""
    scored = [a for a in answers if a is not None]
    return {
        "scored": len(scored), "failed": len(answers) - len(scored),
        "good": sum(1 for a in scored if a in good),
        "rate": share(sum(1 for a in scored if a in good), len(scored)) if scored else None,
    }  # fmt: skip


# ---- time and cost --------------------------------------------------------------------------


def median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None
