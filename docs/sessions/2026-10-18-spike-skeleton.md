# Session 2026-10-18: repo setup and spike skeleton

Branch `feat/spike-skeleton`. Read this cold: no other context is assumed.

## What this session was for

`SPEC.md` "Session 1": measure the real cost and latency of the call pattern (perception, then
retrieval) *before* building the self-check loop, canvas, service or evals. This session built the
measuring instrument and ran it only in mock mode. **No live API call was made**; every command set
`APP_LLM_MODE=mock`, and the final spike run was done with `ANTHROPIC_API_KEY` unset.

## What changed and why

**Setup (chore).** Template placeholders replaced (README, badge, pyproject, package docstring).
`CLAUDE.md` now points at `SPEC.md` as the contract and states: mock is the default, live runs are
Max's call only, the photo rules, the structured-output gotchas. `.gitignore` covers `spike-photos/`
and `spike-output/`. `eval-standard` and `eval-nightly` now have mock-forced and `-live` forced
targets (same split as `eval-fast`); `spike` (forced mock) and `spike-live` (forced live) run
`scripts/spike.py` with `ARGS`. Recipe lines verified as real tabs with awk. `eval-fast` is unchanged.

**Image path (`src/app/images.py`).** `prepare_image` applies EXIF orientation, rebuilds the image
from raw pixels (so no EXIF/GPS/XMP/ICC survives), downscales to a configurable long edge (default
1568 px, never upscales) and re-encodes JPEG. `app.llm.complete` takes `images=[...]` and puts image
blocks before the text.

**Pricing (`src/app/pricing.py`).** The only place prices live. See "Pricing values" below.
`complete` now returns per-call totals (tokens, searches, estimated dollars, steps, search log) on
`Completion`, and passes `max_retries` (default 1, `APP_SDK_MAX_RETRIES`) to the live client.

**Perception.** `prompts/perception.md` v1 and `RoomPerception` (`schema_version` 1) in
`src/app/schemas.py`. One streamed, schema-constrained, multimodal call through `complete`. Contents:
dimensions with confidence and derivation note, openings (kind, wall, position), fixed items,
furniture (category, normalized position, rough size, keepable likely/unlikely/unclear),
`furniture_gaps` (categories the room lacks), living things (separate list, never furniture), art
and decor, style descriptors, uncertainties. Bounds are validated by pydantic after parsing; the JSON
Schema sent to the API has `maxItems`, `maxLength` and numeric bounds stripped.

**Retrieval, configuration A.** `prompts/retrieval.md` v1 and `src/app/retrieval.py`. Slots are
derived in code (replaceable furniture, then gaps, then room-type defaults; up to N, default 3;
budget split equally). Each slot is one streamed call with Claude's web search tool
(`max_uses` = `APP_MAX_SEARCHES_PER_SLOT`, default 3). A candidate is dropped unless its URL was in
that call's search results. Queries, result counts, search errors and dropped URLs go in the trace.

**Spike (`src/app/spike.py`, `scripts/spike.py`).** `python scripts/spike.py <photo> --budget N --style
"..." [--slots K]` (or `--synthetic` for a generated image). One shared `Budget`, so
`Config.max_steps` and `max_cost_usd` cap the whole run and raise `BudgetExceededError` (exit 2)
rather than truncating. Prints the per-stage table, totals, and the comparison to the proposed
targets labelled "proposed targets, informational" (never enforced, never in
`evals/thresholds.yaml`). Writes `spike-output/<run_id>/{result.json,table.txt,trace.jsonl}`.

**Mock mode.** `mock_llm` routes by schema title: `RoomPerception` gets a labelled placeholder room;
`SlotCandidates` gets two fake candidates (retailer `MOCK`), one with a URL deliberately absent from
the search results so the guard visibly fires. The table opens with a banner saying the numbers are
fake.

## Decisions you may want to revisit

Full reasoning is in `docs/working-notes-and-decisions.md`. Short list: basic web search tool as
default instead of the dynamic-filtering variants; thinking effort defaults (medium/low) are guesses;
slots run sequentially (latency lever for later); equal budget split across slots; `furniture_gaps`
added to the perception schema (not in your field list, needed for slot derivation).

## Asked mid-session

Nothing. The one deviation from the brief: it suggested `claude-haiku-5-5` "to verify". It is real
(models overview page), so it is the retrieval default.

## Pricing values (all in `src/app/pricing.py`)

Source: https://platform.claude.com/docs/en/about-claude/pricing, fetched 2026-10-18; web search
price from https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool (same day).
USD per million tokens (input / output / cache read / 5-min cache write):

| Model | in | out | cache read | cache write |
|---|---|---|---|---|
| claude-sonnet-5-5 (perception default) | 2.00 | 10.00 | 0.10 | 2.50 |
| claude-haiku-5-5 (retrieval default), prompt <= 100k | 0.10 | 0.50 | 0.01 | 0.125 |
| claude-haiku-5-5, prompt > 100k | 0.50 | 2.50 | 0.05 | 0.625 |
| claude-haiku-4-5 | 1.00 | 5.00 | 0.10 | 1.25 |
| claude-sonnet-5 / -4-6 | 2.00 / 3.00 | 10.00 / 15.00 | 0.20 / 0.30 | 2.50 / 3.75 |
| claude-opus-5-5 / -5 | 4.00 / 5.00 | 20.00 / 25.00 | 0.20 / 0.50 | 5.00 / 6.25 |
| claude-fable-5-1 | 10.00 | 50.00 | 0.25 | 12.50 |

Web search: $10 per 1,000 searches, so $0.01 per search (`APP_WEB_SEARCH_USD` overrides).

## Could not verify

- Whether `claude-haiku-5-5` accepts structured output + `web_search_20250305` + `output_config.effort`
  in one request. Docs say the basic tool is available broadly; I did not confirm per model (needs the
  Models API `capabilities`, which needs a key) or a live call.
- Thinking-token volume per stage (they bill as output tokens and are not reported separately).
- That `effort` values "medium"/"low" are sensible for these stages (guesses).
- Whether `usage.server_tool_use.web_search_requests` counts failed searches (docs say failed searches
  are not billed; the estimate may slightly overcount).
- Image token cost at 1568 px is not modeled separately; it appears in `usage.input_tokens`.

## Checks (all with `APP_LLM_MODE=mock`)

- `make install`: ok. `make lint`: clean. `make typecheck` (mypy --strict): clean.
- `make test`: 46 passed.
- `make eval-fast` (mock): pass_rate 1.0, p95 0.0004 s, mean cost $0, 2 cases. This is a plumbing check
  only. `evals/thresholds.yaml` and eval cases were not touched. No live eval was run (CLAUDE.md:
  prompts changed, so `make eval-fast-live` is the real quality gate, but there are no eval cases
  for these prompts yet, and live runs are Max's call).
- `make spike ARGS='--synthetic --budget 800 --style "warm minimalist" --slots 3'` with
  `ANTHROPIC_API_KEY` unset: ok. Repeated with `socket.connect`, `create_connection` and
  `getaddrinfo` patched to raise: still exit 0, so no network call is attempted. A test also asserts
  the real `anthropic.Anthropic` is never constructed in mock mode.
- Live-eval numbers vs thresholds: not applicable (no live run).

## Open questions / flag before merging

- The mock table's dollar figures look plausible. The banner says they're fake; keep that in mind if
  the table is ever pasted somewhere.
- `Config.max_cost_usd` = $0.25 and `max_steps` = 12 are still the template's guesses. 3 slots need
  about 4 steps if no `pause_turn` resumes happen; a search-heavy slot that pauses will use more.
- CI's `eval-standard` / `eval-nightly` jobs now invoke the mock-forced targets (they were effectively
  mock before too). Point them at `-live` deliberately later.
- `schemas.to_output_schema` strips any dict key named `title`/`default` at every level; no current
  field uses those names, but a future field called `title` would vanish from the schema.

## Next command

```bash
git diff main --stat && make test && make spike ARGS='--synthetic --budget 800 --style "warm minimalist"'
```
