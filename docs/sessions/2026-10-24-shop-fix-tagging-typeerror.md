# Session: fix the tagging TypeError

Branch `shop-fix-tagging-typeerror` (from main at PR #21). No network call was made.

## What happened
The owner's live `make shop-diagnose` failed all three calls with "FAILED before a response: TypeError".
Reproduced offline with the real Anthropic SDK client (1.8.0) over an httpx `MockTransport`:
`TypeError: Messages.stream() got an unexpected keyword argument 'temperature'`, raised at
`src/app/llm.py:199` (`_create_message`), reached from `src/app/shop/candidates.py` `run_call`. The SDK's
`messages.create` and `messages.stream` have no `temperature` parameter. Stage 2g added `temperature=0` to
the tagging call, so every live candidate-tagging call failed before sending anything, and the exception
was not caught by the tagging handler (it caught API and output errors only), so it would also have failed
a live Shop Again. The earlier "response has no text content" failures (stage 2f) predate that argument and
remain unexplained; the first real answer to them will come from running the diagnostic again.

## What changed
- `temperature` removed from `llm.complete` and the tagging call.
- A client-side exception in a tagging call is recorded (class, redacted message, repository file and line)
  and the Shop Again continues; a budget error still propagates (`src/app/shop/candidates.py`,
  `src/app/shop/redact.py`).
- `scripts/shop_diagnose_tagging.py` prints the redacted message and the raised-at location.
- `tests/test_shop_sdk_shapes.py` (19 tests): every call shape through the real SDK over a mock transport.
- ADR 0012: a section on the fix, the fields not sent and how to read the diagnostic's new lines.

## Other call paths
Parse, rank, preference and candidate-tag calls were all run through the real client: none passes an
argument the installed SDK rejects; their bodies hold only SDK fields. The only mismatch was `temperature`.

## Not verified
Whether the API accepts `strict` on the tool for this model (a 400 falls back to rows on the one retry),
and the cause of the earlier no-text-content answers.

## Next command
From ~/Desktop/Projects/room-redesign-agent: `make shop-diagnose`
