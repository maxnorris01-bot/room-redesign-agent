# Session: Shop Mode stage 2f, retailers as a signal

Branch `shop-mode-stage-2f` (from main at PR #19). Nothing was committed or run live.

## What changed and why
The owner's third live session (stage 2e) showed: candidate tagging failing with no visible cause ("0 of 72
in 2 calls"), a pool of Lowe's, Walmart and Wayfair for someone who likes West Elm, a West Elm chip blocked by
one dislike in five, style split across four labels, and "matches your preferences: 0 of 24" when nothing
was decided.

- Candidate tagging: a per-call log (items, token limit, stop reason, valid JSON, rows read, first 200
  characters) in the response, session file and trace; a tolerant reader; a retry once in halves; the real
  reason on the page. `llm.complete(keep_truncated=True)` keeps a partial answer.
- Net dislike rule (3 times) and seller canonicalization.
- Retailers (`src/app/shop/retailers.py`): tiers, peers, a "retailers like X" tier chip (design tiers only),
  up to 3 peer searches per Shop Again that rotate across the session, a 9-call total cap
  (`APP_SHOP_AGAIN_MAX_SERPER_CALLS_TOTAL`, replacing the 2e `..._OPEN`), tier allocation up to half a batch.
- Style families from up to two labels per item (`shop_prefs` v6, `shop_candidate_tags` v2).
- Alignment counts every decided signal and says n/a when none; footer shows the tier share and distinct
  sellers.
- Third replay fixture and a mock-only pool provider; browser check runs it on a second server.

## Choices made mid-session
None asked. Decided here: marketplace and budget tiers never earn a chip or peer searches; the tier's
batch quota is its like share (capped at half); peers asked again only after every peer was searched and
only if they found something; pins on the tier chip are stored as `tier:<tier>`; replay 3's shown list and
pool are authored; old pins stored with the previous seller key ("hartwellco") no longer match
("hartwellandco") because `&` now folds to "and".

## Replays (mock)
Replay 1: unchanged decisions, plus the tier chip and 3 peer searches. Replay 2: unchanged (no seller or
tier chip). Replay 3: West Elm chip, tier chip, style one family, shape open, price mixed; peers Article, CB2,
Crate & Barrel found 8 new candidates each; 9 Serper calls (18 credits); batch 14 rectangular, 10 round, 12
design premium.

## Evals and checks
`make eval-fast` is a mock plumbing check. No live eval was run (Max's call). Test, lint and typecheck
numbers are in the final report. Screenshots: `spike-output/shop-screenshots/22` to `26`.

## Flag before merging
The cause of the live tagging failure is unknown (the new log will show it). The retailer table is judgment.
Real peer searches may find few new candidates. Older browser-check scenarios and several earlier tests were
updated for the new rules (seller names now canonical, "WestElm" is "West Elm", warm wood lift in replay 3 is
1.295).

## Next command
From ~/Desktop/Projects/room-redesign-agent: `git diff main`, then `make shop-live`.
