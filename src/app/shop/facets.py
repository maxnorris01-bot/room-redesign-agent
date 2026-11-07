"""What each shown result is (attrs) and the filters the page can offer (facets).

`attrs` per result: `{"color": [...], "material": [...], "shape": [...], "style": [...],
"size": [...], "feature": [...]}`, lowercase keys of `clarify_vocab.json` (colours are keys of its
`colors` table plus "multicolor"); an empty list means unknown for that dimension. Everything but
colour is read from the listing title by word-boundary synonym matching (longest synonym wins, a
shorter synonym inside a longer one's words does not match again, several values allowed); the
material is never inferred from a photo. Colour comes from `app.shop.colortags` (photo tags, a
colour vision call, else these title words).

`facets` for a batch: per dimension the options with their counts over the SHOWN results, ordered
most useful first. Pure and free; it changes no ranking. See `docs/api-contract.md`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.shop.clarify_visual import norm, option_visual, vocabulary

# attrs key order (the contract's) and the default order of the facets
ATTR_DIMENSIONS = ("color", "material", "shape", "style", "size", "feature")
FACET_ORDER = ("color", "material", "style", "shape", "size", "feature")
TITLES = {
    "color": "Colour", "material": "Material", "style": "Style",
    "shape": "Shape", "size": "Size", "feature": "Feature",
}  # fmt: skip
MAX_OPTIONS = 10
MAX_COLORS = 3  # "multicolor" plus up to two dominant colours, or up to three plain ones
MULTICOLOR = "multicolor"
Attrs = dict[str, list[str]]


def empty_attrs() -> Attrs:
    return {dim: [] for dim in ATTR_DIMENSIONS}


def _spans(text: str, phrase: str) -> list[tuple[int, int]]:
    """Every whole-word occurrence of `phrase` in `text`, as (start, end)."""
    out: list[tuple[int, int]] = []
    start = text.find(phrase)
    while start != -1:
        end = start + len(phrase)
        before_ok = start == 0 or not (text[start - 1].isalnum() or text[start - 1] == "_")
        after_ok = end == len(text) or not (text[end].isalnum() or text[end] == "_")
        if before_ok and after_ok:
            out.append((start, end))
        start = text.find(phrase, start + 1)
    return out


def _match(pairs: Iterable[tuple[str, str]], text: str) -> list[str]:
    """The keys whose synonyms occur in `text` (already normalised), in order of first
    appearance. Longest synonym first, and the words it matched are used up, so "faux leather" is
    not also "leather" and "machine washable" is not also "washable"."""
    left = text
    found: list[tuple[int, str]] = []
    for syn, key in sorted(pairs, key=lambda p: -len(p[0])):
        for start, end in _spans(left, syn):
            found.append((start, key))
            left = left[:start] + " " * (end - start) + left[end:]
    out: list[str] = []
    for _pos, key in sorted(found, key=lambda f: f[0]):
        if key not in out:
            out.append(key)
    return out


def title_colors(title: str) -> list[str]:
    """Colour keys the title names ("Gray Blue Rug" -> gray, blue), grey folded into gray, and
    "multicolor" when it names a pattern word; at most `MAX_COLORS`."""
    text = norm(title)
    vocab = vocabulary()
    pairs = [(word, "gray" if word == "grey" else word) for word in vocab["colors"]]
    pairs += [(syn, key) for syn, key in vocab["icons"].get("color-extra", []) if key == MULTICOLOR]
    return _match(pairs, text)[:MAX_COLORS]


def title_attrs(title: str) -> Attrs:
    """All six dimensions from a title alone (the fallback, and old results without attrs)."""
    text = norm(title)
    icons = vocabulary()["icons"]
    out = empty_attrs()
    out["color"] = title_colors(title)
    for dim in ("material", "shape", "style", "size", "feature"):
        out[dim] = _match(icons.get(dim, []), text)
    return out


def clean_attrs(raw: Mapping[str, Any] | None) -> Attrs:
    """`raw` as the contract's attrs: every dimension present, lowercase unique strings."""
    out = empty_attrs()
    for dim in ATTR_DIMENSIONS:
        values = (raw or {}).get(dim) or []
        if isinstance(values, list):
            for v in values:
                if isinstance(v, str) and v.strip().lower() not in out[dim] and v.strip():
                    out[dim].append(v.strip().lower())
    return out


@lru_cache(maxsize=1)
def _raw_vocab() -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(
        Path(__file__).with_name("clarify_vocab.json").read_text("utf-8")
    )
    return raw


def _vocabulary_order(dim: str) -> dict[str, int]:
    """Position of each key in the vocabulary file (colours: its table, then multicolor)."""
    raw = _raw_vocab()
    if dim == "color":
        keys = ["gray" if k == "grey" else k for k in raw["colors"]] + [MULTICOLOR]
    else:
        keys = list(raw["icons"].get(dim, {}))
    return {k: i for i, k in enumerate(dict.fromkeys(keys))}


def label_of(value: str) -> str:
    """A display name from a vocabulary key: gray -> Gray, mid-century -> Mid-century."""
    return value[:1].upper() + value[1:]


def compute_facets(
    all_attrs: Sequence[Mapping[str, Any] | None],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """`(facets, facets_unknown)` over the shown results' attrs.

    count = shown results whose attrs include the value; an option needs count >= 1; a dimension
    is listed only with at least 2 options and when some option tells results apart (not every
    result carrying every option); at most `MAX_OPTIONS` options by count, then vocabulary order.
    Dimensions are ordered by how many results they cover (ties: colour, material, style, shape,
    size, feature). `facets_unknown` counts, for every dimension, the shown results with no value.
    """
    rows = [clean_attrs(a) for a in all_attrs]
    total = len(rows)
    unknown = {dim: sum(1 for r in rows if not r[dim]) for dim in ATTR_DIMENSIONS}
    built: list[tuple[int, int, dict[str, Any]]] = []
    for place, dim in enumerate(FACET_ORDER):
        counts: dict[str, int] = {}
        for r in rows:
            for v in r[dim]:
                counts[v] = counts.get(v, 0) + 1
        if len(counts) < 2 or all(n == total for n in counts.values()):
            continue
        order = _vocabulary_order(dim)
        ranked = sorted(counts, key=lambda v: (-counts[v], order.get(v, len(order)), v))
        options = []
        for v in ranked[:MAX_OPTIONS]:
            label = label_of(v)
            visual = option_visual(dim, label)
            options.append(
                {
                    "value": v, "label": label, "count": counts[v],
                    "icon": visual["icon"], "swatches": visual["swatches"],
                }
            )  # fmt: skip
        built.append(
            (
                total - unknown[dim],
                place,
                {"dimension": dim, "title": TITLES[dim], "options": options},
            )
        )
    built.sort(key=lambda b: (-b[0], b[1]))
    return [b[2] for b in built], unknown
