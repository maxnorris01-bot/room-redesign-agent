# Session: Shop Mode stage 2d, decide only what was read

Branch `shop-mode-stage-2d` (from main at PR #17). Nothing was committed or run live.

## What changed and why
Live use of stage 2c showed the preference call truncating (max_tokens), so decisions came from titles
alone, and the title-only lexicon invented preferences (shape "round" from 4 keyword titles while 4
rectangular titles said nothing; "industrial" from two titles). The price band was wider than the liked
items, no seller chip appeared for 8 of 10 West Elm items, and a material warning fired for a request that
named the material.

- Preference call: max_tokens from the schema (2x, 3,000 to 12,000), compact answer, one retry as two split
  calls (loved and liked, then disliked), tagging record (items tagged N of M, photos, retry, fallback).
- Fallback: with no tags only seller, price and comment-stated likes are decided; the page says so.
- Coverage: shares over all like mass; known coverage of at least 70% before a dimension converges or runs
  the lift test; exposure coverage under 70% skips the lift test.
- Price: the band must hold 80% of the liked mass (and, my choice, 80% of the liked items) or price is mixed.
- Guards: shape and open dimensions 60%, converged value 80%, typed-named dimensions exempt, material no
  longer always guarded, warnings give real shares.
- "How I decided" panel and trace in the response and session file.
- Offline replay fixture of the owner's session (`src/app/shop/replay.py`, `tests/fixtures/shop/`).
- Prompt `shop_prefs` is v4 (compactness section); v1 to v3 kept.

## Choices made mid-session
None asked. Unspecified decisions: the 80% rule applies to both mass and liked items; compactness is
asked in the prompt rather than with short JSON keys; dimensions with no lexicon cannot converge; the
fallback keeps likes and dislikes stated in comments.

## Replay result (mock)
Shape open (57/43); material not converged (lift 1.286, and the request names it); color finish, frame
colour, style: not converged (coverage under 70%); price mixed (a $450 to $800 band holds 86% of the like
mass but 75% of the liked items); West Elm seller chip (86% of like mass, 50% of shown, lift 1.714; if the
pool were 95% West Elm the lift is 0.9 and the chip is dropped, with the numbers in the trace); storage not
learned. Fallback: only seller and price.

## Evals and checks
`make eval-fast` is a mock plumbing check; no live eval was run (Max's call). Test, lint and typecheck
results are in the final report of this session. Screenshots: `spike-output/shop-screenshots/12` to `16`.

## Flag before merging
Live model tagging, the lexicon against real pages and the thresholds on real ratings are unverified.

## Next command
From ~/Desktop/Projects/room-redesign-agent: `git diff main`, then `make shop-live` for the live check.
