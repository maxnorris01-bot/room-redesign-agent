# Exploratory live run, one room (2026-10-20)

Ungraded, by design (`SPEC.md`, ADR plan: explore one room, then lock targets in ADR 0004 before the measured 3-room run). Run by Max; interpreted in a Cowork chat. Raw output is in `spike-output/20261021T032130Z-4a360a/` (gitignored). The photo is the same staged listing-style living room used in `docs/research/2026-10-18-perception-exploration.md`; it is not committed.

## Run

`make spike-live ARGS='spike-photos/room1.png --budget 800 --style "warm minimalist"'`. Defaults untouched: perception `claude-sonnet-5-5`, retrieval `claude-haiku-5-5`, basic `web_search_20250305`, effort medium / low, prompts v1, schema v1. Image sent at 1568 x 1001 (from 2444 x 1560), EXIF stripped.

| Stage | Model | In tok | Out tok | Searches | USD | Seconds |
|---|---|---|---|---|---|---|
| prepare_image | - | 0 | 0 | 0 | 0.0000 | 0.07 |
| perception | claude-sonnet-5-5 | 5,616 | 1,936 | 0 | 0.0306 | 17.28 |
| retrieval: media unit or TV | claude-haiku-5-5 | 21,328 | 1,425 | 2 | 0.0228 | 11.12 |
| retrieval: side table beside sofa | claude-haiku-5-5 | 23,720 | 828 | 2 | 0.0228 | 7.46 |
| **Total** | | 50,664 | 4,189 | 4 | **0.0762** | **35.93** |

Only 2 slots were derived (both from gaps), not the default 3. Estimated cost is from API usage plus $0.01 per search; not yet compared with the Console's usage for the key.

## Where the cost and time went

- Cost: web searches $0.040 (52%), perception $0.031 (40%), retrieval tokens $0.006 (7%). Haiku tokens are almost free; searches are the retrieval cost. Perception output is 63% of perception cost (1,936 tokens at $10/MTok).
- Time (sequential): perception 17.3 s (48%), slot 1 11.1 s (31%), slot 2 7.5 s (21%). Running the slots concurrently would give about 17.3 + 11.1 = 28.5 s for two slots (not measured). Each extra slot adds about $0.023 if it uses 2 searches, so 3 slots would be about $0.10.

## Against the proposed targets (informational, not locked)

- Perception plus retrieval cost: $0.0762 vs proposed at or under $0.08. Met only because 2 slots ran; 3 slots would be about $0.10.
- Perception plus retrieval time: 35.9 s vs proposed at or under 30 s. Missed sequentially; about 28.5 s if concurrent.
- Full-run targets ($0.20 mean, p95 60 s) cannot be judged: proposal and self-check stages do not exist yet and each adds at least one model call.

## Perception quality (schema v1, one photo)

Good: every visible furniture item, fixed item, opening and decor item was found, including the nesting tables, desk and chair, pouf and basket, two rugs, floor lamp and sconce. Sizes are plausible (sofa 2.1 m). The uncertainties list is honest (room dimensions rough, orientation assumed, out-of-frame walls, cropped items). Nothing was invented.

Problems:
- Plan geometry rests on the frontal-view convention ("north = wall farthest from camera") and the model says the orientation is assumed. Dimensions 6.0 x 5.0 m at confidence 0.35. It places the sofa on the "east wall" while listing the east wall as out of frame. This is the weakness the 2026-10-18 exploration predicted for a corner shot.
- Garbled text: a style descriptor reads "coolpale blue and white palette" (two words fused). Claim Verification saw stray characters in free text too. Check the raw text in `trace.jsonl` to see whether the model or our parsing caused it.
- `furniture_gaps` is the model's opinion: "media unit or TV" is a doubtful gap in a room whose focal point is a fireplace, and "side table beside sofa" ignores the nesting tables already there. Slot choice drives cost.
- Every furniture item is `keepable: likely` (staged room), so keepability gave no signal here.

## Retrieval quality (basic web search tool, 2 searches per slot)

- Slot 1 returned 2 candidates with prices, but both URLs are listing pages (a collection page and a Wayfair keyword-search page), not product pages. The URL guard only proves the URL appeared in the search results, so it passed them. Prices were sale prices shown on listing pages. The model said so in its note.
- Slot 2 returned no candidates. It said no verifiable product page and price was found and named leads to check. Honest, but nothing deliverable.
- Net: price accuracy and link validity (the SPEC's retrieval criteria) are weak with this configuration. Nothing fabricated, but few usable product links. This strengthens the case for comparing a shopping API (configuration B) and for defining "link validity" as a product page, not just a URL from the results.

## Implications for ADR 0004 (proposal, not decided)

Derive targets from the product, not from this run's numbers. A visitor should wait about a minute with streamed progress, and a $20 workspace limit should cover many live runs. A stage allocation within the existing full-run targets: perception at or under 20 s and $0.04; retrieval (slots concurrent) at or under 15 s and $0.10 for up to 4 slots; proposal plus self-check at or under 25 s and $0.06. Totals: 60 s and $0.20. The measured run then tests whether each stage fits its allocation.

## Follow-ups (see `docs/todo.md`)

1. Run retrieval slots concurrently (`Budget` is not thread-safe yet).
2. Make the retrieval guard require a product-page URL, not only a URL present in results; decide whether a fetch tool or a shopping API gets prices from product pages.
3. Trim perception output tokens (schema size and effort) and re-measure.
4. Check the raw trace for the "coolpale" fusion.
5. Decide whether gap detection should be conservative (only fill clear gaps).
6. Compare the Console's usage for the key with the $0.0762 estimate.
7. Research shopping API candidates now (configuration B).
