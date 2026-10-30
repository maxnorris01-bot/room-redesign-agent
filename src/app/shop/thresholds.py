"""Every number that decides a preference, in one place (ADR 0012, stage 2c, 2d and 2e amendments).

The model describes the rated items; code decides what they say about the shopper, using these
numbers. They are starting points, not measurements: the stage 3 eval should tune them, and
`all_thresholds()` is the table to read (the docs quote it).
"""

from __future__ import annotations

import math

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
QUERY_NOVELTY_JACCARD = (
    0.8  # a written query sharing this much of its words with a used one is a repeat
)
TYPED_MATCH_MIN_SHARE = 0.70  # a typed colour, material or finish: at least this share of a batch
PHOTO_TAG_BATCH = 12  # candidates per photo tagging call
PHOTO_TAG_WORKERS = 3  # photo tagging calls at once
PHOTO_TAG_BUDGET_S = 6.0  # seconds for the whole photo tagging step; the rest keep title tags
PHOTO_IMAGE_WAIT_S = (
    2.0  # seconds to wait for thumbnails not yet cached, before the calls' clock (2q)
)
PHOTO_FIRST_WAVE_GRACE_S = 4.0  # a first-wave call may finish this long after the budget (2q)
PHOTO_RETRY_MIN_LEFT_S = 2.0  # a failed photo call is retried once only with this much budget left
MIN_TAG_CALL_TIMEOUT_S = 8.0  # a tagging call is never given less than this (stage 2r): the
# post-2p run had "APITimeoutError after 1.0 s" when a call started with a second of budget left
PHOTO_CACHE_MAX = 200  # prepared thumbnails kept per service so none is fetched twice
# stage 2r: the fill loop. A batch short of RESULT_COUNT after every filter, cap and guard runs
# on-request variant searches (and pages of the ones that found something) until it is full.
FIRST_SEARCH_MAX_CALLS = 4  # Serper calls a first search may make in all: its two pages and two
# variants, the same 8 credits as before; a first search is the slowest thing a visitor waits for
PLENTIFUL_UNSEEN = 60  # unseen candidates that pass the filters before the optional searches stop
# (stage 2r): 60 is two and a half batches, enough for the caps, the guards and the quotas to pick
# from; the case-02 Shop Agains found 80 to 218 and still spent all 9 calls
GUARD_ALT_SUPPLY_MIN = 6  # a diversity guard is switched off for a dimension when fewer unseen
# candidates than this carry another value than the pool's top one: the guard can only shrink the
# batch then (stage 2r; the post-2p case-06 pool was 100% rectangular and the guard cut it to 14)
FILL_EMPTY_STREAK_MAX = 2  # stop after this many extra searches in a row that add no new candidate
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
OPEN_UI_TIMEOUT_S = 12  # the page waits this long for a store link before showing the Google page
NEAR_FP_BUDGET_S = 2.0  # seconds for the whole fingerprint step; the rest fall back to titles
# stage 2l: refine instructions typed by the shopper
REFINE_FOCUS_MIN_SHARE = 0.50  # a "focus on" chip takes at least this share of a batch, if supplied
REFINE_MAX_PER_SUBMIT = 5  # interpretations kept from one instruction
REFINE_MAX_CHIPS = 12  # active refine chips in a topic
REFINE_MAX_CHARS = 200  # characters in one instruction
FREE_NOTES_MAX = 3  # active free-note refinements (each is a per-item flag in the tagging calls)
NOTE_MAX_CHARS = 120  # characters in a free note's phrase

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
SELLER_CAP_SHARE = 0.30  # one ordinary seller's share of a batch (stage 2p; was a fixed 4)
SELLER_CAP_MIN = 2  # ... but never fewer than this many results per seller
SELLER_CAP_MAX = 4  # ... and never more than this (the old fixed cap)


def seller_cap(batch_size: int) -> int:
    """Most results one ordinary seller (not an affinity or pinned one) may have in a batch of
    `batch_size`: clamp(floor(0.30 x batch size), 2, 4). Batches of 24 or more get 4 (as before), 12
    gets 3, 8 or fewer get 2, so a short batch does not allow a third of itself from one seller."""
    return max(SELLER_CAP_MIN, min(SELLER_CAP_MAX, math.floor(SELLER_CAP_SHARE * batch_size)))


# stage 2q: an implausibly low price for a piece of furniture (a $2.23 mesh office chair in a pool
# whose median was in the hundreds) is a listing for a part or a swatch, not the product asked for.
# The rule is deliberately conservative: all three tests must hold, and only for furniture.
PRICE_OUTLIER_MEDIAN_SHARE = 0.10  # under this share of the pool's median price ...
PRICE_OUTLIER_FLOOR_USD = 15.0  # ... and under this many dollars, so a cheap pool keeps its items
PRICE_OUTLIER_MIN_POOL = 8  # priced candidates a pool needs before its median means anything
PRICE_GUARD_CATEGORIES = (
    "sofa", "armchair", "dining chair", "desk chair", "chair", "stool", "bench", "ottoman",
    "coffee table", "side table", "console table", "dining table", "table", "desk", "media unit",
    "bookshelf", "nightstand", "dresser", "wardrobe", "bed frame", "sideboard", "cabinet",
    "filing cabinet", "bar cart",
)  # fmt: skip  # furniture-type categories; rugs, curtains and lamps are sold cheap, so excluded


def seller_cap_ceiling(target_size: int) -> int:
    """The highest per-seller cap a short batch may relax to (stage 2q): floor(0.30 x target size),
    7 for a target of 24, so that one seller's share of a full batch stays at or under 0.30."""
    return math.floor(SELLER_CAP_SHARE * target_size)


# stage 2s: a seller bet. A learned seller chip used to give the seller up to half a batch from a
# first sample of 4 rated and 2 liked items, and the result of that boost was read only inside the
# overall like rate, a batch late. Now the allowance (the seller's total slots in a batch, never
# below the ordinary cap) starts small, grows only on proof and is revoked on a bad outcome.
SELLER_FIRST_SHARE = 1 / 6  # a new bet: 4 of a 24-item batch (a sixth), scaled with the batch size
SELLER_GROW_STEP_SHARE = 1 / 12  # each success adds 2 slots of 24
SELLER_PROVEN_MAX_SHARE = 1 / 3  # the most a proven seller gets (8 of 24) ...
# ... until its last two judged boosted batches were liked at this rate: then half the batch
SELLER_TOP_MIN_RATE = 0.60
SELLER_GROW_MIN_RATED = 3  # boosted items of the last batch that must be rated to judge it
SELLER_GROW_MIN_RATE = 0.50  # ... and the like rate that counts as a success
SELLER_REVOKE_MIN_RATED = 3  # rated boosted items before a bet can be revoked
SELLER_REVOKE_MAX_RATE = 0.35  # revoked below this like rate, or below the session baseline
SELLER_REDISMISS_RATED = 8  # a revoked seller can return only after this many further ratings


def seller_slots(share: float, batch_size: int = RESULT_COUNT) -> int:
    """A share of a batch as whole slots (a sixth of 24 is 4), never fewer than 1."""
    return max(1, round(share * batch_size))


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
        "QUERY_NOVELTY_JACCARD": QUERY_NOVELTY_JACCARD,
        "FIRST_SEARCH_MAX_CALLS": FIRST_SEARCH_MAX_CALLS,
        "FILL_EMPTY_STREAK_MAX": FILL_EMPTY_STREAK_MAX,
        "PLENTIFUL_UNSEEN": PLENTIFUL_UNSEEN,
        "GUARD_ALT_SUPPLY_MIN": GUARD_ALT_SUPPLY_MIN,
        "SELLER_FIRST_SHARE": round(SELLER_FIRST_SHARE, 4),
        "SELLER_GROW_STEP_SHARE": round(SELLER_GROW_STEP_SHARE, 4),
        "SELLER_PROVEN_MAX_SHARE": round(SELLER_PROVEN_MAX_SHARE, 4),
        "SELLER_TOP_MIN_RATE": SELLER_TOP_MIN_RATE,
        "SELLER_GROW_MIN_RATED": SELLER_GROW_MIN_RATED,
        "SELLER_GROW_MIN_RATE": SELLER_GROW_MIN_RATE,
        "SELLER_REVOKE_MIN_RATED": SELLER_REVOKE_MIN_RATED,
        "SELLER_REVOKE_MAX_RATE": SELLER_REVOKE_MAX_RATE,
        "SELLER_REDISMISS_RATED": SELLER_REDISMISS_RATED,
        "TYPED_MATCH_MIN_SHARE": TYPED_MATCH_MIN_SHARE,
        "PHOTO_TAG_BATCH": PHOTO_TAG_BATCH,
        "PHOTO_TAG_WORKERS": PHOTO_TAG_WORKERS,
        "PHOTO_TAG_BUDGET_S": PHOTO_TAG_BUDGET_S,
        "PHOTO_IMAGE_WAIT_S": PHOTO_IMAGE_WAIT_S,
        "PHOTO_FIRST_WAVE_GRACE_S": PHOTO_FIRST_WAVE_GRACE_S,
        "PHOTO_RETRY_MIN_LEFT_S": PHOTO_RETRY_MIN_LEFT_S,
        "MIN_TAG_CALL_TIMEOUT_S": MIN_TAG_CALL_TIMEOUT_S,
        "PHOTO_CACHE_MAX": PHOTO_CACHE_MAX,
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
        "OPEN_UI_TIMEOUT_S": OPEN_UI_TIMEOUT_S,
        "REFINE_MAX_PER_SUBMIT": REFINE_MAX_PER_SUBMIT,
        "REFINE_MAX_CHIPS": REFINE_MAX_CHIPS,
        "REFINE_MAX_CHARS": REFINE_MAX_CHARS,
        "FREE_NOTES_MAX": FREE_NOTES_MAX,
        "NOTE_MAX_CHARS": NOTE_MAX_CHARS,
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
        "SELLER_CAP_SHARE": SELLER_CAP_SHARE,
        "SELLER_CAP_MIN": SELLER_CAP_MIN,
        "SELLER_CAP_MAX": SELLER_CAP_MAX,
        "PRICE_OUTLIER_MEDIAN_SHARE": PRICE_OUTLIER_MEDIAN_SHARE,
        "PRICE_OUTLIER_FLOOR_USD": PRICE_OUTLIER_FLOOR_USD,
        "PRICE_OUTLIER_MIN_POOL": PRICE_OUTLIER_MIN_POOL,
        "MINORITY_POOL_SHARE": MINORITY_POOL_SHARE,
        "OPEN_STRATUM_FLOOR": OPEN_STRATUM_FLOOR,
        "CANDIDATE_TAG_BATCH": CANDIDATE_TAG_BATCH,
    }
