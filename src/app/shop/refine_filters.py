"""Refine filters (shop-refine B1): typed preferences for the NEXT Shop Again.

The page sends `filters: [{"dimension": "color", "values": ["gray", "blue"]}, ...]` (keys of the
shared vocabulary, the same as `attrs` and `facets`). They are kept as refine chips of the topic
(kind `filter`, id `filter:<dimension>`), replaced as a set by each new payload, and they do three
things, only for a Shop Again that has them:

  * one targeted fetch: the typed search text plus at most `MAX_ADDED_WORDS` filter words;
  * tiers: candidates are ordered by how well their attrs satisfy the filters (OR within a
    dimension, AND across), so the batch fills from the best tier first and still fills;
  * `filters_match`: how many of the shown results satisfy every filter.

Nothing here runs for a first search or for a Shop Again without filters.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Literal, TypeVar

from app.shop.colortags import photo_tag_color
from app.shop.facets import ATTR_DIMENSIONS, TITLES, _vocabulary_order, title_attrs
from app.shop.models import MAX_SEARCH_TEXT_CHARS, RefineChip
from app.shop.refine_price import Price, price_words

_T = TypeVar("_T")
Filters = dict[str, list[str]]  # dimension -> accepted values, in priority order

MAX_ADDED_WORDS = 4  # filter words added to the typed search text for the targeted fetch
MAX_FILTER_VALUES = 6  # values accepted per dimension
MAX_FETCHES = 2  # targeted fetches per Shop Again: one, and a second only if it found nothing new
PHOTO_CAP = 24  # candidates whose photo is read for colour in a filtered Shop Again
PRIORITY = ("color", "style", "material", "shape", "size", "feature")  # colour > style > ...
CHIP_KIND: Literal["filter"] = "filter"
CHIP_PREFIX = "filter:"


class FilterError(ValueError):
    """The filters payload is not valid (the message is safe to show)."""


def _norm(dimension: str, value: str) -> str:
    key = value.strip().lower()
    return "gray" if dimension == "color" and key == "grey" else key


def parse_filters(raw: Any) -> Filters:
    """Validate the payload: a list of {"dimension", "values"} naming keys of the vocabulary.
    An empty list (or dimensions with no values) means no filters. Order is kept as priority
    order (colour, style, material, then the rest), values in the order given, duplicates once."""
    if not isinstance(raw, list):
        raise FilterError("filters must be a list")
    out: Filters = {}
    for item in raw:
        if not isinstance(item, dict):
            raise FilterError("each filter needs a dimension and values")
        dimension = item.get("dimension")
        values = item.get("values")
        if not isinstance(dimension, str) or dimension not in ATTR_DIMENSIONS:
            raise FilterError(f"dimension must be one of {', '.join(ATTR_DIMENSIONS)}")
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise FilterError("values must be a list of text")
        known = _vocabulary_order(dimension)
        kept = out.setdefault(dimension, [])
        for v in values:
            key = _norm(dimension, v)
            if key not in known:
                raise FilterError(f"unknown {dimension} value")
            if key not in kept:
                kept.append(key)
        if len(kept) > MAX_FILTER_VALUES:
            raise FilterError(f"at most {MAX_FILTER_VALUES} values per filter")
    return {d: out[d] for d in PRIORITY if out.get(d)}


def label_of(dimension: str, values: Sequence[str]) -> str:
    return f"{TITLES[dimension]}: {' or '.join(values)}"


def chip_of(dimension: str, values: Sequence[str]) -> RefineChip:
    return RefineChip(
        id=f"{CHIP_PREFIX}{dimension}", kind=CHIP_KIND, target=f"{dimension}:{','.join(values)}",
        strength="focus", label=label_of(dimension, values),
    )  # fmt: skip


def with_filters(chips: Iterable[RefineChip], filters: Filters) -> list[RefineChip]:
    """`chips` with every filter chip replaced by the ones for `filters` (other chips untouched)."""
    return [c for c in chips if c.kind != CHIP_KIND] + [chip_of(d, v) for d, v in filters.items()]


def filters_of(chips: Iterable[RefineChip]) -> Filters:
    """The filters the filter chips hold, in priority order."""
    found: Filters = {}
    for c in chips:
        if c.kind != CHIP_KIND:
            continue
        dim, _, joined = c.target.partition(":")
        values = [v for v in joined.split(",") if v]
        if dim in ATTR_DIMENSIONS and values:
            found[dim] = values
    return {d: found[d] for d in PRIORITY if d in found}


def targeted_text(base_text: str, filters: Filters, price: Price | None = None) -> str:
    """The typed search text plus at most `MAX_ADDED_WORDS` added words: the typed price's words
    first ("under $200", only with a typed price), then the filters (colour first, then style,
    material, the rest). A word already in the text is not added."""
    have = set(base_text.lower().split())
    added: list[str] = []
    for word in price_words(price) if price is not None else []:
        if word.lower() not in have and word not in added and len(added) < MAX_ADDED_WORDS:
            added.append(word)
    for values in filters.values():
        for value in values:
            for word in value.replace("_", " ").split():
                if word.lower() not in have and word not in added and len(added) < MAX_ADDED_WORDS:
                    added.append(word)
    return " ".join([base_text, *added]).strip()[:MAX_SEARCH_TEXT_CHARS].strip()


def candidate_attrs(title: str, photo_tag: str | None = None) -> dict[str, list[str]]:
    """The same attrs the first search publishes: the colour a photo tag gave first, the title's
    words for the rest."""
    attrs = title_attrs(title)
    got = photo_tag_color(photo_tag)
    if got:
        attrs["color"] = got
    return attrs


def _state(attrs: Mapping[str, Sequence[str]], dimension: str, wanted: Sequence[str]) -> int:
    """2 satisfied, 1 unknown (no value read), 0 known and not one of the wanted values."""
    have = attrs.get(dimension) or []
    if not have:
        return 1
    return 2 if set(have) & set(wanted) else 0


def satisfies_all(attrs: Mapping[str, Sequence[str]], filters: Filters) -> bool:
    return bool(filters) and all(_state(attrs, d, v) == 2 for d, v in filters.items())


def tier_key(attrs: Mapping[str, Sequence[str]], filters: Filters) -> tuple[Any, ...]:
    """Sort key, lowest first: every dimension satisfied; then (when colour is a filter) a
    colour that fits before an unread one before one that is known not to fit, so a known
    wrong colour never outranks a right one; then the most dimensions satisfied; then colour >
    style > material > the rest; then unread before known-wrong."""
    states = {d: _state(attrs, d, v) for d, v in filters.items()}
    yes = [d for d in PRIORITY if states.get(d) == 2]
    colour = 2 - states["color"] if "color" in states else 0
    return (
        0 if len(yes) == len(filters) else 1,
        colour,
        -len(yes),
        tuple(0 if d in yes else 1 for d in PRIORITY if d in states),
        tuple(0 if states.get(d) != 0 else 1 for d in PRIORITY if d in states),
    )


def tier_sorted(
    items: Sequence[_T], attrs_of: Callable[[_T], Mapping[str, Sequence[str]]], filters: Filters
) -> list[_T]:
    """`items` ordered by tier; items in the same tier keep their order (a stable sort)."""
    if not filters:
        return list(items)
    return sorted(items, key=lambda x: tier_key(attrs_of(x), filters))


def match_stats(
    shown: Sequence[Mapping[str, Sequence[str]] | None], filters: Filters
) -> dict[str, Any]:
    """`filters_match` for the page: how many shown results satisfy every filter."""
    rows = [a or {} for a in shown]
    return {
        "satisfied_all": sum(1 for a in rows if satisfies_all(a, filters)),
        "shown": len(rows),
        "filters": list(filters),
    }
