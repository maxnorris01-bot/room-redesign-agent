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
import re
from types import SimpleNamespace
from typing import Any

# ---- Response-construction primitives ------------------------------------------------------
# Shared by ScriptedClient and MockAnthropicClient below, and usable directly from tests that
# need exact control over a specific response.


# Deterministic fake time-to-first-token reported by every mock response (never a measurement).
MOCK_TTFT_SECONDS = 0.25


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
        _ttft_seconds=MOCK_TTFT_SECONDS,
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


MOCK_RETAILER = "MOCK"
MOCK_URL_FOUND = "https://mock.example/product/found"
MOCK_URL_UNVERIFIED = "https://mock.example/product/never-searched"


def mock_perception_payload() -> dict[str, Any]:
    """A schema-valid, obviously fake room (title-routed from `RoomPerception`)."""
    return {
        "schema_version": 1,
        "room_type": "MOCK living room",
        "dimensions": {
            "width_m": 4.0,
            "length_m": 5.0,
            "ceiling_height_m": 2.5,
            "confidence": 0.1,
            "derivation": "MOCK placeholder, not derived from the image",
        },
        "openings": [
            {"kind": "window", "wall": "east", "position_along_wall": 0.5, "width_m": 1.2,
             "note": "MOCK window"},
            {"kind": "door", "wall": "south", "position_along_wall": 0.2, "width_m": 0.9,
             "note": "MOCK door"},
        ],
        "fixed_items": [
            {"category": "radiator", "position": {"x": 0.9, "y": 0.5},
             "size": {"width_m": 0.2, "depth_m": 1.0}, "note": "MOCK fixed item"},
        ],
        "furniture": [
            {"id": "f1", "category": "sofa", "position": {"x": 0.3, "y": 0.6},
             "size": {"width_m": 2.0, "depth_m": 0.9}, "keepable": "unlikely",
             "note": "MOCK sofa, marked replaceable"},
            {"id": "f2", "category": "armchair", "position": {"x": 0.7, "y": 0.7},
             "size": {"width_m": 0.9, "depth_m": 0.9}, "keepable": "likely",
             "note": "MOCK armchair, marked keepable"},
        ],
        "furniture_gaps": [
            {"category": "coffee table", "reason": "MOCK: bare floor in front of sofa"},
            {"category": "floor lamp", "reason": "MOCK: dark corner beside armchair"},
        ],
        "living_things": [
            {"kind": "pet", "description": "MOCK pet, never furniture",
             "position": {"x": 0.5, "y": 0.8}},
        ],
        "art_and_decor": [
            {"kind": "wall art", "location": "north wall", "note": "MOCK decor"},
        ],
        "style_descriptors": ["mock modern", "mock warm wood", "mock bright"],
        "uncertainties": ["MOCK: placeholder data, not a real room"],
    }  # fmt: skip


def mock_retrieval_payload(slot: str) -> dict[str, Any]:
    """Clearly labelled fake candidates; one URL is deliberately absent from the search results,
    so the unverified-URL guard visibly drops it in every mock run."""
    return {
        "slot": slot,
        "candidates": [
            {"name": f"MOCK {slot} (fake)", "retailer": MOCK_RETAILER, "price": 123.45,
             "currency": "USD", "url": MOCK_URL_FOUND},
            {"name": f"MOCK {slot} (fake, URL not in search results)", "retailer": MOCK_RETAILER,
             "price": 99.0, "currency": "USD", "url": MOCK_URL_UNVERIFIED},
        ],
        "note": "MOCK data from the mock client; nothing here is a real product",
    }  # fmt: skip


MOCK_INVENTED_ID = "mock-invented-id"


def mock_rank_payload(request_text: str) -> dict[str, Any]:
    """Finalists for a rank request: the first N real ids from its list, plus one invented id.

    The invented id is deliberate: the id-validation step visibly drops it in every mock run.
    """
    ids = re.findall(r"^- id: (\S+) \|", request_text, flags=re.MULTILINE)
    match = re.search(r"Pick at most (\d+) finalists", request_text)
    n = int(match.group(1)) if match else 2
    finalists = [{"id": i, "reason": "MOCK pick: first on the list"} for i in ids[:n]]
    finalists.append({"id": MOCK_INVENTED_ID, "reason": "MOCK: id not in the candidate list"})
    return {"finalists": finalists}


def _request_text(kwargs: dict[str, Any]) -> str:
    content = kwargs["messages"][0]["content"]
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if b.get("type") == "text")


def _slot_from_request(kwargs: dict[str, Any]) -> str:
    """The retrieval user message names its slot on a line starting 'Slot: '."""
    content = kwargs["messages"][0]["content"]
    text = (
        content
        if isinstance(content, str)
        else " ".join(b.get("text", "") for b in content if b.get("type") == "text")
    )
    for line in text.splitlines():
        if line.startswith("Slot: "):
            return line.removeprefix("Slot: ").strip()
    return "unknown slot"


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
        title = schema.get("title")
        if title == "RankedFinalists":
            return response(
                [text_block(mock_rank_payload(_request_text(kwargs)))],
                input_tokens=900,
                output_tokens=80,
            )
        if title == "RoomPerception":
            return response(
                [text_block(mock_perception_payload())], input_tokens=1800, output_tokens=700
            )
        if title == "SlotCandidates":
            slot = _slot_from_request(kwargs)
            return response(
                [
                    *search_blocks(f"MOCK query: {slot}", [MOCK_URL_FOUND]),
                    text_block(mock_retrieval_payload(slot)),
                ],
                input_tokens=2500,
                output_tokens=250,
                searches=1,
            )
        payload = _placeholder_for(schema)
        uses_search = bool(kwargs.get("tools"))
        if uses_search:
            content = [
                *search_blocks("mock query", ["https://mock.example/source"]),
                text_block(payload),
            ]
            return response(content, input_tokens=300, output_tokens=150, searches=1)
        return response([text_block(payload)], input_tokens=200, output_tokens=60)
