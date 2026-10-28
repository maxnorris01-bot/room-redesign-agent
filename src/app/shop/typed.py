"""Typed attributes as quotas (ADR 0012, stage 2p).

A request that names a product colour ("navy velvet accent chairs"), a material ("wood and metal")
or a finish makes those attributes quotas, not hints. At least `TYPED_MATCH_MIN_SHARE` of a batch
must match by the best tag the item has (a photo tag when there is one, else the title tag), where
tagged supply exists. An untagged item, or one whose tag is `other`, does not match. When the pool
cannot supply enough, the shortfall is reported in the trace and in a banner naming the attribute.
Match rules (what "navy" or "dark blue" accepts) live in `app.shop.vocab`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from app.shop.models import ShopQuery
from app.shop.thresholds import RESULT_COUNT, TYPED_MATCH_MIN_SHARE
from app.shop.vocab import COLOR_LABELS, title_value, typed_values

COLOR_DIM = "color"
MATERIAL_DIM = "material"
FINISH_DIM = "color_finish"
Tags = Mapping[str, str | None]


@dataclass(frozen=True)
class TypedAttr:
    """One typed attribute: the dimension, what the shopper typed, and the tag values that match."""

    dim: str
    typed: str
    values: frozenset[str]

    @property
    def label(self) -> str:
        names = {"color": "color", "material": "material", "color_finish": "finish"}
        return f"{names.get(self.dim, self.dim)} {self.typed}"


def typed_attributes(query: ShopQuery) -> list[TypedAttr]:
    """The quotas a typed `query` carries: its product colours, its materials the vocabulary knows
    (typed words it does not know, like "rattan", can not be enforced), and a finish named in the
    request. Colours of the room (`suggested_colors`) are never here: a typed product colour
    overrides them, and with none typed they are only a ranking hint."""
    out: list[TypedAttr] = []
    seen: set[tuple[str, str]] = set()

    def add(dim: str, typed: str, values: frozenset[str]) -> None:
        if values and (dim, typed) not in seen:
            seen.add((dim, typed))
            out.append(TypedAttr(dim, typed, values))

    for color in query.color_terms:
        typed_text = COLOR_LABELS.get(color, color)
        add(COLOR_DIM, typed_text, typed_values(COLOR_DIM, color) or frozenset({color}))
    material_values: set[str] = set()
    material_words: list[str] = []
    for term in query.material_terms:
        got = typed_values(MATERIAL_DIM, term)
        if got:
            material_values |= got
            material_words.append(term)
    if material_values:  # "wood and metal": an item made of either matches
        add(MATERIAL_DIM, " or ".join(material_words), frozenset(material_values))
    finish = title_value(FINISH_DIM, query.request)
    if finish:
        words = re.findall(r"[a-z]+", finish)
        add(FINISH_DIM, " ".join(words), typed_values(FINISH_DIM, finish))
    return out


def matches(tags: Tags | None, attr: TypedAttr) -> bool:
    """Whether the item's best tag for the dimension satisfies the typed attribute. No tag, or the
    tag `other`, is not a match."""
    if not tags:
        return False
    value = tags.get(attr.dim)
    return bool(value) and value in attr.values


def target(batch_size: int = RESULT_COUNT) -> int:
    """How many of a batch of `batch_size` must match."""
    return int(math.ceil(TYPED_MATCH_MIN_SHARE * batch_size))


def shortfalls(
    attrs: list[TypedAttr], batch: list[Tags | None], pool: list[Tags | None]
) -> list[dict[str, Any]]:
    """For each typed attribute the batch misses (fewer than `target(len(batch))` matching): how
    many matched, how many were wanted, how many the pool could have supplied and how many of the
    batch carry no tag for the dimension at all."""
    out: list[dict[str, Any]] = []
    if not batch:
        return out
    wanted = target(len(batch))
    for attr in attrs:
        got = sum(1 for t in batch if matches(t, attr))
        if got >= wanted:
            continue
        out.append(
            {
                "attribute": attr.label,
                "dimension": attr.dim,
                "wanted": wanted,
                "got": got,
                "batch": len(batch),
                "supply": sum(1 for t in pool if matches(t, attr)),
                "untagged": sum(1 for t in batch if not (t or {}).get(attr.dim)),
            }  # fmt: skip
        )
    return out


def banner(short: dict[str, Any]) -> str:
    """The sentence the page shows for a shortfall, naming the attribute."""
    text = (
        f"Only {short['got']} of {short['batch']} results match your {short['attribute']}: "
        f"the pool had {short['supply']} that do"
    )
    if short["untagged"]:
        text += (
            f" and {short['untagged']} results show no {short['dimension'].replace('_', ' ')} yet"
        )
    return text + "."


def match_filter(attrs: list[TypedAttr]) -> Callable[[Tags | None], bool]:
    """A test for "matches every typed attribute", for the selection's first pass."""
    return lambda tags: all(matches(tags, a) for a in attrs)
