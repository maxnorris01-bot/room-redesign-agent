# Session 2026-10-24: Shop Mode, stage 2

Branch `shop-mode-stage-2`, from `main` at the stage 1 PR (#14). **No live call was made**: mock mode and
fixtures only, `.env` not read, no network in any test (a socket guard allows loopback only). Nothing was
committed, pushed or opened as a PR.

## What changed and why

Stage 1 worked live. Its findings drove this stage: thumbnails load; a SerpApi lookup is one search; a Serper
shopping call is 2 credits and the footer under-counted; a search takes about 11 s with no loading cue.

- **Preference profile** (`src/app/shop/prefs.py`, prompt `shop_prefs.md` v1, models in `models.py`). One
  small-model call reads the typed request and its parse, every rated result with its comment, and the
  existing profile with its deleted entries, and returns the complete updated profile (likes, dislikes,
  query adjustments). Code sanitizes it and enforces what the model is not trusted with: deleted entries
  never return (likes, dislikes and adjustment terms, by phrase or close variant); category and price floor
  never change; `price_max` only falls, by at most 20% of the typed cap, with a stated reason. A failed call
  keeps the earlier profile. The profile is stored in the session.
- **Shop Again** (`again.py`, `POST /api/shop-again`). Adjusts the last typed query (never an earlier Shop
  Again's), runs the stage 1 pipeline (`search.run_query`, split out of `run_search`) with exclusions (every
  result id shown in the session, and the same title and seller under another id), up to 3 Serper pages
  while fewer than 24 unseen results survive, rank prompt `shop_rank.md` v2 (v1 kept, still used for typed
  searches) with the profile and the loved, liked and disliked titles. Sellers with at least 2 dislikes and
  no love or like are ranked last. A short result says "Only N new results found. Try changing the request."
  Cap `APP_SHOP_AGAINS_PER_SESSION` (10).
- **Preference chips** (`POST /api/preference/remove`). Likes with "+", dislikes with "−", each with an x;
  a plain-language summary line written from the profile with no model call. Shop Again is enabled only after
  a rating (tooltip "Rate a few results first").
- **Saved panel.** Derived from ratings and stored results, so it spans every search and Shop Again. Tabs
  Loved and Liked with counts, same cards (re-rate, note, Open), a dislike or a cleared rating removes the
  item, open or closed state remembered in local storage (guarded). **Start over** (inline confirmation)
  begins a new session.
- **Fixes.** Serper cost is calls x credits x USD per credit (`APP_SERPER_CREDITS_PER_CALL` 2,
  `APP_SERPER_USD_PER_CREDIT` 0.001), shown in the footer and in `make shop-live`, with the session total;
  a spinner and text ("Searching, usually about 10 seconds"), disabled Search and Shop Again while
  anything runs, a readable error with Retry, and no layout jump (the grid holds its height, the
  preference row is always present with a hint). ADR 0012 now gives the owner's reason for dropping the 2D
  plan, and has a stage 2 amendment.
- **Mock.** Fixtures hold 100 items per topic (pages of 20; a typed search sees 2 pages, a Shop Again 3),
  with "boucle" and "low profile" titles; the mock answers the preference call from the rated rows.

## Choices made without being asked

- Env names keep the repo's `APP_` prefix (`APP_SERPER_CREDITS_PER_CALL`, `APP_SERPER_USD_PER_CREDIT`).
- The profile is the complete list the model returns, not a delta; the code only filters it.
- Shop Again moved from the footer to a toolbar above the grid, next to the Saved toggle.
- Ratings got millisecond timestamps (the page uses the same format) because Saved is ordered by them.
- Old session files load: the new fields have defaults, and credits are derived from the call count.
- The Playwright note from stage 1 still holds: `make shop-check` uses `uv run --with playwright`; no
  dependency was added.

## Headless check (`make shop-check`)

60 checks pass, no console errors, three clean runs in a row. Flow: empty page; a failed search (error and
Retry, then it works); a search held mid-flight to look at the loading state (text, spinner, buttons
disabled, grid in the same place); love, like and dislike with "too dark"; Shop Again held mid-flight, then
24 new results with none from the first search, chips, summary "Looking for more metal and wood, fewer too
dark."; delete a chip; Saved opens with Loved (1) and Liked (1); hide and show; re-rate from Saved; a second
typed search keeps Saved; reload restores the search, Saved, chips and notes; Start over. It found and
fixed: the confirmation showing while hidden, skeleton cards that looked empty, and a tie-order flake in
Saved caused by mixed timestamp formats. Screenshots (gitignored) in `spike-output/shop-screenshots/`:
`01-empty`, `02-error-retry`, `03-loading`, `04-rated`, `05-shop-again-loading`, `06-after-shop-again`,
`07-saved-open`, `08-restored`, `09-mobile` (old stage 1 files may remain in the folder).

## Status

- Tests: 641 passed (603 before; 38 in `tests/test_shop_again.py`, plus updated stage 1 cost assertions).
  `make lint` and `make typecheck` clean; mock `make eval-fast` pass_rate 1.0 (plumbing only).
- Not verified: anything live (Serper `page` 3, real comment interpretation, real credits per Shop Again,
  real latency, whether unseen holds on small or repetitive result sets); Safari popups; dark mode visually.

## Questions for review

- Is "complete profile from the model, filtered by code" the right trade against a delta merge?
- Do 10 Shop Agains per session and 3 pages (up to 6 credits) fit the budget you want per visitor?

## Next command

`make shop`, then open the printed URL. Browser check: `make shop-check`.
