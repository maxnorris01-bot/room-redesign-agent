from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app import llm
from app.config import BudgetExceededError, Config
from app.images import synthetic_room_image
from app.mock_llm import ScriptedClient, mock_perception_payload, response, text_block
from app.spike import PROPOSED_TARGETS, format_table, run_spike, write_outputs


@pytest.fixture(autouse=True)
def _no_network_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("mock mode must never construct the real Anthropic client")

    monkeypatch.setattr(llm.anthropic, "Anthropic", boom)
    llm.get_client.cache_clear()


def test_mock_run_end_to_end(tmp_path: Path) -> None:
    result = run_spike(
        synthetic_room_image(),
        budget_usd=900,
        style="warm minimalist",
        config=Config(),
        run_id="t1",
    )
    assert result.llm_mode == "mock"
    assert [r.stage for r in result.rows] == [
        "prepare_image",
        "perception",
        "retrieval: sofa",
        "retrieval: coffee table",
        "retrieval: floor lamp",
    ]
    assert result.models == {"perception": "claude-sonnet-5-5", "retrieval": "claude-haiku-5-5"}
    assert result.prompt_versions == {"perception": "1", "retrieval": "1"}
    assert max(result.image["width"], result.image["height"]) <= 1568
    for slot in result.retrieval:
        assert {c["retailer"] for c in slot["candidates"]} == {"MOCK"}
        assert len(slot["dropped_unverified"]) == 1  # the guard visibly fires in mock mode
    assert result.total_cost_usd > 0

    out_dir = write_outputs(result, tmp_path)
    data = json.loads((out_dir / "result.json").read_text())
    assert data["totals"]["cost_usd"] == pytest.approx(result.total_cost_usd)
    assert data["proposed_targets_informational"] == PROPOSED_TARGETS
    table = (out_dir / "table.txt").read_text()
    assert "MOCK MODE" in table and "TOTAL" in table
    assert "proposed" in table.lower() and "not enforced" in table


def test_trace_records_stages_prompt_versions_and_searches(tmp_path: Path) -> None:
    from app import tracing

    run_spike(synthetic_room_image(), budget_usd=500, style="x", slots=1, config=Config())
    lines = [json.loads(x) for x in tracing.TRACE_PATH.read_text().splitlines()]
    by_name = {rec["name"]: rec for rec in lines}
    assert by_name["stage.perception"]["prompt_version"] == "1"
    assert by_name["stage.retrieval"]["prompt_version"] == "1"
    per_slot = by_name["stage.retrieval"]["per_slot"][0]
    assert per_slot["queries"] and per_slot["result_counts"] == [1]
    assert per_slot["dropped_unverified_urls"] == 1
    assert by_name["llm.perception"]["model"] == "claude-sonnet-5-5"


def test_proposed_targets_are_informational_never_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With every proposed target patched to ~zero the run still completes (not enforced)."""
    for key in PROPOSED_TARGETS:
        monkeypatch.setitem(PROPOSED_TARGETS, key, 1e-9)
    result = run_spike(synthetic_room_image(), budget_usd=100, style="x", config=Config())
    assert "proposed" in format_table(result)


def test_step_cap_raises_instead_of_truncating() -> None:
    cfg = Config(max_steps=2)  # perception + one slot, then the second slot must refuse
    with pytest.raises(BudgetExceededError, match="step cap"):
        run_spike(synthetic_room_image(), budget_usd=100, style="x", slots=3, config=cfg)


def test_cost_cap_raises_instead_of_truncating() -> None:
    cfg = Config(max_cost_usd=0.005)
    with pytest.raises(BudgetExceededError, match="cost cap"):
        run_spike(synthetic_room_image(), budget_usd=100, style="x", config=cfg)


def test_image_block_precedes_text_and_pixels_are_prepared(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ScriptedClient([response([text_block(mock_perception_payload())])])
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    from app.images import prepare_image
    from app.perception import perceive

    perceive(prepare_image(synthetic_room_image(), max_edge=640), budget=llm.Budget(Config()))
    (call,) = client.calls
    content = call["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image", "text"]
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert call["model"] == Config().model_perception
    assert call["output_config"]["format"]["schema"]["title"] == "RoomPerception"
    assert call["output_config"]["effort"] == Config().effort_perception


def test_cli_runs_in_mock_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    import importlib.util

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_LLM_MODE", "mock")
    spec = importlib.util.spec_from_file_location(
        "spike_cli", Path(__file__).resolve().parents[1] / "scripts" / "spike.py"
    )
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    photo = tmp_path / "room.jpg"
    photo.write_bytes(synthetic_room_image())

    assert cli.main([str(photo), "--budget", "500", "--style", "calm", "--slots", "2"]) == 0
    out = capsys.readouterr().out
    assert "MOCK MODE" in out and "TOTAL" in out
    (run_dir,) = list((tmp_path / "spike-output").iterdir())
    assert {p.name for p in run_dir.iterdir()} == {"result.json", "table.txt", "trace.jsonl"}
    with pytest.raises(SystemExit) as exc:  # neither a photo nor --synthetic
        cli.main(["--budget", "5", "--style", "x"])
    assert exc.value.code == 2
