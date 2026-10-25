"""The model's thinking must not eat a Shop Mode call's token budget (found by the live diagnostic on
claude-haiku-5-5: the rows call came back as "thinking, text", or as thinking alone with usage 0).

Every call goes through the real Anthropic SDK client over an httpx MockTransport (no network).
"""

from __future__ import annotations

import importlib.util
import json
import re
import typing
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import ThinkingConfigDisabledParam, ThinkingConfigParam

import app.shop.candidates as candidates_mod
from app import llm
from app.llm import Budget, LLMOutputError, complete
from app.pricing import estimate_cost
from app.prompts import load_prompt
from app.shop.candidates import MODE_TOOL, TagBook, run_call, tag_results
from app.shop.clock import Clock
from app.shop.parse import parse_request
from test_shop import (  # noqa: F401  (the autouse guard must be imported to apply here)
    _loopback_only,
)
from test_shop_sdk_shapes import SDK_FIELDS, Wire, batch, live_config, message_json, sse

ROOT = Path(__file__).resolve().parents[1]


def thinking_sse(usage_in: int = 0, usage_out: int = 0, stop: str = "max_tokens") -> bytes:
    """A canned streaming answer that is one thinking block and nothing else."""
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "m", "type": "message", "role": "assistant", "model": "m", "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": usage_in, "output_tokens": usage_out}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "thinking_delta", "thinking": "let me think"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                           "usage": {"output_tokens": usage_out}}),
        ("message_stop", {"type": "message_stop"}),
    ]  # fmt: skip
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


def wire_with(handler: Any, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return handler(body)  # type: ignore[no-any-return]

    client = anthropic.Anthropic(
        api_key="sk-test-not-a-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handle)),
        max_retries=0,
    )
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    return bodies


def stream_response(content: bytes) -> httpx2.Response:
    return httpx2.Response(200, content=content, headers={"content-type": "text/event-stream"})


# ---- what the SDK supports ----------------------------------------------------------------------------------------


def test_the_installed_sdk_can_disable_thinking_and_set_a_low_effort() -> None:
    assert "thinking" in SDK_FIELDS  # a field of messages.create (and of messages.stream)
    assert typing.get_args(typing.get_type_hints(ThinkingConfigDisabledParam)["type"]) == (
        "disabled",
    )
    assert {"disabled", "enabled", "adaptive"} == {
        typing.get_args(typing.get_type_hints(t)["type"])[0]
        for t in typing.get_args(ThinkingConfigParam)
    }
    from anthropic.types import OutputConfigParam

    effort = typing.get_args(typing.get_type_hints(OutputConfigParam)["effort"])
    assert "low" in str(effort) and "max" in str(effort)
    # the SDK reports thinking tokens when the API does
    from anthropic.types import OutputTokensDetails  # type: ignore[attr-defined]

    assert "thinking_tokens" in OutputTokensDetails.model_fields


def test_every_model_call_in_shop_mode_asks_for_no_thinking() -> None:
    calls = 0
    for path in sorted((ROOT / "src" / "app" / "shop").glob("*.py")):
        text = path.read_text()
        shared = '"disable_thinking": True' in text  # a shared kwargs dict (the tagging calls)
        for match in re.finditer(r"\bcomplete\(", text):
            if text[max(0, match.start() - 4) : match.start()].endswith("def "):
                continue
            calls += 1
            assert (
                shared or "disable_thinking=True" in text[match.start() : match.start() + 1200]
            ), path.name
    assert calls == 6  # parse, rank, the preference calls (live, comment), and two tagging calls


def test_the_wrapper_sends_the_disabled_field_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies = wire_with(
        lambda body: httpx2.Response(
            200, json=message_json([{"type": "text", "text": "{}"}], "end_turn")
        ),
        monkeypatch,
    )
    prompt = load_prompt("shop_parse")
    budget = Budget(live_config())
    complete(span_name="x", prompt=prompt, model="m", user_text="u", budget=budget,
             max_tokens=300, disable_thinking=True)  # fmt: skip
    complete(span_name="x", prompt=prompt, model="m", user_text="u", budget=budget, max_tokens=300)
    assert bodies[0]["thinking"] == {"type": "disabled"} and "thinking" not in bodies[1]


# ---- the same request fields for every Shop Mode call --------------------------------------------------------------


def test_parse_rank_preferences_and_tagging_all_send_thinking_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.shop.prefs import build_profile
    from app.shop.replay import REPLAY_FILE_3, seed_session
    from app.shop.search import order_pool
    from app.shop.store import SessionStore
    from test_shop import cand, query

    wire = Wire().install(monkeypatch)
    budget = Budget(live_config())
    parse_request("boho coffee table under $400", budget)
    order_pool(
        query(request="tables", category="table"),
        [cand(f"p{i}", 10.0 + i) for i in range(4)],
        budget,
    )
    session = seed_session(SessionStore(tmp_path / "s"), replay=REPLAY_FILE_3)
    build_profile(session, session.searches[0].query, budget, None)
    run_call(batch(6), TagBook(), budget, MODE_TOOL, False, Clock(), 12.0)
    titles = [
        b.get("output_config", {}).get("format", {}).get("schema", {}).get("title", "tool")
        for b in wire.bodies
    ]
    assert titles == ["ShopParse", "RankedFinalists", "PreferenceTags", "tool"]
    for body in wire.bodies:
        assert body["thinking"] == {"type": "disabled"}, body
        assert set(body) <= SDK_FIELDS | {"stream"}


# ---- a response that is only thinking -------------------------------------------------------------------------------


def test_a_thinking_only_response_is_its_own_error_not_no_text_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire_with(lambda body: stream_response(thinking_sse(0, 0)), monkeypatch)
    with pytest.raises(LLMOutputError) as exc:
        complete(span_name="llm.x", prompt=load_prompt("shop_parse"), model="claude-haiku-5-5",
                 user_text="u", budget=Budget(live_config()), max_tokens=1480, stream=True,
                 keep_truncated=True)  # fmt: skip
    err = exc.value
    assert "the model used its whole budget thinking" in str(err) and "no text content" not in str(
        err
    )
    assert "stop reason max_tokens, max_tokens 1480" in str(err)
    assert (
        err.thinking_only and err.block_types == ("thinking",) and err.stop_reason == "max_tokens"
    )
    # not keep_truncated: the same distinct error (not the generic "ended with max_tokens")
    with pytest.raises(LLMOutputError) as plain:
        complete(span_name="llm.x", prompt=load_prompt("shop_parse"), model="claude-haiku-5-5",
                 user_text="u", budget=Budget(live_config()), max_tokens=1480, stream=True)  # fmt: skip
    assert plain.value.thinking_only


def test_the_tagging_log_and_reason_name_the_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    wire_with(lambda body: stream_response(thinking_sse(0, 0)), monkeypatch)
    book = TagBook()
    tag_results(batch(10), book, Budget(live_config()), workers=1)
    assert book.failed and book.tagged == 0
    first = book.log[0]
    assert first["thinking_only"] is True and first["had_thinking"] is True
    assert first["block_types"] == ["thinking"]
    assert "used its whole budget thinking" in book.reason and "max_tokens" in book.reason


def test_a_response_with_a_thinking_block_and_an_answer_still_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(body: dict[str, Any]) -> httpx2.Response:
        items = [
            {
                "index": n,
                "shape": "round",
                "material": "wood",
                "finish": None,
                "frame_color": None,
                "style": [],
            }
            for n in range(1, 7)
        ]
        events = thinking_sse(50, 30, "tool_use").decode().split("event: message_delta")[0]
        extra = sse(
            [{"type": "tool_use", "name": "record_tags", "input": {"items": items}}], "tool_use"
        ).decode()
        tool = extra.split("event: content_block_start", 1)[1].split("event: message_delta")[0]
        tool = tool.replace('"index": 0', '"index": 1')
        tail = (
            "event: message_delta"
            + thinking_sse(50, 30, "tool_use").decode().split("event: message_delta")[1]
        )
        return stream_response((events + "event: content_block_start" + tool + tail).encode())

    wire_with(handler, monkeypatch)
    rows, entry = run_call(
        batch(6), TagBook(), Budget(live_config()), MODE_TOOL, False, Clock(), 12.0
    )
    assert len(rows) == 6 and entry["block_types"] == ["thinking", "tool_use"]
    assert entry["had_thinking"] is True and entry["error"] == ""


def test_thinking_tokens_are_read_when_the_api_reports_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(body: dict[str, Any]) -> httpx2.Response:
        msg = message_json([{"type": "text", "text": "{}"}], "end_turn")
        msg["usage"]["output_tokens_details"] = {"thinking_tokens": 700}
        return httpx2.Response(200, json=msg)

    wire_with(handler, monkeypatch)
    done = complete(span_name="x", prompt=load_prompt("shop_parse"), model="claude-haiku-5-5",
                    user_text="u", budget=Budget(live_config()), max_tokens=900)  # fmt: skip
    assert done.thinking_tokens == 700 and done.usage_known and not done.cost_estimated


# ---- when the API refuses the thinking field --------------------------------------------------------------------------


def test_a_refused_thinking_field_is_dropped_once_and_remembered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm, "_THINKING_FIELD_REFUSED", set())

    def handler(body: dict[str, Any]) -> httpx2.Response:
        if "thinking" in body:
            err = {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "thinking: not supported for this model",
                },
            }
            return httpx2.Response(400, json=err)
        return httpx2.Response(200, json=message_json([{"type": "text", "text": "{}"}], "end_turn"))

    bodies = wire_with(handler, monkeypatch)
    budget = Budget(live_config())
    for _ in range(2):
        complete(span_name="x", prompt=load_prompt("shop_parse"), model="some-model", user_text="u",
                 budget=budget, max_tokens=300, disable_thinking=True)  # fmt: skip
    assert ["thinking" in b for b in bodies] == [True, False, False]  # tried once, then left out
    other = wire_with(
        lambda body: httpx2.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "max_tokens too big"},
            },
        ),
        monkeypatch,
    )
    with pytest.raises(anthropic.BadRequestError):  # another 400 is not swallowed
        complete(span_name="x", prompt=load_prompt("shop_parse"), model="m2", user_text="u",
                 budget=budget, max_tokens=300, disable_thinking=True)  # fmt: skip
    assert len(other) == 1


# ---- the retry is the same tool call at a smaller batch size --------------------------------------------------------------


def test_the_retry_is_the_forced_tool_call_at_a_smaller_batch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"n": 0}

    def handler(body: dict[str, Any]) -> httpx2.Response:
        state["n"] += 1
        if state["n"] == 1:
            return stream_response(thinking_sse(0, 0))
        return Wire.default and stream_response(  # the tool call, answered for the smaller batch
            sse(Wire.default(body)[0], "tool_use")
        )

    bodies = wire_with(handler, monkeypatch)
    book = TagBook()
    tag_results(batch(40), book, Budget(live_config()), workers=1)
    assert book.tagged == 40 and not book.failed and book.calls == 3
    assert [e["items"] for e in book.log] == [40, 20, 20] and [e["retry"] for e in book.log] == [
        False,
        True,
        True,
    ]
    assert all(b["tool_choice"] == {"type": "tool", "name": "record_tags"} for b in bodies)
    assert all("output_config" not in b for b in bodies)  # no rows format anywhere


# ---- usage and cost of a failed call ------------------------------------------------------------------------------------


def test_a_failed_call_that_reports_zero_usage_is_costed_from_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire_with(lambda body: stream_response(thinking_sse(0, 0)), monkeypatch)
    budget = Budget(live_config())
    book = TagBook()
    rows, entry = run_call(batch(40), book, budget, MODE_TOOL, False, Clock(), 12.0)
    assert rows == {} and entry["usage_known"] is False and entry["usd_estimated"] is True
    assert entry["input_tokens"] is None and entry["output_tokens"] is None  # unknown, not 0
    ceiling = entry["max_tokens"]
    floor = estimate_cost(
        live_config().rank_model,
        type(
            "U",
            (),
            {
                "input_tokens": 0,
                "output_tokens": ceiling,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        )(),
    )
    assert entry["usd"] >= floor > 0  # at least the whole max_tokens as output
    assert budget.cost_usd == pytest.approx(entry["usd"], rel=1e-6)  # budgeted, not booked as $0
    assert book.estimated_usd == pytest.approx(entry["usd"]) and book.usd == book.estimated_usd


def test_a_reported_usage_is_trusted_and_not_marked_estimated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Wire().install(monkeypatch)
    book = TagBook()
    _, entry = run_call(batch(5), book, Budget(live_config()), MODE_TOOL, False, Clock(), 12.0)
    assert entry["usage_known"] is True and entry["usd_estimated"] is False
    assert (
        entry["input_tokens"] == 120 and entry["output_tokens"] == 40 and book.estimated_usd == 0.0
    )


def test_the_footer_and_the_trace_label_an_estimated_failed_cost() -> None:
    from app.shop.server import STATIC_DIR

    code = (STATIC_DIR / "app.js").read_text()
    assert (
        "(estimated, failed call)" in code and "usage unknown (cost estimated, failed call)" in code
    )
    assert "thinking present" in code


def test_a_failed_shop_again_reports_the_estimated_cost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.config import Config
    from app.shop.providers import ReplayPoolSearch
    from app.shop.replay import REPLAY_FILE_3, seed_session
    from app.shop.service import ShopService
    from app.shop.store import SessionStore
    from test_shop import FakeLookup, RouterClient, use_client

    use_client(monkeypatch, RouterClient())

    def thinking_only(**kwargs: Any) -> Any:
        raise LLMOutputError("x: the model used its whole budget thinking", stop_reason="max_tokens",
                             block_types=("thinking",), cost_usd=0.002, cost_estimated=True, thinking_only=True)  # fmt: skip

    monkeypatch.setattr(candidates_mod, "complete", thinking_only)
    store = SessionStore(tmp_path / "s")
    svc = ShopService(
        Config(tracing_disabled=True), store, ReplayPoolSearch(REPLAY_FILE_3), FakeLookup()
    )
    view = svc.shop_again(seed_session(store, replay=REPLAY_FILE_3).session_id)
    tagging = view["search"]["tagging"]
    assert tagging["tag_estimated_usd"] > 0 and tagging["tag_failed_usd"] > 0
    assert "used its whole budget thinking" in tagging["tag_reason"]
    assert "used its whole budget thinking" in view["search"]["headline"]


# ---- the diagnostic ------------------------------------------------------------------------------------------------------


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "diag2", ROOT / "scripts" / "shop_diagnose_tagging.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_diagnostic_prints_thinking_usage_and_fields_filled(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    script = load_script()
    assert script.main(["--mock", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.count("  thinking:     no") == 3 and out.count("fields filled:") == 3
    assert re.search(
        r"fields filled: shape \d+/40, material \d+/40, color_finish \d+/40, frame_color \d+/40, style \d+/40",
        out,
    )
    assert "block types:  tool_use" in out


def test_the_diagnostic_describes_thinking_and_an_unknown_usage() -> None:
    script = load_script()
    entry = {
        "model": "claude-haiku-5-5", "max_tokens": 1480, "timeout_s": 30.0, "seconds": 9.1, "error": "",
        "block_types": ["thinking", "text"], "stop_reason": "max_tokens", "usage_known": False, "usd": 0.00741,
        "had_thinking": True, "thinking_tokens": 1400, "input_tokens": None, "output_tokens": None,
        "rows_parsed": 0, "items": 40, "json_ok": False, "raw": "", "where": "",
    }  # fmt: skip
    text = "\n".join(script.describe("A", entry, {}))
    assert (
        "block types:  thinking, text" in text
        and "thinking:     yes (1400 thinking tokens reported)" in text
    )
    assert (
        "usage:        unknown (the response reported none); cost $0.00741 is estimated (failed call)"
        in text
    )
    assert "0 input tokens" not in text
    filled = script.fields_filled(
        {
            1: {
                "shape": "round",
                "material": "other",
                "color_finish": None,
                "frame_color": "black",
                "style": "modern",
            }
        },
        40,
    )
    assert filled == [
        "shape 1/40",
        "material 0/40",
        "color_finish 0/40",
        "frame_color 1/40",
        "style 1/40",
    ]
