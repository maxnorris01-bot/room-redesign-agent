# Session: stop the model thinking away the token budget

Branch `shop-fix-thinking` (from main at PR #22). No network call was made.

## What the live diagnostic found
On claude-haiku-5-5 the model thinks by default. The rows call returned "thinking, text" blocks: with
`max_tokens` 1480 the thinking used the whole budget (stop reason `max_tokens`, no text, usage 0); with 3400
the thinking plus the answer hit the limit (97 of 100 rows). The forced `record_tags` tool call returned only
`tool_use`, 40 of 40 items in 7.3 s, no thinking. The stage 2f "no text content" failures are most likely the
same thing.

## What the SDK supports (anthropic 1.8.0)
`thinking` is a documented field of `messages.create` and `messages.stream`: `{"type": "disabled"}`,
`{"type": "enabled", "budget_tokens": n}`, `{"type": "adaptive"}`. `output_config.effort` takes low, medium,
high, xhigh, max. `usage.output_tokens_details.thinking_tokens` is reported when the model thought. The SDK
does not say which models accept which; a refusal is handled at run time.

## What changed
- `llm.complete(disable_thinking=True)` sends `thinking={"type": "disabled"}`; used by the parse, rank,
  preference and candidate-tagging calls (all of Shop Mode's). A 400 that names thinking is retried once
  without the field and remembered for that model.
- A response of only thinking blocks raises "the model used its whole budget thinking (stop reason ...,
  max_tokens N)", not "no text content".
- Candidate tagging: the tool call is the only format; the one retry round is the same tool call in pieces
  of half the batch. The rows parser stays for old logs and the diagnostic.
- A failed call that reports no usage is recorded as unknown, costed from the `max_tokens` ceiling and
  labelled "estimated (failed call)" in the footer, trace and diagnostic.
- Diagnostic: block types, thinking present and tokens, usage or unknown, "fields filled" per field.
- Tests: `tests/test_shop_thinking.py` (16) and updated `tests/test_shop_sdk_shapes.py`, through the real SDK
  over a mock transport; older retry tests updated.

## Not verified
Whether the API accepts `thinking: disabled` for claude-haiku-5-5 (a refusal is handled), whether it removes
the thinking in practice, and how many fields a title-only tagger fills (the new diagnostic lines show it).

## Next command
From ~/Desktop/Projects/room-redesign-agent: `make shop-diagnose`
