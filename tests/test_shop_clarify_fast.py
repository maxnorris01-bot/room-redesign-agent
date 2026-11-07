"""Stage B2: question 1 fast, the rest while the shopper answers, pictures off the critical path.

Fakes only: the canned mock model, the fixture providers, short waits. No network, no key."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.llm import Budget, Completion, LLMOutputError
from app.shop import clarify_flow
from app.shop.clarify import Option, Question
from app.shop.clarify_first import (
    TEMPLATES,
    FirstOut,
    FirstResult,
    first_question,
    sanitize_first,
    stated_dimensions,
    template_question,
)
from app.shop.clarify_flow import (
    ClarifyRun,
    finish_rest,
    run_question_stage,
)
from app.shop.clarify_mock import mock_caller
from app.shop.clarify_visual import needs_picture, option_visual
from app.shop.models import ShopQuery
from app.shop.providers import ShopFixtureLookup, build_shop_providers
from app.shop.service import ShopService
from app.shop.store import SessionStore
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)
from test_shop_clarify_q1 import GRACE, cfg, make_service, questions, search, wait_questions


def q(qid: str, dim: str, *labels: str) -> Question:
    return Question(
        id=qid, text="?", kind="choice", dimension=dim,  # type: ignore[arg-type]
        options=[Option(label=lb, value=lb.lower()) for lb in labels],
    )  # fmt: skip


def parsed(text: str = "dresser", **kw: Any) -> ShopQuery:
    return ShopQuery(request=text, search_text=text, category=text.split()[-1], **kw)


# ---- question 1: templates ---------------------------------------------------------------------


def test_a_template_gives_question_one_with_drawn_options() -> None:
    first = template_question("nice rug")
    assert first is not None and first.id == "q1" and first.dimension == "material"
    assert all(option_visual("material", o.label)["icon"] for o in first.options)


def test_a_template_is_skipped_when_the_request_states_its_dimension() -> None:
    second = template_question("wool rug")
    assert second is not None and second.dimension == "shape"
    assert template_question("round wool rug") is None  # both stated: the model path decides
    assert template_question("blue sofa") is not None  # colour is not a sofa template dimension


def test_no_template_falls_through_to_the_model_path() -> None:
    assert template_question("something for the hallway") is None
    seen: list[str] = []

    def caller(**kw: Any) -> Completion:
        seen.append(kw["user_text"])
        payload = {"dimension": "style", "text": "Which look?", "options": ["Modern", "Boho"]}
        return Completion(text="", model="m", tool_input=payload, cost_usd=0.0)

    got = first_question("something for the hallway", Budget(Config()), caller=caller)
    assert got.source == "model" and got.question is not None and got.question.id == "q1"
    assert seen and "something for the hallway" in seen[0]
    tmpl = first_question("nice rug", Budget(Config()), caller=caller)
    assert tmpl.source == "template" and len(seen) == 1  # no model call for a template


def test_a_failing_model_path_gives_no_question_and_no_error() -> None:
    def boom(**_kw: Any) -> Completion:
        raise LLMOutputError("nope")

    got = first_question("something for the hallway", Budget(Config()), caller=boom)
    assert got.question is None and got.source == "none"


def test_every_template_option_is_drawn_and_every_dimension_is_known() -> None:
    for spec in TEMPLATES.values():
        assert len(spec) == 2
        for dim, labels in spec:
            assert 2 <= len(labels) <= 4
            for label in labels:
                assert not needs_picture(dim, label), (dim, label)
                v = option_visual(dim, label)
                assert v["icon"] or v["swatches"], (dim, label)


def test_the_model_question_is_cut_to_four_cleaned_and_free_of_features() -> None:
    out = FirstOut(
        dimension="material", text="What <b>material</b>?",
        options=["Wool", "Cotton", "Washable", "Jute", "Wool", "Silk", "Linen"],
    )  # fmt: skip
    got = sanitize_first(out)
    assert got is not None and [o.label for o in got.options] == ["Wool", "Cotton", "Jute", "Silk"]
    assert "<" not in got.text
    assert sanitize_first(FirstOut(dimension="style", text="x", options=["One"])) is None


def test_stated_dimensions_read_the_raw_text() -> None:
    assert stated_dimensions("walnut dresser") == {"color"}
    assert {"material", "style"} <= stated_dimensions("modern wool rug")
    assert "size" in stated_dimensions("8x10 rug")


# ---- option_visual -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dimension", "label", "icon", "swatches"),
    [
        ("material", "Wool", "wool", None),
        ("color", "Black or gray", None, ["#1A1A1A", "#8C8C8C"]),
        ("feature", "Washable", "washable", None),
        ("material", "Jute or natural fibre", "jute", None),
        ("material", "Zorbo", None, None),
        ("color", "Zorbo", None, None),
        ("material", "Black leather", "leather", None),  # a colour word in a material question
        ("color", "Wool", None, None),  # a material word in a colour question
        ("shape", "Indoor/outdoor", None, None),
        ("style", "Mid-century", "mid-century", None),
        ("color", "Black, white and gold", None, ["#1A1A1A", "#FFFFFF", "#C9A227"]),
        ("color", "Multicolor", "multicolor", None),
        ("material", "Faux leather", "faux-leather", None),  # longest synonym wins over leather
    ],
)
def test_option_visual(dimension: str, label: str, icon: str | None, swatches: list[str] | None):
    assert option_visual(dimension, label) == {"icon": icon, "swatches": swatches}


def test_swatches_are_capped_at_three() -> None:
    v = option_visual("color", "Black, white, gray or red")
    assert v["swatches"] is not None and len(v["swatches"]) == 3


# ---- the rest of the questions -----------------------------------------------------------------


def test_the_rest_drops_asked_and_stated_dimensions_features_and_repeats() -> None:
    qs = [
        q("a", "material", "Wood", "Metal"),
        q("b", "color", "Black", "White"),
        q("c", "style", "Washable", "Modern", "Boho"),  # a feature inside a style question
        q("d", "style", "Rustic", "Farmhouse"),  # the same dimension again
        q("e", "shape", "Washable", "Round"),  # one option left: the question goes
        q("f", "feature", "Washable", "Reversible"),
    ]
    kept = finish_rest(qs, skip={"material", "color"}, room=4, first_number=2)
    assert [(k.id, k.dimension) for k in kept] == [("q2", "style"), ("q3", "feature")]
    assert [o.label for o in kept[0].options] == ["Modern", "Boho"]
    assert len(finish_rest(qs, skip=set(), room=2, first_number=2)) == 2


def test_the_listing_call_is_told_what_was_asked_and_the_run_appends_and_flips_more_coming() -> (
    None
):
    from app.shop.providers import build_shop_providers

    config = cfg()
    search_provider, _ = build_shop_providers(config)
    found = search_provider.search("dresser", page=1)
    run = ClarifyRun(config)
    first = q("q1", "material", "Wood", "Metal", "Rattan")
    run.set_ready([first], more_coming=True, source="template")
    texts: list[str] = []

    def caller(**kw: Any) -> Completion:
        texts.append(kw["user_text"])
        return mock_caller(**kw)

    budget = Budget(config)
    assert run.snapshot()["more_coming"] is True
    run_question_stage(
        run, "walnut dresser", parsed("dresser", color_terms=["walnut"]), found, budget,
        config=config, thumbnailer=None, question_caller=caller, verify_caller=mock_caller,
    )  # fmt: skip
    assert "Already asked (do not ask again): material" in texts[0]
    snap = run.snapshot()
    assert snap["more_coming"] is False and snap["timing"]["rest_ms"] is not None
    dims = [qq["dimension"] for qq in snap["questions"]]
    assert dims[0] == "material" and "color" not in dims[1:] and "material" not in dims[1:]
    assert [qq["id"] for qq in snap["questions"]] == [f"q{i}" for i in range(1, len(dims) + 1)]
    assert len(dims) <= 5


def test_a_failing_listing_call_keeps_question_one() -> None:
    config = cfg()
    found = build_shop_providers(config)[0].search("dresser", page=1)
    run = ClarifyRun(config)
    run.set_ready([q("q1", "material", "Wood", "Metal")], more_coming=True, source="template")

    def boom(**_kw: Any) -> Completion:
        raise RuntimeError("down")

    run_question_stage(
        run, "dresser", parsed(), found, Budget(config), config=config, thumbnailer=None,
        question_caller=boom, verify_caller=mock_caller,
    )  # fmt: skip
    snap = run.snapshot()
    assert snap["state"] == "ready" and len(snap["questions"]) == 1 and not snap["more_coming"]


# ---- the gate and the wait clock ---------------------------------------------------------------


def later(seconds: float, fn: Any, *args: Any, **kw: Any) -> threading.Thread:
    def go() -> None:
        time.sleep(seconds)
        fn(*args, **kw)

    t = threading.Thread(target=go)
    t.start()
    return t


def test_the_clock_starts_when_question_one_is_shown_and_a_ready_pipeline_waits() -> None:
    run = ClarifyRun(cfg())
    run.set_ready(questions())
    time.sleep(GRACE * 0.6)  # question 1 has been up a while when the pipeline gets ready
    gate = run.wait_at_gate(ShopQuery(request="d", search_text="d", category="dresser"))
    assert gate.reason == "idle_grace"
    assert gate.waited < GRACE * 0.6  # only what is left of the grace since question 1
    ready_first = ClarifyRun(cfg())
    t = later(0.05, ready_first.set_ready, questions())
    g2 = ready_first.wait_at_gate(ShopQuery(request="d", search_text="d", category="dresser"))
    t.join()
    assert g2.reason == "idle_grace" and g2.waited >= 0.05 + GRACE - 0.04


def test_touched_switches_to_the_long_wait_without_an_answer() -> None:
    run = ClarifyRun(cfg())
    run.set_ready(questions())
    run.answer([], False, True)
    assert run.snapshot()["answers"] == []
    t0 = time.monotonic()
    gate = run.wait_at_gate(ShopQuery(request="d", search_text="d", category="dresser"))
    assert gate.reason == "max_wait" and time.monotonic() - t0 > GRACE * 2
    with pytest.raises(clarify_flow.ClarifyInvalid):
        ClarifyRun(cfg()).answer([], False, "yes")


def test_touched_counts_toward_the_post_limit() -> None:
    run = ClarifyRun(cfg())
    run.set_ready(questions())
    for _ in range(clarify_flow.MAX_ANSWER_POSTS):
        run.answer([], False, True)
    with pytest.raises(clarify_flow.ClarifyRateLimited):
        run.answer([], False, True)


def test_the_gate_waits_for_more_coming_but_not_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clarify_flow, "MORE_COMING_WAIT_S", 0.5)
    pq = ShopQuery(request="d", search_text="d", category="dresser")
    run = ClarifyRun(cfg(clarify_idle_grace_s=0.05, clarify_max_wait_s=1.0))
    run.set_ready(questions()[:1], more_coming=True)
    t = later(0.2, lambda: (run.append_questions(questions()[1:]), run.finish_more()))
    gate = run.wait_at_gate(pq)
    t.join()
    assert 0.18 <= gate.waited < 0.45 and run.question_count() == 2
    stuck = ClarifyRun(cfg(clarify_idle_grace_s=0.05, clarify_max_wait_s=1.0))
    stuck.set_ready(questions()[:1], more_coming=True)
    g2 = stuck.wait_at_gate(pq)
    assert 0.45 <= g2.waited < 0.9  # the questions that exist are used after the cap
    assert stuck.snapshot()["more_coming"] is False  # applied: nothing more will be added
    assert stuck.append_questions(questions()[1:]) is False


# ---- the search, end to end in mock mode --------------------------------------------------------


class SlowFetch:
    """The fixture provider, held until `release` is set."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.name = inner, inner.name
        self.thread_safe = getattr(inner, "thread_safe", False)
        self.release = threading.Event()
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, gl: str = "us", hl: str = "en", page: int = 1) -> Any:
        self.calls.append((query, page))
        self.release.wait(10)
        return self.inner.search(query, gl=gl, hl=hl, page=page)


def test_question_one_is_shown_while_the_fetch_is_still_pending(tmp_path: Path) -> None:
    config = cfg(clarify_idle_grace_s=5.0, clarify_max_wait_s=30.0)
    search_provider, _ = build_shop_providers(config)
    slow = SlowFetch(search_provider)
    svc = ShopService(config, SessionStore(tmp_path / "d"), slow, ShopFixtureLookup())
    job = svc.start_search(None, "nice rug")["job_id"]
    try:
        st = wait_questions(svc, job, timeout=3.0)
        clar = st["clarify"]
        assert clar["state"] == "ready" and len(clar["questions"]) == 1
        assert clar["more_coming"] is True and clar["timing"]["source"] == "template"
        assert clar["timing"]["q1_ms"] is not None and clar["timing"]["rest_ms"] is None
        assert not slow.release.is_set() and st["state"] == "running"
        svc.job_answers(job, None, [], True, True)  # touched + submit: do not wait out the grace
    finally:
        slow.release.set()
    svc.jobs.wait(job, 20)
    final = svc.job_status(job, "")
    assert final["state"] == "done" and final["clarify"]["more_coming"] is False
    assert final["clarify"]["timing"]["rest_ms"] is not None


def test_answers_reorder_what_is_fetched_without_another_fetch(tmp_path: Path) -> None:
    def calls_for(answer: bool) -> list[tuple[str, int]]:
        svc = make_service(tmp_path / str(answer))
        seen: list[tuple[str, int]] = []
        inner = svc.search_provider
        real = inner.search

        def counting(query: str, *a: Any, **kw: Any) -> Any:
            seen.append((query, kw.get("page", 1)))
            return real(query, *a, **kw)

        inner.search = counting  # type: ignore[method-assign]
        st = search(svc, "modern couch", answer=answer)
        trace = svc.store.load(st["result"]["session_id"]).searches[0].trace
        assert "extra_search" not in trace.get("clarify_gate", {})
        return seen

    assert sorted(calls_for(True)) == sorted(calls_for(False))


def test_pictures_never_block_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def stuck_verify(*a: Any, **kw: Any) -> Any:
        release.wait(10)
        raise RuntimeError("released")

    unknown = q("q1", "style", "Zorbo", "Quux")  # no icon: a picture may be looked for
    monkeypatch.setattr(
        clarify_flow,
        "first_question",
        lambda *a, **k: FirstResult(question=unknown, source="model"),
    )
    monkeypatch.setattr(clarify_flow, "verify_and_prune", stuck_verify)
    svc = make_service(tmp_path)
    job = svc.start_search(None, "something odd")["job_id"]
    try:
        deadline = time.monotonic() + 5
        st: dict[str, Any] = {}
        while time.monotonic() < deadline:
            st = svc.job_status(job, "")
            if (
                st.get("clarify", {}).get("more_coming") is False
                and st["clarify"]["state"] == "ready"
            ):
                break
            time.sleep(0.01)
        clar = st["clarify"]
        assert clar["state"] == "ready" and clar["pictures_state"] == "pending"
        assert clar["questions"][0]["picture_eligible"] is True
        assert clar["questions"][0]["options"][0]["picture_url"] is None
    finally:
        release.set()
    svc.jobs.wait(job, 20)
    assert svc.job_status(job, "")["clarify"]["pictures_state"] == "done"


def test_the_timing_line_and_fields(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    svc = make_service(tmp_path)
    st = search(svc, "modern couch", answer=True)
    clar = st["clarify"]
    timing = clar["timing"]
    assert timing["source"] == "template" and timing["q1_ms"] >= 0 and timing["rest_ms"] >= 0
    assert "first_pictures_ms" in timing
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.startswith("clarify_timing ")]
    assert len(lines) == 1
    assert f"job={st.get('job_id', '')}" in lines[0] or "job=" in lines[0]
    assert "q1_ms=" in lines[0] and "rest_ms=" in lines[0] and "source=template" in lines[0]
    assert "questions=" in lines[0]
    assert "http" not in lines[0]


def test_options_that_are_drawn_carry_icon_or_swatches_and_no_picture(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    st = search(svc, "modern couch", answer=True)
    for qq in st["clarify"]["questions"]:
        for o in qq["options"]:
            assert set(o) == {"label", "value", "icon", "swatches", "picture_url"}
            if o["icon"] or o["swatches"]:
                assert o["picture_url"] is None
