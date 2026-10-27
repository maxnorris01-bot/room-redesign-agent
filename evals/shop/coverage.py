"""Category coverage findings: where `app.categories` cannot represent a case's product.

A gap is a result, not noise: it is reported (plan and report) and never worked around by changing
the request.
"""

from __future__ import annotations

from typing import Any

from app.categories import canonical_category, known_category
from evals.shop.spec import Case


def category_findings(cases: list[Case]) -> list[dict[str, Any]]:
    """One finding per case whose product noun is not folded into the expected category."""
    out: list[dict[str, Any]] = []
    for case in cases:
        folded = canonical_category(case.category_noun)
        known = known_category(case.category_noun)
        spoken = " ".join(case.category_noun.lower().split())
        if known is None:
            kind = "not in the synonym groups or size table"
        elif folded == case.expected_category:
            continue
        elif folded != spoken:
            kind = "folds into another category"
        else:
            kind = "singular and plural are different topic keys"
        out.append(
            {
                "case": case.id,
                "request": case.request,
                "noun": case.category_noun,
                "expected_category": case.expected_category,
                "categories_py_says": folded,
                "known_category": known,
                "kind": kind,
            }
        )
    return out
