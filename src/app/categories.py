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
    ("media unit", ("media unit", "media console", "tv stand", "tv unit", "tv console")),
    ("bookshelf", ("bookshelf", "bookcase", "shelving unit", "shelf unit")),
    ("nightstand", ("nightstand", "bedside table", "night stand")),
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
