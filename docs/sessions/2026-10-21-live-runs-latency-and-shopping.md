# Live runs after the latency and shopping changes (2026-10-21)

Four live runs by Max the evening of 2026-10-20 (local), interpreted in a Cowork chat. Ungraded, one photo (`room1.png`, the staged living room), one run per configuration, so differences smaller than run-to-run noise are not findings. Raw output is in `spike-output/` (gitignored). The shopping run used code on the `two-stage-shopping-retrieval` branch before it was committed.

## Perception prompt v1 vs v2 (perception only)

Same photo and model (`claude-sonnet-5-5`), `--perception-only`, one run each.

| | v1 | v2 |
|---|---|---|
| Seconds | 12.94 | 9.07 |
| Time to first token | 2.12 s | 1.53 s |
| Output tokens | 1,838 | 1,288 |
| USD | 0.0296 | 0.0246 |
| decor / uncertainties / style / gaps | 11 / 6 / 5 / 2 | 6 / 4 / 5 / 0 |
| Longest note (words) | 34 | 10 |

The same v1 prompt took 17.3 s in the first exploratory run and 12.9 s here, so run-to-run swing is about 4 s and v2's 3.9 s gain is inside it. The output-token drop (30%) and the rule compliance are real: caps followed, notes within 12 words, style words not fused ("coolpale" did not recur in v1 either). v2 dropped some detail (basket, snake plants, orchid, sconce, cushions) and returned no gaps; both are acceptable for retrieval.

Run-to-run drift across the five perception outputs on this photo: room width 5.5 to 6.0 m, length 5.0 to 6.0 m, ceiling 2.6 to 3.0 m, window position 0.35 to 0.5, desk and chair `keepable` flipping between likely and unclear, floor "warm" in one run and "dark" in another. Evals on single runs will be noisy; use repeats or tolerances.

## Full run with web_search retrieval, v2, concurrent slots

`make spike-live ARGS='spike-photos/room1.png --budget 800 --style "warm minimalist"'` (run 20261021T034610Z-b50109, before shopping mode existed).

| Stage | Seconds | USD |
|---|---|---|
| prepare_image | 0.07 | 0 |
| perception | 9.57 (ttft 2.42) | 0.0236 |
| retrieval: area rug | 9.69 | 0.0231 |
| retrieval: floor lamp | 9.22 | 0.0230 |
| retrieval: bookshelf | 12.91 | 0.0234 |
| Sum / critical path | 41.45 / 22.55 | 0.0931 |

Concurrency works: the measured wall clock equals the critical path (22.55 s) against a 41.45 s sum, down from 35.9 s with two slots sequential. Each retrieval slot cost about $0.023, of which $0.02 was the two web searches (87%).

Quality was poor:
- All five surviving candidates were listing pages (Etsy `/market/...`, Wayfair family `/sb1/...-c<digits>-a<digits>.html`). The new product-page guard dropped only the two `keyword.php` URLs. The model's own notes said none was a product page.
- One price looks invented: $133.33 for an 8x10 hand-knotted wool rug is exactly half the $266.67 slot budget. The guard checks URLs, not prices.
- The three slots (rug, floor lamp, bookshelf) were all "default" slots. The room already has a rug and a floor lamp, listed under `art_and_decor`, and the "already owned" check only looked at furniture. Fixed in the shopping change.

This run is what motivated moving retrieval to structured shopping data (ADR 0005).

## Shopping retrieval (Serper search, Haiku rank, SerpApi lookup), `--finalists 1`

`make spike-live ARGS='spike-photos/room1.png --budget 800 --style "warm minimalist" --finalists 1'` (run 20261021T042417Z-a124a3). Perception v2 flagged one gap, so one slot ran.

| Stage | Seconds | USD |
|---|---|---|
| prepare_image | 0.08 | 0 |
| perception | 9.62 (ttft 1.93) | 0.0252 |
| media unit / search (Serper) | 1.80 | 0.0006 est. |
| media unit / rank (Haiku) | 1.88 | 0.0002 |
| media unit / lookup 1 (SerpApi) | 1.44 | 0.0250 est. |
| Sum / critical path | 14.83 / 14.83 | 0.0510 (est. included) |

Real Anthropic charge: about $0.0254. The two provider rows are estimated at assumed list prices and came out of free tiers (2 Serper credits, 1 SerpApi lookup).

Result: Article "Leif 54" media unit, `article.com/pla/26917/leif-54-media-unit-smoked-oak` (a real product page), $599.00 + $59 shipping = $658.00, both from the lookup, rank returned 1 valid id and 0 invented. 40 results returned, 25 considered, 13 over the $800 slot budget, 12 kept. `dimensions_m` was null (the lookup had none).

Retrieval for the slot took 5.1 s; the critical path was 14.8 s. With more slots the chains run concurrently, so the retrieval share should stay near 5 s; not yet measured with several slots.

Observations:
- The single slot received the whole $800 and the pick took $599 of it (75%). The equal-split budget heuristic only makes sense with several slots; allocation belongs to the proposal stage.
- Dimensions were available for 1 of 3 looked-up products so far (IKEA yes, Article twice no). A fit check will often lack sizes. Options: parse sizes from titles or URL slugs ("54"), or read the product page.
- Not exercised live: several slots in shopping mode, `--finalists 2`, lookup failure fallbacks, the web_search mode after the change.

## Cost and Console check

After the first exploratory run Console showed $0.08 against the $0.0762 estimate, which validates the cost code. After the four runs above the expected cumulative Anthropic spend is about $0.25 (0.0762 + 0.0296 + 0.0246 + 0.0931 + 0.0254); not yet read from Console. Provider credits used: Serper 4 credits (two queries), SerpApi 4 searches (three test calls plus one lookup), all inside free tiers.

## Against the proposed targets (still informational; see ADR 0004)

- Perception plus retrieval critical path: 14.8 s with shopping, against the old 30 s proposal and the owner's 20 s limit. Proposal and self-check are not built, and each adds model time.
- Anthropic cost per run for perception plus retrieval: about $0.025 with one slot.
- Provider cost depends on lookups: at the assumed prices, three slots with two finalists is about $0.15 at list price and $0 on free tiers.

## Follow-ups

1. Print a warning when a lookup fails (currently only in `result.json` under `summary.lookup_errors`).
2. Budget allocation across slots in the proposal stage.
3. Dimensions fallback (title and URL parsing, or product page).
4. Run several slots and `--finalists 2` once, then the measured 3-room spike.
5. Confirm both provider prices from the real dashboards.
