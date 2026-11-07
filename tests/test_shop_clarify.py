"""Clarifying questions spike (src/app/shop/clarify.py): the schema and validation, the picture matcher,
the answer mapping and the quality flags. No network, no key: a fake caller stands in for the model."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.llm import Budget, Completion, LLMOutputError
from app.shop.clarify import (
    Answer,
    Option,
    Question,
    Questions,
    QuestionsOut,
    analyze_facets,
    apply_answers,
    candidate_rows,
    map_answers,
    pick_option_images,
    quality_flags,
    sanitize,
    size_in_range,
    size_text,
    title_has_value,
    tool_definition,
)
from app.shop.models import ShopQuery
from app.shopping import SearchCandidate

ROOT = Path(__file__).resolve().parents[1]


def cand(
    n: int, title: str, price: float | None = 300.0, thumb: str | None = "https://img.example/x.jpg"
) -> SearchCandidate:
    return SearchCandidate(
        product_id=f"p{n}", title=title, seller="Shop", price=price, currency="USD", rating=None,
        rating_count=None, google_panel_url=None, thumbnail_url=thumb if thumb is None else f"https://img.example/{n}.jpg",
    )  # fmt: skip


CANDS = [
    cand(1, "Walnut 6-Drawer Dresser 60 in"),
    cand(2, "White 3 Drawer Dresser 40 inch"),
    cand(3, "Oak Mid-Century Dresser, 6 Drawers"),
    cand(4, "Black Metal Dresser with 4 Drawers"),
    cand(5, "Walnut Dresser, no photo", thumb=None),
    cand(6, "Gray 8-Drawer Dresser 66 in"),
]


def query(**kw: Any) -> ShopQuery:
    base: dict[str, Any] = {"request": "dresser", "search_text": "dresser", "category": "dresser"}
    return ShopQuery(**{**base, **kw})


def out(questions: list[dict[str, Any]]) -> QuestionsOut:
    return QuestionsOut.model_validate({"questions": questions})


def q_json(
    qid: str = "q1",
    dim: str = "color",
    options: list[tuple[str, str, list[str]]] | None = None,
    text: str = "Which colour?",
) -> dict[str, Any]:
    opts = (
        options if options is not None else [("Walnut", "walnut", ["1"]), ("White", "white", ["2"])]
    )
    return {
        "id": qid, "text": text, "kind": "size_range" if dim == "size" else "choice", "dimension": dim,
        "options": [{"label": a, "value": b, "evidence_product_ids": c} for a, b, c in opts],
    }  # fmt: skip


# ---- the schema and the call ------------------------------------------------------------------


def test_the_tool_schema_is_strict_and_has_no_unsupported_keywords() -> None:
    tool = tool_definition()
    assert tool["name"] == "record_clarifying_questions" and tool["strict"] is True
    text = json.dumps(tool["input_schema"])
    assert (
        "maxItems" not in text and "maxLength" not in text
    )  # CLAUDE.md: not accepted, never relied on
    props = tool["input_schema"]["properties"]["questions"]["items"]["properties"]
    assert set(props) == {"id", "text", "kind", "dimension", "options"}
    assert set(props["options"]["items"]["properties"]) == {
        "label",
        "value",
        "evidence_product_ids",
    }


def test_the_prompt_is_versioned_like_the_others_and_forbids_what_the_brief_forbids() -> None:
    from app.prompts import load_prompt

    current = load_prompt("shop_clarify")
    assert current.version == "1" and load_prompt("shop_clarify", "1").text == current.text
    low = current.text.lower()
    for rule in (
        "never ask about anything the request",
        "no questions about price",
        "no questions about brands",
        "mutually exclusive",
        "jargon",
        "nine in ten",
        "size_range",
    ):
        assert rule in low, rule


class FakeCaller:
    def __init__(
        self, payload: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.payload, self.error = payload, error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kw: Any) -> Completion:
        self.calls.append(kw)
        if self.error:
            raise self.error
        kw["budget"].add_cost(0.002)
        return Completion(text="", tool_input=self.payload, model=kw["model"])


def test_analyze_facets_makes_one_forced_tool_call_with_thinking_off_and_the_rows() -> None:
    caller = FakeCaller({"questions": [q_json()]})
    result = analyze_facets("dresser", query(), CANDS, budget=Budget(Config()), caller=caller)
    assert len(caller.calls) == 1
    call = caller.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "record_clarifying_questions"}
    assert call["disable_thinking"] is True and call["span_name"] == "llm.shop_clarify"
    text = call["user_text"]
    assert (
        "<request>dresser</request>" in text
        and "1 | Walnut 6-Drawer Dresser 60 in | $300 | Shop | " in text
        and "60 in" in text.splitlines()[3]
    )
    assert "https://" not in text and "p1" not in text  # no links, no thumbnails, no product ids
    assert result.cost_usd == pytest.approx(0.002) and result.shown == 6 and result.model
    assert [q.id for q in result.questions] == ["q1"]


def test_only_the_first_forty_candidates_are_shown() -> None:
    many = [cand(i, f"Dresser {i}") for i in range(1, 61)]
    rows, ids = candidate_rows(many)
    assert len(rows) == 40 and ids["40"] == "p40" and "41" not in ids


def test_a_failed_or_unusable_call_gives_no_questions_and_a_note_not_an_exception() -> None:
    for caller in (
        FakeCaller(error=LLMOutputError("boom")),
        FakeCaller({"questions": "nope"}),
        FakeCaller(None),
    ):
        result = analyze_facets("dresser", query(), CANDS, budget=Budget(Config()), caller=caller)
        assert result.questions == [] and result.notes
    assert analyze_facets(
        "dresser", query(), [], budget=Budget(Config()), caller=FakeCaller()
    ).notes == ["no candidates to read"]


def test_sanitize_bounds_questions_cleans_text_and_turns_evidence_numbers_into_product_ids() -> (
    None
):
    ids = {"1": "p1", "2": "p2"}
    raw = out(
        [
            q_json(
                f"Q {i}",
                "style",
                [("A", "a", []), ("B", "b", [])],
                text=f"  Which <b>look</b> {i}?\n",
            )
            for i in range(7)
        ]
    )
    questions, notes = sanitize(raw, ids)
    assert len(questions) == 5 and any("first 5 of 7" in n for n in notes)
    assert (
        "<" not in questions[0].text
        and ">" not in questions[0].text
        and not questions[0].text.startswith(" ")
    )
    assert len({q.id for q in questions}) == 5  # ids are made unique and safe
    one, _ = sanitize(out([q_json(options=[("Walnut", "Walnut", ["1", "9", "2"])])]), ids)
    assert one[0].options[0].evidence_product_ids == ["p1", "p2"]  # 9 is not a listing: dropped
    assert one[0].options[0].value == "walnut"


def test_a_size_question_is_tagged_as_one_and_option_counts_are_kept_for_the_flags() -> None:
    questions, _ = sanitize(
        out([{**q_json("s", "size", [("Small", "small", [])]), "kind": "choice"}]), {}
    )
    assert questions[0].kind == "size_range" and questions[0].dimension == "size"
    assert len(questions[0].options) == 1  # not forced into 2 to 4: flagged instead
    flags = quality_flags("dresser", query(), questions)
    assert [f.kind for f in flags] == ["option_count"]


# ---- the picture matcher ----------------------------------------------------------------------


def test_title_matching_is_explainable_hyphens_plurals_and_word_order() -> None:
    assert title_has_value("Walnut 6-Drawer Dresser", "6 drawer")
    assert title_has_value("Oak Dresser, 6 Drawers", "6 drawer")
    assert title_has_value("Mid Century Walnut Dresser", "mid-century")
    assert not title_has_value("Walnut Dresser", "oak")
    assert not title_has_value("White 3 Drawer Dresser", "6 drawer")
    assert not title_has_value("anything", "")


def test_size_ranges_match_the_longest_side_the_title_states() -> None:
    assert size_in_range("White Dresser 40 inch", "under 50 in") and not size_in_range(
        "White Dresser 40 inch", "over 50 in"
    )
    assert size_in_range("Walnut 60 in", "40 to 70 in") and not size_in_range(
        "Walnut Dresser", "under 50 in"
    )
    assert size_text("Gray 8-Drawer Dresser 66 in") and size_text("no size here") == ""


def test_pictures_come_from_titles_with_thumbnails_one_listing_per_option() -> None:
    questions = Questions(
        questions=[
            Question(id="c", text="Colour?", kind="choice", dimension="color", options=[
                Option(label="Walnut", value="walnut", evidence_product_ids=["p3"]),  # p3 is oak: the title must confirm
                Option(label="Oak", value="oak"),
                Option(label="Teal", value="teal"),
            ]),
            Question(id="s", text="Size?", kind="size_range", dimension="size", options=[
                Option(label="Under 50 in", value="under 50 in"), Option(label="Over 50 in", value="over 50 in"),
            ]),
        ]
    )  # fmt: skip
    picks = pick_option_images(questions, CANDS)
    assert (
        picks.for_option("c", 0)
        and picks.for_option("c", 0).product_id == "p1"
        and picks.for_option("c", 0).how == "scan"
    )  # type: ignore[union-attr]
    assert picks.for_option("c", 1).product_id == "p3"  # type: ignore[union-attr]
    assert picks.for_option("c", 2) is None  # nothing says teal
    assert (
        picks.for_option("s", 0).product_id == "p2" and picks.for_option("s", 1).product_id == "p1"
    )  # type: ignore[union-attr]
    assert picks.total == 5 and picks.with_image == 4 and picks.coverage == pytest.approx(0.8)
    # the listing without a thumbnail (p5) is never picked, and no listing pictures two options
    chosen = [p.product_id for p in picks.by_option.values() if p]
    assert "p5" not in chosen
    one_q = [p.product_id for (qid, _), p in picks.by_option.items() if qid == "c" and p]
    assert len(one_q) == len(set(one_q))


def test_the_models_evidence_pointer_is_used_only_when_the_title_confirms_it() -> None:
    q = Question(
        id="c",
        text="?",
        kind="choice",
        dimension="color",
        options=[Option(label="Walnut", value="walnut", evidence_product_ids=["p5", "p1"])],
    )
    picks = pick_option_images([q], CANDS)
    assert picks.for_option("c", 0).product_id == "p1"  # type: ignore[union-attr]  p5 says walnut but has no thumbnail
    q2 = Question(
        id="c",
        text="?",
        kind="choice",
        dimension="color",
        options=[Option(label="Oak", value="oak", evidence_product_ids=["p3"])],
    )
    assert pick_option_images([q2], CANDS).for_option("c", 0).how == "evidence"  # type: ignore[union-attr]
    assert (
        pick_option_images(
            [q], [cand(1, "Walnut", thumb="http://insecure.example/a.jpg")]
        ).with_image
        == 1
    )  # cand() always builds https


def test_an_http_thumbnail_is_never_picked() -> None:
    c = SearchCandidate(
        "p9",
        "Walnut Dresser",
        "Shop",
        100.0,
        "USD",
        None,
        None,
        None,
        thumbnail_url="http://insecure.example/a.jpg",
    )
    q = Question(
        id="c",
        text="?",
        kind="choice",
        dimension="color",
        options=[Option(label="Walnut", value="walnut")],
    )
    assert pick_option_images([q], [c]).with_image == 0


# ---- apply_answers ----------------------------------------------------------------------------


def answer(dim: str, label: str, value: str, qid: str = "q") -> Answer:
    q = Question(
        id=qid,
        text="?",
        kind="size_range" if dim == "size" else "choice",
        dimension=dim,
        options=[],
    )  # type: ignore[arg-type]
    return Answer(q, Option(label=label, value=value))


def test_answers_become_the_typed_attributes_the_pipeline_already_accepts() -> None:
    parsed = query(search_text="dresser", style_terms=["modern"])
    applied = map_answers(
        parsed,
        [
            answer("color", "Navy", "navy", "a"), answer("material", "Velvet", "velvet", "b"),
            answer("shape", "Round", "round", "c"), answer("style", "Boho", "boho", "d"),
            answer("size", "40 to 60 in", "40 to 60 in", "e"), answer("feature", "6 drawers", "6 drawer", "f"),
        ],
    )  # fmt: skip
    q = applied.query
    assert q.color_terms == ["navy_dark_blue"]  # a colour id: a typed quota
    assert q.material_terms == ["velvet"] and q.shape_terms == ["round"]
    assert q.style_terms == ["modern", "boho"]
    assert q.must_have == ["40 to 60 in wide", "6 drawer"]
    assert q.search_text.split()[:2] == ["navy", "velvet"] and q.search_text.endswith("dresser")
    assert len(q.search_text) <= 100
    assert [m["dimension"] for m in applied.mapped] == [
        "color",
        "material",
        "shape",
        "style",
        "size",
        "feature",
    ]
    # the request, the category and the price are untouched; the input is not modified
    assert (q.request, q.category, q.price_max) == (
        parsed.request,
        parsed.category,
        parsed.price_max,
    )
    assert parsed.color_terms == [] and parsed.style_terms == ["modern"]


def test_typed_attributes_produced_are_ones_the_pipeline_enforces() -> None:
    from app.shop.typed import typed_attributes

    q = apply_answers(
        query(),
        [
            answer("color", "Navy", "navy"),
            answer("material", "Wood", "wood"),
            answer("shape", "Oval", "oval"),
        ],
    )
    dims = {a.dim for a in typed_attributes(q)}
    assert dims == {"color", "material", "shape"}  # the existing quota mechanism reads them


def test_no_preference_and_unknown_words_change_nothing_or_degrade_to_a_must_have() -> None:
    parsed = query()
    none = Answer(Question(id="q", text="?", kind="choice", dimension="color", options=[]), None)
    assert apply_answers(parsed, [none]) == parsed
    q = apply_answers(parsed, [answer("color", "Sunset", "sunset")])
    assert q.color_terms == [] and q.must_have == ["sunset color"]
    q2 = apply_answers(parsed, [answer("shape", "Kidney", "kidney")])
    assert q2.shape_terms == [] and q2.must_have == ["kidney shape"]


def test_at_most_two_words_are_added_to_the_search_text_and_never_repeated() -> None:
    parsed = query(search_text="navy dresser")
    q = apply_answers(
        parsed,
        [
            answer("color", "Navy", "navy"),
            answer("style", "Boho", "boho"),
            answer("material", "Wood", "wood"),
        ],
    )
    assert q.search_text.split().count("navy") == 1
    assert len(q.search_text.split()) == len(parsed.search_text.split()) + 2


# ---- quality flags ----------------------------------------------------------------------------


def test_flags_catch_restating_duplicates_option_counts_and_price_or_brand_questions() -> None:
    parsed = query(request="walnut dresser", material_terms=["walnut"], style_terms=["modern"])
    qs = [
        Question(id="a", text="Which material?", kind="choice", dimension="material", options=[Option(label="Walnut", value="walnut"), Option(label="Oak", value="oak")]),
        Question(id="b", text="Which look?", kind="choice", dimension="style", options=[Option(label="Modern", value="modern"), Option(label="Boho", value="boho")]),
        Question(id="c", text="What is your budget?", kind="choice", dimension="feature", options=[Option(label="Low", value="low")]),
        Question(id="d", text="Which brand do you like?", kind="choice", dimension="feature", options=[Option(label=str(i), value=f"v{i}") for i in range(5)]),
        Question(id="e", text="How many drawers?", kind="choice", dimension="feature", options=[Option(label="6", value="6 drawer"), Option(label="3", value="3 drawer")]),
        Question(id="f", text="How many drawers do you want?", kind="choice", dimension="feature", options=[Option(label="6", value="6 drawer"), Option(label="3", value="3 drawer")]),
    ]  # fmt: skip
    kinds = {(f.question_id, f.kind) for f in quality_flags("walnut dresser", parsed, qs)}
    assert ("a", "restates_request") in kinds and ("b", "restates_request") in kinds
    assert ("c", "asks_price") in kinds and ("c", "option_count") in kinds
    assert ("d", "asks_brand") in kinds and ("d", "option_count") in kinds
    assert ("f", "duplicate_or_overlap") in kinds
    assert not any(q == "e" for q, _ in kinds)  # a fair question is not flagged


def test_a_clean_set_has_no_flags() -> None:
    qs = [
        Question(id="a", text="Which colour?", kind="choice", dimension="color", options=[Option(label="Cream", value="cream"), Option(label="Black", value="black")]),
        Question(id="b", text="Which shape?", kind="choice", dimension="shape", options=[Option(label="Round", value="round"), Option(label="Square", value="square")]),
    ]  # fmt: skip
    assert quality_flags("dresser", query(), qs) == []


# ---- isolation --------------------------------------------------------------------------------


def test_only_the_clarify_flow_and_its_callers_import_the_spike() -> None:
    """Stage Q1 wired the spike modules in behind a flag: the flow module (and the two modules
    that call it) import them; the pipeline, the parse, the store and the server do not."""
    importers = set()
    for path in (ROOT / "src").rglob("*.py"):
        if path.name in ("clarify.py", "clarify_verify.py"):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or "", *[a.name for a in node.names]]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any("clarify" in n for n in names):
                importers.add(path.name)
    assert importers <= {
        "clarify_flow.py",
        "clarify_mock.py",
        "clarify_first.py",
        "service.py",
        "again.py",
    }
    assert not importers & {"search.py", "parse.py", "store.py", "server.py", "jobs.py"}
