"""Stage Q1: clarifying questions in the search job, behind `APP_SHOP_CLARIFY` (ADR 0017).

Nothing here touches the network or spends money: the model is the canned mock (priced from its
token counts, so the spend counters move), providers are the fixtures, and the waits are fractions
of a second (`clarify_idle_grace_s`, `clarify_max_wait_s`)."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.llm import LLMOutputError
from app.shop import clarify_flow
from app.shop.auth import purge
from app.shop.clarify import Option, Question
from app.shop.clarify_first import FirstResult
from app.shop.clarify_flow import (
    NO_PREFERENCE,
    ClarifyConflict,
    ClarifyInvalid,
    ClarifyRateLimited,
    ClarifyRun,
    apply_stored,
    clarify_record,
    materially_same,
)
from app.shop.dbstore import SqliteDatabase, SqlSessionStore, migrate
from app.shop.deploy import DeploySettings
from app.shop.models import ShopQuery
from app.shop.providers import ShopFixtureLookup, build_shop_providers
from app.shop.routes import ROUTES, match
from app.shop.server import Identity, ShopApp
from app.shop.service import ApiError, ShopService
from app.shop.store import ClarifyAnswer, ClarifyRecord, Session, SessionStore
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)

GRACE, MAX_WAIT = 0.2, 0.8


def cfg(**kw: Any) -> Config:
    base: dict[str, Any] = {
        "tracing_disabled": True,
        "shop_clarify": True,
        "clarify_idle_grace_s": GRACE,
        "clarify_max_wait_s": MAX_WAIT,
    }
    return Config(**{**base, **kw})


def questions() -> list[Question]:
    return [
        Question(
            id="q_material", text="What should it be made of?", kind="choice",
            dimension="material",
            options=[Option(label="Wood", value="wood"), Option(label="Metal", value="metal")],
        ),
        Question(
            id="q_color", text="Which colour?", kind="choice", dimension="color",
            options=[Option(label="Walnut", value="walnut"), Option(label="Black", value="black")],
        ),
    ]  # fmt: skip


def parsed() -> ShopQuery:
    return ShopQuery(request="dresser", search_text="dresser", category="dresser")


def ready_run(**kw: Any) -> ClarifyRun:
    run = ClarifyRun(cfg(**kw))
    run.set_ready(questions())
    return run


def later(seconds: float, fn: Any, *args: Any) -> threading.Thread:
    t = threading.Timer(seconds, fn, args)
    t.start()
    return t


# ---- settings ----------------------------------------------------------------------------------


def test_the_flag_is_off_by_default_and_the_numbers_have_the_documented_defaults() -> None:
    bare = Config()
    assert bare.shop_clarify is False
    assert (bare.clarify_idle_grace_s, bare.clarify_max_wait_s) == (30.0, 60.0)
    assert Config.from_env().shop_clarify is False


def test_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_SHOP_CLARIFY", "1")
    monkeypatch.setenv("CLARIFY_IDLE_GRACE_S", "3")
    monkeypatch.setenv("APP_CLARIFY_MAX_WAIT_S", "20")
    c = Config.from_env()
    assert c.shop_clarify and c.clarify_idle_grace_s == 3.0 and c.clarify_max_wait_s == 20.0
    monkeypatch.setenv("APP_CLARIFY_IDLE_GRACE_S", "9")  # the APP_ name wins over the bare one
    assert Config.from_env().clarify_idle_grace_s == 9.0


def test_bad_settings_are_refused() -> None:
    with pytest.raises(ValueError):
        Config(clarify_idle_grace_s=-1)
    with pytest.raises(ValueError):
        Config(clarify_idle_grace_s=10, clarify_max_wait_s=5)


def test_the_answers_route_is_in_the_table_and_matches_only_its_own_path() -> None:
    assert any(r.name == "job_answers" and r.method == "POST" for r in ROUTES)
    route, _other, job = match("POST", "/job/abcDEF123456789_-x/answers")
    assert route is not None and route.name == "job_answers" and job == "abcDEF123456789_-x"
    assert match("GET", "/job/abc/answers")[0] is None and match("GET", "/job/abc/answers")[1]
    assert match("POST", "/job//answers")[0] is None
    assert match("POST", "/job/a/b/answers")[0] is None
    assert match("GET", "/job/abc")[0] is not None


# ---- the apply point ---------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["fail", "skip"])
def test_failed_or_skipped_questions_never_wait(how: str) -> None:
    run = ClarifyRun(cfg())
    getattr(run, how)("because")
    gate = run.wait_at_gate(parsed())
    assert gate.waited < 0.05 and gate.query is None
    assert gate.reason == ("failed" if how == "fail" else "skipped")


def test_questions_still_being_written_wait_the_grace_then_are_skipped() -> None:
    run = ClarifyRun(cfg())
    gate = run.wait_at_gate(parsed())
    assert GRACE - 0.02 <= gate.waited < GRACE + 0.3
    assert gate.reason == "too_slow" and run.state == "skipped" and gate.query is None
    run.set_ready(questions())  # too late: the apply point has passed
    assert run.state == "skipped"


def test_no_interaction_waits_the_grace_then_ranks_without_answers() -> None:
    run = ready_run()
    gate = run.wait_at_gate(parsed())
    assert GRACE - 0.02 <= gate.waited < GRACE + 0.3
    assert gate.reason == "idle_grace" and gate.query is None
    assert run.snapshot()["applied"] is True


def test_questions_that_appear_late_get_their_own_grace() -> None:
    run = ClarifyRun(cfg())
    t = later(GRACE / 2, run.set_ready, questions())
    gate = run.wait_at_gate(parsed())
    t.join()
    assert gate.reason == "idle_grace"
    assert gate.waited >= GRACE / 2 + GRACE - 0.03  # the grace runs from when they were shown


def test_an_answer_without_a_submit_waits_until_the_max_wait_after_the_questions_were_shown() -> (
    None
):
    run = ready_run()
    t = later(0.05, run.answer, [{"question_id": "q_material", "option_value": "wood"}], False)
    gate = run.wait_at_gate(parsed())
    t.join()
    assert gate.reason == "max_wait" and MAX_WAIT - 0.1 <= gate.waited < MAX_WAIT + 0.3
    assert gate.query is not None and "wood" in gate.query.material_terms


def test_a_submit_ends_the_wait_at_once_and_the_answers_are_applied() -> None:
    run = ready_run()
    body = [
        {"question_id": "q_material", "option_value": "metal"},
        {"question_id": "q_color", "option_value": "black"},
    ]
    t = later(0.1, run.answer, body, True)
    gate = run.wait_at_gate(parsed())
    t.join()
    assert gate.reason == "submit" and gate.waited < 0.4
    q = gate.query
    assert q is not None and q.material_terms == ["metal"] and q.color_terms == ["black"]
    assert "metal" in q.search_text and q.category == "dresser"
    assert {m["dimension"] for m in gate.mapped} == {"material", "color"}


def test_a_submit_with_no_answers_or_all_no_preference_changes_nothing() -> None:
    run = ready_run()
    run.answer([], True)
    gate = run.wait_at_gate(parsed())
    assert gate.reason == "submit" and gate.query is None and gate.waited < 0.05
    run = ready_run()
    run.answer([{"question_id": "q_color", "option_value": NO_PREFERENCE}], True)
    assert run.wait_at_gate(parsed()).query is None


def test_an_answer_inside_the_grace_moves_the_deadline_past_it() -> None:
    run = ready_run()
    t = later(GRACE / 2, run.answer, [{"question_id": "q_color", "option_value": "walnut"}], False)
    gate = run.wait_at_gate(parsed())
    t.join()
    assert gate.waited > GRACE + 0.1 and gate.reason == "max_wait"


def test_a_failure_while_waiting_ends_the_wait() -> None:
    run = ready_run()
    t = later(0.05, run.fail, "boom")
    gate = run.wait_at_gate(parsed())
    t.join()
    assert gate.reason == "failed" and gate.waited < 0.3 and gate.query is None


# ---- answers: validation, idempotence, conflicts -----------------------------------------------


def test_answers_are_validated_against_the_stored_questions() -> None:
    run = ready_run()
    for bad in ("x", [1], [{"question_id": "q_color"}], [{"question_id": 1, "option_value": "a"}]):
        with pytest.raises(ClarifyInvalid):
            run.answer(bad, False)
    with pytest.raises(ClarifyInvalid):
        run.answer([{"question_id": "q_color", "option_value": "plaid"}], False)
    with pytest.raises(ClarifyInvalid):
        run.answer([], "yes")
    # a bad item changes nothing: the good one next to it is not recorded either
    with pytest.raises(ClarifyInvalid):
        run.answer(
            [
                {"question_id": "q_material", "option_value": "wood"},
                {"question_id": "q_color", "option_value": "plaid"},
            ],
            False,
        )
    assert run.snapshot()["answers"] == []


def test_unknown_question_ids_are_ignored() -> None:
    run = ready_run()
    snap = run.answer([{"question_id": "q_nope", "option_value": "x"}], False)
    assert snap["answers"] == [] and snap["submitted"] is False


def test_answering_is_idempotent_and_a_later_answer_replaces_the_earlier() -> None:
    run = ready_run()
    body = [{"question_id": "q_color", "option_value": "walnut"}]
    first = run.answer(body, False)
    again = run.answer(body, False)
    assert (
        first["answers"]
        == again["answers"]
        == [{"question_id": "q_color", "option_value": "walnut"}]
    )
    run.answer([{"question_id": "q_color", "option_value": "black"}], False)
    assert run.snapshot()["answers"] == [{"question_id": "q_color", "option_value": "black"}]
    run.answer([], True)
    assert run.answer([], True)["submitted"] is True  # a repeated submit is fine


def test_no_preference_is_a_valid_answer_and_counts_as_interaction() -> None:
    run = ready_run()
    snap = run.answer([{"question_id": "q_color", "option_value": NO_PREFERENCE}], False)
    assert snap["answers"][0]["option_value"] == "no_preference"


def test_answers_are_a_conflict_without_questions_and_past_the_apply_point() -> None:
    pending = ClarifyRun(cfg())
    with pytest.raises(ClarifyConflict):
        pending.answer([], False)
    failed = ClarifyRun(cfg())
    failed.fail("x")
    with pytest.raises(ClarifyConflict):
        failed.answer([], False)
    run = ready_run()
    run.answer([{"question_id": "q_color", "option_value": "walnut"}], True)
    run.wait_at_gate(parsed())
    with pytest.raises(ClarifyConflict):
        run.answer([{"question_id": "q_color", "option_value": "black"}], False)
    # a repeat of what was applied is accepted (a double click on Submit)
    assert run.answer([{"question_id": "q_color", "option_value": "walnut"}], True)["applied"]


def test_too_many_answer_posts_are_refused() -> None:
    run = ready_run()
    with pytest.raises(ClarifyRateLimited):
        for _ in range(clarify_flow.MAX_ANSWER_POSTS + 1):
            run.answer([], False)


def test_the_snapshot_shape() -> None:
    run = ready_run()
    snap = run.snapshot()
    assert set(snap) == {
        "state", "questions", "pictures_state", "no_preference_value", "answers", "submitted",
        "applied", "deadline", "more_coming", "timing",
    }  # fmt: skip
    assert snap["state"] == "ready" and snap["pictures_state"] == "pending"
    assert set(snap["questions"][0]) == {
        "id", "text", "kind", "dimension", "options", "picture_eligible",
    }  # fmt: skip
    assert snap["questions"][0]["options"][0] == {
        "label": "Wood", "value": "wood", "icon": "wood", "swatches": None, "picture_url": None,
    }  # fmt: skip
    assert snap["questions"][0]["picture_eligible"] is False  # every option is drawn
    assert snap["more_coming"] is False
    assert set(snap["timing"]) == {"q1_ms", "rest_ms", "first_pictures_ms", "source"}
    assert set(snap["deadline"]) == {
        "idle_grace_s", "max_wait_s", "waiting", "seconds_left", "shown_s_ago",
    }  # fmt: skip
    run.set_pictures({("q_material", "wood"): "https://img.example/a.jpg"})
    snap = run.snapshot()
    assert snap["pictures_state"] == "done"
    # an option with an icon never carries a picture, even if one was stored for it
    assert snap["questions"][0]["options"][0]["picture_url"] is None
    json.dumps(snap)


def test_failed_questions_show_no_questions() -> None:
    run = ClarifyRun(cfg())
    run.fail("x")
    snap = run.snapshot()
    assert (
        snap["state"] == "failed" and snap["questions"] == [] and snap["pictures_state"] == "done"
    )


def test_the_deadline_is_shown_while_the_pipeline_waits() -> None:
    run = ready_run()
    seen: list[dict[str, Any]] = []
    t = later(0.05, lambda: seen.append(run.snapshot()["deadline"]))
    run.wait_at_gate(parsed())
    t.join()
    assert seen[0]["waiting"] is True and 0 < seen[0]["seconds_left"] <= GRACE


# ---- small helpers -----------------------------------------------------------------------------


def test_the_prefetch_is_reused_only_when_the_parsed_text_is_materially_the_same() -> None:
    assert materially_same("modern couch", "modern couch")
    assert materially_same("Couch  Modern", "modern couch")
    assert not materially_same("modern couch under $1000", "sectional sofa")
    assert not materially_same("", "couch")


def test_stored_answers_apply_through_apply_answers_and_a_gone_option_is_skipped() -> None:
    rec = ClarifyRecord(
        topic_key="dresser",
        questions=[q.model_dump(mode="json") for q in questions()],
        answers=[
            ClarifyAnswer(question_id="q_material", option_value="wood"),
            ClarifyAnswer(question_id="q_color", option_value="plaid"),  # not an option any more
            ClarifyAnswer(question_id="q_gone", option_value="x"),
        ],
    )
    q = apply_stored(parsed(), rec)
    assert q.material_terms == ["wood"] and q.color_terms == []
    assert apply_stored(parsed(), None) == parsed()


def test_a_record_is_made_only_from_answered_questions() -> None:
    run = ready_run()
    assert clarify_record(run, parsed()) is None
    run.answer([{"question_id": "q_color", "option_value": "walnut"}], True)
    run.wait_at_gate(parsed())
    rec = clarify_record(run, parsed())
    assert rec is not None and rec.topic_key == "dresser"
    assert [a.option_value for a in rec.answers] == ["walnut"]


# ---- the session --------------------------------------------------------------------------------


def test_a_session_without_clarify_data_saves_no_clarify_key(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "s")
    session = store.create()
    assert "clarify" not in session.model_dump(mode="json")  # older code can still read the file
    with store.transaction(session.session_id) as live:
        live.clarify = ClarifyRecord(topic_key="dresser")
    assert store.load(session.session_id).clarify == ClarifyRecord(topic_key="dresser")
    assert (
        json.loads(
            Session.model_validate(
                json.loads((tmp_path / "s" / f"{session.session_id}.json").read_text())
            ).model_dump_json()
        )["clarify"]["topic_key"]
        == "dresser"
    )


def test_the_retention_purge_removes_a_session_with_clarify_data() -> None:
    db = SqliteDatabase(":memory:")
    migrate(db)
    store = SqlSessionStore(db)
    session = store.create()
    with store.transaction(session.session_id) as live:
        live.clarify = ClarifyRecord(topic_key="sofa", questions=[{"id": "q"}])
    assert store.load(session.session_id).clarify is not None
    purge(db, datetime.now(UTC) + timedelta(days=31), 30)
    assert not store.exists(session.session_id)


# ---- the service, end to end in mock mode ----


def make_service(tmp_path: Path, config: Config | None = None) -> ShopService:
    config = config or cfg()
    search, _ = build_shop_providers(config)
    return ShopService(config, SessionStore(tmp_path / "d"), search, ShopFixtureLookup())


def wait_questions(
    svc: ShopService, job: str, sid: str = "", timeout: float = 5.0
) -> dict[str, Any]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        st = svc.job_status(job, sid)
        if st.get("clarify", {}).get("state") in ("ready", "failed", "skipped"):
            return st
        time.sleep(0.01)
    raise AssertionError("no questions state")


def search(svc: ShopService, request: str, sid: str | None = None, answer: bool = False) -> dict:
    """Run a search job; with `answer`, answer the first question's first option and submit."""
    job = svc.start_search(sid, request)["job_id"]
    if answer:
        st = wait_questions(svc, job, sid or "")
        q = st["clarify"]["questions"][0]
        svc.job_answers(
            job, sid, [{"question_id": q["id"], "option_value": q["options"][0]["value"]}], True
        )
    svc.jobs.wait(job, 20)
    st = svc.job_status(job, sid or "")
    assert st["state"] == "done", st
    return st


def sessions(svc: ShopService, st: dict[str, Any]) -> Session:
    return svc.store.load(st["result"]["session_id"])


def test_with_the_flag_off_nothing_changes(tmp_path: Path) -> None:
    off = make_service(tmp_path / "off", cfg(shop_clarify=False))
    st = search(off, "modern couch")
    assert "clarify" not in st and "clarify_answers" not in st["result"]
    assert set(st) == {"state", "phase", "kind", "steps", "elapsed_s", "result"}
    on = make_service(tmp_path / "on", cfg())
    st_on = search(on, "modern couch")
    assert set(st_on["result"]) - set(st["result"]) == {"clarify_answers"}
    assert set(st["result"]) - set(st_on["result"]) == set()
    assert set(st_on) - set(st) == {"clarify"}
    assert [r["id"] for r in st["result"]["search"]["results"]] == [
        r["id"] for r in st_on["result"]["search"]["results"]
    ]


def test_questions_show_first_as_text_then_with_pictures_and_the_search_goes_on(
    tmp_path: Path,
) -> None:
    svc = make_service(tmp_path)
    job = svc.start_search(None, "modern couch")["job_id"]
    st = wait_questions(svc, job)
    clar = st["clarify"]
    assert clar["state"] == "ready" and 1 <= len(clar["questions"]) <= 5
    assert st["state"] == "running" and clar["deadline"]["idle_grace_s"] == GRACE
    for q in clar["questions"]:
        assert 2 <= len(q["options"]) <= 4
    svc.jobs.wait(job, 20)
    final = svc.job_status(job, "")
    assert final["state"] == "done" and final["clarify"]["pictures_state"] == "done"
    assert final["clarify"]["applied"] is True and final["clarify"]["answers"] == []
    urls = [o["picture_url"] for q in final["clarify"]["questions"] for o in q["options"]]
    assert all(u is None or u.startswith("https://") for u in urls)


def test_a_submitted_answer_shapes_the_search_and_is_stored(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    st = search(svc, "modern couch", answer=True)
    session = sessions(svc, st)
    rec = session.searches[0]
    q = st["clarify"]["questions"][0]
    value = q["options"][0]["value"]
    assert st["clarify"]["submitted"] and st["clarify"]["answers"][0]["option_value"] == value
    assert rec.used_query is not None and rec.used_query != rec.query  # the answers are applied
    assert rec.query.material_terms == [] and rec.query.color_terms == []  # the record stays plain
    assert session.clarify is not None and session.clarify.answers[0].option_value == value
    chips = st["result"]["clarify_answers"]
    assert chips and chips[0]["id"] == f"answer:{q['id']}"
    assert st["result"]["session_usd"] > 0
    assert st["clarify"]["deadline"]["waiting"] is False


def test_a_job_without_interaction_ranks_after_the_grace_and_stores_nothing(
    tmp_path: Path,
) -> None:
    svc = make_service(tmp_path)
    t0 = time.monotonic()
    st = search(svc, "modern couch")
    assert time.monotonic() - t0 < GRACE + 3
    session = sessions(svc, st)
    assert session.clarify is None and session.searches[0].used_query is None
    assert st["result"]["clarify_answers"] == []
    trace = session.searches[0].trace["clarify"]
    assert trace["applied_reason"] == "idle_grace" and trace["answers_applied"] == 0


def test_partial_answers_apply_at_the_max_wait(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    job = svc.start_search(None, "modern couch")["job_id"]
    st = wait_questions(svc, job)
    q = st["clarify"]["questions"][0]
    svc.job_answers(job, None, [{"question_id": q["id"], "option_value": NO_PREFERENCE}], False)
    t0 = time.monotonic()
    svc.jobs.wait(job, 20)
    assert time.monotonic() - t0 > GRACE  # one answer moved the deadline out to the max wait
    final = svc.job_status(job, "")
    assert final["state"] == "done" and final["clarify"]["submitted"] is False
    trace = sessions(svc, final).searches[0].trace["clarify"]
    assert trace["applied_reason"] == "max_wait"


def test_question_failures_never_slow_or_break_the_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**_kw: Any) -> Any:
        raise LLMOutputError("nope")

    monkeypatch.setattr(clarify_flow, "mock_caller", boom)
    monkeypatch.setattr(clarify_flow, "first_question", lambda *a, **k: FirstResult())
    svc = make_service(tmp_path / "a", cfg(clarify_idle_grace_s=5.0, clarify_max_wait_s=30.0))
    t0 = time.monotonic()
    st = search(svc, "modern couch")
    assert time.monotonic() - t0 < 3  # no wait on a failure, however long the grace
    assert st["clarify"]["state"] in ("skipped", "failed") and st["clarify"]["questions"] == []
    assert st["result"]["search"]["results"]
    base = make_service(tmp_path / "b", cfg(shop_clarify=False))
    plain = search(base, "modern couch")
    assert [r["id"] for r in st["result"]["search"]["results"]] == [
        r["id"] for r in plain["result"]["search"]["results"]
    ]
    job = svc.start_search(None, "modern couch")["job_id"]
    with pytest.raises(ApiError) as exc:
        svc.job_answers(job, None, [], True)
    assert exc.value.status == 409  # no open questions to answer
    svc.jobs.wait(job, 20)


def test_a_picture_check_failure_keeps_the_text_questions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**_kw: Any) -> Any:
        raise LLMOutputError("nope")

    monkeypatch.setattr(clarify_flow, "mock_verify_caller", boom)
    svc = make_service(tmp_path)
    st = search(svc, "modern couch")
    clar = st["clarify"]
    assert clar["state"] == "ready" and clar["pictures_state"] == "done"
    assert all(o["picture_url"] is None for q in clar["questions"] for o in q["options"])


def test_the_spend_is_in_the_search_record_and_the_session_total(tmp_path: Path) -> None:
    on = make_service(tmp_path / "on")
    st_on = search(on, "modern couch")
    off = make_service(tmp_path / "off", cfg(shop_clarify=False))
    st_off = search(off, "modern couch")
    rec_on, rec_off = sessions(on, st_on).searches[0], sessions(off, st_off).searches[0]
    trace = rec_on.trace["clarify"]
    extra = trace["question_usd"] + trace["verification_usd"]
    assert extra > 0
    assert rec_on.anthropic_usd - rec_off.anthropic_usd == pytest.approx(extra, abs=1e-5)
    assert st_on["result"]["session_usd"] > st_off["result"]["session_usd"]
    assert rec_on.provider_usd >= rec_off.provider_usd


def test_the_trace_has_the_timing_and_counts(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    st = search(svc, "modern couch", answer=True)
    trace = sessions(svc, st).searches[0].trace["clarify"]
    for key in (
        "seconds_to_text", "seconds_to_pictures", "options_shown", "options_dropped_by_title",
        "verification_usd", "answers_applied", "applied_reason", "waited_s", "prefetch_reused",
    ):  # fmt: skip
        assert key in trace
    assert trace["answers_applied"] == 1 and trace["applied_reason"] == "submit"
    assert trace["seconds_to_text"] is not None
    assert set(trace["timing"]) == {"q1_ms", "rest_ms", "first_pictures_ms", "source"}


def test_the_first_page_is_fetched_once_when_the_parse_matches_the_request(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    calls: list[tuple[str, int]] = []
    inner = svc.search_provider
    real = inner.search

    def counting(query: str, *a: Any, **kw: Any) -> Any:
        calls.append((query, kw.get("page", 1)))
        return real(query, *a, **kw)

    inner.search = counting  # type: ignore[method-assign]
    st = search(svc, "modern couch")
    page_one = [c for c in calls if c[1] == 1]
    trace = sessions(svc, st).searches[0].trace["clarify"]
    if trace["prefetch_reused"]:
        assert len(page_one) == len([c for c in page_one if c[0] == "modern couch"]) == 1
    assert len(page_one) >= 1


def test_a_different_category_clears_the_answers_and_the_same_one_keeps_them(
    tmp_path: Path,
) -> None:
    svc = make_service(tmp_path)
    st = search(svc, "modern couch", answer=True)
    sid = st["result"]["session_id"]
    held = sessions(svc, st).clarify
    assert held is not None
    again = search(svc, "leather couch", sid)  # the same canonical category: kept, not asked again
    assert again["clarify"]["state"] == "skipped"
    assert svc.store.load(sid).clarify == held
    assert again["result"]["clarify_answers"]
    other = search(svc, "oak coffee table", sid)  # another category: cleared
    assert svc.store.load(sid).clarify is None
    assert other["result"]["clarify_answers"] == []


def test_a_deleted_answer_chip_removes_the_constraint_for_the_next_shop_again(
    tmp_path: Path,
) -> None:
    from app.shop.again import base_query

    config = cfg()
    svc = make_service(tmp_path, config)
    st = search(svc, "modern couch", answer=True)
    sid = st["result"]["session_id"]
    plain = base_query(svc.store.load(sid))
    with_answers = base_query(svc.store.load(sid), config)
    assert plain is not None and with_answers is not None and with_answers != plain
    assert base_query(svc.store.load(sid), cfg(shop_clarify=False)) == plain  # the kill switch
    chip = st["result"]["clarify_answers"][0]["id"]
    out = svc.remove_refine(sid, chip)
    assert out["clarify_answers"] == []
    assert base_query(svc.store.load(sid), config) == plain
    with pytest.raises(ApiError) as exc:
        svc.remove_refine(sid, chip)
    assert exc.value.status == 404


def test_shop_again_reuses_the_stored_answers(tmp_path: Path) -> None:
    config = cfg()
    svc = make_service(tmp_path, config)
    st = search(svc, "modern couch", answer=True)
    sid = st["result"]["session_id"]
    for r in st["result"]["search"]["results"][:2]:
        svc.rate(sid, r["id"], "like", "")
    job = svc.start_shop_again(sid, None)["job_id"]
    svc.jobs.wait(job, 30)
    done = svc.job_status(job, sid)
    assert done["state"] == "done" and "clarify" not in done  # no questions on a Shop Again
    last = svc.store.load(sid).searches[-1]
    assert last.kind == "shop_again" and last.used_query is not None
    base = svc.store.load(sid).searches[0]
    answered = base.used_query
    assert answered is not None
    for field in ("material_terms", "color_terms", "style_terms", "must_have", "shape_terms"):
        for term in getattr(answered, field):
            if term not in getattr(base.query, field):
                assert term in getattr(last.used_query, field)


def test_a_shop_again_job_has_no_questions_to_answer(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    st = search(svc, "modern couch")
    sid = st["result"]["session_id"]
    for r in st["result"]["search"]["results"][:2]:
        svc.rate(sid, r["id"], "like", "")
    job = svc.start_shop_again(sid, None)["job_id"]
    svc.jobs.wait(job, 30)
    with pytest.raises(ApiError) as exc:
        svc.job_answers(job, sid, [], True)
    assert exc.value.status == 409


def test_answering_is_not_a_search(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    st = search(svc, "modern couch", answer=True)
    assert len(sessions(svc, st).searches) == 1  # one search record, whatever was answered


# ---- the endpoint -------------------------------------------------------------------------------

SECRET = "test-origin-secret"
ORIGIN = "https://portfolio.example"
HOSTS = "shop.fly.dev"


class Auth:
    def start(self, ip: str, headers: Any = None) -> str:
        return "ok"

    def logout(self, cookie: str | None) -> None:
        return None

    def identify(self, cookie: str | None) -> Identity | None:
        return {"ok": Identity("v1", "l1"), "other": Identity("v2", "l2")}.get(cookie or "")

    def usage(self, identity: Identity) -> dict[str, Any]:
        return {}

    def tick(self) -> None:
        return None

    def contact(self) -> str:
        return "me@example.org"


def app_for(tmp_path: Path) -> ShopApp:
    config = cfg()
    search_provider, _ = build_shop_providers(config)
    svc = ShopService(config, SessionStore(tmp_path / "d"), search_provider, ShopFixtureLookup())
    settings = DeploySettings(
        deploy=True, host="0.0.0.0", api_prefix="/api/shop",
        allowed_hosts=frozenset({HOSTS}), allowed_origins=frozenset({ORIGIN}),
        origin_secret=SECRET, public_url=ORIGIN, contact_email="me@example.org",
        database_url="sqlite:///x",
    )  # fmt: skip
    return ShopApp(svc, 0, settings, Auth())  # type: ignore[arg-type]


def call(app: ShopApp, method: str, path: str, body: Any = None, cookie: str | None = "ok") -> Any:
    headers = {
        "Host": HOSTS, "X-Origin-Secret": SECRET, "Origin": ORIGIN,
        "Content-Type": "application/json",
    }  # fmt: skip
    if cookie:
        headers["Cookie"] = f"shop_login={cookie}"
    raw = b"" if body is None else json.dumps(body).encode()
    return app.handle(method, path, headers, raw)


def test_the_endpoint_end_to_end_with_auth_validation_idempotence_and_409(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    started = call(app, "POST", "/api/shop/search", {"request": "modern couch"})
    assert started.status == 202
    job = json.loads(started.body)["job_id"]
    svc = app.service
    st = wait_questions(svc, job)
    q = st["clarify"]["questions"][0]
    path = f"/api/shop/job/{job}/answers"
    good = {"answers": [{"question_id": q["id"], "option_value": q["options"][0]["value"]}]}

    assert call(app, "POST", path, good, cookie=None).status == 401  # no visit
    assert call(app, "POST", path, good, cookie="other").status == 404  # someone else's job
    assert call(app, "GET", path).status == 405
    bad = call(app, "POST", path, {"answers": [{"question_id": q["id"], "option_value": "zz"}]})
    assert bad.status == 400
    assert call(app, "POST", path, {"answers": "x"}).status == 400
    assert call(app, "POST", path, {"answers": [], "submit": "yes"}).status == 400
    ignored = call(app, "POST", path, {"answers": [{"question_id": "nope", "option_value": "a"}]})
    assert ignored.status == 200 and json.loads(ignored.body)["clarify"]["answers"] == []

    one = call(app, "POST", path, good)
    two = call(app, "POST", path, good)
    assert one.status == two.status == 200
    assert json.loads(one.body) == json.loads(two.body)
    body = json.loads(one.body)["clarify"]
    assert body["answers"] == [good["answers"][0]] and body["submitted"] is False

    last = call(app, "POST", path, {**good, "submit": True})
    assert last.status == 200 and json.loads(last.body)["clarify"]["submitted"] is True
    svc.jobs.wait(job, 20)
    done = json.loads(call(app, "GET", f"/api/shop/job/{job}").body)
    assert done["state"] == "done" and done["clarify"]["applied"] is True
    changed = {"answers": [{"question_id": q["id"], "option_value": NO_PREFERENCE}]}
    late = call(app, "POST", path, changed)
    assert late.status == 409  # past the apply point
    assert call(app, "POST", path, {**good, "submit": True}).status == 200  # a repeat is fine
    assert call(app, "POST", f"/api/shop/job/{'x' * 22}/answers", good).status == 404


def test_a_search_does_not_use_the_search_limit_when_answered(tmp_path: Path) -> None:
    svc = make_service(tmp_path)
    calls: list[str] = []
    real = svc._cap

    def spy(kind: str, session_id: Any = None) -> None:
        calls.append(kind)
        real(kind, session_id)

    svc._cap = spy  # type: ignore[method-assign]
    search(svc, "modern couch", answer=True)
    assert calls == ["search"]  # one for the search; the answers made none
