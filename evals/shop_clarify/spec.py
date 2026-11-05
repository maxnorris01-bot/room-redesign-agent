"""The twelve clarify cases and their hidden personas (`cases.json`).

A case is a request plus a persona: `persona` is the taste text the existing persona judge reads, and
`answers` is the scripted answer to each trait a question might be about (one line per trait, keyed
by the question's dimension). Neither reaches the pipeline or the question writer. `to_case()` turns a
case into the `evals.shop.spec.Case` the existing judges and providers take.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from evals.shop.spec import Case, Persona

CLARIFY_DIR = Path(__file__).resolve().parent
CASES_PATH = CLARIFY_DIR / "cases.json"
RESULTS_DIR = CLARIFY_DIR / "results"


class ClarifyCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    request: str
    vagueness: Literal["vague", "specific"]
    category_noun: str
    expected_category: str
    fixture: Literal["couches", "coffee_tables", "curtains", "generic"]
    persona: str
    answers: dict[str, str]

    def to_case(self, extra_must_have: list[str] | None = None) -> Case:
        """The case the existing judges read. `extra_must_have` adds the persona's answered traits to
        the attributes the attribute judge checks (the same list for both arms of a comparison)."""
        return Case(
            id=self.id, version=1, request=self.request, category_noun=self.category_noun,
            expected_category=self.expected_category, expected_price=None, kind="plain",
            context_items=[], context_colors=[], avoid_colors=[], product_color=None, styles=[],
            materials=[], must_have=list(extra_must_have or []), refine=None, fixture=self.fixture,
            persona=Persona(id=f"persona-{self.id}", text=self.persona),
        )  # fmt: skip


def load_cases(path: Path = CASES_PATH) -> list[ClarifyCase]:
    data = json.loads(path.read_text())
    return [ClarifyCase.model_validate(c) for c in data["cases"]]
