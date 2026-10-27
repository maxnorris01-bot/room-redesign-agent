"""Shop eval, phase 1: the versioned cases, the personas, the thresholds and their hash."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from evals.shop import spec
from evals.shop.coverage import category_findings
from evals.shop.spec import Case, load_cases, load_thresholds, thresholds_sha256

SHOP = Path(spec.__file__).resolve().parent


def test_twelve_cases_load_in_index_order_with_unique_ids() -> None:
    cases = load_cases()
    assert len(cases) == 12
    assert [c.id for c in cases] == [f"case-{n:02d}" for n in range(1, 13)]
    assert len({c.request for c in cases}) == 12
    index = json.loads((SHOP / "cases" / "index.json").read_text())
    assert index["version"] == 1 and len(index["cases"]) == 12
    assert all(c.version == 1 for c in cases)
    assert "Version 1" in (SHOP / "cases" / "CHANGELOG.md").read_text()


def test_the_requests_are_the_owners_list() -> None:
    wanted = [
        "modern couches under $1000",
        "coffee tables with a mix of wood and metal with storage",
        "boho curtains that pair well with a dark blue room",
        "mid-century dining chairs under $150 each",
        "small bookshelves for a studio apartment under $200",
        "8x10 neutral area rugs under $400",
        "brass floor lamps",
        "queen bed frames with storage in wood",
        "round black metal side tables",
        "ergonomic desk chairs under $300",
        "sectional sofas for a small living room",
        "navy velvet accent chairs",
    ]
    assert [c.request for c in load_cases()] == wanted


def test_refine_steps_context_cases_and_the_contrast_case() -> None:
    by = {c.id: c for c in load_cases()}
    assert by["case-02"].refine is not None and by["case-02"].refine.text == "no Walmart"
    assert by["case-02"].refine.sellers == ["Walmart"]
    assert by["case-10"].refine is not None and by["case-10"].refine.text == "focus on mesh backs"
    assert by["case-10"].refine.title_term == "mesh"
    assert sum(1 for c in by.values() if c.refine) == 2
    assert by["case-03"].kind == "context" and by["case-03"].avoid_colors
    assert "blue" in by["case-03"].context_colors
    assert by["case-12"].kind == "contrast" and by["case-12"].product_color == "navy_dark_blue"
    assert by["case-05"].context_items and by["case-11"].context_items
    prices = {c.id: c.expected_price.max for c in by.values() if c.expected_price}
    assert prices == {
        "case-01": 1000.0, "case-04": 150.0, "case-05": 200.0, "case-06": 400.0, "case-10": 300.0,
    }  # fmt: skip


def test_every_persona_is_two_to_four_sentences_and_varied() -> None:
    cases = load_cases()
    texts = [c.persona.text for c in cases]
    assert len(set(texts)) == 12
    for case in cases:
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", case.persona.text.strip()) if s]
        assert 2 <= len(sentences) <= 4, case.id
        assert re.search(r"\blikes?\b", case.persona.text) and "dislike" in case.persona.text


def test_a_persona_does_not_restate_the_request() -> None:
    stop = {"with", "and", "for", "that", "pair", "well", "under", "each", "small", "a", "the"}
    for case in load_cases():
        request_words = {w for w in re.findall(r"[a-z]+", case.request.lower()) if w not in stop}
        persona_words = set(re.findall(r"[a-z]+", case.persona.text.lower()))
        shared = request_words & persona_words
        # at most the product noun and one shared descriptive word: the taste adds new things
        assert len(shared) <= 3, (case.id, shared)
        assert case.request.lower() not in case.persona.text.lower()
        assert not re.search(r"\$\d", case.persona.text), case.id


def test_cases_reject_unknown_fields() -> None:
    raw = json.loads((SHOP / "cases" / "case-01.json").read_text())
    raw["surprise"] = 1
    with pytest.raises(ValueError):
        Case.model_validate(raw)


def test_thresholds_have_the_proposed_values() -> None:
    t = load_thresholds()
    assert t.price_compliance_min == 1.0
    assert (t.category_mean_min, t.category_case_min) == (0.90, 0.75)
    assert (t.attribute_mean_min, t.attribute_case_min) == (0.70, 0.50)
    assert (t.avoid_color_share_max, t.context_colors_in_search_text_max) == (0.15, 0)
    assert t.product_color_share_min == 0.70
    assert (t.near_duplicates_mean_max, t.near_duplicates_case_max) == (0.5, 1)
    assert (t.distinct_sellers_min, t.top_seller_share_max, t.repeats_max) == (6, 0.30, 0)
    assert (t.refine_exclusion_min, t.refine_focus_share_min) == (1.0, 0.50)
    assert (t.lift_mean_min_points, t.lift_improved_cases_min) == (15.0, 8)
    assert t.lift_case_worst_min_points == -10.0
    assert (t.search_median_s_max, t.again_median_s_max, t.again_max_s_max) == (30.0, 20.0, 45.0)
    assert t.cost_per_request_usd_max == 0.05
    assert (t.hard_stop_anthropic_usd, t.hard_stop_serper_credits) == (0.60, 500)


def test_the_thresholds_hash_is_the_sha256_of_the_file_bytes() -> None:
    path = SHOP / "thresholds.json"
    assert thresholds_sha256() == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(thresholds_sha256()) == 64


def test_every_threshold_key_is_explained() -> None:
    doc = (SHOP / "THRESHOLDS.md").read_text()
    assert "judgment values" in doc and "before any live run" in doc
    for key in json.loads((SHOP / "thresholds.json").read_text()):
        if key != "version":
            assert f"`{key}`" in doc, key


def test_category_gaps_are_findings_not_substitutions() -> None:
    findings = {f["case"]: f for f in category_findings(load_cases())}
    assert set(findings) == {"case-03", "case-04", "case-05", "case-10"}
    assert findings["case-10"]["categories_py_says"] == "desk"
    assert findings["case-10"]["kind"] == "folds into another category"
    assert findings["case-03"]["known_category"] is None
    assert findings["case-05"]["noun"] == "bookshelves"
