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
`pricing.web_searches`) assume Claude's built-in web search tool. Delete them if a project
doesn't use
search; keep them if it does (research/retrieval-style calls - most agent projects built from this
template will).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol, cast

import anthropic
from anthropic.types import Message

from app.config import Config, check_budget
from app.images import PreparedImage
from app.mock_llm import MockAnthropicClient
from app.pricing import estimate_cost, web_searches
from app.prompts import Prompt
from app.tracing import span


class _MessagesClient(Protocol):
    """Structural type for whatever `get_client` returns - the real SDK client or the mock."""

    @property
    def messages(self) -> Any: ...


WEB_SEARCH_TOOL_TYPE = "web_search_20250305"  # default; Config.web_search_tool overrides

REQUEST_TIMEOUT_S = 120.0


class LLMOutputError(RuntimeError):
    """The model's response was unusable (refusal, truncation, or malformed JSON)."""


class Budget:
    """Step and cost accounting for one `pipeline.run` call. Safe to share across threads.

    Under concurrency the step cap is exact (a step is reserved atomically before its request goes
    out). The cost cap cannot be: requests already in flight when the cap trips still complete and
    bill, so a concurrent run can overshoot `max_cost_usd` by up to the in-flight calls' cost.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.steps = 0
        self.cost_usd = 0.0
        self._lock = threading.Lock()

    def start_step(self) -> int:
        """Reserve a step before making a request, so the cap is hit before spending.

        Returns the reserved step number (1-based), read under the lock.
        """
        with self._lock:
            check_budget(self.config, self.steps + 1, self.cost_usd)
            self.steps += 1
            return self.steps

    def add_cost(self, usd: float) -> None:
        with self._lock:
            self.cost_usd += usd
            check_budget(self.config, self.steps, self.cost_usd)


@dataclass
class Completion:
    text: str
    retrieved_urls: set[str] = field(default_factory=set)
    # Totals across every request this logical call made (pause_turn resumes add requests).
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_searches: int = 0
    cost_usd: float = 0.0
    steps: int = 0
    # Seconds from sending the first request to its first streamed content delta; None when the
    # call was not streamed. First request only (a pause_turn resume does not reset it).
    time_to_first_token_s: float | None = None
    # Search log across requests: {"query": ...}, {"results": n} or {"error": code} entries.
    searches: list[dict[str, Any]] = field(default_factory=list)


@lru_cache(maxsize=4)
def get_client(mode: str, max_retries: int = 1) -> _MessagesClient:
    """Return the client for `mode` ("mock" or "live"), cached per mode.

    Called with `budget.config.llm_mode`, so which client answers is controlled entirely by
    `Config.llm_mode` / `APP_LLM_MODE`. Tests should bypass this by monkeypatching the function
    itself, so its signature is the only thing they need to track.
    """
    if mode == "mock":
        return MockAnthropicClient()
    return anthropic.Anthropic(timeout=REQUEST_TIMEOUT_S, max_retries=max_retries)


def web_search_tool(max_uses: int, tool_type: str = WEB_SEARCH_TOOL_TYPE) -> dict[str, Any]:
    return {"type": tool_type, "name": "web_search", "max_uses": max_uses}


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
) -> tuple[Message, float | None]:
    """Issue one request, streaming it on the live client when `stream` is set.

    Returns `(message, time_to_first_token_s)`. Only the live client streams: a mock client that
    only implements `.create()` is unaffected by `stream`; it reports a deterministic fake
    time-to-first-token from the response's `_ttft_seconds` attribute when it has one.
    `.get_final_message()` returns the same `Message` shape `.create()` does, so nothing downstream
    (usage/tool-block inspection, pause_turn handling, text extraction) needs to know which path
    was taken.

    "First token" is the first `content_block_delta` event of any kind: with thinking on, the
    visible text may start later, so this is a lower bound on when the user could see output.
    """
    if stream and config.llm_mode == "live":
        started = time.perf_counter()
        first: float | None = None
        with client.messages.stream(**params) as stream_ctx:
            for event in stream_ctx:
                if first is None and getattr(event, "type", None) == "content_block_delta":
                    first = time.perf_counter() - started
            return cast(Message, stream_ctx.get_final_message()), first
    message = cast(Message, client.messages.create(**params))
    ttft = getattr(message, "_ttft_seconds", None) if stream else None
    return message, ttft


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
    images: list[PreparedImage] | None = None,
    effort: str | None = None,
    stream: bool = False,
) -> Completion:
    """Run one logical model call (resuming `pause_turn`) and return the final text.

    Pass `stream=True` for calls that can run long (research/tool-heavy calls) - see
    `_create_message`. Leave it `False` (default) for short calls with nothing to fix.
    """
    config = budget.config
    client = get_client(config.llm_mode, config.sdk_max_retries)
    # Images go first, then the text: the documented best placement for vision prompts.
    user_content: str | list[dict[str, Any]] = user_text
    if images:
        user_content = [
            *(img.content_block() for img in images),
            {"type": "text", "text": user_text},
        ]
    messages: list[Any] = [{"role": "user", "content": user_content}]
    output_config: dict[str, Any] = {}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    if effort is not None:
        output_config["effort"] = effort

    retrieved: set[str] = set()
    done = Completion(text="", model=model)
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
            step = budget.start_step()
            rec["step"] = step
            response, ttft = _create_message(client, config, params, stream)
            if done.steps == 0:
                done.time_to_first_token_s = ttft
            searches, urls = _inspect_tool_blocks(list(response.content))
            retrieved |= urls
            cost = estimate_cost(model, response.usage, web_search_usd=config.web_search_usd)
            usage = response.usage
            done.input_tokens += usage.input_tokens
            done.output_tokens += usage.output_tokens
            done.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
            done.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
            done.web_searches += web_searches(usage)
            done.cost_usd += cost
            done.steps += 1
            done.searches.extend(searches)
            rec.update(
                stop_reason=response.stop_reason,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                web_searches=web_searches(response.usage),
                searches=searches,
                cost_usd=round(cost, 5),
                time_to_first_token_s=None if ttft is None else round(ttft, 4),
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
        done.text = text_blocks[0]
        done.retrieved_urls = retrieved
        return done


def parse_json_object(text: str, *, what: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMOutputError(f"{what}: model did not return valid JSON: {text[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise LLMOutputError(f"{what}: expected a JSON object, got {type(parsed).__name__}")
    return parsed
