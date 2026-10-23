"""Category names: normalisation, synonyms and coverage checks shared by slot policy, preferences
and the self-check.

"floor lamp", "lamp" and "torchiere" are one thing here, as are "area rug" and "rug". This is
deliberately simple keyword matching, not a taxonomy.
"""

from __future__ import annotations

import re


def norm(category: str) -> str:
    return " ".join(category.lower().split())


# Simple synonym groups: any category containing one of the keywords counts as the canonical name.
# Order matters (first match wins). "floor lamp", "lamp" and "torchiere" are all one thing here, as
# are "area rug" and "rug".
_SYNONYMS: list[tuple[str, tuple[str, ...]]] = [
    ("lamp", ("lamp", "torchiere", "torchère", "floor light", "standing light")),
    ("rug", ("rug", "carpet")),
    ("sofa", ("sofa", "couch", "loveseat", "sectional")),
    ("armchair", ("armchair", "accent chair", "lounge chair", "recliner")),
    ("coffee table", ("coffee table", "cocktail table")),
    ("side table", ("side table", "end table", "accent table", "nesting table")),
    (
        "media unit",
        (
            "media unit",
            "media console",
            "tv stand",
            "tv unit",
            "tv console",
            "entertainment center",
            "entertainment centre",
            "entertainment unit",
        ),
    ),  # fmt: skip
    ("bookshelf", ("bookshelf", "bookcase", "shelving unit", "shelf unit")),
    ("nightstand", ("nightstand", "bedside table", "night stand")),
    ("ottoman", ("ottoman", "pouf", "footstool", "hassock")),
    ("console table", ("console table", "entry table", "entryway table", "hall table")),
    ("bed frame", ("bed frame", "bedstead", "platform bed", "bed base")),
    ("dresser", ("dresser", "chest of drawers", "tallboy")),
    ("wardrobe", ("wardrobe", "armoire")),
    ("desk", ("desk", "writing table")),  # "desk lamp" is already a lamp: the lamp group is first
    ("filing cabinet", ("filing cabinet", "file cabinet")),
    ("sideboard", ("sideboard", "buffet", "credenza")),
    ("cabinet", ("cabinet",)),  # after filing cabinet and sideboard
    ("bar cart", ("bar cart", "drinks trolley", "serving cart")),
    ("bench", ("bench",)),
]


def canonical_category(category: str) -> str:
    """Normalized category with synonyms folded: "Area rug" -> "rug", "torchiere" -> "lamp"."""
    c = norm(category)
    for canonical, keywords in _SYNONYMS:
        if any(k in c for k in keywords):
            return canonical
    return c


def already_covered(category: str, taken: list[str]) -> bool:
    c = norm(category)
    canon = canonical_category(category)
    return any(c in t or t in c or canon == canonical_category(t) for t in taken)


FLAT_WORDS = {"rug", "runner", "mat", "carpet"}


def is_flat_item(category: str) -> bool:
    """Flat goods (rug, runner, mat, carpet): a footprint rule makes no sense for them."""
    return canonical_category(category) == "rug" or bool(
        FLAT_WORDS & set(re.findall(r"[a-z]+", category.lower()))
    )


# ---- plausible sizes ----------------------------------------------------------------------------
# Longest side in INCHES a product of each canonical category can plausibly have. A parsed size
# outside the range is discarded as a mis-parse (a slug number that was not a size, a unit typo) and
# noted in the trace. UNTUNED GUESSES: wide on purpose, only meant to catch absurd values.
SIZE_RANGE_IN: dict[str, tuple[float, float]] = {
    "sofa": (48.0, 130.0),
    "armchair": (24.0, 50.0),
    "coffee table": (24.0, 66.0),
    "side table": (10.0, 34.0),
    "console table": (28.0, 80.0),
    "media unit": (36.0, 96.0),
    "bookshelf": (18.0, 96.0),
    "desk": (30.0, 84.0),
    "dining table": (36.0, 110.0),
    "bed frame": (50.0, 100.0),
    "dresser": (24.0, 80.0),
    "nightstand": (12.0, 34.0),
    "rug": (24.0, 180.0),
    "lamp": (10.0, 80.0),
    "ottoman": (12.0, 60.0),
    "bench": (24.0, 72.0),
    "sideboard": (36.0, 90.0),
    "wardrobe": (24.0, 100.0),
    "filing cabinet": (12.0, 50.0),
    "cabinet": (12.0, 80.0),
    "bar cart": (16.0, 50.0),
    "office chair": (18.0, 46.0),
    "dining chairs": (14.0, 46.0),
    "pendant light": (6.0, 60.0),
}
DEFAULT_SIZE_RANGE_IN = (6.0, 200.0)


def size_range_in(category: str) -> tuple[float, float]:
    """Plausible longest-side range in inches for `category` (synonyms folded)."""
    canon = canonical_category(category)
    if canon in SIZE_RANGE_IN:
        return SIZE_RANGE_IN[canon]
    for key, rng in SIZE_RANGE_IN.items():  # "dining chairs", "office chair" and friends
        if key in canon or canon in key:
            return rng
    return DEFAULT_SIZE_RANGE_IN


def category_words(category: str) -> set[str]:
    """Words that signal the category in a URL slug: its own words and its synonym group's."""
    words = set(re.findall(r"[a-z]+", norm(category)))
    canon = canonical_category(category)
    for name, keywords in _SYNONYMS:
        if name == canon:
            for k in (name, *keywords):
                words |= set(re.findall(r"[a-z]+", k))
    return words - {"or", "and", "the", "a"}


# ---- families ------------------------------------------------------------
# Interchangeable pieces: the slot planner takes at most one slot per family (unless a replacement
# in it was explicitly requested), so "media unit" and "console table" are not both shopped for.
# Chosen to group things a buyer would see as the same purchase, not everything that is seating.
# UNTUNED. A category not listed is its own family.
FAMILIES: dict[str, tuple[str, ...]] = {
    "storage": ("media unit", "console table", "sideboard", "cabinet", "filing cabinet"),
    "bedroom storage": ("dresser", "wardrobe"),
    "occasional tables": ("coffee table", "side table"),
    "chairs": ("armchair", "office chair", "dining chairs"),
    "extra seating": ("ottoman", "bench"),
    "lighting": ("lamp", "pendant light"),
}


def family_of(category: str) -> str:
    """The family a category belongs to (a category outside every family is its own family)."""
    canon = canonical_category(category)
    for family, members in FAMILIES.items():
        if canon in members or any(canon == canonical_category(m) for m in members):
            return family
    return canon
