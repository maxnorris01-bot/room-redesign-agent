"""Every number that decides a preference, in one place (ADR 0012, stage 2c amendment).

The model describes the rated items; code decides what they say about the shopper, using these
numbers. They are starting points, not measurements: the stage 3 eval should tune them, and
`all_thresholds()` is the table to read (the docs quote it).
"""

from __future__ import annotations

# item weights: how much a rated item counts as evidence
LOVE_WEIGHT = 3
LIKE_WEIGHT = 1

# a dimension (shape, material, finish, ...) is classified from the weighted like mass of its values
CONVERGE_SHARE = 0.70  # one value holds at least this share of the like mass: converged
OPEN_SHARE = 0.30  # two or more values each hold at least this share: open
MIN_SUPPORT_ITEMS = (
    2  # items behind a converged value (a loved item with a comment naming it can be the 2nd)
)
LIFT_MIN = 1.3  # like share over exposure share (what was shown) needed to call a value preferred

# dislikes
DISLIKE_MIN_ITEMS = 2  # disliked items that share a value before it becomes a dislike chip
SELLER_DISLIKES_TO_DEPRIORITIZE = 2  # unchanged from stage 2

# seller affinity
MIN_AFFINITY_ITEMS = 2  # loved or liked items from one seller, and no dislike of it

# quality cues (non-exclusive phrases such as "solid wood"): share of the positive items showing it
QUALITY_CUE_MIN_SHARE = 0.50
QUALITY_CUE_MIN_ITEMS = 2

# price: positions are within the sorted prices of everything shown in the session
PRICE_UPPER_PERCENTILE = 0.67  # loved prices clustering at or above this: upper cluster
PRICE_LOWER_PERCENTILE = 0.33
PRICE_MIXED_LIKED_SPAN = 2 / 3  # liked prices spanning this much of the shown range: mixed
MIN_SHOWN_PRICES = 8  # fewest shown prices before any price signal

# a Shop Again batch
RESULT_COUNT = 24
DIVERSITY_MAX_SHARE = 0.60  # no single known value of a guarded dimension above this share
SELLER_MAX_SHARE = 0.50  # an affinity seller's share of a batch
SELLER_PINNED_SHARE = 0.80  # a pinned seller's share
SELLER_DEFAULT_CAP = 4  # results from any other seller (as in a typed search)


def all_thresholds() -> dict[str, float | int]:
    """Every threshold by name (for the docs and the eval)."""
    return {
        "LOVE_WEIGHT": LOVE_WEIGHT,
        "LIKE_WEIGHT": LIKE_WEIGHT,
        "CONVERGE_SHARE": CONVERGE_SHARE,
        "OPEN_SHARE": OPEN_SHARE,
        "MIN_SUPPORT_ITEMS": MIN_SUPPORT_ITEMS,
        "LIFT_MIN": LIFT_MIN,
        "DISLIKE_MIN_ITEMS": DISLIKE_MIN_ITEMS,
        "SELLER_DISLIKES_TO_DEPRIORITIZE": SELLER_DISLIKES_TO_DEPRIORITIZE,
        "MIN_AFFINITY_ITEMS": MIN_AFFINITY_ITEMS,
        "QUALITY_CUE_MIN_SHARE": QUALITY_CUE_MIN_SHARE,
        "QUALITY_CUE_MIN_ITEMS": QUALITY_CUE_MIN_ITEMS,
        "PRICE_UPPER_PERCENTILE": PRICE_UPPER_PERCENTILE,
        "PRICE_LOWER_PERCENTILE": PRICE_LOWER_PERCENTILE,
        "PRICE_MIXED_LIKED_SPAN": round(PRICE_MIXED_LIKED_SPAN, 4),
        "MIN_SHOWN_PRICES": MIN_SHOWN_PRICES,
        "RESULT_COUNT": RESULT_COUNT,
        "DIVERSITY_MAX_SHARE": DIVERSITY_MAX_SHARE,
        "SELLER_MAX_SHARE": SELLER_MAX_SHARE,
        "SELLER_PINNED_SHARE": SELLER_PINNED_SHARE,
        "SELLER_DEFAULT_CAP": SELLER_DEFAULT_CAP,
    }
