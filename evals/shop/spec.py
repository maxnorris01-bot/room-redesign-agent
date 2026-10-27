"""Cases, personas and thresholds: loaded from the versioned files beside this module.

Personas are hidden taste descriptions. They are read here and by the judges only; nothing in the
pipeline's path receives one (`tests/test_eval_shop_runner.py` checks that).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

SHOP_DIR = Path(__file__).resolve().parent
CASES_DIR = SHOP_DIR / "cases"
THRESHOLDS_PATH = SHOP_DIR / "thresholds.json"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PriceRange(_Strict):
    min: float | None = None
    max: float | None = None


class Persona(_Strict):
    id: str
    text: str


class RefineStep(_Strict):
    text: str
    effect: Literal["exclude_seller", "focus_term"]
    sellers: list[str] = []  # exclude_seller: sellers that must not appear after the step
    title_term: str = ""  # focus_term: the word a focused result's title carries


class Case(_Strict):
    id: str
    version: int
    request: str
    category_noun: str
    expected_category: str
    expected_price: PriceRange | None
    kind: Literal["plain", "context", "contrast"]
    context_items: list[str]
    context_colors: list[str]
    avoid_colors: list[str]
    product_color: str | None
    styles: list[str]
    materials: list[str]
    must_have: list[str]
    refine: RefineStep | None
    fixture: Literal["couches", "coffee_tables", "curtains", "generic"]
    persona: Persona


class Thresholds(_Strict):
    version: int
    price_compliance_min: float
    category_mean_min: float
    category_case_min: float
    attribute_mean_min: float
    attribute_case_min: float
    avoid_color_share_max: float
    context_colors_in_search_text_max: int
    product_color_share_min: float
    near_duplicates_mean_max: float
    near_duplicates_case_max: int
    distinct_sellers_min: int
    top_seller_share_max: float
    repeats_max: int
    refine_exclusion_min: float
    refine_focus_share_min: float
    lift_mean_min_points: float
    lift_improved_cases_min: int
    lift_case_worst_min_points: float
    search_median_s_max: float
    again_median_s_max: float
    again_max_s_max: float
    cost_per_request_usd_max: float
    hard_stop_anthropic_usd: float
    hard_stop_serper_credits: int


def load_index(cases_dir: Path = CASES_DIR) -> dict[str, Any]:
    data = json.loads((cases_dir / "index.json").read_text())
    assert isinstance(data, dict)
    return data


def load_cases(cases_dir: Path = CASES_DIR) -> list[Case]:
    index = load_index(cases_dir)
    return [Case.model_validate_json((cases_dir / name).read_text()) for name in index["cases"]]


def load_thresholds(path: Path = THRESHOLDS_PATH) -> Thresholds:
    return Thresholds.model_validate_json(path.read_text())


def thresholds_sha256(path: Path = THRESHOLDS_PATH) -> str:
    """The SHA-256 of the thresholds file as committed (bytes, not the parsed values)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()
