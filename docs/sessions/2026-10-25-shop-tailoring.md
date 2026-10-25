# 2026-10-25: Shop Again tailors from like rates

Written for someone with no other context. Mock mode only: nothing ran live and no live number is claimed.

## What changed and why

The last live Shop Again (13.7 s) learned nothing: every rule said "not enough evidence", the batch was
pages 3 and 4 of the original query, 10 of 22 results were from Walmart and Lowe's and none from West Elm,
Article or CB2, though 7 of 15 liked items came from those three. Details and thresholds: ADR 0012, stage 2i.

1. **Group like-rate evidence** (`src/app/shop/evidence.py`): rated, liked, like rate and the session
   baseline for any seller, tier or soft value. Seller and tier chips now need 4 rated, 2 liked, 1.5 times
   the baseline and 35%; marketplace retailers never earn one. A leaning layer (toward and away) feeds the
   ranker (`shop_rank` v5) and the batch (at least 30% toward, at most 30% away, never over a converged cap).
2. **Marketplace exclusion**: a deletable chip at 8 or more rated, at most half the baseline and 20%;
   marketplace sellers leave the pool before ranking and the caps; a banner under 12 results; a dismissed
   chip returns after 8 more ratings if the evidence holds.
3. **Finding more**: seller searches for the liked sellers (no chip needed), peers fill free slots, a forced
   `record_queries` call (`shop_queries` v1) writes one or two validated queries, the base query stops at
   page 2 (page 3 last resort, never 4), and the leftover tagging cap tags the next-ranked candidates.
4. **Display**: leaning and exclusion chips, a footer that says what it counted, a like-rate evidence table,
   the written queries and the excluded count in How I decided, canonical seller names on cards.
5. **Pool snapshot and export**: each round keeps at most 100 pool items in the session file;
   `scripts/shop_export_replay.py` writes a redacted replay fixture.

## Decisions and tradeoffs to review

- Literal rule: with a baseline over 67% no group can be 1.5 times it, so sessions that liked almost
  everything (the owner's first three) get no seller or tier chip. Say if a fallback is wanted.
- Up to 3 seller chips instead of one; leanings fill an empty dimension from title keywords; query writing
  is off in a bare `Config` and on from `APP_SHOP_MODEL_QUERIES` (default on).
- The brief's premium numbers (7 liked of 12) do not add up with its seller rows (5 + 4 + 4 rated); the
  fixture uses the seller rows, so the tier is 7 of 13 (54%).
- `redact` was left as it was; the export script scrubs emails, phone numbers and links itself.

## Not done

The fifth replay fixture from the newest session in `shop-data/`: that session (48 rated) was saved before
pool snapshots, so it has no candidate pool, and a fixture without one cannot be replayed. Export it after
the next live run (command in `docs/todo.md`). A synthetic `tailoring_evidence.json` stands in.

## Existing tests edited because the design changed

`test_shop_stage2e.py` (two seller-rule tests rewritten, replay 1 and 2 expectations), `test_shop_stage2f.py`
(net dislike, tier and sub-seller tests rewritten; replay 3 now dislikes ten extra results; peers now start
with the liked seller), `test_shop_stage2g.py` (replay 4 has no chips; phase text), `test_shop_learn.py`
(new `seller_scenario`; paging tests now page 2 and 3 only), `test_shop_again.py` (paging, call counts),
`test_shop_decide.py` (thresholds, seller and pin tests), `test_shop_replay.py` (replay 1 has no chip; chips
via extra dislikes), `test_shop_thinking.py` (7 call sites), `test_shop.py` (the double answers
`record_queries`), and `scripts/shop_browser_check.py` (seeds, expectations, new tailoring section).

## Checks

- Tests: see the report below the line; ruff, ruff format and `mypy --strict` clean; `make shop-check`
  passes (new section: chips, evidence table, exclusion and deletion; screenshots 37 to 42).
- `make eval-fast` (mock): plumbing check only.

## Next command

From the repository root: `git diff main`. Then, when you choose to spend, `make shop-live` on a session
with 40 or more ratings.
