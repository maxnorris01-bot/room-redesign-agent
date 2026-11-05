"""Round 2 of the clarify spike: the strict title matcher, the vision verification flow (with a fake
vision client), the drop rules, the independent audit. No network, no key."""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

import pytest

from app.config import Config
from app.images import PreparedImage
from app.llm import Budget, Completion, LLMOutputError
from app.shop import clarify
from app.shop.clarify import (
    HARDWARE_WORDS,
    SECONDARY_LEADS,
    Option,
    Question,
    Questions,
    match_rank,
    option_variants,
    pick_option_images,
    title_has_value,
)
from app.shop.clarify_verify import (
    Audit,
    audit_pictures,
    named_agrees,
    verify_and_prune,
)
from app.shopping import SearchCandidate
from app.thumbnails import ThumbResult


def opt(label: str, value: str | None = None) -> Option:
    return Option(label=label, value=value or label.lower())


def cand(pid: str, title: str, thumb: bool = True) -> SearchCandidate:
    return SearchCandidate(
        product_id=pid, title=title, seller="Shop", price=300.0, currency="USD", rating=None,
        rating_count=None, google_panel_url=None,
        thumbnail_url=f"https://img.example/{pid}.jpg" if thumb else None,
    )  # fmt: skip


# ---- the strict matcher -----------------------------------------------------------------------


def test_the_word_lists_are_one_constant_each_and_hold_the_brief_s_words() -> None:
    for w in (
        "handle",
        "handles",
        "pull",
        "pulls",
        "knob",
        "knobs",
        "hardware",
        "leg",
        "legs",
        "base",
        "trim",
        "accent",
        "accents",
        "feet",
        "hinge",
        "hinges",
    ):
        assert w in HARDWARE_WORDS, w
    for w in ("with", "and", "w/"):
        assert w in SECONDARY_LEADS, w


# Max's two cases (the real titles of the first live run; the sheet showed a light oak dresser for "Black or
# gray" and a wood nightstand with a black metal pull for "Metal or steel").
BLACK_OR_GRAY = opt("Black or gray", "black")
METAL_OR_STEEL = opt("Metal or steel", "metal")


@pytest.mark.parametrize(
    "title",
    [
        "Nathan James Light Oak 6 Drawer Dresser with Black Metal Pulls",
        "Oak Wood Dresser with Black Handles",
        "Light Wood Dresser, Black Hardware, 6 Drawers",
        "Walnut Dresser Black Metal Legs",
        "Natural Oak Dresser w/ Black Knobs",
        "Oak Dresser Black Top 4 Drawer",
        "Oak Dresser and Gray Accents",
    ],
)
def test_a_value_word_on_the_hardware_legs_or_a_top_is_not_the_main_color(title: str) -> None:
    assert match_rank(title, BLACK_OR_GRAY, "color", "dresser") is None, title


@pytest.mark.parametrize(
    "title",
    [
        "Modern Black Dresser 6 Drawer",
        "Nest Dresser in Black, 2x2",
        "Charcoal Gray 5-Drawer Chest Dresser",
        "Mid Century Gray Dresser with Wood Legs",
        "Dresser for Bedroom Black Finish 8 Drawers",
    ],
)
def test_a_value_word_that_describes_the_product_is_accepted(title: str) -> None:
    assert match_rank(title, BLACK_OR_GRAY, "color", "dresser") is not None, title


@pytest.mark.parametrize(
    ("title", "ok"),
    [
        ("Nathan James Modern 1-Drawer Nightstand with Metal Handle Light Brown", False),
        ("Wood Nightstand with Metal Legs", False),
        ("Walnut Nightstand Gold Metal Base", False),
        ("Oak Nightstand and Steel Accents", False),
        ("Nightstand Wood Frame with Metal Pull", False),
        ("Black Metal Nightstand with Drawer", True),
        ("Steel Frame Nightstand 2 Drawer", True),
        ("Industrial Metal Nightstand, Open Shelf", True),
    ],
)
def test_the_metal_nightstand_case(title: str, ok: bool) -> None:
    assert (match_rank(title, METAL_OR_STEEL, "material", "nightstand") is not None) is ok, title


def test_a_top_is_secondary_only_for_a_colour_question() -> None:
    assert (
        match_rank("Marble Top Nightstand Gold Base", opt("Marble"), "material", "nightstand")
        is not None
    )
    assert match_rank("Dresser Black Top Oak Base", opt("Black"), "color", "dresser") is None


def test_leads_and_hardware_are_looked_for_only_near_the_value() -> None:
    far = "Walnut Dresser with Soft Close Drawers and Easy Assembly Kit Included in Black for the Bedroom"
    assert (
        match_rank(far, opt("Black"), "color", "dresser") is not None
    )  # the lead is more than four words back
    assert match_rank("Dresser with Black Finish", opt("Black"), "color", "dresser") is None
    assert (
        match_rank("Black Dresser with Metal Pulls", opt("Black"), "color", "dresser") is not None
    )


def test_preferred_matches_are_the_first_six_words_or_just_before_the_noun() -> None:
    early = match_rank("Modern Black Dresser 6 Drawer Chest", opt("Black"), "color", "dresser")
    late = match_rank(
        "Bedroom Storage Chest of Drawers Organizer for Closet, Hallway or Nursery Black",
        opt("Black"),
        "color",
        "dresser",
    )
    before_noun = match_rank(
        "Bedroom Storage Furniture Organizer Chest Large Black Dresser",
        opt("Black"),
        "color",
        "dresser",
    )
    assert early is not None and early[0] == 0
    assert before_noun is not None and before_noun[0] == 0
    assert late is not None and late[0] == 1 and early < late


def test_variants_come_from_the_value_and_the_parts_of_the_label() -> None:
    assert option_variants(BLACK_OR_GRAY) == ["black", "gray"]
    assert option_variants(opt("Natural or light wood", "natural")) == ["natural", "light wood"]
    assert option_variants(opt("6 drawers", "6 drawer")) == ["6 drawer", "6 drawers"]
    assert (
        match_rank("Gray Dresser", BLACK_OR_GRAY, "color", "dresser") is not None
    )  # a part of the label
    assert title_has_value("Walnut 6-Drawer Dresser", "6 drawer", "feature", "dresser")
    assert not title_has_value("Walnut Dresser", "oak")


def test_a_multi_word_value_needs_its_words_together() -> None:
    assert match_rank("Light Oak Wood Dresser", opt("Light wood"), "color", "dresser") is not None
    assert (
        match_rank(
            "Light Dresser, Solid Oak, Wood Frame Bed", opt("Light wood"), "color", "dresser"
        )
        is None
    )


# ---- picking candidates -----------------------------------------------------------------------


def q_of(dimension: str, options: list[Option], kind: str = "choice", qid: str = "q") -> Question:
    return Question(id=qid, text="?", kind=kind, dimension=dimension, options=options)  # type: ignore[arg-type]


def test_candidates_are_ranked_strict_matches_with_a_picture_the_cited_ones_first() -> None:
    cands = [
        cand("p1", "Oak Dresser with Black Pulls"),  # secondary: not a match
        cand(
            "p2", "Bedroom Chest of Drawers Organizer Storage for Closet Hallway Black"
        ),  # acceptable, late
        cand("p3", "Modern Black Dresser"),  # preferred
        cand("p4", "Black Dresser, no photo", thumb=False),  # a match but no picture
        cand("p5", "Black 6 Drawer Dresser"),  # preferred, cited
    ]
    o = Option(label="Black or gray", value="black", evidence_product_ids=["p5", "p1"])
    picks = pick_option_images([q_of("color", [o])], cands, category="dresser")
    assert [p.product_id for p in picks.candidates[("q", 0)]] == ["p5", "p3", "p2"]
    assert (
        picks.candidates[("q", 0)][0].how == "evidence"
    )  # p1 is cited but fails the strict match: never used
    assert (
        picks.title_evidence[("q", 0)] == 4
    )  # p2, p3, p4, p5 (p4 has no picture but is title evidence)
    assert picks.with_image == 1 and picks.coverage == 1.0


def test_an_option_no_title_supports_has_no_evidence_and_the_metal_pull_is_not_metal() -> None:
    cands = [
        cand("p1", "Nathan James Modern 1-Drawer Nightstand with Metal Handle Light Brown"),
        cand("p2", "Wood Nightstand"),
    ]
    picks = pick_option_images(
        [q_of("material", [METAL_OR_STEEL, opt("Wood")])], cands, category="nightstand"
    )
    assert picks.for_option("q", 0) is None and picks.title_evidence[("q", 0)] == 0
    assert picks.for_option("q", 1) is not None


def test_two_options_of_a_question_do_not_share_their_first_picture() -> None:
    cands = [cand("p1", "Black Gray Dresser"), cand("p2", "Gray Dresser")]
    picks = pick_option_images(
        [q_of("color", [opt("Black"), opt("Gray")])], cands, category="dresser"
    )
    assert (
        picks.for_option("q", 0).product_id == "p1" and picks.for_option("q", 1).product_id == "p2"
    )  # type: ignore[union-attr]


# ---- the fake vision client -------------------------------------------------------------------


class Thumbs:
    """A thumbnailer whose image carries the listing's id, so a fake vision client can 'see' it."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.fail = fail or set()

    def fetch(self, url: str) -> ThumbResult:
        pid = url.rsplit("/", 1)[-1].removesuffix(".jpg")
        if pid in self.fail:
            return ThumbResult(False, "boom", "img.example")
        data = base64.b64encode(pid.encode()).decode()
        return ThumbResult(
            True, "ok", "img.example", PreparedImage(data, "image/jpeg", 8, 8, 8, 8, 1), 1
        )


def pid_of(image: PreparedImage) -> str:
    return base64.b64decode(image.data_b64).decode()


class FakeVision:
    """Answers verify calls from a script {(option value, listing id): verdict} (default: yes) and audit
    calls from {listing id: (colour, material, shape)}; records every call."""

    def __init__(
        self,
        script: dict[tuple[str, str], str] | None = None,
        audit: dict[str, tuple[str, str, str]] | None = None,
        fail_calls: int = 0,
    ) -> None:
        self.script, self.audit_script, self.fail_calls = script or {}, audit or {}, fail_calls
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kw: Any) -> Completion:
        self.calls.append(kw)
        if self.fail_calls > 0:
            self.fail_calls -= 1
            raise LLMOutputError("vision down")
        kw["budget"].add_cost(0.001)
        ids = [pid_of(i) for i in kw["images"]]
        if kw["span_name"].endswith("verify"):
            values = [line.split("(")[-1].rstrip(").") for line in kw["user_text"].splitlines()]
            items = [
                {"index": n, "answer": self.script.get((v, p), "yes")}
                for n, (v, p) in enumerate(zip(values, ids, strict=True), start=1)
            ]
        else:
            rows = [self.audit_script.get(p, ("unclear", "unclear", "unclear")) for p in ids]
            items = [
                {"index": n, "main_color": c, "main_material": m, "shape": s}
                for n, (c, m, s) in enumerate(rows, start=1)
            ]
        return Completion(text="", tool_input={"items": items}, model=kw["model"])


def run(
    questions: list[Question],
    cands: list[SearchCandidate],
    vision: Callable[..., Completion],
    mode: str = "batched",
    category: str = "dresser",
    thumbs: Thumbs | None = None,
) -> Any:
    return verify_and_prune(
        Questions(questions=questions), cands, category=category, thumbnailer=thumbs or Thumbs(),
        budget=Budget(Config()), caller=vision, mode=mode,
    )  # fmt: skip


COLOR_Q = q_of("color", [opt("Black"), opt("White")], qid="c")
CANDS = [
    cand("b1", "Black Dresser 6 Drawer"), cand("b2", "Modern Black Dresser"), cand("b3", "Modern Style Black Dresser Chest"),
    cand("w1", "White Dresser 4 Drawer"), cand("w2", "White Wood Dresser"),
]  # fmt: skip


def test_a_picture_answered_yes_is_kept_and_is_the_one_call_in_batched_mode() -> None:
    vision = FakeVision()
    v = run([COLOR_Q], CANDS, vision)
    assert [o.status for o in v.options] == ["picture", "picture"]
    assert v.picks.with_image == 2 and v.picks.total == 2 and v.picks.coverage == 1.0
    assert (
        v.picks.for_option("c", 0).product_id == "b1"
        and v.picks.for_option("c", 1).product_id == "w1"
    )  # type: ignore[union-attr]
    assert len(vision.calls) == 1 and v.calls == 1 and v.rounds == 1  # both options in one call
    call = vision.calls[0]
    assert (
        call["tool_choice"]["name"] == "record_picture_verdicts"
        and call["disable_thinking"] is True
    )
    assert (
        len(call["images"]) == 2
        and "Image 1. Question: ? Option: Black (black)." in call["user_text"]
    )
    assert v.cost_usd == pytest.approx(0.001) and v.seconds >= 0 and len(v.images) == 2


def test_the_prompt_for_each_picture_is_the_one_the_brief_gives() -> None:
    from app.prompts import load_prompt

    verify, audit = load_prompt("shop_clarify_verify"), load_prompt("shop_clarify_audit")
    assert verify.version == audit.version == "1"
    assert (
        load_prompt("shop_clarify_verify", "1").text == verify.text
        and load_prompt("shop_clarify_audit", "1").text == audit.text
    )
    assert "MAIN BODY" in verify.text and "yes" in verify.text and "unclear" in verify.text
    assert "told what to expect" in audit.text and "main_color" in audit.text


def test_a_no_tries_the_next_candidate_then_a_third_then_text_only_for_unclear() -> None:
    script = {("black", "b1"): "no", ("black", "b2"): "unclear", ("black", "b3"): "unclear"}
    vision = FakeVision(script)
    v = run([COLOR_Q], CANDS, vision)
    black = v.options[0]
    assert black.status == "text_only" and [t.verdict for t in black.tried] == [
        "no",
        "unclear",
        "unclear",
    ]
    assert [t.product_id for t in black.tried] == ["b1", "b2", "b3"]
    assert (
        v.rounds == 3
        and v.picks.for_option("c", 0) is None
        and v.picks.for_option("c", 1) is not None
    )
    assert v.options[1].status == "picture"


def test_a_second_candidate_that_passes_becomes_the_picture() -> None:
    v = run([COLOR_Q], CANDS, FakeVision({("black", "b1"): "no"}))
    assert v.options[0].status == "picture" and v.picks.for_option("c", 0).product_id == "b2"  # type: ignore[union-attr]
    assert [t.verdict for t in v.options[0].tried] == ["no", "yes"]


def test_an_option_whose_every_picture_is_a_clear_no_is_dropped_and_a_question_left_with_one_option_goes() -> (
    None
):
    all_no = {("black", p): "no" for p in ("b1", "b2", "b3")}
    v = run([COLOR_Q], CANDS, FakeVision(all_no))
    assert [o.status for o in v.options] == ["dropped", "picture"]
    assert v.options[0].reason == "every picture tried showed something else"
    assert (
        v.questions.questions == []
        and v.dropped_questions[0]["id"] == "c"
        and v.dropped_questions[0]["reason"] == "fewer than two options left"
    )
    assert v.picks.total == 0


def test_no_title_evidence_drops_the_option_and_a_three_option_question_keeps_two() -> None:
    q = q_of("color", [opt("Black"), opt("White"), opt("Teal")], qid="c")
    v = run([q], CANDS, FakeVision())
    assert [(o.label, o.status) for o in v.options] == [
        ("Black", "picture"),
        ("White", "picture"),
        ("Teal", "dropped"),
    ]
    assert v.options[2].reason.startswith("no listing's title shows it")
    kept = v.questions.questions[0]
    assert [o.label for o in kept.options] == ["Black", "White"]
    assert v.picks.total == 2 and v.picks.for_option("c", 1).product_id == "w1"  # type: ignore[union-attr]  re-keyed after the drop


def test_a_title_match_without_any_picture_stays_as_a_text_option_with_nothing_to_verify() -> None:
    cands = [cand("b1", "Black Dresser", thumb=False), cand("w1", "White Dresser")]
    vision = FakeVision()
    v = run([q_of("color", [opt("Black"), opt("White")], qid="c")], cands, vision)
    assert [o.status for o in v.options] == ["text_only", "picture"]
    assert (
        v.options[0].reason == "no picture to check"
        and len(vision.calls) == 1
        and len(vision.calls[0]["images"]) == 1
    )


def test_a_failed_call_or_fetch_is_unclear_never_a_no_so_nothing_is_dropped_for_it() -> None:
    v = run([COLOR_Q], CANDS, FakeVision(fail_calls=3))
    assert [o.status for o in v.options] == ["text_only", "text_only"]
    assert all(t.verdict == "unclear" for o in v.options for t in o.tried)
    v2 = run([COLOR_Q], CANDS, FakeVision(), thumbs=Thumbs(fail={"b1", "b2", "b3"}))
    assert (
        v2.options[0].status == "text_only"
        and [t.verdict for t in v2.options[0].tried] == ["no_image"] * 3
    )
    assert v2.images_failed >= 3


def test_single_mode_makes_one_call_per_picture() -> None:
    vision = FakeVision()
    v = run([COLOR_Q], CANDS, vision, mode="single")
    assert (
        len(vision.calls) == 2
        and all(len(c["images"]) == 1 for c in vision.calls)
        and v.mode == "single"
    )
    with pytest.raises(ValueError):
        run([COLOR_Q], CANDS, vision, mode="both")


def test_batched_mode_sends_at_most_twelve_pictures_a_call() -> None:
    options = [opt(f"Color{i}", f"color{i}") for i in range(4)]
    qs = [q_of("color", options, qid=f"q{n}") for n in range(4)]  # 16 options
    cands = [cand(f"p{i}", f"Color{i} Dresser") for i in range(4)]
    vision = FakeVision()
    run(qs, cands, vision)
    sizes = sorted(len(c["images"]) for c in vision.calls)
    assert max(sizes) <= 12 and sum(sizes) == 16 and len(sizes) == 2


def test_verification_never_changes_questions_it_was_not_given() -> None:
    original = Questions(questions=[COLOR_Q])
    run([COLOR_Q], CANDS, FakeVision({("black", "b1"): "no"}))
    assert [o.label for o in original.questions[0].options] == ["Black", "White"]


# ---- the independent audit --------------------------------------------------------------------


def test_the_audit_names_colour_material_and_shape_without_being_told_the_option() -> None:
    q = q_of("color", [opt("Black or gray", "black"), opt("White")], qid="c")
    cands = [cand("b1", "Modern Black Dresser"), cand("w1", "White Dresser")]
    v = run([q], cands, FakeVision())
    vision = FakeVision(
        audit={
            "b1": ("dark grey", "wood", "rectangular"),
            "w1": ("light oak", "wood", "rectangular"),
        }
    )
    audit = audit_pictures(v.questions, v.picks, v.images, budget=Budget(Config()), caller=vision)
    assert (
        len(vision.calls) == 1 and vision.calls[0]["tool_choice"]["name"] == "record_picture_audit"
    )
    text = vision.calls[0]["user_text"]
    assert (
        "black" not in text.lower()
        and "option" not in text.lower()
        and "question" not in text.lower()
    )
    by = {r.option_index: r for r in audit.rows}
    assert by[0].agree is True and by[1].agree is False and by[1].named == "light oak"
    assert (
        audit.auditable == 2
        and audit.agreed == 1
        and audit.accuracy == pytest.approx(0.5)
        and audit.cost_usd > 0
    )


def test_style_size_and_feature_options_are_not_auditable_and_a_missing_reading_is_not_counted() -> (
    None
):
    qs = [
        q_of("style", [opt("Modern"), opt("Boho")], qid="s"),
        q_of("color", [opt("Black"), opt("White")], qid="c"),
    ]
    cands = [
        cand("m1", "Modern Dresser"),
        cand("o1", "Boho Dresser"),
        cand("b1", "Black Dresser"),
        cand("w1", "White Dresser"),
    ]
    v = run(qs, cands, FakeVision())
    audit = audit_pictures(
        v.questions,
        v.picks,
        v.images,
        budget=Budget(Config()),
        caller=FakeVision(audit={"b1": ("black", "wood", "round")}),
    )
    assert [r.agree for r in audit.rows if r.dimension == "style"] == [None, None]
    assert (
        audit.auditable == 2
    )  # w1 was read as unclear: it counts as a disagreement, not as missing
    assert audit.agreed == 1
    broken = audit_pictures(
        v.questions, v.picks, v.images, budget=Budget(Config()), caller=FakeVision(fail_calls=1)
    )
    assert broken.auditable == 0 and broken.accuracy is None
    assert isinstance(Audit().rows, list)


@pytest.mark.parametrize(
    ("dimension", "option", "named", "agrees"),
    [
        ("color", opt("Black or gray", "black"), "grey", True),
        ("color", opt("Black or gray", "black"), "charcoal black", True),
        ("color", opt("Black or gray", "black"), "light oak", False),
        ("color", opt("White"), "unclear", False),
        ("material", opt("Wood"), "wood", True),
        ("material", opt("Metal or steel", "metal"), "steel", True),
        ("material", opt("Metal or steel", "metal"), "wood", False),
        ("shape", opt("Round"), "round", True),
        ("shape", opt("Round"), "oval", False),
    ],
)
def test_named_values_are_compared_with_the_option_in_code(
    dimension: str, option: Option, named: str, agrees: bool
) -> None:
    assert named_agrees(dimension, option, named) is agrees


def test_the_spike_modules_are_not_part_of_the_pipeline() -> None:
    assert "Not imported by the API" in (clarify.__doc__ or "") or "Nothing in the API" in (
        clarify.__doc__ or ""
    )
