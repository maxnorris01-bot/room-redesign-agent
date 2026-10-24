# Session 2026-10-24: Shop Mode, stage 2c

Branch `shop-mode-stage-2c`, from `main` at the stage 2b PR (#16). **No live call was made**: mock mode and
fixtures only, `.env` not read, no network in any test (a socket guard allows loopback only). Nothing was
committed, pushed or opened as a PR.

## Why

On the owner's real ratings, after two or more Shop Agains, results leaned round. Four loved West Elm tables
($699 to $899, all with storage: round walnut drum, round brushed brass drum, rectangular wood and black metal
lift-top, rectangular walnut with drawers) and eight or more liked ones ($140 to $800, several sellers, both
shapes) share the seller, warm wood with dark or brass metal, clean lines and a premium finish, not a shape.
The tool made the chip "round drum", the dislike "rectangular with drawers" (a loved item has it) and wrote
learned values into the request chips. Two causes: the model decided what counted as a preference and
over-read mixed likes; and a mostly-round batch, rated, taught "likes round" (exposure feedback).

## What changed

- **Model describes** (`prompts/shop_prefs.md` v3; v1, v2 kept; models `ItemTags`, `PreferenceTags`).
  Per rated item (with its thumbnail when it can be fetched, same limits and guards as 2b): shape, material,
  color or finish, frame or legs, frame color, style, size class, quality cues, each a value or null. The
  comments are still read by the model into the shopper's own words (full weight), a price ask, and
  contradicted parser guesses. It never says what the shopper prefers.
- **Code decides** (`src/app/shop/decide.py`, `thresholds.py`, `textutil.py`). From ALL the ratings every
  time: weighted like mass per dimension (loved 3, liked 1) against an exposure baseline from a title-keyword
  lexicon of everything shown. *Converged*: 70% of the mass, 2 supporting items (a loved item with a naming
  comment may be the second), like share at least 1.3 times exposure. *Open*: two or more values at 30% each
  (a neutral chip, no pressure). Dislikes: 2 disliked items sharing a value no loved or liked item has, with
  the lift test; one dislike only excludes that item; a contradicted dislike chip is dropped. Seller affinity
  (2b's rule plus the lift test). Price: *mixed* when loved prices cluster in the upper third and liked prices
  span two thirds of the shown range (neutral chip, no price pressure), else 2b's band rule; the typed cap
  never changes. Quality cues need 2 items and half the positives, are visible, and are never features.
  Typed request terms are never repeated as learned chips. A model failure still gives a profile (titles
  alone).
- **Request chips unchanged.** The chips show only what the parser read; the adjusted query is stored apart
  (`SearchRecord.used_query`).
- **Search** (`prefs.build_phrasings`, `search.run_query`): the typed phrasing (no learned value, no seller
  name), the seller phrasing, and one phrasing per value of one open dimension (each carrying the converged
  words, never one value in every phrasing). When the 4-call cap bites, the open values go as a group, then
  the standalone converged words; the dropped phrasings are recorded and shown as a warning.
- **Ranking and guards** (`prompts/shop_rank.md` v4; v1 to v3 kept; `search.build_results`). The ranker is told
  what converged, what is open, and that a mixed price is no signal. In code, on the final 24: a guarded
  dimension (open ones, plus shape and material while not converged) never lets one known value pass 60%; an
  affinity seller up to 50%; a pinned seller up to 80%; any other seller 4. A guard the pool cannot meet
  returns the remainder with a message. The largest share per dimension and the largest seller share are in
  the response and the footer.
- **Chips and pin.** Neutral "open" and "mixed" chips (solid border, evidence tooltip, deletable); the seller
  chip's name is the pin control (`POST /api/preference/pin`, stored in the profile, carried across
  recomputes); the summary line says "Shape is open: showing both round and rectangular. Price is mixed."
- **Saved panel** is a wrapping grid (no sideways scroll at 1280 or 390 px).
- **Mock.** The coffee-table fixture holds the owner's four loved items as early Hartwell & Co items, round,
  rectangular, oval and square shapes across 12 sellers, walnut, oak, black steel, brushed brass, marble and
  glass, $140 to $900, and 36 more Hartwell items past the 80th; the mock search surfaces the seller's items
  and named shapes for specific queries; the v3 mock tags from title keywords, with a photo-only cue added
  only when an image came with the item.

## Thresholds as implemented (`src/app/shop/thresholds.py`; table in ADR 0012)

Love 3, like 1; converge 70% of like mass, 2 supporting items, lift 1.3 (like share over exposure share);
open 30% each for two or more values; dislike needs 2 items and the lift; seller affinity 2 items, no dislike,
lift 1.3; seller deprioritized after 2 dislikes and no love or like (unchanged); quality cue 2 items and 50%
of positives; price clusters at the top or bottom third (0.67, 0.33) of shown prices, mixed when loved is
upper and liked spans 2/3 of the shown range, at least 8 shown prices; diversity 60%; seller 50% (affinity),
80% (pinned), 4 (others) of a batch of 24.

## Choices made without being asked

- "Dark or brass metal" is one frame-colour value, and "warm wood" (walnut, oak, teak, natural) one finish
  value, in the lexicon: the owner's loved items need it to read as converged. A judgement call, not a finding.
- When the cap bites, the priority is typed, seller, open values (whole group), standalone converged words;
  the converged words ride inside every open-value phrasing so they are not lost.
- Only one open dimension is varied in search (the one with the most like mass, shape first on a tie); the
  others still show as chips and still guard the final list.
- A guard that cannot be met returns the pool's remainder (not a padded list) and says so.
- Style has a short lexicon; size class has none and so no lift test.
- The affinity seller cap fell from 8 to 50% (12 of 24), as asked; the request chips keep the typed price
  cap even when a comment lowered the cap actually searched (the summary line says so).
- A model-free fallback: if the preference call fails, the profile is decided from titles alone.

## Headless check (`make shop-check`)

39 checks, no console errors, passing. Scenario A: round walnut drum and rectangular wood and black metal
lift-top loved from one seller (no comments), a round and a rectangular liked from other sellers, an oval
disliked, no lookups; Shop Again: nothing seen before, request chips unchanged, an open shape chip listing
both shapes with an evidence tooltip, a mixed-price chip ("loved $699 to $749, liked $146 to $695"), a finish
chip, the seller chip; the first Shop Again's batch has no seller above 50% and no shape above 60%; pin the
seller; the next batch is 79% from it (never above 80%) with request chips still unchanged; the Saved panel is
a grid with no sideways scroll at 1280 or 390 px; a reload restores chips and the pin. Scenario B: two round
items loved from the premium seller; shape converges on round (no open shape chip); the batch is 42% round
against 25% in the first. Screenshots (gitignored) in `spike-output/shop-screenshots/`: `04-open-chip-tooltip`,
`05-price-mixed-chip`, `06-pinned-seller-chip`, `03` and `07` (scenario A grids), `10` and `11` (scenario B
grids), `08-saved-panel`, `09-mobile`. Fixed after looking: the new chip class `open` collided with the card's
Open button style (renamed `neutral`), and the pinned tag was blue on blue.

## Status

- Tests: 731 passed (689 before; 42 in `tests/test_shop_decide.py`, plus updated stage 2 and 2b tests).
  `make lint` and `make typecheck` clean; mock `make eval-fast` pass_rate 1.0 (plumbing only).
- Not verified: anything live: how well the model tags real photos and titles; the lexicon against real result
  pages; the thresholds on the owner's ratings; Serper pages 3 and 4; Safari; dark mode.

## Questions for review

- Are 70% and a lift of 1.3 the right bar for "converged"? The owner's own batches are the test.
- Should the pool of one value (all round) that cannot meet the 60% guard still be shown in full when the
  value has converged? (It is: a converged dimension is not guarded.)

## Next command

`make shop`, then open the printed URL. Browser check: `make shop-check`.
