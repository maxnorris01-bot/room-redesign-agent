"""Category coverage findings: where `app.categories` cannot represent a case's product.

A gap is a result, not noise: it is reported (plan and report) and never worked around by changing
the request.
"""

from __future__ import annotations

from typing import Any

from app.categories import canonical_category, known_category, singular
from evals.shop.spec import Case


def category_findings(cases: list[Case]) -> list[dict[str, Any]]:
    """One finding per case whose product noun `categories.py` does not represent as the expected
    category: not known at all, or a different key once singular and plural are folded.

    Stage 2p folded plurals and added curtains, dining chairs and desk chairs, so the twelve eval
    cases now produce no findings; the first live run's four findings (curtains, bookshelves, dining
    chairs, desk chairs) are kept in `docs/evals/shop-eval-baseline-2026-10-26.md`. The function
    stays: a case added later with an unknown noun is reported here.
    """
    out: list[dict[str, Any]] = []
    for case in cases:
        folded = canonical_category(case.category_noun)
        known = known_category(case.category_noun)
        expected = canonical_category(case.expected_category)
        spoken = " ".join(case.category_noun.lower().split())
        if known is None:
            kind = "not in the synonym groups or size table"
        elif folded == expected:
            continue
        elif folded != spoken and folded != singular_text(spoken):
            kind = "folds into another category"
        else:
            kind = "not the expected category key"
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


def singular_text(text: str) -> str:
    return " ".join(singular(w) for w in text.split())
