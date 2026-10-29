"""Shop eval, phase 3: the runner (rounds, ratings, guards, isolation), the CLI's refusals, the
plan, the results file and the Makefile targets. Mock mode only; the live eval never runs here."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from evals.shop import runner
from evals.shop.__main__ import CONFIRM_VAR, main, refuse_live
from evals.shop.plan import estimate, plan_lines
from evals.shop.runner import build_env, eval_config, run_all, run_case, write_results
from evals.shop.spec import load_cases, load_thresholds, thresholds_sha256

from app import llm
from app.mock_llm import MockAnthropicClient
from app.shop.service import ShopService

ROOT = Path(__file__).resolve().parents[1]
CASES = {c.id: c for c in load_cases()}


class Recorder:
    """The mock model, recording every request (to prove what the pipeline was and was not sent)."""

    def __init__(self) -> None:
        self.inner = MockAnthropicClient()
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.inner._create(**kwargs)


def text_of(kwargs: dict[str, Any]) -> str:
    content = kwargs["messages"][0]["content"]
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if isinstance(b, dict))


def is_judge(kwargs: dict[str, Any]) -> bool:
    return (kwargs.get("tool_choice") or {}).get("name", "").endswith("_judgements")


def test_the_eval_config_is_mock_with_lookups_and_gallery_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_LLM_MODE", "live")  # a stray live setting cannot make a mock run live
    cfg = eval_config("mock")
    assert cfg.llm_mode == "mock" and cfg.shop_auto_lookups is False and cfg.shop_gallery is False
    assert build_env("mock").config.llm_mode == "mock"


def test_a_case_runs_three_rounds_and_records_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    env = build_env("mock")
    result = run_case(CASES["case-04"], env)
    assert result["status"] == "ok" and result["error"] is None
    assert [r["round"] for r in result["rounds"]] == [1, 2, 3]
    assert [r["kind"] for r in result["rounds"]] == ["search", "shop_again", "shop_again"]
    first = result["rounds"][0]
    for key in ("price", "category", "attributes", "persona", "sellers", "near_dups", "footer",
                "trace", "cost", "items", "seconds"):  # fmt: skip
        assert key in first
    assert first["footer"].startswith("Search took") and "Serper calls" in first["footer"]
    assert result["rounds"][1]["footer"].startswith("Shop Again took")
    assert result["repeats"] == {"count": 0, "ids": []}
    assert set(result["lift"]) == {"r2_minus_r1", "r3_minus_r1", "rates"}
    assert result["cost"]["pipeline_usd"] > 0 and result["cost"]["judge_usd"] > 0
    assert result["cost"]["serper_credits"] > 0 and result["wall_seconds"] >= 0


def test_the_persona_rates_everything_shown_but_submits_only_the_first_twelve() -> None:
    env = build_env("mock")
    result = run_case(CASES["case-04"], env)
    r1, r2, r3 = result["rounds"]
    assert r1["count"] > 12 and r1["persona"]["scored"] == r1["count"]  # all judged
    assert r1["ratings_submitted"] == 12 and r2["ratings_submitted"] <= 12
    assert r3["ratings_submitted"] == 0  # round 3 is only measured
    rated = [i for i in r1["items"] if i["rated"]]
    assert [i["id"] for i in rated] == [i["id"] for i in r1["items"][:12]]  # display order
    assert all(i["rated"] == i["persona"] for i in rated)
    assert all(i["rated"] is None for i in r1["items"][12:])


def test_the_pipeline_never_receives_the_persona_and_the_judges_never_get_pipeline_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = Recorder()
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: rec)
    case = CASES["case-02"]
    run_case(case, build_env("mock"))
    persona_words = [w for w in re.findall(r"[a-z]+", case.persona.text.lower()) if len(w) > 6]
    pipeline = [c for c in rec.calls if not is_judge(c)]
    judges = [c for c in rec.calls if is_judge(c)]
    assert pipeline and judges
    for call in pipeline:
        text = text_of(call).lower()
        assert case.persona.text not in text
        assert "persona" not in text
        # ratings reach the pipeline as ratings, never as the persona's words
        assert not any(
            sentence in text for sentence in re.split(r"(?<=\.)\s", case.persona.text.lower())
        )
    assert any(case.request in text_of(c) for c in pipeline)
    assert any("no Walmart" in text_of(c) for c in pipeline)  # the refine text is a pipeline input
    for call in judges:
        text = re.sub(r"<persona>.*?</persona>", "", text_of(call).lower(), flags=re.DOTALL)
        for banned in ("converged", "leaning", "profile", "chip", "rank", "alignment"):
            assert banned not in text
    del persona_words


def test_the_pipeline_is_called_only_through_the_service_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, list[Any]] = {"search": [], "rate": [], "refine": [], "again": []}
    orig = {n: getattr(ShopService, n) for n in ("search", "rate", "refine", "shop_again")}

    def spy(name: str, key: str) -> Any:
        def inner(self: Any, *a: Any, **kw: Any) -> Any:
            seen[key].append((a, kw))
            return orig[name](self, *a, **kw)

        return inner

    for name, key in (
        ("search", "search"),
        ("rate", "rate"),
        ("refine", "refine"),
        ("shop_again", "again"),
    ):
        monkeypatch.setattr(ShopService, name, spy(name, key))
    case = CASES["case-02"]
    run_case(case, build_env("mock"))
    assert [a[1] for a, _ in seen["search"]] == [case.request]
    assert [a[1] for a, _ in seen["refine"]] == ["no Walmart"]
    assert len(seen["again"]) == 2
    assert all(
        len(a) == 3 and not kw for a, kw in seen["rate"]
    )  # (session, id, rating): no comment
    assert {a[2] for a, _ in seen["rate"]} <= {"love", "like", "dislike"}


def test_no_lookup_open_or_gallery_call_is_made(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: Any, *a: Any, **kw: Any) -> Any:
        raise AssertionError("the eval must not call this")

    monkeypatch.setattr(ShopService, "resolve", boom)
    monkeypatch.setattr(ShopService, "photos", boom)
    env = build_env("mock")
    lookups: list[Any] = []
    real = env.make_providers

    def providers(case: Any) -> Any:
        search, lookup = real(case)
        lookups.append(lookup)
        return search, lookup

    env.make_providers = providers
    result = run_case(CASES["case-07"], env)
    assert result["status"] == "ok"
    assert all(lk.product_ids == [] for lk in lookups)


def test_the_refine_steps_are_applied_before_round_two_and_scored() -> None:
    env = build_env("mock")
    two = run_case(CASES["case-02"], env)
    assert two["refine_step"]["accepted"] is True and two["refine_step"]["text"] == "no Walmart"
    assert two["rounds"][0]["refine"] is None
    for rd in two["rounds"][1:]:
        assert rd["refine"]["exclusion"]["share"] == 1.0  # nothing from Walmart after the step
    ten = run_case(CASES["case-10"], env)
    assert ten["refine_step"]["text"] == "focus on mesh backs"
    assert "focus" in ten["rounds"][1]["refine"] and "supply" in ten["rounds"][1]["refine"]["focus"]


def test_context_and_contrast_cases_record_their_extra_scores() -> None:
    env = build_env("mock")
    curtains = run_case(CASES["case-03"], env)
    assert "leakage" in curtains["context"] and curtains["rounds"][0]["avoid"] is not None
    assert curtains["context"]["leakage"]["count"] == 0  # the setting never reaches the search text
    navy = run_case(CASES["case-12"], env)
    assert navy["rounds"][0]["product_color"] is not None
    assert navy["rounds"][0]["product_color"]["share"] >= 0.7  # navy is the product here


def test_a_case_that_errors_is_recorded_and_the_run_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orig = ShopService.search

    def search(self: Any, session_id: Any, request: Any) -> Any:
        if request == CASES["case-01"].request:
            raise RuntimeError("provider exploded")
        return orig(self, session_id, request)

    monkeypatch.setattr(ShopService, "search", search)
    cases = [CASES["case-01"], CASES["case-07"]]
    out = run_all(cases, build_env("mock"))
    first, second = out["cases"]
    assert first["status"] == "failed" and "provider exploded" in first["error"]
    assert second["status"] == "ok"
    assert out["verdict"]["status"] == "fail" and out["verdict"]["cases_not_ok"] == ["case-01"]


def test_an_anthropic_spend_stop_writes_partial_results_with_the_reason(tmp_path: Path) -> None:
    env = build_env("mock")
    env.state.thresholds = env.state.thresholds.model_copy(
        update={"hard_stop_anthropic_usd": 0.0005}
    )
    cases = [CASES["case-07"], CASES["case-08"], CASES["case-09"]]
    out = run_all(cases, env)
    assert out["verdict"]["status"] == "incomplete"
    assert "Anthropic spend" in out["stopped"] and "hard stop" in out["stopped"]
    statuses = [c["status"] for c in out["cases"]]
    assert statuses[0] == "partial" and statuses[1:] == ["not_run", "not_run"]
    assert out["cases"][1]["error"] == out["stopped"]
    path = write_results(out, tmp_path)
    assert json.loads(path.read_text())["stopped"] == out["stopped"]


def test_a_serper_credit_stop_works_the_same_way() -> None:
    env = build_env("mock")
    env.state.thresholds = env.state.thresholds.model_copy(update={"hard_stop_serper_credits": 3})
    out = run_all([CASES["case-07"], CASES["case-08"]], env)
    assert out["verdict"]["status"] == "incomplete" and "Serper credits" in out["stopped"]
    assert [c["status"] for c in out["cases"]] == ["partial", "not_run"]
    assert out["totals"]["serper_credits"] > 3


def test_the_results_file_carries_the_hash_versions_and_a_run_id(tmp_path: Path) -> None:
    out = run_all([CASES["case-07"]], build_env("mock"))
    path = write_results(out, tmp_path)
    data = json.loads(path.read_text())
    assert data["thresholds_sha256"] == thresholds_sha256() == out["thresholds_sha256"]
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", data["run_id"])
    assert path.name == f"shop-mock-{data['run_id']}.json"
    assert data["prompt_versions"]["eval_shop_category_judge"] == "2"
    assert data["prompt_versions"]["shop_prefs"] and data["judge_versions"] == {
        "category": "2",
        "persona": "1",
    }
    assert data["thresholds"] == load_thresholds().model_dump()
    assert data["started_at"] <= data["finished_at"] and data["mode"] == "mock"
    ids = {run_all([CASES["case-07"]], build_env("mock"))["run_id"] for _ in range(3)}
    assert len(ids) == 3  # not derived from git or the clock alone
    assert "evals/shop/results/" in (ROOT / ".gitignore").read_text().splitlines()


# ---- the CLI, the plan and the Makefile -----------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "refused"),
    [
        ({}, True),
        ({CONFIRM_VAR: "no"}, True),
        ({CONFIRM_VAR: "yes"}, False),
        ({CONFIRM_VAR: "yes", "CI": "true"}, True),
        ({CONFIRM_VAR: "yes", "PYTEST_CURRENT_TEST": "x"}, True),
    ],
)
def test_the_live_eval_needs_the_confirm_flag_and_never_runs_in_ci_or_tests(
    environ: dict[str, str], refused: bool
) -> None:
    assert bool(refuse_live(environ)) is refused


def test_live_refuses_without_the_flag_and_even_with_it_under_pytest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise AssertionError("a live run must not start")

    monkeypatch.setattr("evals.shop.__main__.run_all", boom)
    monkeypatch.setattr("evals.shop.__main__.build_env", boom)
    monkeypatch.delenv(CONFIRM_VAR, raising=False)
    assert main(["live"]) == 2
    err = capsys.readouterr()
    assert "REFUSED" in err.err and "Estimated live cost" in err.out  # the estimate comes first
    monkeypatch.setenv(CONFIRM_VAR, "yes")
    assert main(["live"]) == 2  # PYTEST_CURRENT_TEST is set: still refused
    assert "never runs from CI or from tests" in capsys.readouterr().err


def test_the_plan_lists_everything_and_spends_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise AssertionError("the plan must not run anything")

    monkeypatch.setattr("evals.shop.__main__.run_all", boom)
    monkeypatch.setattr(llm, "get_client", boom)
    assert main(["plan"]) == 0
    out = capsys.readouterr().out
    assert thresholds_sha256() in out
    for case in CASES.values():
        assert case.id in out and case.request in out and case.persona.text in out
    assert "Category coverage findings" in out and "case-10" in out
    assert "Anthropic $" in out and "Serper" in out and "credits" in out and "hard stops" in out


def test_the_estimate_uses_the_measured_full_run() -> None:
    from evals.shop import plan as pl

    cfg = eval_config("mock")
    t = load_thresholds()
    est = estimate(cfg, list(CASES.values()), t)
    assert est["cases"] == 12 and est["judge_calls"] == 12 * 12
    # the first full 12-case run after 2p and 2q is the base (writer and photo calls included)
    assert pl.MEASURED_PIPELINE_USD_PER_CASE == 0.0325 and pl.MEASURED_CREDITS_PER_CASE == 19
    assert pl.MEASURED_WORST_CASE_PIPELINE_USD == 0.053 and pl.MEASURED_JUDGE_CALL_USD == 0.00045
    assert est["judge_usd"] == pytest.approx(144 * 0.00045, abs=1e-4)
    assert est["per_case_pipeline_usd"] == pytest.approx(0.0325, abs=1e-4)  # the threshold's figure
    assert est["serper_credits_expected"] == 12 * 19
    assert est["total_usd_cached"] == est["anthropic_usd"]  # every Serper call a cache hit
    assert (
        est["serper_credits_worst"]
        == 12 * (cfg.shop_again_max_serper_calls + 2 * 9) * cfg.serper_credits_per_call
    )
    assert est["anthropic_usd"] == pytest.approx(
        est["pipeline_anthropic_usd"] + est["judge_usd"], abs=1e-4
    )
    assert est["within_credit_stop"] is (est["serper_credits_worst"] <= t.hard_stop_serper_credits)
    assert est["within_anthropic_stop"] is True
    assert est["per_case_pipeline_usd"] < t.cost_per_request_usd_max
    assert est["worst_measured_case_usd"] > t.cost_per_request_usd_max  # case-02 cost 0.053
    assert est["assumptions"] and any("full 12-case run" in a for a in est["assumptions"])


def test_the_estimate_gives_one_shop_again_and_a_five_case_rerun() -> None:
    cfg = eval_config("mock")
    t = load_thresholds()
    cases = list(CASES.values())
    est = estimate(cfg, cases, t)
    assert 0 < est["per_shop_again_anthropic_usd"] < 0.01 and est["per_shop_again_credits"] == 6.3
    assert est["per_shop_again_usd"] > est["per_shop_again_anthropic_usd"]
    five = estimate(cfg, cases[:5], t)
    assert (
        est["rerun_5_cases_usd"] == five["rerun_5_cases_usd"]
    )  # a re-run price, whatever the list
    assert est["rerun_5_cases_credits"] == 5 * 19
    assert (
        five["cases"] == 5
        and five["judge_calls"] == 5 * 12
        and five["serper_credits_expected"] < est["serper_credits_expected"]
    )
    lines = "\n".join(plan_lines(cfg, cases, t))
    assert (
        "one Shop Again about $" in lines
        and "a 5-case re-run about $" in lines
        and "CASES=" in lines
    )
    assert "hard stops" in lines


def test_the_plan_and_the_runs_take_a_list_of_cases(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plan", "--case", "case-04,case-06"]) == 0
    out = capsys.readouterr().out
    assert (
        "case-04" in out
        and "case-06" in out
        and "case-05" not in out
        and "Shop eval plan: 2 cases" in out
    )
    assert main(["plan", "--case", "case-99"]) == 2


def test_the_makefile_targets_force_mock_and_pass_the_confirm_flag_explicitly() -> None:
    make = (ROOT / "Makefile").read_text()
    for target in ("eval-shop-plan", "eval-shop-mock", "eval-shop-live", "eval-shop-report"):
        assert re.search(rf"^{target}:", make, flags=re.MULTILINE), target
        assert target in " ".join(make.splitlines()[:4])  # in .PHONY
    assert (
        "APP_LLM_MODE=mock uv run python -m evals.shop plan $(if $(CASES),--case $(CASES),)" in make
    )
    assert "evals.shop mock $(if $(CASES),--case $(CASES),) $(ARGS)" in make
    assert (
        "EVAL_SHOP_CONFIRM=$(EVAL_SHOP_CONFIRM) APP_LLM_MODE=live uv run python -m evals.shop live"
        in make
    )
    assert "eval-fast:\n\tAPP_LLM_MODE=mock uv run python -m evals.run --tier fast" in make
    assert "sleep" not in make and "pkill" not in make


def test_no_test_or_script_starts_a_live_eval() -> None:
    for path in (ROOT / "tests").glob("*.py"):
        text = path.read_text()
        if path.name == "test_eval_shop_runner.py":
            continue
        assert 'main(["live"' not in text and "evals.shop live" not in text, path.name
    assert runner.RATED_PER_ROUND == 12 and runner.ROUNDS == 3


def test_the_saved_trace_keeps_what_the_pipeline_learned_and_nothing_third_party() -> None:
    """The first live run saved too little of the trace to see which chips existed in each round;
    the runner now keeps a compact `learned`, `request` and `pool` block (no titles, no links)."""
    result = run_case(CASES["case-02"], build_env("mock"))
    first, second = result["rounds"][0]["trace"], result["rounds"][1]["trace"]
    assert set(first) >= {"duplicates", "refine_chips", "warnings", "request", "learned", "pool"}
    learned = second["learned"]
    assert set(learned) >= {
        "alignment",
        "alignment_counted",
        "chips",
        "stated",
        "dimensions",
        "marketplace",
    }
    assert learned["alignment"]["signals"] >= 0 and "converged" in learned["alignment_counted"]
    assert (
        {"kind", "text", "status", "dimension", "values"} == set(learned["chips"][0])
        if learned["chips"]
        else True
    )
    assert "shape" in learned["dimensions"] and "decision" in learned["dimensions"]["shape"]
    assert [(c["label"].lower(), c["kind"]) for c in second["refine_chips"]] == [
        ("no walmart", "exclude_seller")
    ]
    dump = json.dumps([r["trace"] for r in result["rounds"]])
    assert "http" not in dump and "FIXTURE" not in dump  # no titles, no links


def test_the_footer_line_says_none_yet_when_nothing_was_decided_like_the_page() -> None:
    from evals.shop.runner import footer_line

    base = {"kind": "shop_again", "seconds": {"total": 1.0}, "serper_calls": 1, "serper_credits": 2,
            "provider_usd": 0.002, "anthropic_usd": 0.001, "total_usd": 0.003}  # fmt: skip
    none = footer_line({"search": {**base, "alignment": {"matches": 0, "total": 24, "signals": 0}}})
    some = footer_line({"search": {**base, "alignment": {"matches": 0, "total": 24, "signals": 2}}})
    assert none.endswith("matches your preferences: none yet")
    assert some.endswith("matches your preferences: 0 of 24")
