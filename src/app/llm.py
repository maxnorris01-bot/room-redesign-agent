"""Budgeted, traced wrapper around the Anthropic Messages API.

Every request goes through `complete`, which:
  * counts one step per API request and checks the cost cap after each one (`Budget`),
  * records a `tracing.span` tagged with the prompt version,
  * resumes server-side tool turns that stop with `pause_turn`,
  * streams the request when both `stream=True` is passed and the live client is in use (see
    `_create_message`) - a buffered `create()` call can sit fully idle until the entire response
    is ready. For any call that might involve multiple search/tool rounds or a long generation,
    pass `stream=True`: a non-streamed call with a short client timeout is exactly what produced a
    real, reproduced `APITimeoutError` on a search-heavy call in the first project built from this
    template (see docs/lessons-learned.md). Leave it `False` for short, single-turn calls.

`get_client` switches on `Config.llm_mode` ("mock" -> `app.mock_llm.MockAnthropicClient`, "live"
-> the real SDK client) so every entry point is free by default - see `Config.llm_mode`'s
docstring in `app.config`.

Web-search-specific pieces (`web_search_tool`, the search-related fields on `Completion`,
`_web_searches`) assume Claude's built-in web search tool. Delete them if a project doesn't use
search; keep them if it does (research/retrieval-style calls - most agent projects built from this
template will).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol, cast

import anthropic
from anthropic.types import Message

from app.config import Config, check_budget
from app.mock_llm import MockAnthropicClient
from app.prompts import Prompt
from app.tracing import span


class _MessagesClient(Protocol):
    """Structural type for whatever `get_client` returns - the real SDK client or the mock."""

    @property
    def messages(self) -> Any: ...


# USD per million tokens (input, output). Verify against current pricing when starting a new
# project - these change, and an unknown/renamed model falls back to the most expensive known
# rate below so the cost cap stays conservative rather than silently under-counting.
PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-fable-5-1": (10.0, 50.0),
}
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25
WEB_SEARCH_USD_PER_REQUEST = 0.01
WEB_SEARCH_TOOL_TYPE = "web_search_20260209"  # dynamic filtering; needs Sonnet 4.6+/Opus 4.6+

REQUEST_TIMEOUT_S = 120.0


class LLMOutputError(RuntimeError):
    """The model's response was unusable (refusal, truncation, or malformed JSON)."""


class Budget:
    """Step and cost accounting for one `pipeline.run` call."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.steps = 0
        self.cost_usd = 0.0

    def start_step(self) -> None:
        """Reserve a step before making a request, so the cap is hit before spending."""
        check_budget(self.config, self.steps + 1, self.cost_usd)
        self.steps += 1

    def add_cost(self, usd: float) -> None:
        self.cost_usd += usd
        check_budget(self.config, self.steps, self.cost_usd)


@dataclass
class Completion:
    text: str
    retrieved_urls: set[str] = field(default_factory=set)


@lru_cache(maxsize=2)
def get_client(mode: str) -> _MessagesClient:
    """Return the client for `mode` ("mock" or "live"), cached per mode.

    Called with `budget.config.llm_mode`, so which client answers is controlled entirely by
    `Config.llm_mode` / `APP_LLM_MODE`. Tests should bypass this by monkeypatching the function
    itself, so its signature is the only thing they need to track.
    """
    if mode == "mock":
        return MockAnthropicClient()
    return anthropic.Anthropic(timeout=REQUEST_TIMEOUT_S)


def web_search_tool(max_uses: int) -> dict[str, Any]:
    return {"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": max_uses}


def estimate_cost(model: str, usage: Any) -> float:
    # Unknown models are priced at the most expensive known rate so the cap stays conservative.
    in_price, out_price = PRICING.get(model, max(PRICING.values()))
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    token_cost = (
        usage.input_tokens * in_price
        + cache_read * in_price * CACHE_READ_MULTIPLIER
        + cache_write * in_price * CACHE_WRITE_MULTIPLIER
        + usage.output_tokens * out_price
    ) / 1_000_000
    return float(token_cost + _web_searches(usage) * WEB_SEARCH_USD_PER_REQUEST)


def _web_searches(usage: Any) -> int:
    server_tool_use = getattr(usage, "server_tool_use", None)
    return int(getattr(server_tool_use, "web_search_requests", 0) or 0)


def _inspect_tool_blocks(content: list[Any]) -> tuple[list[dict[str, Any]], set[str]]:
    """Return (search log for the trace, URLs the search tool actually returned)."""
    searches: list[dict[str, Any]] = []
    urls: set[str] = set()
    for block in content:
        if block.type == "server_tool_use" and block.name == "web_search":
            searches.append({"query": block.input.get("query")})
        elif block.type == "web_search_tool_result":
            if isinstance(block.content, list):
                urls.update(r.url for r in block.content)
                searches.append({"results": len(block.content)})
            else:  # server-tool errors arrive as a 200 with an error object, not an exception
                searches.append({"error": getattr(block.content, "error_code", "unknown")})
    return searches, urls


def _create_message(
    client: _MessagesClient, config: Config, params: dict[str, Any], stream: bool
) -> Message:
    """Issue one request, streaming it on the live client when `stream` is set.

    Only the live client streams: a mock client that only implements `.create()` is unaffected by
    `stream` either way. `.get_final_message()` returns the same `Message` shape `.create()` does,
    so nothing downstream (usage/tool-block inspection, pause_turn handling, text extraction)
    needs to know which path was taken.
    """
    if stream and config.llm_mode == "live":
        with client.messages.stream(**params) as stream_ctx:
            return cast(Message, stream_ctx.get_final_message())
    return cast(Message, client.messages.create(**params))


def complete(
    *,
    span_name: str,
    prompt: Prompt,
    model: str,
    user_text: str,
    budget: Budget,
    max_tokens: int,
    schema: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    effort: str | None = None,
    stream: bool = False,
) -> Completion:
    """Run one logical model call (resuming `pause_turn`) and return the final text.

    Pass `stream=True` for calls that can run long (research/tool-heavy calls) - see
    `_create_message`. Leave it `False` (default) for short calls with nothing to fix.
    """
    config = budget.config
    client = get_client(config.llm_mode)
    messages: list[Any] = [{"role": "user", "content": user_text}]
    output_config: dict[str, Any] = {}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    if effort is not None:
        output_config["effort"] = effort

    retrieved: set[str] = set()
    while True:
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": prompt.text,
            "messages": messages,
        }
        if tools:
            params["tools"] = tools
        if output_config:
            params["output_config"] = output_config

        with span(
            span_name,
            config=config,
            prompt_version=prompt.version,
            prompt_name=prompt.name,
            model=model,
        ) as rec:
            budget.start_step()
            rec["step"] = budget.steps
            response = _create_message(client, config, params, stream)
            searches, urls = _inspect_tool_blocks(list(response.content))
            retrieved |= urls
            cost = estimate_cost(model, response.usage)
            rec.update(
                stop_reason=response.stop_reason,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                web_searches=_web_searches(response.usage),
                searches=searches,
                cost_usd=round(cost, 5),
                request_id=getattr(response, "_request_id", None),
            )
            budget.add_cost(cost)

        if response.stop_reason == "pause_turn":
            # Long server-tool turn: re-send with the assistant content appended to resume.
            messages.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason in ("max_tokens", "refusal"):
            raise LLMOutputError(f"{span_name}: response ended with {response.stop_reason}")
        # If every call here passes a schema, structured output guarantees the *first* text block
        # is the valid JSON - take only that one. Joining every text block together can silently
        # concatenate two separate JSON objects back-to-back into one unparseable string on a call
        # that happens to emit more than one text block.
        text_blocks = [b.text for b in response.content if b.type == "text"]
        if not text_blocks:
            raise LLMOutputError(f"{span_name}: response has no text content")
        return Completion(text=text_blocks[0], retrieved_urls=retrieved)


def parse_json_object(text: str, *, what: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMOutputError(f"{what}: model did not return valid JSON: {text[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise LLMOutputError(f"{what}: expected a JSON object, got {type(parsed).__name__}")
    return parsed
