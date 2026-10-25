"""Every live call shape Shop Mode makes, run through the REAL Anthropic SDK client over an
httpx MockTransport (no network): the request is built by the installed SDK, so a keyword argument it
does not accept raises here exactly as it does live, and the JSON body that would be sent is checked.

Regression for the live failure "FAILED before a response: TypeError": `messages.stream()` got an
unexpected keyword argument 'temperature' (anthropic 1.8.0 has no such parameter), raised at
src/app/llm.py in `_create_message` before any request was sent.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
import time
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.resources.messages import Messages

import app.shop.candidates as candidates_mod
from app import llm
from app.config import Config
from app.llm import Budget
from app.mock_llm import (
    mock_candidate_tool_input,
    mock_prefs_v3_payload,
    mock_shop_parse_payload,
)
from app.shop.candidates import MODE_ROWS, MODE_TOOL, TagBook, Taggable, run_call, tag_results
from app.shop.clock import Clock
from app.shop.parse import parse_request
from app.shop.prefs import build_profile
from app.shop.providers import ReplayPoolSearch
from app.shop.redact import describe_exception, redact
from app.shop.replay import REPLAY_FILE_3, seed_session
from app.shop.search import order_pool
from app.shop.service import ShopService
from app.shop.store import SessionStore
from app.shopping import SearchCandidate
from test_shop import (  # noqa: F401  (the autouse guard must be imported to apply here)
    FakeLookup,
    RouterClient,
    _loopback_only,
    cand,
    query,
    use_client,
)

ROOT = Path(__file__).resolve().parents[1]
SDK_FIELDS = set(inspect.signature(Messages.create).parameters) - {"self"}
STREAM_FIELDS = set(inspect.signature(Messages.stream).parameters) - {"self"}
TAG_TOOL_BODY = {
    "model", "max_tokens", "system", "messages", "tools", "tool_choice", "stream", "thinking",
}  # fmt: skip
TAG_ROWS_BODY = {"model", "max_tokens", "system", "messages", "output_config", "stream", "thinking"}


def live_config() -> Config:
    return Config(llm_mode="live", tracing_disabled=True)


def request_text(body: dict[str, Any]) -> str:
    content = body["messages"][0]["content"]
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if isinstance(b, dict))


def schema_title(body: dict[str, Any]) -> str:
    return str(body.get("output_config", {}).get("format", {}).get("schema", {}).get("title", ""))


def sse(blocks: list[dict[str, Any]], stop: str) -> bytes:
    """A canned streaming response (server-sent events) holding `blocks`."""
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
                    "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 120, "output_tokens": 1},
                },
            },
        )
    ]  # fmt: skip
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            start = {"type": "tool_use", "id": "toolu_1", "name": block["name"], "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        events += [
            ("content_block_start", {"type": "content_block_start", "index": i, "content_block": start}),
            ("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta}),
            ("content_block_stop", {"type": "content_block_stop", "index": i}),
        ]  # fmt: skip
    events += [
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 40},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


def message_json(blocks: list[dict[str, Any]], stop: str) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for block in blocks:
        content.append(
            {"type": "text", "text": block["text"]}
            if block["type"] == "text"
            else {
                "type": "tool_use",
                "id": "toolu_1",
                "name": block["name"],
                "input": block["input"],
            }
        )
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "m", "content": content,
        "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 120, "output_tokens": 40},
    }  # fmt: skip


class Wire:
    """An SDK client whose transport is a handler: records every request body and answers it."""

    def __init__(self, answer: Any = None) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.answer = answer or self.default
        self.client = anthropic.Anthropic(
            api_key="sk-test-not-a-key",
            http_client=httpx2.Client(transport=httpx2.MockTransport(self.handle)),
            max_retries=0,
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Wire:
        monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: self.client)
        return self

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        blocks, stop = self.answer(body)
        if body.get("stream"):
            return httpx2.Response(
                200, content=sse(blocks, stop), headers={"content-type": "text/event-stream"}
            )
        return httpx2.Response(200, json=message_json(blocks, stop))

    @staticmethod
    def default(body: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        text = request_text(body)
        if (body.get("tool_choice") or {}).get("name") == "record_tags":
            return [
                {
                    "type": "tool_use",
                    "name": "record_tags",
                    "input": mock_candidate_tool_input(text),
                }
            ], "tool_use"
        title = schema_title(body)
        if title == "CandidateTags":
            rows = [f"{n}|round|wood|warm wood|-|-" for n in range(1, text.count("\n") + 1)]
            return [{"type": "text", "text": json.dumps({"rows": rows})}], "end_turn"
        if title == "ShopParse":
            return [{"type": "text", "text": json.dumps(mock_shop_parse_payload(text))}], "end_turn"
        if title == "RankedFinalists":
            ids = re.findall(r"^- id: (\S+) \|", text, flags=re.MULTILINE)
            payload = {"finalists": [{"id": i, "reason": "ok"} for i in ids]}
            return [{"type": "text", "text": json.dumps(payload)}], "end_turn"
        if title == "PreferenceTags":
            return [{"type": "text", "text": json.dumps(mock_prefs_v3_payload(text))}], "end_turn"
        raise AssertionError(f"unexpected call shape: {title or sorted(body)}")


def batch(n: int) -> list[Taggable]:
    return [
        Taggable(f"d{i}", f"Round Oak Coffee Table {i}", 100.0 + i, "Wayfair") for i in range(n)
    ]


# ---- the failure that was live ----------------------------------------------------------------------------------


def test_the_installed_sdk_takes_no_temperature_and_the_repo_sends_none() -> None:
    assert "temperature" not in SDK_FIELDS and "temperature" not in STREAM_FIELDS
    assert "temperature" not in inspect.signature(llm.complete).parameters
    for path in [*ROOT.glob("src/app/**/*.py"), *ROOT.glob("scripts/*.py")]:
        body = path.read_text()
        assert '"temperature"' not in body and "temperature=" not in body, path.name


@pytest.mark.parametrize(
    ("label", "mode", "n"),
    [
        ("A rows, 100", MODE_ROWS, 100),
        ("B rows, 40", MODE_ROWS, 40),
        ("C tool call, 40", MODE_TOOL, 40),
    ],
)
def test_the_three_diagnostic_shapes_run_without_a_type_error_and_send_the_intended_body(
    monkeypatch: pytest.MonkeyPatch, label: str, mode: str, n: int
) -> None:
    wire = Wire().install(monkeypatch)
    book = TagBook()
    rows, entry = run_call(batch(n), book, Budget(live_config()), mode, False, Clock(), 30.0)
    assert entry["error"] == "" and entry["where"] == "", label
    assert len(wire.bodies) == 1 and len(rows) == n and entry["rows_parsed"] == n
    body = wire.bodies[0]
    assert set(body) <= STREAM_FIELDS | {"stream"} and set(body) <= SDK_FIELDS | {"stream"}
    assert body["model"] == live_config().rank_model and isinstance(body["max_tokens"], int)
    assert body["stream"] is True and body["messages"][0]["role"] == "user"
    assert body["thinking"] == {"type": "disabled"}  # no thinking against the max_tokens budget
    assert "temperature" not in body and "effort" not in json.dumps(body)
    if mode == MODE_TOOL:
        assert set(body) == TAG_TOOL_BODY and "output_config" not in body
        assert body["tool_choice"] == {"type": "tool", "name": "record_tags"}
        (tool,) = body["tools"]
        assert tool["name"] == "record_tags" and tool["strict"] is True
        assert tool["input_schema"]["required"] == ["items"]
        assert entry["block_types"] == ["tool_use"] and entry["stop_reason"] == "tool_use"
    else:
        assert set(body) == TAG_ROWS_BODY and "tools" not in body and "tool_choice" not in body
        assert body["output_config"]["format"]["type"] == "json_schema"
        assert "effort" not in body["output_config"]
        assert entry["block_types"] == ["text"] and entry["stop_reason"] == "end_turn"


def test_a_canned_tool_use_response_and_a_canned_text_response_both_parse_into_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text_rows = "1|round|wood|warm wood|black|modern\n2|-|-|-|-|-"

    def answer(body: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        if (body.get("tool_choice") or {}).get("name") == "record_tags":
            items = [{"index": 1, "shape": "Round", "material": "walnut", "finish": "walnut", "frame_color": None, "style": ["modern", "glam"]},
                     {"index": 2, "shape": None, "material": None, "finish": None, "frame_color": None, "style": []}]  # fmt: skip
            return [
                {"type": "tool_use", "name": "record_tags", "input": {"items": items}}
            ], "tool_use"
        return [{"type": "text", "text": text_rows}], "end_turn"

    Wire(answer).install(monkeypatch)
    budget = Budget(live_config())
    tool_rows, tool_entry = run_call(batch(2), TagBook(), budget, MODE_TOOL, False, Clock(), 30.0)
    assert tool_entry["json_ok"] is True and sorted(tool_rows) == [1, 2]
    assert tool_rows[1]["shape"] == "round" and tool_rows[1]["color_finish"] == "warm wood"
    assert tool_rows[1]["style"] == "modern,glam" and tool_rows[2]["shape"] is None
    text_parsed, text_entry = run_call(batch(2), TagBook(), budget, MODE_ROWS, False, Clock(), 30.0)
    assert text_entry["json_ok"] is False and sorted(text_parsed) == [1, 2]  # plain rows, not JSON
    assert text_parsed[1]["frame_color"] == "black" and text_parsed[1]["style"] == "modern"
    # a text answer to the tool call (an older shape) is read by the rows parser too
    as_text, entry = run_call(batch(2), TagBook(), budget, MODE_TOOL, False, Clock(), 30.0)
    assert entry["rows_parsed"] == 2 and entry["block_types"] == ["tool_use"] and len(as_text) == 2


def test_tag_results_through_the_real_client_tags_batches_of_forty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = Wire().install(monkeypatch)
    book = TagBook()
    tag_results(batch(100), book, Budget(live_config()), workers=1)
    assert book.tagged == 100 and not book.failed and len(wire.bodies) == 3
    assert all(set(b) == TAG_TOOL_BODY for b in wire.bodies)


# ---- a client-side exception is recorded, never fatal --------------------------------------------------------------------


def test_a_type_error_in_the_tagging_call_is_recorded_with_its_message_and_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**kwargs: Any) -> Any:
        raise TypeError(
            "Messages.stream() got an unexpected keyword argument 'x' Authorization: Bearer sk-ant-SECRET123456"
        )

    monkeypatch.setattr(candidates_mod, "complete", boom)
    book = TagBook()
    tag_results(batch(10), book, Budget(live_config()), workers=1)
    assert (
        book.failed and book.tagged == 0 and book.calls == 3
    )  # the call, then a retry in 2 pieces
    entry = book.log[0]
    assert entry["error"].startswith(
        "TypeError: Messages.stream() got an unexpected keyword argument 'x'"
    )
    assert "SECRET" not in json.dumps(book.log) and "[redacted]" in entry["error"]
    assert "TypeError" in book.reason


def test_a_failing_tagging_call_does_not_crash_a_shop_again_and_shows_in_the_trace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(**kwargs: Any) -> Any:
        raise TypeError("an unexpected keyword argument 'temperature'")

    use_client(monkeypatch, RouterClient())
    monkeypatch.setattr(candidates_mod, "complete", boom)
    store = SessionStore(tmp_path / "shop-data")
    cfg = Config(tracing_disabled=True)
    svc = ShopService(cfg, store, ReplayPoolSearch(REPLAY_FILE_3), FakeLookup())
    sid = seed_session(store, replay=REPLAY_FILE_3).session_id
    view = svc.shop_again(sid)
    found = view["search"]
    assert len(found["results"]) > 0  # the Shop Again went on without tags
    calls = found["trace"]["tag_calls"]
    assert calls and all(
        c["error"].startswith("TypeError: an unexpected keyword argument 'temperature'")
        for c in calls
    )
    assert "TypeError" in found["tagging"]["tag_reason"]
    assert found["headline"].startswith(
        "24 new results. Candidate tagging failed: the call failed: TypeError"
    )
    assert any("Candidate tagging failed:" in w and "TypeError" in w for w in found["warnings"])


def test_a_budget_error_is_not_swallowed_by_the_tagging_failure_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import BudgetExceededError

    def over(**kwargs: Any) -> Any:
        raise BudgetExceededError("cost cap exceeded")

    monkeypatch.setattr(candidates_mod, "complete", over)
    with pytest.raises(BudgetExceededError):
        tag_results(batch(5), TagBook(), Budget(live_config()), workers=1)


# ---- every other live call path ---------------------------------------------------------------------------------------


def test_the_parse_call_shape_is_accepted_by_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = Wire().install(monkeypatch)
    parsed, completion, warning = parse_request(
        "boho coffee table with storage under $400", Budget(live_config())
    )
    assert warning == "" and completion is not None and parsed.category
    (body,) = wire.bodies
    assert set(body) <= SDK_FIELDS and schema_title(body) == "ShopParse" and "stream" not in body
    assert set(body) == {"model", "max_tokens", "system", "messages", "output_config", "thinking"}
    assert body["thinking"] == {"type": "disabled"}
    assert body["output_config"]["effort"] == live_config().effort_retrieval


def test_the_rank_call_shape_is_accepted_by_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    wire = Wire().install(monkeypatch)
    pool: list[SearchCandidate] = [
        cand(f"p{i}", 100.0 + i, title=f"Round Table {i}") for i in range(6)
    ]
    ordered, warning = order_pool(
        query(request="tables", category="table"), pool, Budget(live_config())
    )
    assert warning == "" and [c.product_id for c, _ in ordered] == [f"p{i}" for i in range(6)]
    (body,) = wire.bodies
    assert set(body) <= SDK_FIELDS | {"stream"} and schema_title(body) == "RankedFinalists"
    assert body["stream"] is True and set(body) == TAG_ROWS_BODY


def test_the_preference_call_shape_is_accepted_by_the_sdk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wire = Wire().install(monkeypatch)
    store = SessionStore(tmp_path / "shop-data")
    session = seed_session(store, replay=REPLAY_FILE_3)
    base = session.searches[0].query
    profile, warning = build_profile(session, base, Budget(live_config()), None)
    assert warning == "" and profile.tagging["items_tagged"] == 7
    assert wire.bodies and all(set(b) <= SDK_FIELDS for b in wire.bodies)
    body = wire.bodies[0]
    assert schema_title(body) == "PreferenceTags" and "stream" not in body
    assert set(body) == {"model", "max_tokens", "system", "messages", "output_config", "thinking"}
    assert body["thinking"] == {"type": "disabled"}


def test_the_style_check_and_other_room_pipeline_calls_use_only_sdk_fields() -> None:
    seen: set[str] = set()
    for path in ROOT.glob("src/app/**/*.py"):
        for match in re.finditer(r"params\[\"(\w+)\"\]", path.read_text()):
            seen.add(match.group(1))
    assert seen <= SDK_FIELDS, seen - SDK_FIELDS


def test_the_per_request_timeout_option_is_accepted_by_the_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = Wire().install(monkeypatch)
    rows, entry = run_call(
        batch(5), TagBook(), Budget(live_config()), MODE_TOOL, False, Clock(), 7.5
    )
    assert entry["error"] == "" and len(rows) == 5 and entry["timeout_s"] == 7.5
    assert len(wire.bodies) == 1


# ---- redaction and the diagnostic ----------------------------------------------------------------------------------


def test_redact_hides_keys_tokens_and_authorization_values() -> None:
    text = (
        "failed Authorization: Bearer sk-ant-api03-abcdef123456 x-api-key=zzzzzzzz token: abc123 "
        + "A" * 40
    )
    out = redact(text)
    for secret in ("sk-ant", "abcdef123456", "zzzzzzzz", "abc123", "A" * 40):
        assert secret not in out
    assert out.count("[redacted]") >= 3 and out.startswith("failed ")
    assert redact("plain message about a coffee table") == "plain message about a coffee table"


def test_describe_exception_gives_class_message_and_the_repository_frame() -> None:
    from app.shop.dedupe import title_key

    try:
        title_key(None)  # type: ignore[arg-type]
    except AttributeError as exc:
        text, where = describe_exception(exc)
    assert text.startswith("AttributeError: ") and where.startswith("src/app/shop/dedupe.py:")
    long = describe_exception(ValueError("x " * 400))[0]
    assert len(long) <= len("ValueError: ") + 300
    assert describe_exception(KeyError())[0] == "KeyError: ''" or describe_exception(KeyError())[
        0
    ].startswith("KeyError")
    assert (
        describe_exception(RuntimeError("no frame here"))[1] == ""
    )  # raised outside src and scripts


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "diag", ROOT / "scripts" / "shop_diagnose_tagging.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_diagnostic_prints_the_message_the_location_and_no_secret(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ENVSECRET-111111")
    script = load_script()
    monkeypatch.setattr(script, "load_env", lambda: None)

    def boom(*args: Any, **kwargs: Any) -> Any:
        from app.shop.dedupe import title_key

        title_key(None)  # type: ignore[arg-type]

    monkeypatch.setattr(script, "run_call", boom)
    assert script.main(["--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.count("FAILED before a response") == 3
    assert "exception:    AttributeError:" in out and "raised at:    src/app/shop/dedupe.py:" in out
    assert "ENVSECRET" not in out and "sk-ant" not in out
    secret = "TypeError text with Authorization: Bearer sk-ant-LEAK123456 inside"

    def leaky(*args: Any, **kwargs: Any) -> Any:
        raise TypeError(secret)

    monkeypatch.setattr(script, "run_call", leaky)
    assert script.main(["--data-dir", str(tmp_path)]) == 0
    out2 = capsys.readouterr().out
    assert "exception:    TypeError: TypeError text with [redacted]" in out2 and "LEAK" not in out2
    assert (
        "raised at:    scripts/shop_diagnose_tagging.py:" in out2
    )  # the innermost frame in this repository


def test_a_failure_recorded_by_the_call_is_printed_by_the_diagnostic(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(**kwargs: Any) -> Any:
        from app.shop.dedupe import title_key

        title_key(None)  # type: ignore[arg-type]

    monkeypatch.setattr(candidates_mod, "complete", boom)
    script = load_script()
    assert script.main(["--mock", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.count("outcome:      FAILED: AttributeError:") == 3
    assert out.count("raised at:    src/app/shop/dedupe.py:") == 3


def test_nothing_sleeps_and_no_real_network_is_used() -> None:
    started = time.monotonic()
    for path in [*ROOT.glob("src/app/shop/*.py"), *ROOT.glob("scripts/shop*.py")]:
        assert "sleep(" not in path.read_text(), path.name
    assert time.monotonic() - started < 5


def test_a_rejected_tool_call_is_retried_as_the_same_tool_call_never_as_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = Wire()

    def handle(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        wire.bodies.append(body)
        err = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad tool"}}
        return httpx2.Response(400, json=err)

    wire.client = anthropic.Anthropic(
        api_key="sk-test-not-a-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handle)),
        max_retries=0,
    )
    wire.install(monkeypatch)
    book = TagBook()
    tag_results(batch(10), book, Budget(live_config()), workers=1)
    assert book.failed and book.tagged == 0 and book.calls == 3  # the call, then 2 pieces of 5
    assert [e["mode"] for e in book.log] == [MODE_TOOL] * 3
    assert [e["items"] for e in book.log] == [10, 5, 5]  # the same tool call, a smaller batch
    assert all("tools" in b and "output_config" not in b for b in wire.bodies)
    assert book.log[0]["error"].startswith("BadRequestError (HTTP 400)")
