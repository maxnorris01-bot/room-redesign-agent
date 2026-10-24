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
    LIFT_MIN,
    LIKE_WEIGHT,
    LOVE_WEIGHT,
    MIN_AFFINITY_ITEMS,
    MIN_SHOWN_PRICES,
    MIN_SUPPORT_ITEMS,
    OPEN_SHARE,
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
    """The item's values: the model's tags (canonicalized), with the title lexicon filling any
    dimension the model left null. Without a model answer the lexicon alone is used."""
    values: dict[str, str] = dict(lexicon_tags(rated.title))
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


# ---- one dimension ----------------------------------------------------------------------------------


@dataclass
class DimensionResult:
    dimension: str
    status: str  # converged | open | none
    value: str | None = None  # converged
    values: list[str] = field(default_factory=list)  # open (converged: [value])
    share: float | None = None
    exposure: float | None = None
    lift: float | None = None
    support: list[str] = field(default_factory=list)  # ids of the supporting loved or liked items
    shares: dict[str, float] = field(default_factory=dict)  # every value's like share


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
    dim: str, positives: list[TaggedItem], exposure: dict[str, dict[str, float]]
) -> DimensionResult:
    """Converged, open or none for one dimension, from the loved and liked items' values."""
    mass: dict[str, int] = {}
    ids: dict[str, list[str]] = {}
    for item in sorted(positives, key=lambda i: i.rated.id):
        value = item.values.get(dim)
        if value:
            mass[value] = mass.get(value, 0) + item.weight
            ids.setdefault(value, []).append(item.rated.id)
    total = sum(mass.values())
    if total == 0:
        return DimensionResult(dim, "none")
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
    if shares[top] >= CONVERGE_SHARE and support >= MIN_SUPPORT_ITEMS and passes:
        return DimensionResult(
            dim, "converged", top, [top], round(shares[top], 4), seen, lift, ids[top],
            {v: round(s, 4) for v, s in shares.items()},
        )  # fmt: skip
    open_values = [v for v in ordered if shares[v] >= OPEN_SHARE]
    if len(open_values) >= 2:
        support_ids = [i for v in open_values for i in ids[v]]
        return DimensionResult(
            dim, "open", None, open_values, None, None, None, support_ids,
            {v: round(s, 4) for v, s in shares.items()},
        )  # fmt: skip
    return DimensionResult(dim, "none", shares={v: round(s, 4) for v, s in shares.items()})


def dislike_values(
    dim: str,
    disliked: list[TaggedItem],
    positives: list[TaggedItem],
    exposure: dict[str, dict[str, float]],
) -> list[tuple[str, list[str], float, float | None, float | None]]:
    """Values disliked: at least `DISLIKE_MIN_ITEMS` disliked items share it, no loved or liked item
    has it, and its dislike share beats its exposure share by the lift. Returns
    (value, ids, share, exposure, lift)."""
    counts: dict[str, list[str]] = {}
    for item in sorted(disliked, key=lambda i: i.rated.id):
        value = item.values.get(dim)
        if value:
            counts.setdefault(value, []).append(item.rated.id)
    total = sum(len(v) for v in counts.values())
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


def seller_affinity_entry(
    rated: dict[str, Rated], shown: list[ShopResult] | None = None
) -> ProfileEntry | None:
    """A seller with at least 2 loved or liked items and no dislike, whose share of the like mass
    beats its share of what was shown by the lift (stage 2b's rule plus the exposure test)."""
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
    for key, items in sorted(groups.items()):
        positive = [r for r in items if r.rating in ("love", "like")]
        if len(positive) < MIN_AFFINITY_ITEMS or any(r.rating == "dislike" for r in items):
            continue
        weight = sum(LOVE_WEIGHT if r.rating == "love" else LIKE_WEIGHT for r in positive)
        share = weight / total_mass if total_mass else 0.0
        seen = exposure.get(key)
        lift = round(share / seen, 3) if seen else None
        if lift is not None and lift < LIFT_MIN:
            continue
        if best is None or weight > best[0]:
            best = (weight, key, positive, lift, round(seen, 4) if seen else None)
    if best is None:
        return None
    weight, _, positive, lift, seen = best
    name = " ".join(positive[0].seller.split())[:40]
    ids = sorted(r.id for r in positive)
    return ProfileEntry(
        text=f"seller: {name}", attr_kind="seller", polarity="like", evidence=ids[:6],
        evidence_titles=[" ".join(rated[i].title.split())[:100] for i in ids[:3]],
        confidence=1.0, weight=weight, share=round(weight / total_mass, 4) if total_mass else None,
        exposure=seen, lift=lift, dimension="seller",
    )  # fmt: skip


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


def price_entry(rated: dict[str, Rated], shown: list[ShopResult]) -> ProfileEntry | None:
    """Price as a spread, from where the loved and liked prices sit among every price shown.

    mixed: the loved prices cluster in the upper third while the liked prices span at least two
    thirds of the shown range. A neutral chip; no price pressure on search or ranking; the loved
    prices serve only as a quality hint. Otherwise, loved and liked prices that together cluster in
    the upper (or lower) third give a band (stage 2b's rule). The typed price cap is never touched.
    """
    prices = sorted(r.price for r in shown if r.price is not None and r.price > 0)
    if len(prices) < MIN_SHOWN_PRICES:
        return None
    loved = [r for r in rated.values() if r.rating == "love" and r.price]
    liked = [r for r in rated.values() if r.rating == "like" and r.price]
    positive = loved + liked
    if len(positive) < MIN_AFFINITY_ITEMS:
        return None
    span = prices[-1] - prices[0]
    loved_pos = (
        sum(_rank_fraction(prices, r.price or 0.0) for r in loved) / len(loved) if loved else None
    )
    if (
        loved
        and liked
        and loved_pos is not None
        and loved_pos >= PRICE_UPPER_PERCENTILE
        and span > 0
        and (max(r.price or 0.0 for r in liked) - min(r.price or 0.0 for r in liked))
        >= PRICE_MIXED_LIKED_SPAN * span
    ):
        lo_l, hi_l = min(r.price or 0.0 for r in loved), max(r.price or 0.0 for r in loved)
        lo_k, hi_k = min(r.price or 0.0 for r in liked), max(r.price or 0.0 for r in liked)
        ids = sorted(r.id for r in positive)
        return ProfileEntry(
            text="price: mixed", attr_kind="price_mixed", polarity="like", status="mixed",
            detail=f"loved {_money(lo_l)} to {_money(hi_l)}, liked {_money(lo_k)} to {_money(hi_k)}",
            evidence=ids[:6], evidence_titles=[" ".join(rated[i].title.split())[:100] for i in ids[:3]],
            weight=len(positive), low=lo_l, high=hi_l, dimension="price",
        )  # fmt: skip
    mean = sum(_rank_fraction(prices, r.price or 0.0) for r in positive) / len(positive)
    if mean >= PRICE_UPPER_PERCENTILE:
        band, lo, hi = "upper", _percentile(prices, PRICE_UPPER_PERCENTILE), prices[-1]
    elif mean <= PRICE_LOWER_PERCENTILE:
        band, lo, hi = "lower", prices[0], _percentile(prices, PRICE_LOWER_PERCENTILE)
    else:
        return None
    lo, hi = _round_money(lo, hi)
    ids = sorted(r.id for r in positive)
    return ProfileEntry(
        text=f"{band} range, about {_money(lo)} to {_money(hi)}", attr_kind="price_band",
        polarity="like", evidence=ids[:6],
        evidence_titles=[" ".join(rated[i].title.split())[:100] for i in ids[:3]],
        confidence=round(mean, 2), weight=len(positive), low=lo, high=hi, band=band, dimension="price",
    )  # fmt: skip


# ---- the decision -------------------------------------------------------------------------------------


@dataclass
class Decisions:
    entries: list[ProfileEntry]
    dimensions: dict[str, DimensionResult]
    items: dict[str, TaggedItem]


def _titles(items: dict[str, TaggedItem], ids: list[str]) -> list[str]:
    return [" ".join(items[i].rated.title.split())[:100] for i in ids[:3] if i in items]


def decide(
    rated: dict[str, Rated],
    tags: dict[str, ItemTags],
    shown: list[ShopResult],
    removed: list[str],
    typed_terms: list[str],
    image_ids: set[str] | None = None,
) -> Decisions:
    """The preference entries for the session's ratings. Pure and order independent: the same
    ratings, tags and shown results always give the same entries."""
    items = {rid: tag_item(r, tags.get(rid)) for rid, r in rated.items()}
    positives = [i for i in items.values() if i.rated.rating in ("love", "like")]
    disliked = [i for i in items.values() if i.rated.rating == "dislike"]
    exposure = exposure_shares(shown)
    entries: list[ProfileEntry] = []
    results: dict[str, DimensionResult] = {}

    def blocked(text: str) -> bool:
        key = normalize_phrase(text)
        return bool(key) and (
            any(phrases_match(key, r) for r in removed)
            or any(phrases_match(key, normalize_phrase(t)) for t in typed_terms)
        )

    for dim in DIMENSIONS:
        res = classify_dimension(dim, positives, exposure)
        results[dim] = res
        label = DIMENSION_LABELS[dim].lower()
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
        for value, ids, share, seen, lift in dislike_values(dim, disliked, positives, exposure):
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
    entries += _quality_cues(positives, items, blocked, image_ids or set())
    seller = seller_affinity_entry(rated, shown)
    if seller is not None and not blocked(seller.text):
        entries.append(seller)
    price = price_entry(rated, shown)
    if price is not None and not blocked(price.text):
        entries.append(price)
    order = {"seller": 0, "price_band": 1, "price_mixed": 2, "open": 3}
    entries.sort(key=lambda e: (e.polarity != "like", order.get(e.attr_kind, 4), -e.weight, e.text))
    return Decisions(entries, results, items)


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
