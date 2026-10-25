# Session: Shop Mode stage 2e, the model tags what was shown

Branch `shop-mode-stage-2e` (from main at PR #18). Nothing was committed or run live.

## What changed and why
Live use of stage 2d (the owner's second session, tagging of the 11 rated items worked) still gave a
91% rectangular batch though shape was open, a Home Depot seller chip from 2 items of 8, and almost no
converged preference: the title keyword baseline for "what was shown" covered shape 46%, finish 40%, frame
color 8%, so the lift test could not run, and synonyms ("metal base" and "metal frame"; "warm wood", "brown
wood", "rustic brown") split one value into several under the thresholds.

- Candidate tagging (`src/app/shop/candidates.py`, prompt `shop_candidate_tags` v1): one call per up to 100
  titles tags shown and pool results from title, price and seller (no image). Rows are text
  (`n|shape|material|finish|frame|style`). Cached by result id in the session file (`tag_cache`). If the
  call fails the keyword reader is the fallback and no attribute converges; the page says so.
- Vocabularies (`src/app/shop/vocab.py`): defined once, passed into both prompts, with a normalization map.
  `shop_prefs` is v5 (v1 to v4 kept). Size class and legs are no longer asked for or decided.
- Thresholds: shape 80%, soft dimensions 60%, known coverage 60%, lift 1.3, dislikes at the same shares.
- Seller: 2 items and 40% of the like mass, no dislike, the lift; the trace shows every rule that failed.
- Supply: an open dimension's value under 40% of the pool triggers targeted searches (cap
  `APP_SHOP_AGAIN_MAX_SERPER_CALLS_OPEN`, default 6), new candidates are tagged, supplied candidates survive
  the cut to 80.
- Stratified selection when shape is open: slots by like mass, 25% floor for a loved value, 60% cap, ranker
  order inside a value, all other caps kept.
- Trace and page: pool before and after, slots planned, final batch by shape, finish and seller, calls,
  coverage, per-seller like share. Footer: candidates tagged N of M.

## Choices made mid-session
None asked. Not specified by the brief and decided here: rows as text instead of JSON; `other` never
converges or is open; a metal frame with no colour is `other`; the rank model runs the tagging; open
supply covers shape, material and finish (the dimensions that have search words); a supplied phrasing that
finds nothing new is not asked again; a typed phrase blocks a learned finish or frame only when it is the
whole phrase ("wood" no longer hides "warm wood"). Replay 2's 26 shown titles are authored.

## Replays (mock)
Replay 1 unchanged: shape open (57/43), price mixed, West Elm affinity (86% of like mass, lift 1.714), no
guessed attributes. Replay 2: shape open (rectangular 69%, round 31%); no seller chip (Home Depot 25%);
warm wood finish converged (81%, lift 1.625); material none (lift 1.25); frame color none (black 44%, 29%
of shown titles tagged for it); style none; price mixed. Batch: 14 rectangular and 10 round, supply ran
(6 Serper calls, 12 credits).

## Evals and checks
`make eval-fast` is a mock plumbing check. No live eval was run (Max's call). Test, lint and typecheck
numbers are in the final report. Screenshots: `spike-output/shop-screenshots/12` to `21`.

## Flag before merging
Live: how well the model tags titles, whether 60% shown coverage is reached, whether the supply step finds
round tables on real pages, the real token cost of tagging. An older 2c browser scenario was changed
(round now needs one liked rectangular, not two, to reach 80%).

## Next command
From ~/Desktop/Projects/room-redesign-agent: `git diff main`, then `make shop-live`.
