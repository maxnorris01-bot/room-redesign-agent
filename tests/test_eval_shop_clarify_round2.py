"""Round 2 of the clarify harness: a full mock run with verification, the report fields and the contact
sheet, the timing variants, the plan and the round 2 spend guard. Free; nothing live runs from tests."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from evals.shop.runner import eval_config
from evals.shop_clarify import runner
from evals.shop_clarify.__main__ import main
from evals.shop_clarify.mock import mock_audit_caller, mock_caller, mock_verify_caller
from evals.shop_clarify.plan import estimate_round2, plan_lines_round2
from evals.shop_clarify.report import (
    render_contact_sheet,
    render_report,
    summarise,
    verdict_lines,
    write_report,
)
from evals.shop_clarify.runner import (
    HardStop,
    Options,
    Spend,
    load_saved_questions,
    make_env,
    overlap,
    run_all,
)
from evals.shop_clarify.spec import load_cases

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def run2() -> dict[str, Any]:
    env, _ = make_env("mock")
    options = Options(
        verify=True, variants=True, vision_caller=mock_verify_caller, audit_caller=mock_audit_caller
    )
    return run_all(load_cases(), env, mock_caller, options=options)


def test_a_round_two_mock_run_verifies_audits_prunes_and_measures_the_variants(
    run2: dict[str, Any],
) -> None:
    assert run2["round"] == 2 and run2["verify_mode"] == "batched" and run2["stopped"] == ""
    assert set(run2["prompt_versions"]) == {
        "shop_clarify",
        "shop_clarify_verify",
        "shop_clarify_audit",
    }
    for c in run2["cases"]:
        assert c["status"] == "ok"
        assert set(c["verify"]) >= {
            "calls",
            "cost_usd",
            "seconds",
            "rounds",
            "mode",
            "images_offered",
            "images_failed",
        }
        assert c["audit"]["auditable"] <= c["images"]["with_image"]
        t = c["timing"]
        assert t["pictures_s"] == pytest.approx(t["text_s"] + t["verify_s"], abs=0.01)
        assert c["images"]["options"] <= c["images"]["options_before"]
        assert c["images"]["questions_after"] <= c["images"]["questions_before"]
        assert (
            c["variants"]["overlap_s"] > 0
            and c["variants"]["overlap25_s"] > 0
            and c["variants"]["shown25"] <= 25
        )
        # every shown picture was verified yes (by construction), every option carries its record
        for q in c["questions"]:
            for o in q["options"]:
                assert o["status"] in ("picture", "text_only") and (o["pick"] is not None) == (
                    o["status"] == "picture"
                )
    removed = [o for c in run2["cases"] for o in c["removed"]["options"]]
    assert (
        removed
        and all(o["reason"] for o in removed)
        and {o["status"] for o in removed} <= {"dropped", "text_only"}
    )


def test_the_report_has_the_round_two_numbers_and_targets(run2: dict[str, Any]) -> None:
    s = summarise(run2)
    for key in (
        "picture_accuracy",
        "coverage_after",
        "question_survival",
        "verify_median_s",
        "verify_usd",
        "audit_usd",
        "overlap_median_s",
        "overlap25_median_s",
        "text_median_s",
        "pictures_median_s",
        "removed_reasons",
    ):
        assert key in s, key
    assert s["coverage_after"] == pytest.approx(s["shown_pictures"] / s["options_after"])
    assert 0 <= s["question_survival"] <= 1 and s["questions_after"] <= s["questions_before"]
    text = "\n".join(verdict_lines(s, "mock"))
    for target in (
        "Verified pictures",
        "Picture accuracy (independent audit",
        "Added latency of verification",
        "Pruning:",
        "Cost: verification",
        "Time to questions by variant",
        "MOCK run",
    ):
        assert target in text, target
    md = render_report(run2)
    assert (
        "| text (s) | pictures (s) |" in md
        and "Removed or downgraded:" in md
        and "Independent audit:" in md
        and "Variants: overlap" in md
    )


def test_verdict_marks_the_round_two_targets_met_or_not() -> None:
    base: dict[str, Any] = {
        "round": 2, "median_s": 3.0, "max_s": 5.0, "median_parse_s": 1.0, "median_fetch_s": 1.0, "median_model_s": 1.0, "median_overlapped_s": 2.0,
        "picture_coverage": 0.7, "flagged_per_request": 0.5, "mean_lift_points": 2.0, "mean_lift_points_all": 1.0, "mean_attribute_lift_points": 3.0,
        "requests_with_answers": 10, "improved": 7, "worsened": 3, "audit_agreed": 95, "audit_auditable": 100, "picture_accuracy": 0.95, "shown_pictures": 100,
        "verify_median_s": 2.0, "verify_max_s": 3.5, "text_median_s": 3.0, "text_max_s": 5.0, "pictures_median_s": 5.0, "pictures_max_s": 8.0,
        "questions_after": 45, "questions_before": 50, "question_survival": 0.9, "options_removed": 5, "removed_reasons": {}, "options_before": 120,
        "options_after": 110, "coverage_title_before": 0.8, "verify_usd": 0.02, "verify_calls": 30, "verify_mode": "batched", "verify_images": 150,
        "verify_failed_images": 0, "audit_usd": 0.01, "overlap_median_s": 3.0, "overlap_max_s": 5.0, "overlap25_median_s": 2.5, "overlap25_max_s": 4.0,
        "variants_measured": 12, "variants_skipped": [],
    }  # fmt: skip
    good = "\n".join(verdict_lines(base, "live"))
    assert good.count("**met**") == 8 and "NOT met" not in good
    bad = "\n".join(
        verdict_lines(
            {
                **base,
                "picture_accuracy": 0.8,
                "picture_coverage": 0.5,
                "verify_median_s": 4.0,
                "question_survival": 0.7,
                "overlap25_median_s": 4.5,
            },
            "live",
        )
    )
    assert bad.count("**NOT met**") == 5


def test_the_contact_sheet_shows_text_only_cards_a_slim_no_preference_chip_and_the_verdicts_in_checks(
    run2: dict[str, Any],
) -> None:
    sheet = render_contact_sheet(run2)
    assert (
        "class='opt textonly'" in sheet
    )  # a clean text card for an option with no verified picture
    assert "class='opt none'" not in sheet and sheet.count(
        "<span class='chip'>No preference</span>"
    ) == sum(len(c["questions"]) for c in run2["cases"])
    assert (
        "verifier:" in sheet
        and "audit saw" in sheet
        and "removed or downgraded by verification" in sheet
    )
    # the verdicts are inside the collapsed block, not on the card itself
    first_card = sheet[sheet.index("class='card") : sheet.index("</details>")]
    assert "<details><summary>checks</summary>" in first_card and first_card.index(
        "verifier:"
    ) > first_card.index("<details>")
    assert "<script" not in sheet and "https://mock.example" not in sheet


def test_a_text_only_option_is_a_compact_card_not_a_big_empty_one() -> None:
    from evals.shop_clarify.report import CSS

    rule = CSS[CSS.index(".opt.textonly{") : CSS.index("}", CSS.index(".opt.textonly{")) + 1]
    assert "width:auto" in rule and "min-width" in rule
    assert ".opt.textonly .lab{padding:10px 14px}" in CSS and ".chips{" in CSS


def test_write_report_writes_both_files(run2: dict[str, Any], tmp_path: Path) -> None:
    results = tmp_path / "clarify-mock-x.json"
    results.write_text(json.dumps(run2))
    md, html = write_report(results)
    assert "Picture accuracy" in md.read_text() and html.read_text().startswith("<!doctype html>")


# ---- the timing variants ----------------------------------------------------------------------


def test_overlap_is_word_overlap_and_the_parsed_query_is_searched_again_only_when_it_differs() -> (
    None
):
    assert overlap("modern couch", "modern couches") == 1.0
    assert overlap("boho curtains", "bohemian curtain panels") < runner.MATERIALLY_DIFFERENT
    assert overlap("dresser", "dresser") == 1.0 and overlap("", "") == 1.0


def test_the_variants_use_the_raw_request_in_a_real_thread_and_the_small_call_shows_25_listings(
    run2: dict[str, Any],
) -> None:
    by = {c["id"]: c for c in run2["cases"]}
    v = by["cl-07"][
        "variants"
    ]  # "modern couch under $1000": the price words are stripped from the raw text
    assert (
        v["raw_text"] == "modern couch" and v["overlap_s"] >= v["raw_fetch_s"] and v["wall_s"] >= 0
    )
    assert all(
        c["variants"]["shown25"] == 20 or c["variants"]["shown25"] <= 25 for c in run2["cases"]
    )


def test_a_raw_request_that_was_never_saved_skips_the_variants_not_the_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shopping import ProviderError

    real = runner.TimedSearch.search
    calls = {"n": 0}

    def search(self: Any, query: str, gl: str = "us", hl: str = "en", page: int = 1) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:  # the raw-request fetch (the first is the parsed query's)
            raise ProviderError("serper", "not in the saved responses (replay mode)")
        return real(self, query, gl, hl, page)

    monkeypatch.setattr(runner.TimedSearch, "search", search)
    env, _ = make_env("mock")
    options = Options(
        verify=True,
        variants=True,
        vision_caller=mock_verify_caller,
        audit_caller=mock_audit_caller,
        skip_arms=True,
    )
    payload = run_all(load_cases()[:1], env, mock_caller, options=options)
    c = payload["cases"][0]
    assert c["status"] == "ok" and "error" in c["variants"] and c["verify"]["calls"] >= 1


def test_a_pipeline_call_that_was_never_saved_marks_that_arm_not_the_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shopping import ProviderError

    def boom(*a: Any, **k: Any) -> Any:
        raise ProviderError("serper", "not in the saved responses (replay mode)")

    monkeypatch.setattr(runner, "run_query", boom)
    env, _ = make_env("mock")
    payload = run_all(
        load_cases()[:1],
        env,
        mock_caller,
        options=Options(
            verify=True, vision_caller=mock_verify_caller, audit_caller=mock_audit_caller
        ),
    )
    c = payload["cases"][0]
    assert c["status"] == "ok" and "not in the saved" in c["arms"]["baseline"]["error"]
    assert "Serper" not in c["error"] if c["error"] else True


def test_the_arms_can_use_the_questions_saved_by_the_first_round(
    tmp_path: Path, run2: dict[str, Any]
) -> None:
    saved = tmp_path / "clarify-live-x.json"
    saved.write_text(json.dumps(run2))
    questions = load_saved_questions(saved)
    assert set(questions) == {c["id"] for c in run2["cases"]}
    first = questions["cl-01"][0]
    assert first.options and first.options[0].evidence_product_ids == []
    env, _ = make_env("mock")
    payload = run_all(
        load_cases()[:1], env, mock_caller,
        options=Options(verify=True, vision_caller=mock_verify_caller, audit_caller=mock_audit_caller, arms_questions=questions),
    )  # fmt: skip
    assert payload["cases"][0]["status"] == "ok"


# ---- the plan, the guard and the commands -----------------------------------------------------


def test_the_round_two_plan_prints_the_estimate_and_the_stops() -> None:
    cases = load_cases()
    text = "\n".join(plan_lines_round2(eval_config("mock"), cases))
    assert (
        "ROUND 2" in text
        and "WORST CASE" in text
        and "hard stop $0.15" in text
        and "0 new credits" in text
    )
    assert "batched" in text and "audit" in text and "CLARIFY_VERIFY_MODE=single" in text
    est = estimate_round2(eval_config("mock"), cases)
    assert 0 < est["anthropic_usd_expected"] < est["anthropic_usd_worst"]
    assert (
        est["verify_usd_single"] > est["verify_usd_batched"] > 0
        and est["stop_usd"] == 0.15
        and est["stop_credits"] == 0
    )
    assert (
        estimate_round2(eval_config("mock"), cases, "single")["verify_usd"]
        == est["verify_usd_single"]
    )


def test_the_round_two_guard_stops_at_fifteen_cents_and_at_any_credit() -> None:
    assert (runner.ROUND2_STOP_USD, runner.ROUND2_STOP_CREDITS) == (0.15, 0)
    s = Spend(limit_usd=runner.ROUND2_STOP_USD, limit_credits=runner.ROUND2_STOP_CREDITS)
    s.add(0.14)
    with pytest.raises(HardStop):
        s.add(0.02)
    c = Spend(limit_usd=0.15, limit_credits=0)
    with pytest.raises(HardStop):
        c.add(0.0, 2)
    assert Spend().limit_usd == 0.30 and Spend().limit_credits == 150  # round 1 is as it was


def test_the_cli_plans_round_two_by_default_and_round_one_on_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["plan"]) == 0
    assert "ROUND 2" in capsys.readouterr().out
    assert main(["plan", "--round", "1"]) == 0
    assert "Clarify spike plan:" in capsys.readouterr().out


def test_live_replays_serper_only_and_has_the_round_two_stops() -> None:
    import inspect

    from evals.shop_clarify import __main__ as cli

    source = inspect.getsource(cli.main)
    assert '"replay"' in source and "ROUND2_STOP_USD" in source and "ROUND2_STOP_CREDITS" in source
    mk = (ROOT / "Makefile").read_text()
    assert re.search(
        r"^clarify-live:\n\tEVAL_CLARIFY_CONFIRM=\$\(EVAL_CLARIFY_CONFIRM\) APP_LLM_MODE=live",
        mk,
        re.M,
    )
