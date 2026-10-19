"""A mock Anthropic client, so the pipeline runs end-to-end with zero API calls.

Two things live here:

  * The response-construction primitives (`text_block`, `search_blocks`, `response`) and
    `ScriptedClient`, which replays pre-built responses in order. Use these in unit tests that
    need exact control over a specific scenario (a `pause_turn`, malformed output, a budget-cap
    trip) that an auto-generated response can't express.
  * `MockAnthropicClient`, which auto-generates a schema-valid response for *any* structured-output
    request by walking the request's `output_config.format.schema` and filling in a placeholder
    value for each property (the first `enum` value if the schema gives one, otherwise a
    type-appropriate placeholder). This is what `APP_LLM_MODE=mock` (the default - see
    `Config.llm_mode`) wires into `app.llm.get_client`, so every entry point runs for free by
    default, with zero real API calls.

`MockAnthropicClient` is deliberately NOT a stand-in for real model quality: values are picked
generically from the schema, not reasoned about from input. It's good for exercising routing,
schema shape, tracing, and budget accounting for free - not for checking whether an answer is
actually correct. Only a live run (the real API) tests that. If a project's mock needs to route
differently per input (the way a classifier does, so downstream routing can be exercised
structurally), add that logic here per-project - see docs/lessons-learned.md for a worked example
(a keyword-based router built for one project) to crib the shape from without copying its
domain-specific keywords.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

# ---- Response-construction primitives ------------------------------------------------------
# Shared by ScriptedClient and MockAnthropicClient below, and usable directly from tests that
# need exact control over a specific response.


def text_block(payload: dict[str, Any] | str) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(type="text", text=text)


def search_blocks(query: str, urls: list[str]) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(type="server_tool_use", name="web_search", input={"query": query}),
        SimpleNamespace(
            type="web_search_tool_result", content=[SimpleNamespace(url=u) for u in urls]
        ),
    ]


def response(
    content: list[SimpleNamespace],
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 50,
    searches: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            server_tool_use=SimpleNamespace(web_search_requests=searches),
        ),
        _request_id="req_mock",
    )


class ScriptedClient:
    """Returns scripted responses in order and records every request. Unit-test use only.

    Unlike `MockAnthropicClient`, this has no logic of its own - it exists to let a test pin down
    an exact sequence of responses that the auto-generating client's always-succeeds happy path
    can't express.
    """

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return self._responses.pop(0)


# ---- Generic schema-driven placeholder generator ---------------------------------------------


def _placeholder_for(schema: dict[str, Any]) -> Any:
    """Build one JSON-schema-valid placeholder value.

    Handles the JSON Schema subset structured output actually uses: object/properties/required,
    array/items, string/enum, integer, number, boolean. Falls back to a bare string for anything
    else, which is usually enough to keep a request schema-valid even for a shape this hasn't
    seen.
    """
    if "enum" in schema:
        value: Any = schema["enum"][0]
        return value
    schema_type = schema.get("type", "string")
    if schema_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", list(properties))
        return {name: _placeholder_for(properties.get(name, {})) for name in required}
    if schema_type == "array":
        item_schema = schema.get("items", {})
        return [_placeholder_for(item_schema)]
    if schema_type in ("integer", "number"):
        return 0
    if schema_type == "boolean":
        return False
    return "mock"


class MockAnthropicClient:
    """Auto-generates a schema-valid response for any structured-output request.

    Wired in by `app.llm.get_client` when `Config.llm_mode == "mock"` (the default). Inspects the
    request's `output_config.format.schema` and fills it in generically via `_placeholder_for` -
    good enough to exercise routing/shape/tracing/budget accounting for any schema, with no
    project-specific logic needed. If a project's mock needs to route differently based on the
    input (e.g. a classifier that should pick different labels for different inputs so downstream
    routing can be exercised structurally), add that per-project rather than here - see
    docs/lessons-learned.md.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        schema = kwargs.get("output_config", {}).get("format", {}).get("schema")
        if schema is None:
            # No structured-output schema on this request - just echo something schema-free.
            return response([text_block("mock response")], input_tokens=100, output_tokens=20)
        payload = _placeholder_for(schema)
        uses_search = bool(kwargs.get("tools"))
        if uses_search:
            content = [
                *search_blocks("mock query", ["https://mock.example/source"]),
                text_block(payload),
            ]
            return response(content, input_tokens=300, output_tokens=150, searches=1)
        return response([text_block(payload)], input_tokens=200, output_tokens=60)
