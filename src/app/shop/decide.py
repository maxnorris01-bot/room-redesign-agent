"""The model describes; code decides (ADR 0012, stage 2c amendment; coverage 2d; vocabularies 2e).

A language model is good at reading one product ("round, walnut, brass frame") and bad at deciding what
a shopper's mixed ratings mean. Stage 2b let it propose preferences and it over-read: with a liked round
item and a liked rectangular one it picked round, and a batch that was mostly round made "likes round"
look true. So here the model only tags each rated item (shape, material, finish, frame color, style,
quality cues, from the fixed vocabularies in `app.shop.vocab`) and this module decides, from the tags of
ALL the session's ratings every time:

  * per dimension, the weighted distribution of values among loved (3) and liked (1) items;
  * an exposure baseline: how common each value is among everything the shopper was shown, from the
    model's tags of those results (`app.shop.candidates`); a title with no tag is unknown;
  * converged (one value holds 80% of the like mass for shape, 60% for the soft dimensions, at least 2
    items support it, and its like share beats its exposure share by a lift of 1.3), open (two or more
    values each hold 30%: a neutral chip, no pressure on search or ranking), or none;
  * dislikes only when 2 disliked items share a value that no loved or liked item has, at the same
    per-dimension share and the same lift test; one dislike only excludes that item;
  * seller affinity (2 loved or liked items AND 40% of the like mass, no dislike, and the lift test),
    price as a band or as mixed, quality cues.

Every number is in `app.shop.thresholds`. If the candidate tagging call failed, the exposure baseline
comes from title keywords (poor coverage) and no attribute converges at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.shop.evidence import (
    Evidence,
    group_rows,
    lean_text,
    leaning_entries,
    marketplace_decision,
    seller_entries,
    tier_entry,
)
from app.shop.models import ItemTags, ProfileEntry, ShopQuery, ShopResult
from app.shop.retailers import seller_key
from app.shop.textutil import (
    Rated,
    mentions,
    normalize_phrase,
    phrases_match,
    text_matches,
)
from app.shop.thresholds import (
    DISLIKE_MIN_ITEMS,
    KNOWN_COVERAGE_MIN,
    LIFT_MIN,
    LIKE_WEIGHT,
    LOVE_WEIGHT,
    MIN_AFFINITY_ITEMS,
    MIN_SHOWN_PRICES,
    MIN_SUPPORT_ITEMS,
    OPEN_SHARE,
    PRICE_BAND_MIN_MASS,
    PRICE_LOWER_PERCENTILE,
    PRICE_MIXED_LIKED_SPAN,
    PRICE_UPPER_PERCENTILE,
    QUALITY_CUE_MIN_ITEMS,
    QUALITY_CUE_MIN_SHARE,
    SHAPE_CONVERGE_SHARE,
    SOFT_CONVERGE_SHARE,
)
from app.shop.vocab import (
    DIMENSIONS,
    FAMILY_LABELS,
    LABELS,
    OTHER,
    STRUCTURAL,
    normalize,
    normalize_style,
    style_leaves,
    tag_values,
    title_tags,
    title_value,
)

# features, not build or finish: never a learned quality cue
FEATURE_WORDS = frozenset(
    {"storage", "drawer", "drawers", "lift", "shelf", "shelves", "adjustable", "foldable", "wheels"}
)
DIMENSION_LABELS = LABELS
TagMap = Mapping[str, Mapping[str, str | None]]  # result id -> dimension -> vocabulary value


def converge_share(dim: str) -> float:
    """The share of the like mass one value needs: shape (structural) 80%, the rest 60%."""
    return SHAPE_CONVERGE_SHARE if dim in STRUCTURAL else SOFT_CONVERGE_SHARE


# ---- the tagged items --------------------------------------------------------------------------------


@dataclass(frozen=True)
class TaggedItem:
    """A rated item with one value (or none) per dimension and its quality cues."""

    rated: Rated
    values: dict[str, str]
    cues: tuple[str, ...] = ()

    @property
    def weight(self) -> int:
        return LOVE_WEIGHT if self.rated.rating == "love" else LIKE_WEIGHT

    def vals(self, dim: str) -> list[str]:
        """The decision values for `dim`: one, or for style the families of up to two labels."""
        return tag_values(dim, self.values.get(dim))


def tag_item(
    rated: Rated, model: ItemTags | None, cached: Mapping[str, str | None] | None = None
) -> TaggedItem:
    """The item's values from the model's tags only, folded into the fixed vocabularies. A dimension
    the model left null, and every dimension of an item the model did not tag, is unknown: title
    keywords are never used to fill it. The colour (stage 2k) is not in the preference call: it comes
    from the item's cached tags (the photo or title tagging of what was shown), `cached`."""
    values: dict[str, str] = {}
    cues: list[str] = []
    if model is not None:
        for dim in DIMENSIONS:
            if dim == "color":
                got = normalize("color", (cached or {}).get("color"))
            elif dim == "style":
                got = normalize_style(model.style)
            else:
                got = normalize(dim, getattr(model, dim))
            if got:
                values[dim] = got
        cues = [normalize_phrase(c) for c in model.quality_cues if normalize_phrase(c)]
    return TaggedItem(rated, values, tuple(dict.fromkeys(cues)))


@dataclass
class Exposure:
    """What the shopper was shown, by value: the baseline the lift test compares a like share to."""

    shares: dict[str, dict[str, float]] = field(default_factory=dict)  # dim -> value -> share
    coverage: dict[str, float] = field(default_factory=dict)  # dim -> share of shown with a value
    # per dimension, how many shown results got their value from a photo, the title model or title
    # keywords (stage 2h): the trace shows it
    sources: dict[str, dict[str, int]] = field(default_factory=dict)
    source: str = "model"  # "model" (candidate tags) or "keywords" (the tagging call failed)
    tagged: int = 0  # shown results that came with tags
    total: int = 0  # shown results

    @property
    def usable(self) -> bool:
        """Whether attributes may converge: only with the model's tags of what was shown."""
        return self.source == "model"


def build_exposure(
    shown: list[ShopResult],
    tags: TagMap | None = None,
    keywords: bool = False,
    tag_source: Mapping[str, str] | None = None,
) -> Exposure:
    """Each dimension's value shares over the shown results that have a value for it, and the share
    of the shown results that have one (coverage). `tags` are the model's (by result id). With
    `keywords=True` (the candidate tagging call failed) a result with no tags is read by its title's
    keywords and the baseline is marked as such: no attribute may converge on it."""
    tags = tags or {}
    counts: dict[str, dict[str, int]] = {}
    known: dict[str, int] = {}
    by_source: dict[str, dict[str, int]] = {}
    tagged = 0
    for r in shown:
        got = tags.get(r.id)
        if got is not None:
            tagged += 1
            values = {d: v for d, v in got.items() if v and d in DIMENSIONS}
            source = (tag_source or {}).get(r.id, "title")
        elif keywords:
            values = title_tags(r.title)
            source = "keyword"
        else:
            values = {}
            source = ""
        for dim in values:
            by_source.setdefault(dim, {})
            by_source[dim][source] = by_source[dim].get(source, 0) + 1
        for dim, raw in values.items():
            found = tag_values(dim, raw)
            for value in found:
                counts.setdefault(dim, {}).setdefault(value, 0)
                counts[dim][value] += 1
            if found:
                known[dim] = known.get(dim, 0) + 1
    total = len(shown)
    return Exposure(
        {d: {v: n / known[d] for v, n in by.items()} for d, by in counts.items() if by},
        {d: n / total for d, n in known.items()} if total else {},
        sources=by_source,
        source="keywords" if keywords else "model",
        tagged=tagged,
        total=total,
    )


# ---- one dimension ----------------------------------------------------------------------------------


@dataclass
class DimensionResult:
    dimension: str
    status: str  # converged | open | none
    value: str | None = None  # converged
    values: list[str] = field(default_factory=list)  # open (converged: [value])
    share: float | None = None  # the converged value's share of ALL like mass
    exposure: float | None = None
    lift: float | None = None
    support: list[str] = field(default_factory=list)  # ids of the supporting loved or liked items
    shares: dict[str, float] = field(default_factory=dict)  # every value's share of ALL like mass
    trace: dict[str, Any] = field(default_factory=dict)  # numbers and the reason, for the panel


def _lift(
    share: float, exposure: dict[str, dict[str, float]], dim: str, value: str
) -> tuple[float | None, float | None, bool]:
    """(lift, exposure share, passes). A dimension with no known exposure cannot be tested: it
    passes, with no lift. A value never shown has exposure 0 and passes."""
    by_value = exposure.get(dim)
    if not by_value:
        return None, None, True
    seen = by_value.get(value, 0.0)
    if seen <= 0:
        return None, 0.0, True
    lift = share / seen
    return round(lift, 3), round(seen, 4), lift >= LIFT_MIN


def classify_dimension(
    dim: str, positives: list[TaggedItem], exposure: Exposure
) -> DimensionResult:
    """Converged, open or none for one dimension, from the loved and liked items' values.

    Shares are over ALL like mass: an item with no known value counts against every value, so
    silent items cannot inflate one value. Converged needs known values on at least
    `KNOWN_COVERAGE_MIN` of the like mass, one value on `converge_share(dim)` of it (80% for shape,
    60% for the soft dimensions), 2 supporting items, tags for the dimension on at least
    `KNOWN_COVERAGE_MIN` of what was shown (else the lift test cannot run) and a lift of `LIFT_MIN`.
    Open needs two values at `OPEN_SHARE` of all mass. `other` is a read value that is never a
    preference. When the exposure baseline came from title keywords (the candidate tagging call
    failed) nothing converges.
    """
    mass: dict[str, int] = {}
    ids: dict[str, list[str]] = {}
    known = 0
    leaves: dict[str, int] = {}
    for item in sorted(positives, key=lambda i: i.rated.id):
        found = item.vals(dim)
        if found:
            known += item.weight
        for value in found:
            mass[value] = mass.get(value, 0) + item.weight
            ids.setdefault(value, []).append(item.rated.id)
        if dim == "style":
            for leaf in style_leaves(item.values.get(dim)):
                leaves[leaf] = leaves.get(leaf, 0) + item.weight
    total = sum(item.weight for item in positives)
    exp_cov = exposure.coverage.get(dim, 0.0)
    need = converge_share(dim)
    by_rating = {i.rated.id: i.rated.rating for i in positives}
    trace: dict[str, Any] = {
        "like_mass": {**{v: m for v, m in sorted(mass.items(), key=lambda kv: (-kv[1], kv[0]))}, "(unknown)": total - known},
        "total_mass": total,
        "known_coverage": round(known / total, 4) if total else 0.0,
        "exposure_coverage": round(exp_cov, 4),
        "exposure_sources": dict(exposure.sources.get(dim, {})),
        "exposure": {v: round(x, 4) for v, x in sorted(exposure.shares.get(dim, {}).items())},
        "converge_share": need,
        "loved": sorted({v for v, i in ids.items() if any(by_rating[x] == "love" for x in i)}),
    }  # fmt: skip
    if dim == "style":
        trace["leaves"] = dict(sorted(leaves.items(), key=lambda kv: (-kv[1], kv[0])))
    usable = {v: m for v, m in mass.items() if v != OTHER}
    if total == 0 or not usable:
        trace.update(decision="none", reason="no loved or liked item shows a usable value")
        return DimensionResult(dim, "none", trace=trace)
    shares = {v: m / total for v, m in mass.items()}
    ordered = sorted(usable, key=lambda v: (-shares[v], v))
    top = ordered[0]
    by_id = {i.rated.id: i.rated for i in positives}
    support = len(ids[top])
    if support < MIN_SUPPORT_ITEMS and any(
        by_id[i].rating == "love" and mentions(top, by_id[i].comment) for i in ids[top]
    ):
        support = MIN_SUPPORT_ITEMS  # a loved item whose comment names it stands in for a second
    lift, seen, passes = _lift(shares[top], exposure.shares, dim, top)
    trace.update(top=top, top_share=round(shares[top], 4), support=support, lift=lift)
    why: list[str] = []
    if known / total < KNOWN_COVERAGE_MIN:
        why.append(f"known coverage {known / total:.0%} is under {KNOWN_COVERAGE_MIN:.0%}")
    if shares[top] < need:
        why.append(f"top value {top} holds {shares[top]:.0%} of all like mass, under {need:.0%}")
    if support < MIN_SUPPORT_ITEMS:
        why.append(f"{top} has {support} supporting item(s), needs {MIN_SUPPORT_ITEMS}")
    if not exposure.usable:
        why.append("the shown results could not be tagged by the model (title keywords only)")
    elif exp_cov < KNOWN_COVERAGE_MIN:
        why.append(
            f"only {exp_cov:.0%} of shown results are tagged for it (under {KNOWN_COVERAGE_MIN:.0%}): the lift test cannot run"
        )
    elif not passes:
        why.append(f"lift {lift} for {top} is under {LIFT_MIN}")
    shares_out = {v: round(x, 4) for v, x in shares.items()}
    if not why:
        trace.update(
            decision="converged", reason=f"{top}: {shares[top]:.0%} of like mass, lift {lift}"
        )
        return DimensionResult(
            dim, "converged", top, [top], round(shares[top], 4), seen, lift, ids[top],
            shares_out, trace,
        )  # fmt: skip
    open_values = [v for v in ordered if shares[v] >= OPEN_SHARE]
    if len(open_values) >= 2:
        support_ids = [i for v in open_values for i in ids[v]]
        trace.update(
            decision="open",
            reason="open: "
            + " and ".join(f"{v} {shares[v]:.0%}" for v in open_values)
            + f" each hold at least {OPEN_SHARE:.0%} of all like mass (not converged: {'; '.join(why)})",
        )
        return DimensionResult(
            dim, "open", None, open_values, None, None, None, support_ids, shares_out, trace
        )  # fmt: skip
    trace.update(decision="none", reason="not converged: " + "; ".join(why))
    return DimensionResult(dim, "none", shares=shares_out, trace=trace)


def dislike_values(
    dim: str,
    disliked: list[TaggedItem],
    positives: list[TaggedItem],
    exposure: Exposure,
) -> list[tuple[str, list[str], float, float | None, float | None]]:
    """Values disliked: at least `DISLIKE_MIN_ITEMS` disliked items share it, its share of ALL the
    disliked items is at least the dimension's converge share (unknowns count against it), no
    loved or liked item has it, and its dislike share beats its exposure share by the lift (which
    needs tags on `KNOWN_COVERAGE_MIN` of what was shown). Returns (value, ids, share, exposure,
    lift)."""
    if not exposure.usable or exposure.coverage.get(dim, 0.0) < KNOWN_COVERAGE_MIN:
        return []
    counts: dict[str, list[str]] = {}
    for item in sorted(disliked, key=lambda i: i.rated.id):
        for value in item.vals(dim):
            if value != OTHER:
                counts.setdefault(value, []).append(item.rated.id)
    total = len(disliked)  # unknown values count against every value here too
    liked_values = {v for i in positives for v in i.vals(dim)}
    out: list[tuple[str, list[str], float, float | None, float | None]] = []
    for value, ids in sorted(counts.items()):
        if len(ids) < DISLIKE_MIN_ITEMS or value in liked_values:
            continue
        share = len(ids) / total
        if share < converge_share(dim):
            continue
        lift, seen, passes = _lift(share, exposure.shares, dim, value)
        if passes:
            out.append((value, ids, round(share, 4), seen, lift))
    return out


# ---- seller and price --------------------------------------------------------------------------------


def seller_exposure(shown: list[ShopResult]) -> dict[str, float]:
    total = sum(1 for r in shown if r.seller)
    if not total:
        return {}
    counts: dict[str, int] = {}
    for r in shown:
        if r.seller:
            counts[seller_key(r.seller)] = counts.get(seller_key(r.seller), 0) + 1
    return {k: n / total for k, n in counts.items()}


def seller_decision(
    rated: dict[str, Rated], shown: list[ShopResult] | None = None
) -> tuple[ProfileEntry | None, dict[str, Any]]:
    """The first seller chip, if any, and the numbers behind the decision for every seller.

    Stage 2i: a seller needs `GROUP_MIN_RATED` rated items, `GROUP_MIN_LIKED` liked, and a like rate
    of at least `GROUP_RATE_RATIO` times the baseline and `GROUP_MIN_RATE` (see `app.shop.evidence`).
    What was shown no longer matters (`shown` is kept only so older callers still work). Marketplace
    and budget retailers never earn a chip. `decide` keeps every seller chip (up to
    `MAX_SELLER_CHIPS`); this returns the best one."""
    entries, trace = seller_entries(rated)
    return (entries[0] if entries else None), trace


def tier_decision(rated: dict[str, Rated]) -> tuple[ProfileEntry | None, dict[str, Any]]:
    """The retailer tier chip ("retailers like West Elm"), if any, and the numbers behind it: the
    same group like-rate rule as a seller, over the tier of each item's seller
    (`app.shop.retailers`; a retailer not in the table has no tier)."""
    return tier_entry(rated)


def seller_affinity_entry(
    rated: dict[str, Rated], shown: list[ShopResult] | None = None
) -> ProfileEntry | None:
    return seller_decision(rated, shown)[0]


def _percentile(values: list[float], q: float) -> float:
    pos = q * (len(values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def _rank_fraction(values: list[float], value: float) -> float:
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return (below + 0.5 * equal) / len(values)


def _round_money(lo: float, hi: float) -> tuple[float, float]:
    import math

    step = 50.0 if hi >= 300 else 10.0
    return math.floor(lo / step) * step, math.ceil(hi / step) * step


def _money(x: float) -> str:
    return f"${x:,.0f}"


def price_decision(
    rated: dict[str, Rated], shown: list[ShopResult]
) -> tuple[ProfileEntry | None, dict[str, Any]]:
    """Price as a spread, from where the loved and liked prices sit among every price shown, and the
    numbers behind it.

    A *band* (the upper or lower third of the shown prices, by the average position of the loved and
    liked prices) is only kept when it holds at least `PRICE_BAND_MIN_MASS` of the like mass AND of
    the liked-only mass; a band that leaves liked items outside is wrong, so the answer is *mixed*: a
    neutral chip with the loved and liked ranges, no price pressure, the loved prices only a quality
    hint. With no upper or lower cluster, a spread of at least two thirds of the shown range is also
    mixed, and a narrow mid-range cluster says nothing. The typed price cap is never touched.
    """
    prices = sorted(r.price for r in shown if r.price is not None and r.price > 0)
    loved = [r for r in rated.values() if r.rating == "love" and r.price]
    liked = [r for r in rated.values() if r.rating == "like" and r.price]
    positive = loved + liked
    trace: dict[str, Any] = {
        "shown_prices": len(prices),
        "loved": [min((r.price or 0) for r in loved), max((r.price or 0) for r in loved)] if loved else None,
        "liked": [min((r.price or 0) for r in liked), max((r.price or 0) for r in liked)] if liked else None,
    }  # fmt: skip
    if len(prices) < MIN_SHOWN_PRICES:
        trace.update(
            decision="none", reason=f"only {len(prices)} shown prices, needs {MIN_SHOWN_PRICES}"
        )
        return None, trace
    if len(positive) < MIN_AFFINITY_ITEMS:
        trace.update(
            decision="none",
            reason=f"{len(positive)} priced loved or liked item(s), needs {MIN_AFFINITY_ITEMS}",
        )
        return None, trace
    span = prices[-1] - prices[0]
    mean = sum(_rank_fraction(prices, r.price or 0.0) for r in positive) / len(positive)
    ids = sorted(r.id for r in positive)
    titles = [" ".join(rated[i].title.split())[:100] for i in ids[:3]]
    trace["mean_position"] = round(mean, 3)

    def mixed(reason: str) -> tuple[ProfileEntry, dict[str, Any]]:
        parts = []
        if loved:
            lo, hi = _money(trace["loved"][0]), _money(trace["loved"][1])
            parts.append(f"loved {lo}" if lo == hi else f"loved {lo} to {hi}")
        if liked:
            parts.append(f"liked {_money(trace['liked'][0])} to {_money(trace['liked'][1])}")
        trace.update(decision="mixed", reason=reason)
        entry = ProfileEntry(
            text="price: mixed", attr_kind="price_mixed", polarity="like", status="mixed",
            detail=", ".join(parts), evidence=ids[:6], evidence_titles=titles,
            weight=len(positive), low=trace["loved"][0] if loved else None,
            high=trace["loved"][1] if loved else None, dimension="price",
        )  # fmt: skip
        return entry, trace

    if mean >= PRICE_UPPER_PERCENTILE:
        band, lo, hi = "upper", _percentile(prices, PRICE_UPPER_PERCENTILE), prices[-1]
    elif mean <= PRICE_LOWER_PERCENTILE:
        band, lo, hi = "lower", prices[0], _percentile(prices, PRICE_LOWER_PERCENTILE)
    else:
        spread = max(r.price or 0 for r in positive) - min(r.price or 0 for r in positive)
        if span > 0 and spread >= PRICE_MIXED_LIKED_SPAN * span:
            return mixed(
                f"no cluster (mean position {mean:.2f}) and the prices span {spread / span:.0%} of the shown range"
            )
        trace.update(
            decision="none",
            reason=f"prices cluster mid-range (mean position {mean:.2f}): no signal",
        )
        return None, trace
    lo, hi = _round_money(lo, hi)

    def inside(r: Rated) -> bool:
        return lo <= (r.price or 0.0) <= hi

    total_w = sum(LOVE_WEIGHT if r.rating == "love" else LIKE_WEIGHT for r in positive)
    in_w = sum((LOVE_WEIGHT if r.rating == "love" else LIKE_WEIGHT) for r in positive if inside(r))
    liked_in = sum(1 for r in liked if inside(r)) / len(liked) if liked else 1.0
    overall = in_w / total_w
    trace.update(
        band=f"{band} {_money(lo)} to {_money(hi)}",
        mass_in_band=round(overall, 4),
        liked_in_band=round(liked_in, 4),
    )
    if overall < PRICE_BAND_MIN_MASS or liked_in < PRICE_BAND_MIN_MASS:
        return mixed(
            f"the {band} band {_money(lo)} to {_money(hi)} would hold {overall:.0%} of the like mass and "
            f"{liked_in:.0%} of the liked items, under {PRICE_BAND_MIN_MASS:.0%}"
        )
    trace.update(
        decision="band",
        reason=f"{band} band holds {overall:.0%} of the like mass and {liked_in:.0%} of liked",
    )
    entry = ProfileEntry(
        text=f"{band} range, about {_money(lo)} to {_money(hi)}", attr_kind="price_band",
        polarity="like", evidence=ids[:6], evidence_titles=titles,
        confidence=round(mean, 2), weight=len(positive), low=lo, high=hi, band=band, dimension="price",
    )  # fmt: skip
    return entry, trace


def price_entry(rated: dict[str, Rated], shown: list[ShopResult]) -> ProfileEntry | None:
    return price_decision(rated, shown)[0]


# ---- the decision -------------------------------------------------------------------------------------


@dataclass
class Decisions:
    entries: list[ProfileEntry]
    dimensions: dict[str, DimensionResult]
    items: dict[str, TaggedItem]
    trace: dict[str, Any] = field(default_factory=dict)


def _titles(items: dict[str, TaggedItem], ids: list[str]) -> list[str]:
    return [" ".join(items[i].rated.title.split())[:100] for i in ids[:3] if i in items]


FALLBACK_NOTE = "Could not read item details; only seller and price were learned."


def decide(
    rated: dict[str, Rated],
    tags: dict[str, ItemTags],
    shown: list[ShopResult],
    removed: list[str],
    typed_terms: list[str],
    image_ids: set[str] | None = None,
    fallback: bool = False,
    exposure: Exposure | None = None,
    dismissed: Mapping[str, int] | None = None,
    cached_tags: TagMap | None = None,
) -> Decisions:
    """The preference entries for the session's ratings, and the trace of how each was decided.

    Pure and order independent: the same ratings, tags and shown results always give the same
    entries. With `fallback=True` (the model's tags are unavailable) no attribute is decided at all
    (shape, material, finish, frame color, style, size, cues, attribute dislikes): titles alone would
    count only the keyword-bearing ones. Only seller affinity and price are decided then.
    """
    items = {
        rid: tag_item(r, None if fallback else tags.get(rid), (cached_tags or {}).get(rid))
        for rid, r in rated.items()
    }
    positives = [i for i in items.values() if i.rated.rating in ("love", "like")]
    disliked = [i for i in items.values() if i.rated.rating == "dislike"]
    exposure = exposure or build_exposure(shown, None, keywords=True)
    entries: list[ProfileEntry] = []
    results: dict[str, DimensionResult] = {}
    dim_trace: dict[str, Any] = {}

    typed_keys = [normalize_phrase(t) for t in typed_terms]

    def removed_hit(text: str) -> bool:
        key = normalize_phrase(text)
        return bool(key) and any(phrases_match(key, r) for r in removed)

    def typed_hit(text: str, dim: str | None = None) -> bool:
        key = normalize_phrase(text)
        if not key:
            return False
        if dim in ("shape", "color_finish", "frame_color"):
            return key in typed_keys
        return any(phrases_match(key, t) for t in typed_keys)

    def blocked(text: str, dim: str | None = None) -> bool:
        """Deleted by the shopper, or already said in the request. For shape, finish and frame
        color only the whole phrase counts ("warm wood" is not the typed "wood")."""
        return removed_hit(text) or typed_hit(text, dim)

    for dim in DIMENSIONS:
        if fallback:
            results[dim] = DimensionResult(dim, "none")
            dim_trace[dim] = {"decision": "none", "reason": "fallback: item details were not read"}
            continue
        res = classify_dimension(dim, positives, exposure)
        results[dim] = res
        dim_trace[dim] = res.trace
        label = DIMENSION_LABELS[dim].lower()
        shown_value = (
            FAMILY_LABELS.get(res.value, res.value) + " style"
            if dim == "style" and res.value
            else res.value
        )
        if res.status == "converged" and res.value and blocked(res.value, dim):
            res.trace["reason"] += (
                " (not shown: the typed request already says it, or you deleted it)"
            )
        if res.status == "converged" and res.value and not blocked(res.value, dim):
            entries.append(
                ProfileEntry(
                    text=shown_value or res.value,
                    attr_kind=dim,
                    polarity="like",
                    dimension=dim,
                    status="converged",
                    values=[res.value],
                    evidence=sorted(res.support)[:6],
                    evidence_titles=_titles(items, sorted(res.support)),
                    confidence=round(res.share or 0.0, 2),
                    weight=sum(items[i].weight for i in res.support),
                    share=res.share,
                    exposure=res.exposure,
                    lift=res.lift,
                )
            )
        elif res.status == "open" and not blocked(f"{label} open"):
            entries.append(
                ProfileEntry(
                    text=f"{label}: open",
                    attr_kind="open",
                    polarity="like",
                    dimension=dim,
                    status="open",
                    values=res.values,
                    detail="liked "
                    + _join([FAMILY_LABELS.get(v, v) if dim == "style" else v for v in res.values]),
                    evidence=sorted(res.support)[:6],
                    evidence_titles=_titles(items, sorted(res.support)),
                    weight=sum(items[i].weight for i in res.support),
                )
            )
        for value, ids, share, seen, lift in dislike_values(dim, disliked, positives, exposure):
            if blocked(value, dim) or any(value in i.vals(dim) for i in positives):
                continue  # a loved or liked item has it: no dislike chip (the contradiction check)
            entries.append(
                ProfileEntry(
                    text=value,
                    attr_kind=dim,
                    polarity="dislike",
                    dimension=dim,
                    status="converged",
                    values=[value],
                    evidence=ids[:6],
                    evidence_titles=_titles(items, ids),
                    weight=len(ids),
                    share=share,
                    exposure=seen,
                    lift=lift,
                )
            )
    if not fallback:
        entries += _quality_cues(positives, items, blocked, image_ids or set())
    ev = _evidence(rated, items, results, blocked, removed_hit, typed_hit, dismissed, fallback)
    for seller in ev.sellers:
        if not blocked(seller.text):
            entries.append(seller)
    if ev.tier is not None and not blocked(ev.tier.text):
        entries.append(ev.tier)
    entries += ev.leanings
    if ev.marketplace is not None:
        entries.append(ev.marketplace)
    seller_trace = ev.trace["sellers"]
    tier_trace = ev.trace["tiers"]
    price, price_trace = price_decision(rated, shown)
    if price is not None and not blocked(price.text):
        entries.append(price)
    order = {
        "exclude_marketplace": 0, "seller": 1, "tier": 2, "price_band": 3, "price_mixed": 4,
        "open": 5, "leaning_toward": 6, "leaning_away": 7,
    }  # fmt: skip
    entries.sort(
        key=lambda e: (e.polarity != "like", order.get(e.attr_kind, 5.5), -e.weight, e.text)
    )
    trace = {
        "mode": "fallback" if fallback else "model",
        "rated": {"love": sum(1 for r in rated.values() if r.rating == "love"),
                  "like": sum(1 for r in rated.values() if r.rating == "like"),
                  "dislike": sum(1 for r in rated.values() if r.rating == "dislike")},
        "shown": len(shown),
        "shown_tags": {"source": exposure.source, "tagged": exposure.tagged, "total": exposure.total},
        "dimensions": dim_trace,
        "seller": seller_trace,
        "tier": tier_trace,
        "price": price_trace,
        "evidence": ev.trace["evidence"],
        "marketplace": ev.trace["marketplace"],
    }  # fmt: skip
    return Decisions(entries, results, items, trace)


def _evidence(
    rated: dict[str, Rated],
    items: dict[str, TaggedItem],
    results: dict[str, DimensionResult],
    blocked: Callable[..., bool],
    removed_hit: Callable[[str], bool],
    typed_hit: Callable[..., bool],
    dismissed: Mapping[str, int] | None,
    fallback: bool,
) -> Evidence:
    """Stage 2i: the like-rate evidence of the session (sellers, tiers, leanings, the marketplace
    exclusion) and its table. Item values are the best available: the photo or title model's tags,
    and for a dimension they left empty, the title's keywords."""

    def values_of(rid: str, dim: str) -> list[str]:
        item = items.get(rid)
        if item is None:
            return []
        raw = item.values.get(dim)
        if raw is None:
            raw = title_tags(item.rated.title).get(dim)
            raw = normalize_style(raw) if dim == "style" and raw else raw
        if not raw:
            return []
        return style_leaves(raw) if dim == "style" else [raw]

    converged = {d: r.value for d, r in results.items() if r.status == "converged" and r.value}

    def skip(direction: str, dim: str, value: str) -> bool:
        text = FAMILY_LABELS.get(value, value)
        return removed_hit(lean_text(direction, dim, value)) or typed_hit(text, dim)

    sellers, seller_trace = seller_entries(rated)
    tier, tier_trace = tier_entry(rated)
    leanings: list[ProfileEntry] = []
    leaning_rows: list[dict[str, Any]] = []
    if not fallback:
        leanings, leaning_rows = leaning_entries(rated, values_of, converged, skip)
    dismissed_at = (dismissed or {}).get("marketplace")
    market, market_trace = marketplace_decision(rated, dismissed_at)
    rows = group_rows(rated) + leaning_rows
    base = seller_trace.get("baseline", 0.0)
    return Evidence(
        sellers, tier, leanings, market, rows,
        {
            "sellers": seller_trace, "tiers": tier_trace, "marketplace": market_trace,
            "evidence": {"baseline": base, "rows": rows[:80]},
        },
    )  # fmt: skip


def _join(values: list[str]) -> str:
    if len(values) <= 1:
        return "".join(values)
    return ", ".join(values[:-1]) + " and " + values[-1]


def _quality_cues(
    positives: list[TaggedItem],
    items: dict[str, TaggedItem],
    blocked: Callable[[str], bool],
    image_ids: set[str],
) -> list[ProfileEntry]:
    """Cue phrases (solid wood, brushed finish) shown by at least half of the loved and liked items
    and at least two of them, and visible: in a title, or in an image that was sent. Features such
    as storage are not build or finish and are never learned."""
    total = len(positives)
    if total < QUALITY_CUE_MIN_ITEMS:
        return []
    support: dict[str, list[str]] = {}
    for item in positives:
        for cue in item.cues:
            if any(w in FEATURE_WORDS for w in re.findall(r"[a-z]+", cue)):
                continue
            support.setdefault(cue, []).append(item.rated.id)
    out: list[ProfileEntry] = []
    for cue, ids in sorted(support.items()):
        ids = sorted(set(ids))
        if (
            len(ids) < QUALITY_CUE_MIN_ITEMS
            or len(ids) / total < QUALITY_CUE_MIN_SHARE
            or blocked(cue)
        ):
            continue
        visible = any(text_matches(cue, items[i].rated.title) for i in ids) or any(
            i in image_ids for i in ids
        )
        if not visible:
            continue
        out.append(
            ProfileEntry(
                text=cue,
                attr_kind="quality_cue",
                polarity="like",
                dimension="quality_cue",
                status="converged",
                values=[cue],
                evidence=ids[:6],
                evidence_titles=_titles(items, ids),
                weight=sum(items[i].weight for i in ids),
                share=round(len(ids) / total, 4),
            )
        )
    return out[:3]


def typed_dimensions(base: ShopQuery) -> set[str]:
    """Dimensions the typed request itself names (a typed "wood and metal" names material): the
    batch guard leaves them alone and never warns about them."""
    typed = {"material"} if base.material_terms else set()
    request = base.request
    for dim in ("shape", "color_finish", "frame_color", "color"):
        if title_value(dim, request) or (dim == "color" and base.color_terms):
            typed.add(dim)
    guesses = {t for t in base.style_terms if not mentions_words(t, request)}
    if any(t not in guesses for t in base.style_terms):
        typed.add("style")
    return typed


def mentions_words(term: str, text: str) -> bool:
    """Whether every significant word of `term` appears in `text`."""
    words = re.findall(r"[a-z]{3,}", term.lower())
    low = text.lower()
    return bool(words) and all(w in low for w in words)


def typed_terms_of(base: ShopQuery) -> list[str]:
    """Words the shopper typed (as the parser read them): learned chips never repeat these."""
    return [*base.material_terms, *base.must_have, *base.style_terms]


# ---- exposure and diversity of a result list ---------------------------------------------------------


def composition(
    results: list[ShopResult], tags: TagMap, dims: list[str]
) -> dict[str, dict[str, int]]:
    """For each dimension, how many of `results` carry each value ("(unknown)" for no tag)."""
    out: dict[str, dict[str, int]] = {}
    for dim in dims:
        counts: dict[str, int] = {}
        for r in results:
            value = (tags.get(r.id) or {}).get(dim) or "(unknown)"
            counts[value] = counts.get(value, 0) + 1
        out[dim] = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return out


def top_share(counts: dict[str, int]) -> tuple[str, float] | None:
    """The most common KNOWN value and its share of the results that have a known value."""
    known = {v: n for v, n in counts.items() if v != "(unknown)"}
    if not known:
        return None
    top = max(sorted(known), key=lambda v: known[v])
    return top, round(known[top] / sum(known.values()), 4)
