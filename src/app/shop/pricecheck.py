"""Drop implausibly cheap furniture listings (ADR 0012, stage 2q).

The post-2p live re-run showed a $2.23 "Evo Mesh Backrest Ergonomic Office Chair" in a batch of
desk chairs priced in the hundreds: a listing for a part, not the chair. A furniture candidate is
dropped when its price is under `PRICE_OUTLIER_MEDIAN_SHARE` of the pool's median AND under
`PRICE_OUTLIER_FLOOR_USD`, for a pool of at least `PRICE_OUTLIER_MIN_POOL` priced candidates. A
cheap pool keeps its cheap items (10% of a $40 median is $4); an unpriced candidate is not touched
here (the price filter deals with it as it did before); only furniture-type categories are checked.
"""

from __future__ import annotations

import statistics
from typing import Any

from app.shop.thresholds import (
    PRICE_GUARD_CATEGORIES,
    PRICE_OUTLIER_FLOOR_USD,
    PRICE_OUTLIER_MEDIAN_SHARE,
    PRICE_OUTLIER_MIN_POOL,
)
from app.shopping import SearchCandidate


def guarded_category(category_key: str | None) -> bool:
    return category_key in PRICE_GUARD_CATEGORIES


def split_price_outliers(
    candidates: list[SearchCandidate], category_key: str | None
) -> tuple[list[SearchCandidate], list[dict[str, Any]]]:
    """(the candidates to keep, one row per dropped candidate with the reason). Nothing is dropped
    outside the guarded categories or from a pool too small to have a meaningful median."""
    if not guarded_category(category_key):
        return candidates, []
    prices = [c.price for c in candidates if c.price is not None and c.currency == "USD"]
    if len(prices) < PRICE_OUTLIER_MIN_POOL:
        return candidates, []
    median = statistics.median(prices)
    limit = min(PRICE_OUTLIER_MEDIAN_SHARE * median, PRICE_OUTLIER_FLOOR_USD)
    kept: list[SearchCandidate] = []
    dropped: list[dict[str, Any]] = []
    for c in candidates:
        if c.price is not None and c.currency == "USD" and c.price < limit:
            dropped.append({
                "title": " ".join(c.title.split())[:80], "seller": c.seller, "price": c.price,
                "median": round(median, 2),
                "reason": (
                    f"${c.price:.2f} is under {PRICE_OUTLIER_MEDIAN_SHARE:.0%} of the pool's "
                    f"median ${median:.0f} and under ${PRICE_OUTLIER_FLOOR_USD:.0f}"
                ),
            })  # fmt: skip
        else:
            kept.append(c)
    return kept, dropped
