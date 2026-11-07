"""What an answer option looks like without a photo: an icon key and colour swatches (stage B2).

`option_visual(dimension, label)` is pure and free. The vocabulary (`clarify_vocab.json`, shared
with the portfolio site, which draws one SVG per icon key) maps plain words to icon keys and colour
words to hex values. An option with an icon or swatches is never given a picture search or a vision
check; only an option with neither may get a real thumbnail (`needs_picture`).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

MAX_SWATCHES = 3
# the icon group each question dimension reads from (colour questions only get the extra patterns)
GROUPS = {
    "material": "material",
    "feature": "feature",
    "shape": "shape",
    "size": "size",
    "style": "style",
    "color": "color-extra",
}
VISUAL_DIMENSIONS = frozenset({"color", "material", "shape", "style"})  # a picture can show these
_SPLIT = re.compile(r"\s+or\s+|\s+and\s+|[/,]")


def norm(text: str) -> str:
    """Lowercase, hyphens and punctuation to spaces, whitespace collapsed."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower().replace("-", " ")).split())


@lru_cache(maxsize=1)
def vocabulary() -> dict[str, Any]:
    raw = json.loads((Path(__file__).with_name("clarify_vocab.json")).read_text("utf-8"))
    icons: dict[str, list[tuple[str, str]]] = {}
    for group, table in raw["icons"].items():
        pairs = [(norm(syn), key) for key, syns in table.items() for syn in syns]
        icons[group] = sorted(pairs, key=lambda p: -len(p[0]))  # longest synonym first
    return {"icons": icons, "colors": {norm(k): v for k, v in raw["colors"].items()}}


def parts(label: str) -> list[str]:
    """A compound label split on " or ", " and ", "/" and ","."""
    return [p for p in (norm(x) for x in _SPLIT.split(label.lower())) if p]


def _has(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def icon_in(group: str, text: str) -> str | None:
    """The icon key of the longest synonym of `group` that `text` (normalised) contains."""
    pairs: list[tuple[str, str]] = vocabulary()["icons"].get(group, [])
    for syn, key in pairs:
        if _has(text, syn):
            return key
    return None


def colour_swatches(label_parts: list[str]) -> list[str]:
    colors: dict[str, str] = vocabulary()["colors"]
    out: list[str] = []
    for part in label_parts:
        for word in part.split():
            hex_ = colors.get(word)
            if hex_ is not None and hex_ not in out:
                out.append(hex_)
    return out[:MAX_SWATCHES]


def option_visual(dimension: str, label: str) -> dict[str, Any]:
    """`{icon, swatches}` for an option; both None when the vocabulary knows nothing of it.

    Dimension aware: a colour question gets swatches (and the pattern icons: multicolor, striped,
    floral); every other dimension reads only its own icon group, so a colour word in a material
    question and a material word in a colour question give nothing."""
    split = parts(label)
    swatches: list[str] | None = None
    if dimension == "color":
        found = colour_swatches(split)
        swatches = found or None
    group = GROUPS.get(dimension)
    icon = None
    if group is not None:
        for part in split:
            icon = icon_in(group, part)
            if icon is not None:
                break
    return {"icon": icon, "swatches": swatches}


def has_visual(dimension: str, label: str) -> bool:
    v = option_visual(dimension, label)
    return v["icon"] is not None or v["swatches"] is not None


def needs_picture(dimension: str, label: str) -> bool:
    """Whether a real thumbnail may be looked for: a visual dimension and nothing drawn for it."""
    return dimension in VISUAL_DIMENSIONS and not has_visual(dimension, label)


def is_feature_label(label: str) -> bool:
    """Whether every part of the label is a feature word ("Washable"): not a material, shape ..."""
    split = parts(label)
    return bool(split) and all(icon_in("feature", p) is not None for p in split)
