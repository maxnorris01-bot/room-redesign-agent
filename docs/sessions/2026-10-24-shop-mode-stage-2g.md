# Session: Shop Mode stage 2g, bounded, fast, and no duplicates

Branch `shop-mode-stage-2g` (from main at PR #20). Nothing was committed or run live.

## What changed and why
The owner's fourth live session (stage 2f) took 98.9 s: 8 of 10 candidate-tagging calls failed with "no text
content" and each failure retried in halves; 265 candidates from 7 sequential searches were all sent for
tagging; duplicates reached the grid; the banner was a paragraph; 17 of 24 results with no explanation.

- Tagging (`src/app/shop/candidates.py`): a forced `record_tags` tool call with a strict schema, batches of
  40 run concurrently, 12 s a request, 15 s in all (and never past the hard stop), temperature 0, no
  thinking, one retry per batch (none for a fast no-text failure, none past the deadline), text-rows kept as
  the fallback and the retry format. Prompt `shop_candidate_tags` v3 (v1, v2 kept). `llm.complete` gained
  `tool_choice`, `temperature`, `timeout_s`; its errors carry stop reason, block types and cost.
- Only the top 60 candidates by rank are tagged (the rank call now comes first); keywords stand in for the
  rest; guards and supply act on any tagged candidate; the trace says how many from each source.
- Speed: concurrent searches (thread safe providers only), the preference call, the tagging of what was
  shown and the typed first page at the same time; a 30 s target and 45 s hard stop; phase timings; a Shop
  Again's own step cap (40).
- Jobs (`src/app/shop/jobs.py`): POST starts, GET `/api/job/<id>` reports the phase and the result; the page
  polls every second.
- De-duplication (`src/app/shop/dedupe.py`) and one seller canonicalization (`retailers.seller_key`).
- Fewer than 24: an explanation sentence and one refill search; the banner is one sentence plus "details".
- `make shop-diagnose` (live, about $0.003) and the section "How to read the diagnostic" in ADR 0012.
- Replay 4 and a third mock server in the browser check.

## Choices made mid-session
None asked. Decided here (also in the working notes): rank before tagging, so supplied candidates are
appended unranked; job ids bound to the session (a new session's first search is readable by id alone); one
job per session, 8 in all; a `strict` tool schema whose rejection falls back to rows on the one retry;
replay 4 has 38 dislikes and 8 positive items (the brief's counts did not add up) so shape is 67/33; pool
titles in replay 4 carry no "[FIXTURE]" prefix so copies match shown items; a mock peer catalogue copy has
its own thumbnail (a shared one is, rightly, a duplicate).

## Replay 4 (mock)
West Elm seller chip (5 of 12), no tier chip (22 dislikes in the tier against liked mass 10), shape open
(round 8, rectangular 4), warm wood finish converged (83%, lift 1.417), modern-leaning style converged;
about 30 duplicates and 8 seen or rated removed from 132 raw candidates; 9 Serper calls (18 credits). With
tagging forced to fail the Shop Again finishes in under 45 fake seconds with a full batch and one sentence
of reason.

## Evals and checks
`make eval-fast` is a mock plumbing check. No live run was made (Max's call). Test, lint and typecheck
numbers are in the final report. Screenshots: `spike-output/shop-screenshots/27` to `32`.

## Flag before merging
The cause of the live "no text content" failure is unknown; run `make shop-diagnose`. Concurrency is tested
with fakes only. Earlier tests were adapted for the new flow (rank before tagging, jobs, batches of 40).

## Next command
From ~/Desktop/Projects/room-redesign-agent: `make shop-diagnose`, then `make shop-live`.
