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


def _web() -> Config:
    return Config(retrieval_mode="web_search")


def test_mock_run_end_to_end_web_search_mode(tmp_path: Path) -> None:
    result = run_spike(
        synthetic_room_image(),
        budget_usd=900,
        style="warm minimalist",
        config=_web(),
        run_id="t1",
    )
    assert result.llm_mode == "mock" and result.retrieval_mode == "web_search"
    assert [r.stage for r in result.rows][:5] == [
        "prepare_image",
        "perception",
        "retrieval: coffee table / web_search",
        "retrieval: sofa / web_search",
        "retrieval: area rug / web_search",
    ]
    assert result.models["perception"] == "claude-sonnet-5-5"
    assert result.models["retrieval"] == "claude-haiku-5-5"
    assert (
        result.prompt_versions["perception"] == "3" and result.prompt_versions["retrieval"] == "1"
    )
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


def test_mock_run_end_to_end_shopping_mode(tmp_path: Path) -> None:
    """The default: structured shopping data from fixtures, a mock ranker, mock lookups."""
    result = run_spike(
        synthetic_room_image(),
        budget_usd=800,
        style="warm minimalist",
        config=Config(),
        run_id="s1",
    )
    assert result.retrieval_mode == "shopping" and result.search_provider == "serper"
    stages = [r.stage for r in result.rows]
    assert stages[:2] == ["prepare_image", "perception"]
    for slot in ("coffee table", "area rug"):
        assert f"retrieval: {slot} / search" in stages
        assert f"retrieval: {slot} / rank" in stages
        assert (
            f"retrieval: {slot} / lookup 1" in stages and f"retrieval: {slot} / lookup 2" in stages
        )
    # the sofa slot's results are all over the limit: its second round is labelled as such
    assert "retrieval: sofa / rank (over limit)" in stages
    assert "retrieval: sofa / lookup 1 (over limit)" in stages
    assert result.prompt_versions == {
        "perception": "3", "rank": "3", "proposal": "2", "style_check": "2",
    }  # fmt: skip
    provider_rows = [r for r in result.rows if r.kind in ("search", "lookup")]
    assert provider_rows and all(r.cost_estimated for r in provider_rows)
    assert {r.model for r in provider_rows} == {"serper", "serpapi"}
    assert next(r for r in result.rows if r.kind == "rank").model == "claude-haiku-5-5"

    for slot in result.retrieval:
        cands = slot["candidates"]
        if slot["slot"]["category"] == "sofa":
            assert cands == [] and len(slot["over_budget_options"]) == 1
            continue
        assert len(cands) == 2  # the invented id was dropped
        assert all(c["name"].startswith("[FIXTURE]") for c in cands)
        assert {c["url_kind"] for c in cands} == {"retailer_product", "google_panel"}
        assert all(c["price"] > 0 and c["price_source"] in ("search", "lookup") for c in cands)
        assert [d["reason"] for d in slot["dropped_unverified"]] == ["id_not_in_candidates"]
        retailer = next(c for c in cands if c["url_kind"] == "retailer_product")
        assert "srsltid" not in retailer["url"] and "utm_" not in retailer["url"]

    out_dir = write_outputs(result, tmp_path)
    table = (out_dir / "table.txt").read_text()
    assert "* estimated" in table and "retrieval=shopping/serper" in table
    totals = json.loads((out_dir / "result.json").read_text())["totals"]
    assert totals["critical_path_seconds"] <= totals["seconds"] + 1e-9


def test_serpapi_mock_run_shows_dimensions() -> None:
    cfg = Config(search_provider="serpapi")
    result = run_spike(synthetic_room_image(), budget_usd=900, style="x", slots=1, config=cfg)
    cands = result.retrieval[0]["candidates"]
    ikea = next(c for c in cands if "GUTTANE" in c["name"])
    # the lookup states 58 x 39 cm, but a 58 cm "coffee table" is implausible (it is a side table):
    # the size is discarded and noted rather than trusted
    assert ikea["dimensions_m"] is None and ikea["size_source"] == "none"
    assert "outside the plausible range" in ikea["size_note"]


def test_trace_records_stages_prompt_versions_and_searches_web_mode(tmp_path: Path) -> None:
    from app import tracing

    run_spike(synthetic_room_image(), budget_usd=500, style="x", slots=1, config=_web())
    lines = [json.loads(x) for x in tracing.TRACE_PATH.read_text().splitlines()]
    by_name = {rec["name"]: rec for rec in lines}
    assert by_name["stage.perception"]["prompt_version"] == "3"
    assert by_name["stage.retrieval"]["prompt_version"] == "1"
    per_slot = by_name["stage.retrieval"]["per_slot"][0]
    assert per_slot["queries"] and per_slot["result_counts"] == [1]
    assert per_slot["dropped_unverified_urls"] == 1
    assert by_name["llm.perception"]["model"] == "claude-sonnet-5-5"


def test_trace_records_provider_calls_and_rank_in_shopping_mode() -> None:
    from app import tracing

    run_spike(synthetic_room_image(), budget_usd=900, style="x", slots=1, config=Config())
    lines = [json.loads(x) for x in tracing.TRACE_PATH.read_text().splitlines()]
    names = [rec["name"] for rec in lines]
    assert names.count("provider.search") == 1 and names.count("provider.lookup") == 2
    assert "llm.rank" in names
    for rec in lines:
        if rec["name"].startswith("provider."):
            assert rec["cost_estimated"] is True and rec["provider"] in ("serper", "serpapi")
    stage = next(r for r in lines if r["name"] == "stage.retrieval")
    assert stage["prompt_name"] == "rank" and stage["retrieval_mode"] == "shopping"
    assert (
        stage["per_slot"][0]["search"]["kept"] > 0 and stage["per_slot"][0]["rank"]["invented"] == 1
    )


def test_proposed_targets_are_informational_never_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With every proposed target patched to ~zero the run still completes (not enforced)."""
    for key in PROPOSED_TARGETS:
        monkeypatch.setitem(PROPOSED_TARGETS, key, 1e-9)
    result = run_spike(synthetic_room_image(), budget_usd=100, style="x", config=Config())
    assert "proposed" in format_table(result)


def test_step_cap_raises_instead_of_truncating() -> None:
    cfg = Config(max_steps=2)  # perception + one rank call; the other slots' rank calls must refuse
    with pytest.raises(BudgetExceededError, match="step cap"):
        run_spike(synthetic_room_image(), budget_usd=900, style="x", slots=3, config=cfg)


def test_cost_cap_raises_instead_of_truncating() -> None:
    cfg = Config(max_cost_usd=0.005)
    with pytest.raises(BudgetExceededError, match="cost cap"):
        run_spike(synthetic_room_image(), budget_usd=900, style="x", config=cfg)


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
