"""Shop eval, phase 4: one full mock run, the report, and the docs that go with the harness."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from evals.shop import report
from evals.shop.__main__ import main
from evals.shop.runner import build_env, run_all, write_results
from evals.shop.spec import load_cases, thresholds_sha256

ROOT = Path(__file__).resolve().parents[1]
SHOP = ROOT / "evals" / "shop"


@pytest.fixture(scope="module")
def full_run() -> dict[str, Any]:
    return run_all(load_cases(), build_env("mock"))


def test_a_full_mock_run_exercises_all_twelve_cases(full_run: dict[str, Any]) -> None:
    cases = full_run["cases"]
    assert [c["status"] for c in cases] == ["ok"] * 12
    assert all(len(c["rounds"]) == 3 for c in cases)
    assert full_run["verdict"]["cases_not_ok"] == [] and full_run["stopped"] == ""
    assert full_run["mode"] == "mock" and full_run["thresholds_sha256"] == thresholds_sha256()
    keys = {c["key"] for c in full_run["checks"]}
    assert len(keys) == len(full_run["checks"]) == 22  # every threshold is evaluated
    assert full_run["totals"]["anthropic_usd"] < full_run["thresholds"]["hard_stop_anthropic_usd"]
    assert full_run["totals"]["serper_credits"] < full_run["thresholds"]["hard_stop_serper_credits"]
    assert len(full_run["category_findings"]) == 4
    assert [c["id"] for c in cases if c.get("refine_step")] == ["case-02", "case-10"]
    assert all(c["repeats"]["count"] == 0 for c in cases)  # seen memory holds in every case


def test_the_mock_run_uses_the_fixtures_and_the_generic_catalogue(full_run: dict[str, Any]) -> None:
    by = {c["id"]: c for c in full_run["cases"]}
    fixture_titles = by["case-01"]["rounds"][0]["items"][0]["title"]
    assert fixture_titles.startswith("[FIXTURE]")
    generic = by["case-07"]["rounds"][0]["items"]
    assert generic and all(i["title"].startswith("[FIXTURE]") for i in generic)
    assert all("lamp" in i["title"].lower() for i in generic[:5])  # the product asked for
    for case in full_run["cases"]:
        for rd in case["rounds"]:
            assert rd["category"]["failed"] == 0 and rd["persona"]["failed"] == 0


def test_every_case_scores_the_price_it_parsed(full_run: dict[str, Any]) -> None:
    by = {c["id"]: c for c in full_run["cases"]}
    for cid in ("case-01", "case-04", "case-05", "case-06", "case-10"):
        for rd in by[cid]["rounds"]:
            assert rd["price"]["share"] == 1.0, (cid, rd["round"])
            assert all(i["price"] is not None for i in rd["items"])


def test_the_cli_runs_the_mock_eval_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    written: list[Path] = []

    def write(payload: dict[str, Any]) -> Path:
        path = write_results(payload, tmp_path)
        written.append(path)
        return path

    monkeypatch.setattr("evals.shop.__main__.write_results", write)
    assert main(["mock", "--case", "case-07", "--case", "case-09"]) == 0
    out = capsys.readouterr().out
    assert "case-07" in out and "case-09" in out and "case-08" not in out
    data = json.loads(written[0].read_text())
    assert [c["id"] for c in data["cases"]] == ["case-07", "case-09"]
    assert main(["mock", "--case", "case-99"]) == 2


def test_the_report_has_a_row_per_case_and_a_line_per_threshold(full_run: dict[str, Any]) -> None:
    text = report.render(full_run)
    assert text.startswith("# Shop eval report: mock run ")
    assert "Mock run: fixtures and deterministic stand-in judges" in text
    assert thresholds_sha256() in text
    cases_part = text.split("## Cases")[1].split("## Shop Again lift")[0]
    rows = [ln for ln in cases_part.splitlines() if re.match(r"\| case-\d\d \|", ln)]
    assert len(rows) == 12
    table = text.split("## Thresholds")[1].split("## Cases")[0]
    assert len([ln for ln in table.splitlines() if ln.startswith("| ") and "---" not in ln]) == 23
    for section in ("## Shop Again lift", "## Category coverage findings", "## Refine steps",
                    "## Cost and time", "## Limits of this eval"):  # fmt: skip
        assert section in text
    assert "same model family" in text and "re-run it once" in text
    assert "desk chairs" in text and "folds into another category" in text
    assert re.search(r"Round 3 minus round 1 \(no gate\)", text)


def test_the_report_has_no_thumbnails_or_links_and_cuts_titles(full_run: dict[str, Any]) -> None:
    text = report.render(full_run)
    assert not re.search(r"https?://|www\.|mock\.example", text) and "](" not in text
    assert "thumbnail_url" not in text
    assert report.cut("x" * 200) == "x" * 59 + "…" and len(report.cut("x" * 200)) == 60
    long_title = dict(full_run)
    long_title["cases"] = [dict(full_run["cases"][0], request="r" * 300)]
    assert "r" * 61 not in report.render(long_title)


def test_the_report_marks_a_failure_a_stop_and_a_case_that_did_not_finish() -> None:
    out = run_all(load_cases()[:3], build_env("mock"))
    out["stopped"] = "Anthropic spend $0.7000 passed the hard stop $0.60"
    out["verdict"] = {
        "status": "incomplete",
        "failed_checks": [],
        "cases_not_ok": [],
        "stopped": out["stopped"],
    }
    out["cases"][2] = {"id": "case-03", "request": "boho curtains", "kind": "context",
                       "status": "not_run", "error": out["stopped"], "rounds": []}  # fmt: skip
    text = report.render(out)
    assert "Verdict: **incomplete** (stopped: Anthropic spend" in text
    assert "Cases not completed:" in text and "case-03: not_run" in text
    assert "FAIL" in text or "no data" in text
    assert (
        report.mark(True) == "pass"
        and report.mark(False) == "FAIL"
        and report.mark(None) == "no data"
    )


def test_the_report_command_renders_a_results_file(
    tmp_path: Path, full_run: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_results(full_run, tmp_path)
    assert main(["report", str(path)]) == 0
    assert capsys.readouterr().out.startswith("# Shop eval report:")
    out = tmp_path / "report.md"
    assert main(["report", str(path), "--out", str(out)]) == 0
    assert out.read_text().startswith("# Shop eval report:")
    assert report.render_file(path) == report.render(json.loads(path.read_text()))


def test_the_known_instability_file_and_readme_say_what_matters() -> None:
    text = (SHOP / "known_instability.md").read_text()
    assert "Re-run that case once" in text and "Live search results change" in text
    assert "judges are models" in text and "Never edit `thresholds.json`" in text
    readme = (SHOP / "README.md").read_text()
    for target in ("eval-shop-plan", "eval-shop-mock", "eval-shop-live", "eval-shop-report"):
        assert target in readme
    assert "EVAL_SHOP_CONFIRM=yes" in readme and "same model family" in readme or "Haiku" in readme


def test_the_docs_record_the_judge_limits_and_the_calibration_step() -> None:
    todo = (ROOT / "docs" / "todo.md").read_text()
    assert "calibrat" in todo.lower() and "judge agreement" in todo.lower()
    adr = (ROOT / "docs" / "adr" / "0012-two-modes-shop-first.md").read_text()
    assert "stage 3" in adr.lower() and "what it cannot" in adr.lower()
    assert "evals/shop" in (ROOT / "SPEC.md").read_text()
