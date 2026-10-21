# Session 2026-10-21: latency and retrieval hygiene

Branch `latency-and-retrieval-hygiene`. Mock mode only: no live call, no `make spike-live`, `.env` not read.

## Why

The first live run (20261021T032130Z-4a360a) took 35.9 s and $0.0762: perception 17.3 s (1,936 output
tokens), then two retrieval slots back to back (11.1 s and 7.5 s). Its retrieval returned listing and
keyword URLs, and its perception output was long, with one fused word ("coolpale") and two doubtful
furniture gaps. There is no hard time limit now (the owner first said 20 s, then "judge the wait by
feel on real runs"), so the goal was to cut wall-clock time where cheap and safe, and to make timing
measurable.

## What changed

- **Concurrent retrieval.** Slots run in a `ThreadPoolExecutor` (one worker per slot) and results come
  back in slot order. `Budget` now has a lock around step and cost accounting, and `start_step`
  returns the reserved step number. The step cap is exact under concurrency. The cost cap is not: calls
  already in flight when it trips still complete and bill (documented on the class). If a slot fails,
  the others finish and the first failure in slot order is re-raised.
- **Product-page URL guard.** `retrieval.is_product_page_url` / `product_page_rejection`. A candidate
  must still have appeared in the search results; then its URL is rejected if it has a listing segment
  (`collections`, `category`, `categories`, `search`, `browse`, `s`, `cat`), ends in `keyword.php`, has no
  path, ends in a bare `/c`, or has a query key `keyword`, `q`, `query`, `search` or `filter`. Reason
  strings go to `dropped_unverified` (`url_not_in_search_results`, `not_a_product_page:<rule>`).
  Two deliberate departures from the brief: `cat` was added (IKEA category pages), and
  `/collections/<x>/products/<handle>` is accepted because that is a normal Shopify product URL.
- **CLI flags** on `scripts/spike.py`: `--perception-model`, `--perception-effort`, `--image-long-edge`,
  `--perception-only`, `--perception-prompt-version` (default 2). `--budget` and `--style` are only
  required for a full run. Environment defaults are unchanged.
- **Perception prompt v2** (`prompts/perception.md`; v1 kept as `prompts/perception.v1.md`,
  loadable via `load_prompt("perception", "1")`). Length limits, 3 to 5 separate lowercase style
  phrases, conservative gaps with a visible reason. No `maxItems`/`maxLength` anywhere, schema
  version still 1. Each result records `perception_output_stats` (counts and longest note in words) so
  the effect of v2 is measurable. The mock fixture now has the v2 shape.
- **Timing.** Streaming records `time_to_first_token_seconds` (first `content_block_delta` event; a
  lower bound on when text could appear, since thinking may come first). It is a column in `table.txt`
  and a field on each row in `result.json`; mock mode reports a fixed 0.25. `result.json` `totals`
  now has `seconds` (plain sum), `critical_path_seconds` and `wall_clock_seconds`. Critical path is
  prepare_image + perception + the slowest slot; I included prepare_image (about 0.07 s) because it is
  real sequential time.
- `result.json` also stores `perception_raw_text` (the model's JSON before parsing; same data as
  `perception`).

## "coolpale"

The model produced it. The string is only in the parsed output; our pipeline does no string operation
on those fields; the v1 prompt contains neither word; image prep and schema stripping cannot reach it.
The raw text was not saved in that run, so this is by elimination, not by direct sight. Details in
`docs/working-notes-and-decisions.md`.

## Tests

89 passed (46 before). New: `test_concurrency.py` (7: true parallelism via a barrier, exact totals,
slot order when calls finish out of order, step cap and cost cap under concurrency, counters under
8 threads, a failed slot), `test_product_urls.py` (the two live-run negatives, six positives, each
rule), `test_timing_and_flags.py` (ttft, critical path, perception-only, streaming ttft with a fake
stream, prompt versions, CLI flags, passthrough). Ran the suite three times to check for flakiness.
`make lint`, `make typecheck` and mock `make eval-fast` pass; thresholds untouched.

## Surprising or worth knowing

- `--perception-only` prints two rows (prepare_image and perception), not one. I kept prepare_image so
  the totals add up.
- The brief's example `make spike ARGS='spike-photos/room1.png --perception-only'` would have failed
  because `--budget`/`--style` were required; they now are not in that mode.
- Concurrency can make the cost cap overshoot slightly. With the current caps ($0.25) and about $0.02
  per slot this is small, but it is new behavior.
- The guard is URL-only. It cannot tell a product page from an unusual listing URL, and prices are
  still not verified.
- Nothing in this session was measured live. Whether v2 shortens perception output, and by how
  much, is the first thing to check.

## Next

```bash
make spike-live ARGS='spike-photos/room1.png --perception-only --perception-prompt-version 1'
make spike-live ARGS='spike-photos/room1.png --perception-only'
```
(Max's call; compare output tokens, seconds and time to first token.)
