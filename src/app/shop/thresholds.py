"""Every number that decides a preference, in one place (ADR 0012, stage 2c, 2d and 2e amendments).

The model describes the rated items; code decides what they say about the shopper, using these
numbers. They are starting points, not measurements: the stage 3 eval should tune them, and
`all_thresholds()` is the table to read (the docs quote it).
"""

from __future__ import annotations

# item weights: how much a rated item counts as evidence
LOVE_WEIGHT = 3
LIKE_WEIGHT = 1

# a dimension (shape, material, finish, ...) is classified from the weighted like mass of its values
# stage 2e: converge needs more for the structural dimension (shape) than for soft ones
SHAPE_CONVERGE_SHARE = 0.80  # one shape holds at least this share of the like mass: converged
SOFT_CONVERGE_SHARE = 0.60  # the same for material, finish, frame color and style
OPEN_SHARE = 0.30  # two or more values each hold at least this share: open
MIN_SUPPORT_ITEMS = (
    2  # items behind a converged value (a loved item with a comment naming it can be the 2nd)
)
LIFT_MIN = 1.3  # like share over exposure share (what was shown) needed to call a value preferred
# stage 2d: shares are over ALL like mass (an item with no known value counts against every value);
# a dimension needs this share of the like mass to have a known value, and of what was shown to be
# tagged for it (the lift test cannot run otherwise), before it can converge. 2e: 0.60, because the
# shown baseline now comes from the model's candidate tags, not title keywords.
KNOWN_COVERAGE_MIN = 0.60

# dislikes
DISLIKE_MIN_ITEMS = 2  # disliked items that share a value before it becomes a dislike chip
SELLER_DISLIKES_TO_DEPRIORITIZE = 2  # unchanged from stage 2

# seller affinity
MIN_AFFINITY_ITEMS = 2  # loved or liked items from one seller, and no dislike of it
TIER_BATCH_SHARE = 0.50  # most of a batch the affinity tier may take (0.80 when pinned) (2f)
MAX_PEER_SEARCHES = 3  # peer retailers searched per Shop Again (2f)
ALIGNMENT_NOTE = "n/a (nothing decided yet)"

# stage 2i: group like-rate evidence. These are judgment from a handful of live sessions (one owner,
# about 48 ratings), not measurements; the stage 3 eval should tune them.
GROUP_MIN_RATED = 4  # a seller, tier or value needs this many rated items before it says anything
GROUP_MIN_LIKED = 2  # and this many liked (loved or liked, one each)
GROUP_RATE_RATIO = 1.5  # its like rate must be this many times the baseline like rate
GROUP_MIN_RATE = 0.35  # and at least this, so a low baseline cannot make a weak group a chip
MAX_SELLER_CHIPS = 3  # seller chips (highest like rate first); each also gets a seller search
TABLE_MIN_RATED = 2  # rated items a group needs to appear in the evidence table
LEAN_AWAY_MIN_RATED = 6  # a value needs this many rated items before the page leans away from it
LEAN_AWAY_MAX_RATIO = 0.5  # ... and a like rate of at most this times the baseline
LEAN_TOWARD_MIN_SHARE = 0.30  # a value leaned toward gets at least this share of the batch
LEAN_AWAY_MAX_SHARE = 0.30  # a value leaned away from gets at most this share of the batch
MARKET_MIN_RATED = 8  # marketplace and budget items rated before the exclusion chip can appear
MARKET_MAX_RATIO = 0.5  # ... like rate at most this times the baseline
MARKET_MAX_RATE = 0.20  # ... and at most this
MARKET_MIN_RESULTS = 12  # fewer results than this without marketplaces gets a banner
MARKET_REDISMISS_RATED = 8  # a deleted exclusion chip returns only after this many more ratings
SELLER_SEARCHES = 3  # seller searches per Shop Again (liked sellers first, then peers)
MODEL_QUERIES = 2  # model-written search queries per Shop Again
QUERY_MAX_WORDS = 8  # words in a model-written query
QUERY_LIKED_TITLES = 6  # liked titles shown to the query writer
QUERY_DISLIKED_TITLES = 4  # disliked titles shown to the query writer
BASE_PAGE_MAX = 2  # the base query is paged no deeper than this, page 3 only as a last resort
# stage 2k: colours that complement the room the request names (suggestions, never filters)
COLOR_SUGGESTED_MIN_SHARE = 0.40  # a batch takes at least this share from suggested colours
COLOR_AVOID_MAX_SHARE = 0.15  # and at most this share from the colours to avoid
# stage 2n: the same product from different vendors (near-duplicates). Judgment, not measured.
NEAR_FP_DISTANCE = (
    6  # image fingerprints (64-bit difference hash) this close or closer are one image
)
NEAR_FP_PRICE = 0.40  # ... and the prices must be within this share of each other
NEAR_TITLE_JACCARD = 0.8  # normalized titles sharing at least this share of their words ...
NEAR_TITLE_PRICE = 0.25  # ... with prices within this share, are the same product
NEAR_FP_TOP = 48  # candidates (best ranked first) whose thumbnails are fingerprinted live
NEAR_FP_WORKERS = 10  # parallel fingerprint fetches
NEAR_FP_BUDGET_S = 2.0  # seconds for the whole fingerprint step; the rest fall back to titles
# stage 2l: refine instructions typed by the shopper
REFINE_FOCUS_MIN_SHARE = 0.50  # a "focus on" chip takes at least this share of a batch, if supplied
REFINE_MAX_PER_SUBMIT = 5  # interpretations kept from one instruction
REFINE_MAX_CHIPS = 12  # active refine chips in a topic
REFINE_MAX_CHARS = 200  # characters in one instruction

# quality cues (non-exclusive phrases such as "solid wood"): share of the positive items showing it
QUALITY_CUE_MIN_SHARE = 0.50
QUALITY_CUE_MIN_ITEMS = 2

# price: positions are within the sorted prices of everything shown in the session
PRICE_BAND_MIN_MASS = 0.80  # a price band must hold this share of the like mass (and of liked-only)
PRICE_UPPER_PERCENTILE = 0.67  # loved prices clustering at or above this: upper cluster
PRICE_LOWER_PERCENTILE = 0.33
PRICE_MIXED_LIKED_SPAN = 2 / 3  # liked prices spanning this much of the shown range: mixed
MIN_SHOWN_PRICES = 8  # fewest shown prices before any price signal

# a Shop Again batch
RESULT_COUNT = 24
DIVERSITY_MAX_SHARE = 0.60  # no single known value of an open dimension (or shape) above this
CONVERGED_MAX_SHARE = 0.80  # no single known value of a converged dimension above this share
SELLER_MAX_SHARE = 0.50  # an affinity seller's share of a batch
SELLER_PINNED_SHARE = 0.80  # a pinned seller's share
SELLER_DEFAULT_CAP = 4  # results from any other seller (as in a typed search)

# stage 2e: supply and selection for open dimensions
MINORITY_POOL_SHARE = 0.40  # an open value under this share of the pool triggers targeted searches
OPEN_STRATUM_FLOOR = 0.25  # a value with a loved item gets at least this share of the batch slots
CANDIDATE_TAG_BATCH = 40  # titles per candidate tagging call (stage 2g: was 100)


def all_thresholds() -> dict[str, float | int]:
    """Every threshold by name (for the docs and the eval)."""
    return {
        "LOVE_WEIGHT": LOVE_WEIGHT,
        "LIKE_WEIGHT": LIKE_WEIGHT,
        "SHAPE_CONVERGE_SHARE": SHAPE_CONVERGE_SHARE,
        "SOFT_CONVERGE_SHARE": SOFT_CONVERGE_SHARE,
        "OPEN_SHARE": OPEN_SHARE,
        "MIN_SUPPORT_ITEMS": MIN_SUPPORT_ITEMS,
        "LIFT_MIN": LIFT_MIN,
        "KNOWN_COVERAGE_MIN": KNOWN_COVERAGE_MIN,
        "DISLIKE_MIN_ITEMS": DISLIKE_MIN_ITEMS,
        "SELLER_DISLIKES_TO_DEPRIORITIZE": SELLER_DISLIKES_TO_DEPRIORITIZE,
        "MIN_AFFINITY_ITEMS": MIN_AFFINITY_ITEMS,
        "TIER_BATCH_SHARE": TIER_BATCH_SHARE,
        "MAX_PEER_SEARCHES": MAX_PEER_SEARCHES,
        "GROUP_MIN_RATED": GROUP_MIN_RATED,
        "GROUP_MIN_LIKED": GROUP_MIN_LIKED,
        "GROUP_RATE_RATIO": GROUP_RATE_RATIO,
        "GROUP_MIN_RATE": GROUP_MIN_RATE,
        "MAX_SELLER_CHIPS": MAX_SELLER_CHIPS,
        "TABLE_MIN_RATED": TABLE_MIN_RATED,
        "LEAN_AWAY_MIN_RATED": LEAN_AWAY_MIN_RATED,
        "LEAN_AWAY_MAX_RATIO": LEAN_AWAY_MAX_RATIO,
        "LEAN_TOWARD_MIN_SHARE": LEAN_TOWARD_MIN_SHARE,
        "LEAN_AWAY_MAX_SHARE": LEAN_AWAY_MAX_SHARE,
        "MARKET_MIN_RATED": MARKET_MIN_RATED,
        "MARKET_MAX_RATIO": MARKET_MAX_RATIO,
        "MARKET_MAX_RATE": MARKET_MAX_RATE,
        "MARKET_MIN_RESULTS": MARKET_MIN_RESULTS,
        "MARKET_REDISMISS_RATED": MARKET_REDISMISS_RATED,
        "SELLER_SEARCHES": SELLER_SEARCHES,
        "MODEL_QUERIES": MODEL_QUERIES,
        "QUERY_MAX_WORDS": QUERY_MAX_WORDS,
        "QUERY_LIKED_TITLES": QUERY_LIKED_TITLES,
        "QUERY_DISLIKED_TITLES": QUERY_DISLIKED_TITLES,
        "BASE_PAGE_MAX": BASE_PAGE_MAX,
        "COLOR_SUGGESTED_MIN_SHARE": COLOR_SUGGESTED_MIN_SHARE,
        "COLOR_AVOID_MAX_SHARE": COLOR_AVOID_MAX_SHARE,
        "REFINE_FOCUS_MIN_SHARE": REFINE_FOCUS_MIN_SHARE,
        "NEAR_FP_DISTANCE": NEAR_FP_DISTANCE,
        "NEAR_FP_PRICE": NEAR_FP_PRICE,
        "NEAR_TITLE_JACCARD": NEAR_TITLE_JACCARD,
        "NEAR_TITLE_PRICE": NEAR_TITLE_PRICE,
        "NEAR_FP_TOP": NEAR_FP_TOP,
        "NEAR_FP_WORKERS": NEAR_FP_WORKERS,
        "NEAR_FP_BUDGET_S": NEAR_FP_BUDGET_S,
        "REFINE_MAX_PER_SUBMIT": REFINE_MAX_PER_SUBMIT,
        "REFINE_MAX_CHIPS": REFINE_MAX_CHIPS,
        "REFINE_MAX_CHARS": REFINE_MAX_CHARS,
        "QUALITY_CUE_MIN_SHARE": QUALITY_CUE_MIN_SHARE,
        "QUALITY_CUE_MIN_ITEMS": QUALITY_CUE_MIN_ITEMS,
        "PRICE_BAND_MIN_MASS": PRICE_BAND_MIN_MASS,
        "PRICE_UPPER_PERCENTILE": PRICE_UPPER_PERCENTILE,
        "PRICE_LOWER_PERCENTILE": PRICE_LOWER_PERCENTILE,
        "PRICE_MIXED_LIKED_SPAN": round(PRICE_MIXED_LIKED_SPAN, 4),
        "MIN_SHOWN_PRICES": MIN_SHOWN_PRICES,
        "RESULT_COUNT": RESULT_COUNT,
        "DIVERSITY_MAX_SHARE": DIVERSITY_MAX_SHARE,
        "CONVERGED_MAX_SHARE": CONVERGED_MAX_SHARE,
        "SELLER_MAX_SHARE": SELLER_MAX_SHARE,
        "SELLER_PINNED_SHARE": SELLER_PINNED_SHARE,
        "SELLER_DEFAULT_CAP": SELLER_DEFAULT_CAP,
        "MINORITY_POOL_SHARE": MINORITY_POOL_SHARE,
        "OPEN_STRATUM_FLOOR": OPEN_STRATUM_FLOOR,
        "CANDIDATE_TAG_BATCH": CANDIDATE_TAG_BATCH,
    }
