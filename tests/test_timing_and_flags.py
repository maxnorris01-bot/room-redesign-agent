from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app import llm, tracing
from app.config import Config
from app.images import prepare_image, synthetic_room_image
from app.mock_llm import MOCK_TTFT_SECONDS, mock_perception_payload, response, text_block
from app.perception import output_stats, perceive
from app.prompts import load_prompt
from app.schemas import RoomPerception, parse_model
from app.spike import SpikeResult, StageRow, format_table, run_spike, write_outputs


def test_perception_ttft_is_recorded_and_shown_in_table_and_json(tmp_path: Path) -> None:
    result = run_spike(synthetic_room_image(), budget_usd=500, style="x", config=Config())
    perception = next(r for r in result.rows if r.stage == "perception")
    assert perception.time_to_first_token_seconds == MOCK_TTFT_SECONDS
    out = write_outputs(result, tmp_path)
    table = (out / "table.txt").read_text()
    assert "time_to_first_token" in table
    assert "0.25" in table
    row = next(
        r
        for r in json.loads((out / "result.json").read_text())["rows"]
        if r["stage"] == "perception"
    )
    assert row["time_to_first_token_seconds"] == MOCK_TTFT_SECONDS


def test_critical_path_uses_slowest_slot_not_the_sum(tmp_path: Path) -> None:
    result = SpikeResult(
        run_id="r", llm_mode="mock", started_at="", photo_name="p", budget_usd=1.0, style="s",
        prompt_versions={}, models={}, image={},
    )  # fmt: skip
    result.rows = [
        StageRow("prepare_image", "-", 0, 0, 0, 0.0, 0.1),
        StageRow("perception", "m", 1, 1, 0, 0.01, 17.0),
        StageRow(
            "retrieval: a / web_search", "m", 1, 1, 1, 0.01, 11.0, slot="a", kind="web_search"
        ),
        StageRow("retrieval: b / web_search", "m", 1, 1, 1, 0.01, 7.0, slot="b", kind="web_search"),
    ]
    assert result.total_seconds == pytest.approx(35.1)
    assert result.critical_path_seconds == pytest.approx(0.1 + 17.0 + 11.0)
    totals = result.to_json()["totals"]
    assert totals["seconds"] == pytest.approx(35.1)
    assert totals["critical_path_seconds"] == pytest.approx(28.1)
    assert "critical path" in format_table(result)


def test_result_json_has_critical_path_and_wall_clock(tmp_path: Path) -> None:
    result = run_spike(synthetic_room_image(), budget_usd=500, style="x", config=Config())
    totals = json.loads((write_outputs(result, tmp_path) / "result.json").read_text())["totals"]
    assert totals["critical_path_seconds"] <= totals["seconds"] + 1e-9
    assert totals["wall_clock_seconds"] > 0


def test_perception_only_skips_retrieval() -> None:
    result = run_spike(synthetic_room_image(), config=Config(), perception_only=True)
    assert [r.stage for r in result.rows] == ["prepare_image", "perception"]
    assert result.retrieval == [] and result.steps == 1
    table = format_table(result)
    assert "perception only" in table and "retrieval:" not in table
    assert "Proposed targets" not in table


def test_live_stream_ttft_is_first_content_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the live streaming branch with a fake stream (no network, no SDK client)."""
    final = response([text_block(mock_perception_payload())])
    events = [
        SimpleNamespace(type="message_start"),
        SimpleNamespace(type="content_block_start"),
        SimpleNamespace(type="content_block_delta"),
        SimpleNamespace(type="content_block_delta"),
        SimpleNamespace(type="message_stop"),
    ]

    class FakeStream:
        def __enter__(self) -> FakeStream:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def __iter__(self) -> Any:
            return iter(events)

        def get_final_message(self) -> Any:
            return final

    client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: FakeStream()))
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    prepared = prepare_image(synthetic_room_image(300, 200))
    out = perceive(prepared, budget=llm.Budget(Config(llm_mode="live")))
    ttft = out.completion.time_to_first_token_s
    assert ttft is not None and ttft >= 0.0
    assert out.perception.room_type == mock_perception_payload()["room_type"]


def test_prompt_versions_load_and_unknown_version_raises() -> None:
    assert load_prompt("perception").version == "3"
    assert load_prompt("perception", "3").version == "3"
    assert load_prompt("perception", "2").version == "2"
    v1 = load_prompt("perception", "1")
    assert v1.version == "1" and v1.text != load_prompt("perception").text
    with pytest.raises(FileNotFoundError, match="version 9"):
        load_prompt("perception", "9")


def test_v2_prompt_carries_the_length_and_hygiene_instructions() -> None:
    text = load_prompt("perception", "2").text.lower()
    for must in ("12 words", "at most 6", "at most 4", "3 to 5", "lowercase", "run-together"):
        assert must in text
    assert "maxItems" not in text


def test_run_records_the_selected_prompt_version() -> None:
    cfg = Config(perception_prompt_version="1")
    result = run_spike(synthetic_room_image(), config=cfg, perception_only=True)
    assert result.prompt_versions["perception"] == "1"
    lines = [json.loads(x) for x in tracing.TRACE_PATH.read_text().splitlines()]
    assert next(r for r in lines if r["name"] == "stage.perception")["prompt_version"] == "1"


def test_mock_fixture_has_the_v2_shape() -> None:
    p = parse_model(RoomPerception, json.dumps(mock_perception_payload()), what="mock")
    stats = output_stats(p)
    assert stats["art_and_decor"] <= 6 and stats["uncertainties"] <= 4
    assert 3 <= stats["style_descriptors"] <= 5
    assert all(s == s.lower() for s in p.style_descriptors)
    assert stats["longest_note_words"] <= 12


def test_fused_style_text_passes_through_our_code_unchanged() -> None:
    """The 'coolpale' check: nothing between the model's JSON and result.json edits strings."""
    payload = mock_perception_payload()
    payload["style_descriptors"] = ["cool", "pale blue", "cool pale blue and white palette"]
    raw = json.dumps(payload)
    parsed = parse_model(RoomPerception, raw, what="p")
    assert parsed.style_descriptors == payload["style_descriptors"]
    assert (
        json.loads(json.dumps(parsed.model_dump()))["style_descriptors"]
        == (payload["style_descriptors"])
    )


def test_cli_flags_override_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_LLM_MODE", "mock")
    spec = importlib.util.spec_from_file_location(
        "spike_cli", Path(__file__).resolve().parents[1] / "scripts" / "spike.py"
    )
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    photo = tmp_path / "room.jpg"
    photo.write_bytes(synthetic_room_image(2000, 1000))

    code = cli.main(
        [
            str(photo), "--perception-only", "--perception-model", "claude-haiku-5-5",
            "--perception-effort", "low", "--image-long-edge", "640",
            "--perception-prompt-version", "1",
        ]
    )  # fmt: skip
    assert code == 0
    (run_dir,) = list((tmp_path / "spike-output").iterdir())
    data = json.loads((run_dir / "result.json").read_text())
    assert data["models"] == {"perception": "claude-haiku-5-5"}
    assert data["image"]["width"] == 640
    assert data["prompt_versions"]["perception"] == "1"
    (call_effort,) = [
        json.loads(line)
        for line in (run_dir / "trace.jsonl").read_text().splitlines()
        if '"llm.perception"' in line
    ]
    assert call_effort["model"] == "claude-haiku-5-5"
    assert "perception only" in capsys.readouterr().out

    with pytest.raises(SystemExit):  # full run still needs --budget and --style
        cli.main([str(photo)])
