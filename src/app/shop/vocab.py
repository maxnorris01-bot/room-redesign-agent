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

DIMENSIONS = ("shape", "material", "color_finish", "frame_color", "style", "color")
LABELS = {
    "shape": "Shape",
    "material": "Material",
    "color_finish": "Finish",
    "frame_color": "Frame color",
    "style": "Style",
    "color": "Color",
}
OTHER = "other"
STRUCTURAL = ("shape",)  # converges at the structural share; the rest at the soft share

VOCAB: dict[str, tuple[str, ...]] = {
    "shape": ("round", "oval", "rectangular", "square", OTHER),
    "material": ("wood", "metal", "glass", "stone", "upholstered", "mesh", OTHER),
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
    # stage 2k: the dominant colour of the whole product (the colour you would name first). A fixed,
    # small, unambiguous list: neutrals first, then hues. "navy_dark_blue" is separate from "blue"
    # because a navy room and a light blue product are different matches.
    "color": (
        "white",
        "cream",
        "beige",
        "tan",
        "brown",
        "black",
        "gray",
        "blue",
        "navy_dark_blue",
        "green",
        "red",
        "rust_terracotta",
        "yellow_mustard",
        "pink",
        "purple",
        "orange",
        "metallic",
        "multicolor_pattern",
        OTHER,
    ),  # fmt: skip
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

COLOR_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("white", ("white", "snow")),
    ("cream", ("cream", "ivory", "off white", "ecru", "oatmeal", "natural linen")),
    ("beige", ("beige", "sand", "taupe", "khaki", "stone color")),
    ("tan", ("tan", "camel", "caramel")),
    ("brown", ("brown", "walnut", "chocolate", "espresso", "mocha", "chestnut", "cognac")),
    ("black", ("black", "onyx", "ebony", "jet")),
    ("gray", ("gray", "grey", "charcoal", "slate", "graphite")),
    ("blue", ("blue", "sky", "denim", "cobalt", "teal")),
    ("navy_dark_blue", ("navy", "dark blue", "midnight blue", "midnight", "indigo")),
    ("green", ("green", "sage", "olive", "emerald", "forest")),
    ("red", ("red", "burgundy", "crimson", "maroon", "wine")),
    ("rust_terracotta", ("rust", "terracotta", "terra cotta", "burnt orange", "brick")),
    ("yellow_mustard", ("mustard", "yellow", "ochre", "saffron")),
    ("pink", ("pink", "blush", "rose", "mauve")),
    ("purple", ("purple", "plum", "lavender", "violet", "lilac")),
    ("orange", ("orange", "tangerine", "peach")),
    ("metallic", ("metallic", "gold", "silver", "brass", "chrome", "bronze", "copper", "nickel")),
    (
        "multicolor_pattern",
        ("multicolor", "multi color", "multicolored", "striped", "stripe", "floral", "plaid",
         "patterned", "geometric", "tie dye"),
    ),
]  # fmt: skip
# short names for chips and the page ("navy", "rust"); the stored value is the id above
COLOR_LABELS = {
    "navy_dark_blue": "navy", "rust_terracotta": "rust", "yellow_mustard": "mustard",
    "multicolor_pattern": "multicolor",
}  # fmt: skip

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
        # stage 2p: "mesh" (a mesh back or seat) is a material value, not a new dimension: the
        # existing tag rows stay the same width, and a typed or refined "mesh" is enforced like
        # any other material. It comes first so "mesh back with a metal frame" reads as mesh.
        ("mesh", ("mesh", "netting", "breathable fabric back")),
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
    "color": COLOR_KEYWORDS,
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
    "color": COLOR_KEYWORDS,
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


# stage 2r: the shape words a request can type ("round black metal side tables"); only these, not
# the tagging synonyms (a "drum" lamp is not a typed shape), and each accepts its own value only
TYPED_SHAPE_WORDS = {
    "round": "round", "circular": "round", "oval": "oval", "rectangular": "rectangular",
    "rectangle": "rectangular", "square": "square",
}  # fmt: skip


def typed_shape_words(text: str) -> list[str]:
    """The shape values a request types, in the order written, each once ("round or oval tables"
    gives round and oval)."""
    out: list[str] = []
    for word in re.findall(r"[a-z]+", text.lower()):
        value = TYPED_SHAPE_WORDS.get(word)
        if value and value not in out:
            out.append(value)
    return out


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


MODERN_LEANING = "modern_leaning"
TRADITIONAL_LEANING = "traditional_leaning"
STYLE_FAMILY = {
    "modern": MODERN_LEANING, "mid-century": MODERN_LEANING, "industrial": MODERN_LEANING,
    "glam": MODERN_LEANING, "scandinavian": MODERN_LEANING,
    "traditional": TRADITIONAL_LEANING, "farmhouse": TRADITIONAL_LEANING,
    "boho": TRADITIONAL_LEANING, "rustic": TRADITIONAL_LEANING,
}  # fmt: skip
FAMILY_LABELS = {
    MODERN_LEANING: "modern-leaning", TRADITIONAL_LEANING: "traditional-leaning", **COLOR_LABELS,
}  # fmt: skip
MAX_STYLE_LABELS = 2


def normalize_style(raw: str | list[str] | None) -> str | None:
    """A multi-label style (up to two vocabulary values) as one comma-joined string, or None. The
    model may give a list or one string such as "modern+glam"; each part is folded through the
    vocabulary and `other` is dropped when a real value is present."""
    if raw is None:
        return None
    parts = raw if isinstance(raw, list) else re.split(r"\s*(?:[+,/;&]|\band\b)\s*", raw)
    leaves: list[str] = []
    for part in parts:
        value = normalize("style", part)
        if value is not None and value not in leaves:
            leaves.append(value)
    real = [v for v in leaves if v != OTHER]
    leaves = real or leaves
    return ",".join(leaves[:MAX_STYLE_LABELS]) or None


def style_leaves(value: str | None) -> list[str]:
    return [p for p in (value or "").split(",") if p]


def style_families(value: str | None) -> list[str]:
    """The families (each once, in order) of a stored style value; leaves with no family are
    dropped (`other`)."""
    out: list[str] = []
    for leaf in style_leaves(value):
        family = STYLE_FAMILY.get(leaf)
        if family and family not in out:
            out.append(family)
    return out


def tag_values(dim: str, raw: str | None) -> list[str]:
    """The decision values of a stored tag: the families of a style, the value itself otherwise."""
    if not raw:
        return []
    return style_families(raw) if dim == "style" else [raw]


# What a typed word accepts as a match, by dimension (stage 2p): the typed value itself, and nothing
# looser by default. "navy" and "dark blue" are `navy_dark_blue`; a light "blue" product matches a
# typed "blue" only (navy is its own colour here). A metallic finish (brass, gold, chrome...) is the
# `metallic` value of both the finish and the colour dimension.
TYPED_ACCEPTS: dict[str, dict[str, tuple[str, ...]]] = {
    "color": {"metallic": ("metallic",), "navy_dark_blue": ("navy_dark_blue",), "blue": ("blue",)},
    "color_finish": {"metallic": ("metallic",)},
    "material": {"upholstered": ("upholstered",)},
}


def typed_values(dim: str, text: str | None) -> frozenset[str]:
    """The tag values that satisfy a typed `text` for `dim` ("navy" -> {navy_dark_blue}, "dark
    blue" -> {navy_dark_blue}, "blue" -> {blue}, "velvet" -> {upholstered}); empty when the word is
    not in the vocabulary (nothing can be enforced for it)."""
    value = normalize(dim, text)
    if value is None or value == OTHER:
        return frozenset()
    return frozenset(TYPED_ACCEPTS.get(dim, {}).get(value, (value,)))


def vocab_text() -> str:
    """The allowed values, one line per dimension, for the prompts."""
    return "\n".join(f"{dim}: {', '.join(values)}" for dim, values in VOCAB.items())
