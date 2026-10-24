"""The model describes; code decides (ADR 0012, stage 2c amendment).

A language model is good at reading one product ("round, walnut, brass frame") and bad at deciding what
a shopper's mixed ratings mean. Stage 2b let it propose preferences and it over-read: with a liked round
item and a liked rectangular one it picked round, and a batch that was mostly round made "likes round"
look true. So here the model only tags each rated item along fixed dimensions (shape, material, color or
finish, frame or legs, frame color, style, size class, quality cues) and this module decides, from the
tags of ALL the session's ratings every time:

  * per dimension, the weighted distribution of values among loved (3) and liked (1) items;
  * an exposure baseline: how common each value is among everything the shopper was shown, by a
    title-keyword lexicon (no model; titles with no keyword are unknown and left out);
  * converged (one value holds 70% of the like mass, at least 2 items support it, and its like share
    beats its exposure share by a lift of 1.3), open (two or more values each hold 30%: a neutral chip,
    no pressure on search or ranking), or none;
  * dislikes only when 2 disliked items share a value that no loved or liked item has, with the same
    lift test; one dislike only excludes that item;
  * seller affinity (2 loved or liked, no dislike, and the lift test), price as a band or as mixed,
    quality cues.

Every number is in `app.shop.thresholds`. The lexicon is deliberately small and plain: it is a
baseline for "what was on the page", not a classifier, and a title it cannot read is simply unknown.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.shop.models import ItemTags, ProfileEntry, ShopQuery, ShopResult
from app.shop.textutil import (
    Rated,
    mentions,
    normalize_phrase,
    phrases_match,
    text_matches,
)
from app.shop.thresholds import (
    CONVERGE_SHARE,
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
)

DIMENSIONS = (
    "shape", "material", "color_finish", "frame_or_legs", "frame_color", "style", "size_class",
)  # fmt: skip
LEXICON_DIMENSIONS = ("shape", "material", "color_finish", "frame_or_legs", "frame_color", "style")
# features, not build or finish: never a learned quality cue
FEATURE_WORDS = frozenset(
    {"storage", "drawer", "drawers", "lift", "shelf", "shelves", "adjustable", "foldable", "wheels"}
)
DIMENSION_LABELS = {
    "shape": "Shape", "material": "Material", "color_finish": "Finish", "frame_or_legs": "Legs",
    "frame_color": "Frame color", "style": "Style", "size_class": "Size",
}  # fmt: skip

# canonical value -> keywords. A keyword is a whole word or phrase ("-" and " " are interchangeable).
LEXICON: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "shape": [
        ("round", ("round", "drum", "circular", "cylinder", "cylindrical")),
        ("oval", ("oval", "elliptical", "ellipse", "racetrack")),
        ("rectangular", ("rectangular", "rectangle")),
        ("square", ("square",)),
    ],
    "material": [
        ("wood", ("wood", "wooden", "walnut", "oak", "teak", "pine", "maple", "acacia", "mango", "birch", "ash")),
        ("metal", ("metal", "steel", "iron", "aluminum", "aluminium", "brass", "bronze", "copper")),
        ("glass", ("glass",)),
        ("stone", ("marble", "stone", "travertine", "granite", "concrete", "terrazzo", "slate")),
        ("rattan", ("rattan", "wicker", "cane", "bamboo", "seagrass")),
        ("fabric", ("velvet", "linen", "boucle", "chenille", "fabric", "upholstered", "cotton", "wool")),
        ("leather", ("leather",)),
    ],
    "color_finish": [
        ("warm wood", ("walnut", "oak", "teak", "natural", "honey", "cherry", "mahogany", "cedar")),
        ("dark", ("black", "ebony", "espresso", "charcoal", "onyx", "dark")),
        ("light", ("white", "ivory", "cream", "whitewash", "beige")),
        ("gray", ("gray", "grey")),
        ("brass", ("brass", "gold", "champagne")),
        ("silver", ("chrome", "silver", "nickel", "stainless")),
    ],
    "frame_color": [
        (
            "dark or brass metal",
            ("black metal", "black steel", "black iron", "black frame", "black legs", "black base",
             "powder coated", "matte black", "bronze", "gunmetal", "brass", "brushed brass", "gold frame",
             "gold legs"),
        ),
        ("silver metal", ("chrome", "silver", "nickel", "stainless")),
        ("white", ("white frame", "white legs", "white metal", "white base")),
        ("wood", ("natural wood", "wood legs", "wooden legs", "wood frame", "wood base")),
    ],
    "frame_or_legs": [
        ("hairpin legs", ("hairpin",)),
        ("pedestal", ("pedestal",)),
        ("tapered legs", ("tapered",)),
        ("sled or tripod", ("sled", "tripod", "trestle")),
    ],
    "style": [
        ("modern", ("modern", "contemporary")),
        ("mid-century", ("mid century",)),
        ("industrial", ("industrial",)),
        ("farmhouse", ("farmhouse", "rustic")),
        ("boho", ("boho", "bohemian")),
        ("scandinavian", ("scandinavian", "scandi")),
        ("minimalist", ("minimalist",)),
        ("traditional", ("traditional",)),
        ("glam", ("glam",)),
    ],
}  # fmt: skip


def _pattern(keyword: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in re.split(r"[-\s]+", keyword.strip().lower()) if w]
    return re.compile(r"(?<![a-z0-9])" + r"[-\s]+".join(words) + r"s?(?![a-z0-9])")


_COMPILED: dict[str, list[tuple[str, list[re.Pattern[str]]]]] = {
    dim: [(value, [_pattern(k) for k in keys]) for value, keys in groups]
    for dim, groups in LEXICON.items()
}


def lexicon_match(dim: str, text: str) -> tuple[str, str] | None:
    """(canonical value, the keyword that matched) for `dim` in `text`; the earliest keyword wins."""
    lowered = text.lower().replace("powder-coated", "powder coated")
    best: tuple[int, str, str] | None = None
    for (value, keys), (_, patterns) in zip(
        LEXICON.get(dim, []), _COMPILED.get(dim, []), strict=True
    ):
        for key, pat in zip(keys, patterns, strict=True):
            m = pat.search(lowered)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), value, key)
    return (best[1], best[2]) if best else None


def lexicon_value(dim: str, text: str) -> str | None:
    """The canonical value of `dim` that `text` shows (the earliest keyword wins), else None."""
    found = lexicon_match(dim, text)
    return found[0] if found else None


PHRASE_DIMS = ("shape", "material", "color_finish", "frame_or_legs")  # dimensions search can use
PHRASE_WORDS = {
    "color_finish": {"warm wood": "walnut", "dark": "black", "light": "white", "silver": "chrome"},
    "frame_or_legs": {"sled or tripod": "tripod"},
}  # fmt: skip


def phrase_word(dim: str, value: str) -> str:
    """The words to put in a search for a canonical value ("warm wood" finish: "walnut")."""
    return PHRASE_WORDS.get(dim, {}).get(value, value)


def lexicon_tags(title: str) -> dict[str, str]:
    """Every dimension the title shows by keyword. Unknown dimensions are absent."""
    out: dict[str, str] = {}
    for dim in LEXICON_DIMENSIONS:
        value = lexicon_value(dim, title)
        if value is not None:
            out[dim] = value
    return out


def canon_value(dim: str, text: str | None) -> str | None:
    """A model's free-text tag in the lexicon's value space ("walnut" for material is "wood").

    A tag the lexicon does not know stays as normalized text, except a shape outside the fixed set
    ("other" and anything unreadable) which is dropped, and size class, which is a fixed set.
    """
    if text is None or not text.strip():
        return None
    low = text.strip().lower()
    if dim == "shape":
        value = lexicon_value("shape", low)
        return (
            value if value else (low if low in ("round", "oval", "rectangular", "square") else None)
        )
    if dim == "size_class":
        return low if low in ("small", "medium", "large") else None
    found = lexicon_value(dim, low)
    if found:
        return found
    cleaned = normalize_phrase(low)
    return cleaned[:30] if cleaned else None


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


def tag_item(rated: Rated, model: ItemTags | None) -> TaggedItem:
    """The item's values from the model's tags only (canonicalized). A dimension the model left
    null, and every dimension of an item the model did not tag, is unknown: the title lexicon is not
    used to fill it (only keyword-bearing titles would count, which biased stage 2c)."""
    values: dict[str, str] = {}
    cues: list[str] = []
    if model is not None:
        for dim in DIMENSIONS:
            got = canon_value(dim, getattr(model, dim))
            if got:
                values[dim] = got
        cues = [normalize_phrase(c) for c in model.quality_cues if normalize_phrase(c)]
    return TaggedItem(rated, values, tuple(dict.fromkeys(cues)))


def exposure_shares(shown: list[ShopResult] | list[str]) -> dict[str, dict[str, float]]:
    """For each lexicon dimension, each value's share of the shown titles that have a value at all.

    Titles with no keyword for a dimension are unknown and left out of that dimension's baseline. A
    dimension with no known title at all is absent (the lift test then cannot run for it).
    """
    counts: dict[str, dict[str, int]] = {}
    for item in shown:
        title = item if isinstance(item, str) else item.title
        for dim, value in lexicon_tags(title).items():
            counts.setdefault(dim, {}).setdefault(value, 0)
            counts[dim][value] += 1
    return {
        dim: {v: n / sum(by.values()) for v, n in by.items()} for dim, by in counts.items() if by
    }


def exposure_coverage(shown: list[ShopResult] | list[str]) -> dict[str, float]:
    """For each lexicon dimension, the share of the shown titles that have a keyword for it. A
    dimension with no lexicon (size class) is absent, i.e. coverage 0."""
    total = len(shown)
    if not total:
        return {}
    known: dict[str, int] = {}
    for item in shown:
        title = item if isinstance(item, str) else item.title
        for dim in lexicon_tags(title):
            known[dim] = known.get(dim, 0) + 1
    return {dim: n / total for dim, n in known.items()}


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
    dim: str,
    positives: list[TaggedItem],
    exposure: dict[str, dict[str, float]],
    exposure_cov: dict[str, float] | None = None,
) -> DimensionResult:
    """Converged, open or none for one dimension, from the loved and liked items' values.

    Shares are over ALL like mass: an item with no known value counts against every value, so
    silent items can no longer inflate one value. Converged needs known values on at least
    `KNOWN_COVERAGE_MIN` of the like mass, one value on `CONVERGE_SHARE` of it, 2 supporting items,
    keywords for the dimension in at least `KNOWN_COVERAGE_MIN` of what was shown (else the lift
    test cannot run) and a lift of `LIFT_MIN`. Open needs two values at `OPEN_SHARE` of all mass.
    `exposure_cov` is the share of shown titles with a keyword per dimension; when omitted, a
    dimension that has an exposure baseline counts as fully covered.
    """
    mass: dict[str, int] = {}
    ids: dict[str, list[str]] = {}
    for item in sorted(positives, key=lambda i: i.rated.id):
        value = item.values.get(dim)
        if value:
            mass[value] = mass.get(value, 0) + item.weight
            ids.setdefault(value, []).append(item.rated.id)
    total = sum(item.weight for item in positives)
    known = sum(mass.values())
    exp_cov = (
        exposure_cov.get(dim, 0.0)
        if exposure_cov is not None
        else (1.0 if exposure.get(dim) else 0.0)
    )
    trace: dict[str, Any] = {
        "like_mass": {**{v: m for v, m in sorted(mass.items(), key=lambda kv: (-kv[1], kv[0]))}, "(unknown)": total - known},
        "total_mass": total,
        "known_coverage": round(known / total, 4) if total else 0.0,
        "exposure_coverage": round(exp_cov, 4),
        "exposure": {v: round(x, 4) for v, x in sorted(exposure.get(dim, {}).items())},
    }  # fmt: skip
    if total == 0 or known == 0:
        trace.update(decision="none", reason="no loved or liked item shows this dimension")
        return DimensionResult(dim, "none", trace=trace)
    shares = {v: m / total for v, m in mass.items()}
    ordered = sorted(shares, key=lambda v: (-shares[v], v))
    top = ordered[0]
    by_id = {i.rated.id: i.rated for i in positives}
    support = len(ids[top])
    if support < MIN_SUPPORT_ITEMS and any(
        by_id[i].rating == "love" and mentions(top, by_id[i].comment) for i in ids[top]
    ):
        support = MIN_SUPPORT_ITEMS  # a loved item whose comment names it stands in for a second
    lift, seen, passes = _lift(shares[top], exposure, dim, top)
    trace.update(top=top, top_share=round(shares[top], 4), support=support, lift=lift)
    why: list[str] = []
    if known / total < KNOWN_COVERAGE_MIN:
        why.append(f"known coverage {known / total:.0%} is under {KNOWN_COVERAGE_MIN:.0%}")
    if shares[top] < CONVERGE_SHARE:
        why.append(
            f"top value {top} holds {shares[top]:.0%} of all like mass, under {CONVERGE_SHARE:.0%}"
        )
    if support < MIN_SUPPORT_ITEMS:
        why.append(f"{top} has {support} supporting item(s), needs {MIN_SUPPORT_ITEMS}")
    if exp_cov < KNOWN_COVERAGE_MIN:
        why.append(
            f"only {exp_cov:.0%} of shown titles have a keyword for it (under {KNOWN_COVERAGE_MIN:.0%}): the lift test cannot run"
        )
    elif not passes:
        why.append(f"lift {lift} for {top} is under {LIFT_MIN}")
    if not why:
        trace.update(
            decision="converged", reason=f"{top}: {shares[top]:.0%} of like mass, lift {lift}"
        )
        return DimensionResult(
            dim, "converged", top, [top], round(shares[top], 4), seen, lift, ids[top],
            {v: round(x, 4) for v, x in shares.items()}, trace,
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
            dim, "open", None, open_values, None, None, None, support_ids,
            {v: round(x, 4) for v, x in shares.items()}, trace,
        )  # fmt: skip
    trace.update(decision="none", reason="not converged: " + "; ".join(why))
    return DimensionResult(
        dim, "none", shares={v: round(x, 4) for v, x in shares.items()}, trace=trace
    )


def dislike_values(
    dim: str,
    disliked: list[TaggedItem],
    positives: list[TaggedItem],
    exposure: dict[str, dict[str, float]],
    exposure_cov: dict[str, float] | None = None,
) -> list[tuple[str, list[str], float, float | None, float | None]]:
    """Values disliked: at least `DISLIKE_MIN_ITEMS` disliked items share it, no loved or liked item
    has it, and its dislike share beats its exposure share by the lift (which needs keywords in
    `KNOWN_COVERAGE_MIN` of what was shown). Returns (value, ids, share, exposure, lift)."""
    if exposure_cov is not None and exposure_cov.get(dim, 0.0) < KNOWN_COVERAGE_MIN:
        return []
    counts: dict[str, list[str]] = {}
    for item in sorted(disliked, key=lambda i: i.rated.id):
        value = item.values.get(dim)
        if value:
            counts.setdefault(value, []).append(item.rated.id)
    total = len(disliked)  # unknown values count against every value here too
    liked_values = {i.values.get(dim) for i in positives}
    out: list[tuple[str, list[str], float, float | None, float | None]] = []
    for value, ids in sorted(counts.items()):
        if len(ids) < DISLIKE_MIN_ITEMS or value in liked_values:
            continue
        share = len(ids) / total
        lift, seen, passes = _lift(share, exposure, dim, value)
        if passes:
            out.append((value, ids, round(share, 4), seen, lift))
    return out


# ---- seller and price --------------------------------------------------------------------------------


def seller_key(name: str) -> str:
    lowered = name.lower().strip()
    lowered = re.sub(r"\.(com|net|org|co)$", "", lowered)
    return re.sub(r"[^a-z0-9]+", "", lowered) or "?"


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
    """The seller affinity chip, if any, and the numbers behind the decision for every candidate.

    A seller needs at least 2 loved or liked items, no dislike of it, and a share of the like mass
    that beats its share of what was shown by `LIFT_MIN`. The trace lists each seller with a loved or
    liked item, so a missing chip can be explained.
    """
    groups: dict[str, list[Rated]] = {}
    for r in rated.values():
        if r.seller:
            groups.setdefault(seller_key(r.seller), []).append(r)
    total_mass = sum(
        LOVE_WEIGHT if r.rating == "love" else LIKE_WEIGHT
        for r in rated.values()
        if r.rating in ("love", "like") and r.seller
    )
    exposure = seller_exposure(shown or [])
    best: tuple[int, str, list[Rated], float | None, float | None] | None = None
    candidates: list[dict[str, Any]] = []
    for key, items in sorted(groups.items()):
        positive = [r for r in items if r.rating in ("love", "like")]
        if not positive:
            continue
        weight = sum(LOVE_WEIGHT if r.rating == "love" else LIKE_WEIGHT for r in positive)
        share = weight / total_mass if total_mass else 0.0
        seen = exposure.get(key)
        lift = round(share / seen, 3) if seen else None
        dislikes = sum(1 for r in items if r.rating == "dislike")
        why = ""
        if len(positive) < MIN_AFFINITY_ITEMS:
            why = f"{len(positive)} loved or liked item(s), needs {MIN_AFFINITY_ITEMS}"
        elif dislikes:
            why = f"{dislikes} disliked item(s) from this seller"
        elif lift is not None and lift < LIFT_MIN:
            why = f"lift {lift} is under {LIFT_MIN} (like share {share:.0%} against {seen:.0%} of what was shown)"
        candidates.append(
            {
                "seller": " ".join(positive[0].seller.split())[:40], "items": len(positive), "like_mass": weight,
                "like_share": round(share, 4), "exposure": round(seen, 4) if seen else None,
                "lift": lift, "dislikes": dislikes, "verdict": "chip" if not why else "no chip: " + why,
            }
        )  # fmt: skip
        if not why and (best is None or weight > best[0]):
            best = (weight, key, positive, lift, round(seen, 4) if seen else None)
    top = max(candidates, key=lambda c: (c["like_mass"], c["seller"]), default=None)
    trace: dict[str, Any] = {
        "candidates": sorted(candidates, key=lambda c: -c["like_mass"])[:5],
        "decision": "chip" if best else "none",
        "reason": (top["verdict"] if top else "no loved or liked item has a seller")
        if not best
        else "seller affinity",
    }
    if best is None:
        return None, trace
    weight, _, positive, lift, seen = best
    name = " ".join(positive[0].seller.split())[:40]
    ids = sorted(r.id for r in positive)
    entry = ProfileEntry(
        text=f"seller: {name}", attr_kind="seller", polarity="like", evidence=ids[:6],
        evidence_titles=[" ".join(rated[i].title.split())[:100] for i in ids[:3]],
        confidence=1.0, weight=weight, share=round(weight / total_mass, 4) if total_mass else None,
        exposure=seen, lift=lift, dimension="seller",
    )  # fmt: skip
    return entry, trace


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
) -> Decisions:
    """The preference entries for the session's ratings, and the trace of how each was decided.

    Pure and order independent: the same ratings, tags and shown results always give the same
    entries. With `fallback=True` (the model's tags are unavailable) no attribute is decided at all
    (shape, material, finish, frame color, style, size, cues, attribute dislikes): titles alone would
    count only the keyword-bearing ones. Only seller affinity and price are decided then.
    """
    items = {rid: tag_item(r, None if fallback else tags.get(rid)) for rid, r in rated.items()}
    positives = [i for i in items.values() if i.rated.rating in ("love", "like")]
    disliked = [i for i in items.values() if i.rated.rating == "dislike"]
    exposure = exposure_shares(shown)
    exposure_cov = exposure_coverage(shown)
    entries: list[ProfileEntry] = []
    results: dict[str, DimensionResult] = {}
    dim_trace: dict[str, Any] = {}

    def blocked(text: str) -> bool:
        key = normalize_phrase(text)
        return bool(key) and (
            any(phrases_match(key, r) for r in removed)
            or any(phrases_match(key, normalize_phrase(t)) for t in typed_terms)
        )

    for dim in DIMENSIONS:
        if fallback:
            results[dim] = DimensionResult(dim, "none")
            dim_trace[dim] = {"decision": "none", "reason": "fallback: item details were not read"}
            continue
        res = classify_dimension(dim, positives, exposure, exposure_cov)
        results[dim] = res
        dim_trace[dim] = res.trace
        label = DIMENSION_LABELS[dim].lower()
        if res.status == "converged" and res.value and blocked(res.value):
            res.trace["reason"] += (
                " (not shown: the typed request already says it, or you deleted it)"
            )
        if res.status == "converged" and res.value and not blocked(res.value):
            entries.append(
                ProfileEntry(
                    text=res.value,
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
                    detail="liked " + _join(res.values),
                    evidence=sorted(res.support)[:6],
                    evidence_titles=_titles(items, sorted(res.support)),
                    weight=sum(items[i].weight for i in res.support),
                )
            )
        for value, ids, share, seen, lift in dislike_values(
            dim, disliked, positives, exposure, exposure_cov
        ):
            if blocked(value) or any(i.values.get(dim) == value for i in positives):
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
    seller, seller_trace = seller_decision(rated, shown)
    if seller is not None and not blocked(seller.text):
        entries.append(seller)
    price, price_trace = price_decision(rated, shown)
    if price is not None and not blocked(price.text):
        entries.append(price)
    order = {"seller": 0, "price_band": 1, "price_mixed": 2, "open": 3}
    entries.sort(key=lambda e: (e.polarity != "like", order.get(e.attr_kind, 4), -e.weight, e.text))
    trace = {
        "mode": "fallback" if fallback else "model",
        "rated": {"love": sum(1 for r in rated.values() if r.rating == "love"),
                  "like": sum(1 for r in rated.values() if r.rating == "like"),
                  "dislike": sum(1 for r in rated.values() if r.rating == "dislike")},
        "shown": len(shown),
        "dimensions": dim_trace,
        "seller": seller_trace,
        "price": price_trace,
    }  # fmt: skip
    return Decisions(entries, results, items, trace)


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
    for dim in ("shape", "color_finish", "frame_color", "frame_or_legs"):
        if lexicon_value(dim, request):
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


def value_shares(titles: list[str], dims: list[str]) -> dict[str, tuple[str, float] | None]:
    """For each dimension, the most common known value in `titles` and its share of the titles
    that have a known value. None when no title shows the dimension."""
    out: dict[str, tuple[str, float] | None] = {}
    for dim in dims:
        counts: dict[str, int] = {}
        for title in titles:
            value = lexicon_value(dim, title) if dim in LEXICON_DIMENSIONS else None
            if value:
                counts[value] = counts.get(value, 0) + 1
        if not counts:
            out[dim] = None
            continue
        top = max(sorted(counts), key=lambda v: counts[v])
        out[dim] = (top, round(counts[top] / sum(counts.values()), 4))
    return out
