"""The fixed vocabularies Shop Mode tags products with (ADR 0012, stage 2e amendment).

One table, defined once, used three ways:

  * passed into the prompts (`vocab_text`), so the model answers from a closed list;
  * `normalize`, which folds whatever the model (or a shopper's comment) says into that list, so
    "metal base", "metal frame" and "metal legs" are one value and "walnut", "brown wood" and
    "rustic brown" are one finish;
  * `title_tags`, a plain title-keyword reader that is ONLY the fallback when the candidate tagging
    call fails (it leaves a title with no keyword unknown, and its coverage is poor).

Dimensions the decisions use: shape, material, finish (stored as `color_finish`), frame colour and
style. Size class and legs are no longer decided (they carried no information).
"""

from __future__ import annotations

import re

DIMENSIONS = ("shape", "material", "color_finish", "frame_color", "style")
LABELS = {
    "shape": "Shape",
    "material": "Material",
    "color_finish": "Finish",
    "frame_color": "Frame color",
    "style": "Style",
}
OTHER = "other"
STRUCTURAL = ("shape",)  # converges at the structural share; the rest at the soft share

VOCAB: dict[str, tuple[str, ...]] = {
    "shape": ("round", "oval", "rectangular", "square", OTHER),
    "material": ("wood", "metal", "glass", "stone", "upholstered", OTHER),
    "color_finish": (
        "light wood",
        "warm wood",
        "dark wood",
        "painted",
        "metallic",
        "glass",
        OTHER,
    ),  # fmt: skip
    "frame_color": ("black", "brass or gold", "silver or chrome", "natural wood", "white", OTHER),
    "style": (
        "modern",
        "mid-century",
        "industrial",
        "farmhouse",
        "traditional",
        "boho",
        "glam",
        OTHER,
    ),  # fmt: skip
}

# value -> keywords (a whole word or phrase; "-" and " " are interchangeable). Lists are in the order
# of precedence for `PRIORITY` dimensions, and the earliest match in the text wins for the rest.
SYNONYMS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "shape": [
        ("round", ("round", "drum", "circular", "cylinder", "cylindrical")),
        ("oval", ("oval", "elliptical", "ellipse", "racetrack")),
        ("rectangular", ("rectangular", "rectangle")),
        ("square", ("square",)),
    ],
    "material": [
        (
            "wood",
            (
                "wood", "wooden", "walnut", "oak", "teak", "pine", "maple", "acacia", "mango",
                "birch", "ash", "veneer", "mdf",
            ),
        ),
        ("metal", ("metal", "steel", "iron", "aluminum", "aluminium", "brass", "bronze", "copper", "chrome")),
        ("glass", ("glass",)),
        ("stone", ("marble", "stone", "travertine", "granite", "concrete", "terrazzo", "slate", "quartz")),
        (
            "upholstered",
            ("upholstered", "velvet", "linen", "boucle", "chenille", "fabric", "cotton", "wool", "leather"),
        ),
    ],
    "color_finish": [
        ("light wood", ("light wood", "light oak", "white oak", "whitewash", "whitewashed", "bleached", "blonde", "light")),
        (
            "warm wood",
            (
                "warm", "brown", "walnut", "honey", "cherry", "mahogany", "teak", "rustic", "natural",
                "oak", "acacia", "mango",
            ),
        ),
        ("dark wood", ("dark", "espresso", "ebony", "charcoal", "onyx")),
        ("metallic", ("brass", "gold", "chrome", "silver", "nickel", "stainless", "metallic", "brushed", "champagne")),
        ("glass", ("glass",)),
        ("painted", ("painted", "lacquer", "lacquered", "enamel", "matte black", "matte white", "white")),
    ],
    "frame_color": [
        ("black", ("black", "matte black", "dark", "bronze", "gunmetal", "ebony", "charcoal")),
        ("brass or gold", ("brass", "gold", "champagne")),
        ("silver or chrome", ("chrome", "silver", "nickel", "stainless")),
        ("natural wood", ("natural wood", "wood legs", "wooden legs", "wood frame", "wood base", "wood")),
        ("white", ("white",)),
    ],
    "style": [
        ("mid-century", ("mid century", "midcentury", "mcm")),
        ("modern", ("modern", "contemporary", "scandinavian", "scandi", "minimalist")),
        ("industrial", ("industrial",)),
        ("farmhouse", ("farmhouse", "rustic")),
        ("traditional", ("traditional",)),
        ("boho", ("boho", "bohemian")),
        ("glam", ("glam", "glamorous")),
    ],
}  # fmt: skip
PRIORITY = (
    "color_finish",
)  # first group with any keyword wins, not the earliest match in the text

# keywords read in a TITLE (the fallback tagger): stricter than `SYNONYMS`, because a title says
# "Black Metal" about the frame and "wood" about the material, not about the finish.
TITLE_SYNONYMS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    **SYNONYMS,
    "color_finish": [
        ("light wood", ("light wood", "light oak", "white oak", "whitewash", "whitewashed", "bleached", "blonde")),
        ("warm wood", ("walnut", "honey", "cherry", "mahogany", "teak", "natural", "rustic", "oak", "acacia", "mango")),
        ("dark wood", ("espresso", "ebony", "dark wood", "dark walnut")),
        ("metallic", ("brushed brass", "brass", "gold", "chrome", "silver", "nickel")),
        ("glass", ("glass",)),
        ("painted", ("painted", "lacquer", "lacquered", "enamel", "matte white", "matte black")),
    ],
    "frame_color": [
        (
            "black",
            ("black metal", "black steel", "black iron", "black frame", "black legs", "black base", "matte black", "black finished", "gunmetal"),
        ),
        ("brass or gold", ("brass", "gold frame", "gold legs", "gold metal", "gold base", "champagne")),
        ("silver or chrome", ("chrome", "silver", "nickel", "stainless")),
        ("natural wood", ("natural wood", "wood legs", "wooden legs", "wood frame", "wood base")),
        ("white", ("white frame", "white legs", "white metal", "white base")),
    ],
}  # fmt: skip

# the words a search carries for a value (search text for "warm wood" finish is "walnut")
PHRASE_DIMS = ("shape", "material", "color_finish")
PHRASE_WORDS = {
    "color_finish": {
        "warm wood": "walnut", "dark wood": "espresso", "light wood": "light wood",
        "metallic": "brass", "painted": "painted",
    },
    "material": {"upholstered": "upholstered"},
}  # fmt: skip


def phrase_word(dim: str, value: str) -> str:
    return PHRASE_WORDS.get(dim, {}).get(value, value)


def _pattern(keyword: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in re.split(r"[-\s]+", keyword.strip().lower()) if w]
    return re.compile(r"(?<![a-z0-9])" + r"[-\s]+".join(words) + r"s?(?![a-z0-9])")


def _compile(
    table: dict[str, list[tuple[str, tuple[str, ...]]]],
) -> dict[str, list[tuple[str, list[re.Pattern[str]]]]]:
    return {
        dim: [(value, [_pattern(k) for k in keys]) for value, keys in groups]
        for dim, groups in table.items()
    }


_SYN = _compile(SYNONYMS)
_TITLE = _compile(TITLE_SYNONYMS)


def _find(
    compiled: dict[str, list[tuple[str, list[re.Pattern[str]]]]], dim: str, text: str
) -> str | None:
    lowered = text.lower().replace("powder-coated", "powder coated")
    best: tuple[int, str] | None = None
    for value, patterns in compiled.get(dim, []):
        hit = [m.start() for p in patterns if (m := p.search(lowered))]
        if not hit:
            continue
        if dim in PRIORITY:
            return value
        if best is None or min(hit) < best[0]:
            best = (min(hit), value)
    return best[1] if best else None


def normalize(dim: str, text: str | None) -> str | None:
    """A value in the fixed vocabulary for the model's (or a comment's) words, or None when there
    are no words. A phrase the table does not know is `other`: read, but not a value to learn."""
    if text is None or not text.strip() or dim not in VOCAB:
        return None
    low = " ".join(text.lower().replace("-", " ").split())
    for value in VOCAB[dim]:
        if low == value.replace("-", " "):
            return value
    return _find(_SYN, dim, text) or OTHER


def title_value(dim: str, title: str) -> str | None:
    """What a title's keywords say about `dim`, else None (unknown)."""
    return _find(_TITLE, dim, title)


def title_tags(title: str) -> dict[str, str]:
    """Every dimension the title shows by keyword (the fallback tagger)."""
    out: dict[str, str] = {}
    for dim in DIMENSIONS:
        value = title_value(dim, title)
        if value is not None:
            out[dim] = value
    return out


def vocab_text() -> str:
    """The allowed values, one line per dimension, for the prompts."""
    return "\n".join(f"{dim}: {', '.join(values)}" for dim, values in VOCAB.items())
