"""The clarify spike's harness (evals/shop_clarify): cases, persona answers, one full mock run, the
timing wrapper, the cost guard, the report writer and the contact sheet, the plan and the make
targets. Free: the mock catalogue and the canned question writer; nothing live runs from tests."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from evals.shop.cache import CacheStats
from evals.shop.runner import eval_config
from evals.shop.spec import thresholds_sha256
from evals.shop_clarify import runner
from evals.shop_clarify.__main__ import refuse_live
from evals.shop_clarify.mock import mock_caller
from evals.shop_clarify.persona import answer_questions, pick_for
from evals.shop_clarify.plan import estimate, plan_lines
from evals.shop_clarify.report import (
    render_contact_sheet,
    render_report,
    summarise,
    verdict_lines,
    write_report,
)
from evals.shop_clarify.runner import HardStop, LatencyStore, Spend, TimedSearch, make_env, run_all
from evals.shop_clarify.spec import load_cases

from app.shop import clarify as clarify_mod
from app.shop.clarify import Option, Question
from app.shopping import SearchCandidate

ROOT = Path(__file__).resolve().parents[1]
LOCKED_HASH = "c64fe35a51c389603c507ed0a31e4388d08a1e96ee25f2d0bf9288f1fb75d237"
REQUESTS = [
    "dresser", "couch", "rug", "bookshelf", "nightstand", "desk", "modern couch under $1000",
    "boho curtains", "walnut desk lamp", "floor mirror", "bed frame", "TV stand",
]  # fmt: skip


@pytest.fixture(scope="module")
def mock_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    env, _ = make_env("mock", tmp_path_factory.mktemp("results"))
    return run_all(load_cases(), env, mock_caller)


# ---- cases and persona ------------------------------------------------------------------------


def test_twelve_requests_six_vague_and_six_specific_each_with_a_hidden_persona() -> None:
    cases = load_cases()
    assert [c.request for c in cases] == REQUESTS
    assert len({c.id for c in cases}) == 12
    assert (
        sum(c.vagueness == "vague" for c in cases) == 6
        and sum(c.vagueness == "specific" for c in cases) == 6
    )
    assert all(len(c.answers) >= 3 and len(c.persona) > 40 for c in cases)
    case = cases[0].to_case(["walnut"])
    assert (
        case.persona.text == cases[0].persona
        and case.must_have == ["walnut"]
        and case.request == "dresser"
    )


def q(dim: str, options: list[tuple[str, str]], kind: str = "choice", text: str = "?") -> Question:
    return Question(
        id=f"q_{dim}",
        text=text,
        kind=kind,
        dimension=dim,
        options=[Option(label=a, value=b) for a, b in options],
    )  # type: ignore[arg-type]


def test_the_persona_picks_by_words_then_closest_then_range_else_marks_unanswerable() -> None:
    answers = {"color": "walnut", "size": "60 in", "style": "mid-century", "feature": "6 drawers"}
    assert (
        pick_for(q("color", [("Oak", "oak"), ("Walnut", "walnut")]), answers).option.label
        == "Walnut"
    )  # type: ignore[union-attr]
    close = pick_for(q("style", [("Midcentury modern", "midcentury"), ("Boho", "boho")]), answers)
    assert (
        close.option is not None
        and close.option.label == "Midcentury modern"
        and close.how == "closest"
    )
    ranged = pick_for(
        q(
            "size",
            [("Under 40 in", "under 40 in"), ("40 to 70 in", "40 to 70 in")],
            kind="size_range",
        ),
        answers,
    )
    assert (
        ranged.option is not None and ranged.option.label == "40 to 70 in" and ranged.how == "range"
    )
    none = pick_for(q("shape", [("Round", "round"), ("Square", "square")]), answers)
    assert none.option is None and none.how.startswith("unanswerable") and none.trait == ""
    nomatch = pick_for(q("color", [("Teal", "teal"), ("Rust", "rust")]), answers)
    assert nomatch.option is None and nomatch.how.startswith("unanswerable")
    by_text = pick_for(
        q("feature", [("Three", "3 drawer"), ("Six", "6 drawers")], text="How many drawers?"),
        {"size": "x", "feature": "6 drawers"},
    )
    assert by_text.option is not None and by_text.option.label == "Six"
    got, picks = answer_questions(
        [
            q("color", [("Walnut", "walnut"), ("Oak", "oak")]),
            q("shape", [("Round", "round"), ("Oval", "oval")]),
        ],
        answers,
    )
    assert len(got) == 1 and len(picks) == 2 and got[0].option is not None


# ---- one full mock run ------------------------------------------------------------------------


def test_a_full_mock_run_exercises_every_step_for_every_request(mock_run: dict[str, Any]) -> None:
    cases = mock_run["cases"]
    assert [c["status"] for c in cases] == ["ok"] * 12 and mock_run["stopped"] == ""
    assert mock_run["mode"] == "mock" and mock_run["prompt_version"] == "1"
    assert (
        mock_run["totals"]["anthropic_usd"] < mock_run["limits"]["anthropic_usd"]
        and mock_run["totals"]["serper_credits"] == 0
    )
    for c in cases:
        t = c["timing"]
        assert set(t) >= {
            "parse_s",
            "fetch_s",
            "model_s",
            "total_s",
            "overlapped_s",
            "candidates",
            "shown_to_model",
        }
        assert t["total_s"] == pytest.approx(t["parse_s"] + t["fetch_s"] + t["model_s"], abs=0.01)
        assert 0 < t["shown_to_model"] <= 40 and t["candidates"] >= t["shown_to_model"]
        assert c["images"]["options"] == sum(len(x["options"]) for x in c["questions"])
        assert "baseline" in c["arms"] and c["arms"]["baseline"]["count"] > 0
        assert set(c["cost"]) >= {"anthropic_usd", "serper_credits", "model_usd", "fetch_credits"}
    assert (
        sum(len(c["questions"]) for c in cases) > 12
    )  # the canned writer finds traits in the mock catalogue
    answered = [c for c in cases if c["arms"].get("answers")]
    assert answered and all("persona_points" in c["lift"] for c in answered)
    assert all(a["became"] for c in answered for a in c["applied"])


def test_the_hidden_persona_never_reaches_the_pipeline_or_the_question_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    real = runner.run_query

    def spy(query: Any, *a: Any, **kw: Any) -> Any:
        seen.append(repr(query))
        return real(query, *a, **kw)

    def caller(**kw: Any) -> Any:
        seen.append(kw["user_text"])
        return mock_caller(**kw)

    monkeypatch.setattr(runner, "run_query", spy)
    case = load_cases()[0]
    env, _ = make_env("mock")
    run_all([case], env, caller)
    assert len(seen) == 3  # the question call, the baseline query and the answers query
    persona_words = ["warm mid-century dresser", "white gloss", "flimsy"]
    assert not any(w in text for text in seen for w in persona_words)


def test_the_mock_run_leaves_the_live_thresholds_and_shop_files_alone() -> None:
    assert thresholds_sha256() == LOCKED_HASH
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "main", "--", "evals/shop", "src/app/shop"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert [
        t for t in tracked if t not in ("src/app/shop/clarify.py", "src/app/shop/clarify_verify.py")
    ] == []


# ---- the timing wrapper -----------------------------------------------------------------------


class Stub:
    name = "serper"
    thread_safe = True

    def __init__(self, stats: CacheStats, hit: bool) -> None:
        self.stats, self.hit = stats, hit

    def search(
        self, query: str, gl: str = "us", hl: str = "en", page: int = 1
    ) -> list[SearchCandidate]:
        if self.hit:
            self.stats.hits += 1
        return []


def test_a_cached_call_reports_the_latency_recorded_when_it_was_live(tmp_path: Path) -> None:
    store = LatencyStore(tmp_path / "lat.json")
    stats = CacheStats(mode="use")
    live = TimedSearch(Stub(stats, hit=False), stats, store, mock=False)
    live.search("dresser")
    assert live.last.live and not live.last.cached and live.live_calls == 1
    recorded = json.loads((tmp_path / "lat.json").read_text())
    assert len(recorded) == 1
    hit = TimedSearch(Stub(stats, hit=True), stats, LatencyStore(tmp_path / "lat.json"), mock=False)
    hit.search("dresser")
    assert (
        hit.last.cached
        and hit.last.seconds == list(recorded.values())[0]
        and hit.last.latency_known
    )
    unknown = TimedSearch(
        Stub(stats, hit=True), stats, LatencyStore(tmp_path / "other.json"), mock=False
    )
    unknown.search("couch")
    assert unknown.last.cached and not unknown.last.latency_known
    assert hit.name == "serper" and hit.thread_safe  # the pipeline reads these through the wrapper


# ---- the cost guard ---------------------------------------------------------------------------


def test_the_spend_guard_stops_past_each_limit() -> None:
    s = Spend()
    s.add(0.29, 149)
    with pytest.raises(HardStop):
        s.add(0.02)
    assert "Anthropic" in s.stopped
    c = Spend()
    with pytest.raises(HardStop):
        c.add(0.0, 151)
    assert "Serper" in c.stopped
    assert (runner.ANTHROPIC_STOP_USD, runner.SERPER_STOP_CREDITS) == (0.30, 150)


def test_a_run_that_hits_the_stop_keeps_what_finished_and_marks_the_rest_not_run() -> None:
    env, _ = make_env("mock")
    spend = Spend(limit_usd=0.0001)
    payload = run_all(load_cases()[:4], env, mock_caller, spend=spend)
    statuses = [c["status"] for c in payload["cases"]]
    assert statuses[0] == "partial" and statuses[1:] == ["not_run"] * 3
    assert payload["stopped"].startswith("Anthropic spend")


def test_a_case_that_raises_is_recorded_and_the_run_goes_on() -> None:
    env, _ = make_env("mock")
    calls = {"n": 0}

    def flaky(**kw: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return mock_caller(**kw)

    payload = run_all(load_cases()[:2], env, flaky)
    assert (
        payload["cases"][0]["status"] == "failed" and "RuntimeError" in payload["cases"][0]["error"]
    )
    assert payload["cases"][1]["status"] == "ok" and payload["cases"][1]["questions"]
    assert payload["stopped"] == ""  # a failed case is not a stop


def test_live_refuses_without_confirmation_and_under_tests_and_ci() -> None:
    assert "EVAL_CLARIFY_CONFIRM=yes" in refuse_live({})
    assert refuse_live({"EVAL_CLARIFY_CONFIRM": "yes", "CI": "1"})
    assert refuse_live({"EVAL_CLARIFY_CONFIRM": "yes", "PYTEST_CURRENT_TEST": "x"})
    assert refuse_live({"EVAL_CLARIFY_CONFIRM": "yes"}) == ""
    # the CLI refuses before it builds an environment or loads a key (checked in the source: a test must
    # never start the live command, and tests/test_eval_shop_runner.py enforces that)
    import inspect

    from evals.shop_clarify import __main__ as cli

    source = inspect.getsource(cli.main)
    assert source.index("refuse_live(") < source.index("load_env()") < source.index("make_env(")


# ---- the plan and the make targets ------------------------------------------------------------


def test_the_plan_lists_the_requests_the_cost_and_the_hash() -> None:
    cases = load_cases()
    text = "\n".join(plan_lines(eval_config("mock"), cases))
    for r in REQUESTS:
        assert r in text
    assert (
        LOCKED_HASH in text
        and "WORST CASE" in text
        and "hard stop $0.30" in text
        and "150 credits" in text
    )
    est = estimate(eval_config("mock"), cases)
    assert 0 < est["anthropic_usd_expected"] < est["anthropic_usd_worst"]
    assert est["serper_credits_worst"] > est["serper_credits_expected"] > 0


def test_the_make_targets_exist_and_live_needs_the_confirmation() -> None:
    mk = (ROOT / "Makefile").read_text()
    for target in ("clarify-plan", "clarify-mock", "clarify-live", "clarify-report"):
        assert re.search(rf"^{target}:", mk, re.M), target
    assert "EVAL_CLARIFY_CONFIRM=$(EVAL_CLARIFY_CONFIRM) APP_LLM_MODE=live" in mk
    assert "clarify-live" in mk.split(".PHONY:")[1].split("\n\n")[0]


# ---- the report writer and the contact sheet --------------------------------------------------


def test_the_report_has_a_verdict_a_summary_table_and_every_request(
    mock_run: dict[str, Any],
) -> None:
    md = render_report(mock_run)
    assert "## Verdict" in md and "## Summary" in md and "MOCK run" in md
    for r in REQUESTS:
        assert f"### {r} (" in md
    assert (
        len(
            re.findall(
                r"^\| (?:dresser|couch|rug|bookshelf|nightstand|desk|modern couch|boho curtains|walnut desk lamp|floor mirror|bed frame|TV stand)",
                md,
                re.M,
            )
        )
        == 12
    )
    s = summarise(mock_run)
    lines = "\n".join(verdict_lines(s, "mock"))
    for target in ("Time to questions", "Pictures", "Question quality", "Effect of answers"):
        assert target in lines
    assert (
        s["measured"] == 12
        and s["picture_coverage"] is not None
        and 0 <= s["picture_coverage"] <= 1
    )


def test_the_verdict_marks_each_target_met_or_not() -> None:
    base: dict[str, Any] = {
        "median_s": 3.0, "max_s": 5.0, "median_parse_s": 1.0, "median_fetch_s": 1.0, "median_model_s": 1.0,
        "median_overlapped_s": 2.0, "picture_coverage": 0.8, "flagged_per_request": 0.5, "mean_lift_points": 2.0,
        "mean_lift_points_all": 1.0, "mean_attribute_lift_points": 3.0, "requests_with_answers": 10, "improved": 7, "worsened": 3,
    }  # fmt: skip
    good = "\n".join(verdict_lines(base, "live"))
    assert good.count("**met**") == 4 and "NOT met" not in good
    bad = "\n".join(
        verdict_lines(
            {
                **base,
                "median_s": 4.5,
                "picture_coverage": 0.5,
                "flagged_per_request": 1.4,
                "mean_lift_points": -1.0,
            },
            "live",
        )
    )
    assert bad.count("**NOT met**") == 4


def test_the_contact_sheet_is_self_contained_escaped_and_shows_no_preference(
    mock_run: dict[str, Any], tmp_path: Path
) -> None:
    sheet = render_contact_sheet(mock_run)
    assert sheet.startswith("<!doctype html>") and "<script" not in sheet and "<link" not in sheet
    n_questions = sum(len(c["questions"]) for c in mock_run["cases"])
    assert sheet.count("No preference") == n_questions and sheet.count("class='q'") == n_questions
    assert "https://mock.example" not in sheet  # placeholders, never a request to a mock host
    evil = json.loads(json.dumps(mock_run))
    evil["cases"][0]["questions"][0]["text"] = "<script>alert(1)</script>"
    evil["cases"][0]["questions"][0]["options"][0]["pick"] = {
        "product_id": "p",
        "title": "t",
        "thumbnail_url": "https://img.example/a.jpg?x=1&y='2'",
        "how": "scan",
    }
    page = render_contact_sheet(evil)
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert "src='https://img.example/a.jpg?x=1&amp;y=&#x27;2&#x27;'" in page
    results = tmp_path / "clarify-mock-x.json"
    results.write_text(json.dumps(mock_run))
    md, html = write_report(results)
    assert (
        md.name == "report.md"
        and html.name == "contact-sheet.html"
        and md.is_file()
        and html.is_file()
    )


def test_results_and_reports_are_gitignored_and_nothing_under_results_is_tracked() -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    ignored = subprocess.run(
        [
            "git",
            "check-ignore",
            "evals/shop_clarify/results/report.md",
            "evals/shop_clarify/results/contact-sheet.html",
            "evals/shop_clarify/results/clarify-live-x.json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert len(ignored.stdout.split()) == 3
    tracked = subprocess.run(
        ["git", "ls-files", "evals/shop_clarify/results"], cwd=ROOT, capture_output=True, text=True
    ).stdout
    assert tracked.strip() == ""


def test_no_thumbnail_url_is_in_any_committed_file() -> None:
    for path in [
        *(ROOT / "evals" / "shop_clarify").glob("*.py"),
        *(ROOT / "evals" / "shop_clarify").glob("*.json"),
        *(ROOT / "evals" / "shop_clarify").glob("*.md"),
        ROOT / "src" / "app" / "shop" / "clarify.py",
        ROOT / "src" / "app" / "shop" / "clarify_verify.py",
        ROOT / "prompts" / "shop_clarify_verify.md",
        ROOT / "prompts" / "shop_clarify_audit.md",
        ROOT / "prompts" / "shop_clarify.md",
    ]:
        if path.is_file():
            assert (
                "gstatic.com" not in path.read_text() and "encrypted-tbn" not in path.read_text()
            ), path


def test_the_clarify_module_is_documented_as_not_part_of_the_pipeline() -> None:
    assert "Nothing in the API" in (clarify_mod.__doc__ or "")
