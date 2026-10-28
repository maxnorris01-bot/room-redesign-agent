"""Shop eval: the rejudge command (persona judge only, on the batches saved in a results file).
A fake judge and mock results: nothing live runs from here."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from evals.shop import rejudge as rj
from evals.shop.__main__ import CONFIRM_VAR, main
from evals.shop.judges import BATCH, JudgeItem, JudgeStats, PersonaVerdict
from evals.shop.plan import judge_call_usd
from evals.shop.runner import build_env, run_all, write_results
from evals.shop.spec import load_cases

from app.config import Config

CASES = {c.id: c for c in load_cases()}
ROOT = Path(__file__).resolve().parents[1]


def saved(n: int, answers: list[str | None]) -> list[dict[str, Any]]:
    return [
        {"id": f"r{i}", "title": f"Item {i}", "price": 10.0, "seller": "IKEA", "thumbnail_url": "",
         "persona": answers[i % len(answers)]}
        for i in range(n)
    ]  # fmt: skip


def results_file(
    rounds: dict[tuple[str, int], list[dict[str, Any]]], mode: str = "live"
) -> dict[str, Any]:
    cases: dict[str, dict[str, Any]] = {}
    for (cid, rnd), items in rounds.items():
        cases.setdefault(cid, {"id": cid, "rounds": []})["rounds"].append(
            {"round": rnd, "items": items}
        )
    return {"run_id": "20261027-000000-abcdef", "mode": mode, "cases": list(cases.values())}


class FakeJudge:
    """Answers from a script (a list per call), so agreement is computed on known numbers."""

    def __init__(self, scripts: list[list[str | None]]) -> None:
        self.scripts = list(scripts)
        self.stats = JudgeStats()
        self.versions = {"category": "1", "persona": "1"}
        self.seen: list[list[JudgeItem]] = []

    def persona(self, case: Any, items: list[JudgeItem]) -> dict[int, PersonaVerdict | None]:
        self.seen.append(items)
        script = self.scripts.pop(0)
        self.stats.calls += 1
        return {
            i.index: (PersonaVerdict(index=i.index, answer=script[n]) if script[n] else None)  # type: ignore[arg-type]
            for n, i in enumerate(items)
        }


def test_compare_counts_three_way_and_match_agreement() -> None:
    old = ["love", "like", "dislike", None, "dislike"]
    new = ["like", "like", "love", "dislike", "dislike"]
    got = rj.compare(old, new)
    assert got["compared"] == 4 and got["failed"] == 1  # the item the old run failed on is left out
    assert got["raw_agree"] == 2 and got["raw_agreement"] == 0.5  # like/like and dislike/dislike
    assert got["match_agree"] == 3 and got["match_agreement"] == 0.75  # love/like agree as a match
    assert got["old_match_rate"] == pytest.approx(2 / 4) and got["new_match_rate"] == pytest.approx(
        3 / 4
    )
    assert got["match_rate_diff_points"] == 25.0
    assert rj.compare([None], ["like"])["raw_agreement"] is None


def test_the_same_answers_agree_completely_and_a_flip_costs_points() -> None:
    same = ["like", "dislike"] * 6
    assert rj.compare(same, same)["match_agreement"] == 1.0
    flipped = rj.compare(["like"] * 12, ["dislike"] * 6 + ["like"] * 6)
    assert flipped["match_rate_diff_points"] == -50.0 and flipped["raw_agreement"] == 0.5


def test_select_batches_filters_rounds_and_cases_and_skips_empty_ones() -> None:
    data = results_file(
        {("case-01", 1): saved(3, ["like"]), ("case-01", 2): saved(0, ["like"]),
         ("case-02", 1): saved(2, ["like"]), ("case-02", 3): saved(2, ["like"])}
    )  # fmt: skip
    assert [(c, r) for c, r, _ in rj.select_batches(data)] == [
        ("case-01", 1),
        ("case-02", 1),
        ("case-02", 3),
    ]
    assert [(c, r) for c, r, _ in rj.select_batches(data, rounds=[1])] == [
        ("case-01", 1),
        ("case-02", 1),
    ]
    assert [(c, r) for c, r, _ in rj.select_batches(data, cases=["case-02"])] == [
        ("case-02", 1),
        ("case-02", 3),
    ]


def test_the_estimate_counts_judge_calls_only() -> None:
    data = results_file({("case-01", 1): saved(24, ["like"]), ("case-01", 2): saved(13, ["like"]),
                         ("case-02", 1): saved(5, ["like"])})  # fmt: skip
    est = rj.estimate_rejudge(Config(llm_mode="mock"), rj.select_batches(data))
    assert est == {"batches": 3, "items": 42, "judge_calls": 2 + 2 + 1, "usd": est["usd"]}
    assert est["usd"] == pytest.approx(5 * judge_call_usd(Config(llm_mode="mock")), abs=1e-4)
    assert BATCH == 12


def test_a_rejudge_runs_the_persona_judge_per_batch_and_pools_the_agreement() -> None:
    data = results_file(
        {("case-01", 1): saved(4, ["love", "like", "dislike", "dislike"]),
         ("case-02", 2): saved(4, ["like", "like", "dislike", "like"])}
    )  # fmt: skip
    judge = FakeJudge([["like", "like", "dislike", "love"], ["dislike", "like", "dislike", "like"]])
    out = rj.run_rejudge(data, CASES, judge, source="x.json")
    assert [r["case"] for r in out["batches"]] == ["case-01", "case-02"]
    one, two = out["batches"]
    assert (
        one["raw_agree"] == 2 and one["match_agree"] == 3 and one["match_rate_diff_points"] == 25.0
    )
    assert (
        two["raw_agree"] == 3 and two["match_agree"] == 3 and two["match_rate_diff_points"] == -25.0
    )
    assert out["per_case"]["case-01"]["match_agreement"] == 0.75
    assert out["overall"]["compared"] == 8 and out["overall"]["match_agreement"] == pytest.approx(
        6 / 8
    )
    assert out["overall"]["mean_abs_match_rate_diff_points"] == 25.0
    assert out["overall"]["mean_match_rate_diff_points"] == 0.0
    assert out["match_rate_diff_sd_points"] == 25.0
    assert out["source_run_id"] == data["run_id"] and out["source_file"] == "x.json"
    assert [i.title for i in judge.seen[0]] == ["Item 0", "Item 1", "Item 2", "Item 3"]
    assert "Rejudge of run" in rj.render(out) and "overall" in rj.render(out)


def test_a_failed_new_judgement_is_left_out_not_scored() -> None:
    data = results_file({("case-01", 1): saved(3, ["like"])})
    out = rj.run_rejudge(data, CASES, FakeJudge([["like", None, "dislike"]]))
    row = out["batches"][0]
    assert row["compared"] == 2 and row["failed"] == 1 and row["new"] == ["like", None, "dislike"]


def mock_results(tmp_path: Path) -> Path:
    payload = run_all([CASES["case-07"]], build_env("mock"))
    return write_results(payload, tmp_path)


def test_the_cli_rejudges_a_mock_file_end_to_end_without_touching_the_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    src = mock_results(tmp_path)
    before = hashlib.sha256(src.read_bytes()).hexdigest()

    def boom(*a: Any, **kw: Any) -> Any:
        raise AssertionError("a rejudge must not call the pipeline")

    from app.shop.service import ShopService

    for name in ("search", "rate", "refine", "shop_again", "resolve", "photos"):
        monkeypatch.setattr(ShopService, name, boom)
    monkeypatch.setattr("evals.shop.__main__.run_all", boom)
    monkeypatch.setattr("evals.shop.__main__.RESULTS_DIR", tmp_path / "out")
    assert main(["rejudge", "--results", str(src), "--rounds", "1,2"]) == 0
    text = capsys.readouterr().out
    assert text.index("Rejudge estimate") < text.index("Rejudge of run")  # the estimate comes first
    assert "persona-judge calls" in text and "judge calls only" in text
    written = list((tmp_path / "out").glob("rejudge-*.json"))
    assert len(written) == 1 and written[0].name.startswith(
        f"rejudge-{json.loads(src.read_text())['run_id']}"
    )
    out = json.loads(written[0].read_text())
    assert out["kind"] == "rejudge" and out["overall"]["match_agreement"] == 1.0  # mock vs mock
    assert {r["round"] for r in out["batches"]} == {1, 2}
    assert (
        hashlib.sha256(src.read_bytes()).hexdigest() == before
    )  # the source file is never changed


def test_a_live_file_is_refused_without_the_flag_and_under_pytest_or_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = results_file({("case-01", 1): saved(24, ["like"])}, mode="live")
    src = tmp_path / "shop-live-x.json"
    src.write_text(json.dumps(data))

    def boom(*a: Any, **kw: Any) -> Any:
        raise AssertionError("no judge may be built or run")

    monkeypatch.setattr("evals.shop.judges.ModelJudge", boom)
    monkeypatch.setattr("evals.shop.rejudge.run_rejudge", boom)
    monkeypatch.delenv(CONFIRM_VAR, raising=False)
    running = os.environ["PYTEST_CURRENT_TEST"]
    monkeypatch.delenv("PYTEST_CURRENT_TEST")  # to reach the flag check, as a plain shell would
    assert main(["rejudge", "--results", str(src)]) == 2
    captured = capsys.readouterr()
    assert "Rejudge estimate: 1 batches, 24 items, 2 persona-judge calls" in captured.out
    assert "REFUSED" in captured.err and CONFIRM_VAR in captured.err
    monkeypatch.setenv(CONFIRM_VAR, "yes")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", running)  # under pytest: refused even with the flag
    assert main(["rejudge", "--results", str(src)]) == 2
    assert "never runs from CI or from tests" in capsys.readouterr().err
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("CI", "true")
    assert main(["rejudge", "--results", str(src)]) == 2
    assert "never runs from CI or from tests" in capsys.readouterr().err


def test_bad_arguments_are_refused_before_anything_runs(tmp_path: Path) -> None:
    src = tmp_path / "r.json"
    src.write_text(json.dumps(results_file({("case-01", 1): saved(2, ["like"])}, mode="mock")))
    assert main(["rejudge", "--results", str(tmp_path / "missing.json")]) == 2
    assert main(["rejudge", "--results", str(src), "--rounds", "1,x"]) == 2
    assert main(["rejudge", "--results", str(src), "--rounds", "4"]) == 2
    assert main(["rejudge", "--results", str(src), "--cases", "case-99"]) == 2


def test_the_make_target_needs_the_flag_and_forces_nothing_else() -> None:
    make = (ROOT / "Makefile").read_text()
    assert "eval-shop-rejudge" in " ".join(make.splitlines()[:5])  # in .PHONY
    assert re.search(
        r"^eval-shop-rejudge:\n\tEVAL_SHOP_CONFIRM=\$\(EVAL_SHOP_CONFIRM\) APP_LLM_MODE=live "
        r"uv run python -m evals.shop rejudge --results \$\(RESULTS\) \$\(ARGS\)$",
        make, flags=re.MULTILINE,
    )  # fmt: skip
    readme = (ROOT / "evals" / "shop" / "README.md").read_text()
    assert "eval-shop-rejudge" in readme and "persona judge only" in readme.lower()
