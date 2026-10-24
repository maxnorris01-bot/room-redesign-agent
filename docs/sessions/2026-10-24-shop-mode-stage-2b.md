# Session 2026-10-24: Shop Mode, stage 2b

Branch `shop-mode-stage-2b`, from `main` at the stage 2 PR (#15). **No live call was made**: mock mode and
fixtures only, `.env` not read, no network in any test (a socket guard allows loopback only). Nothing was
committed, pushed or opened as a PR.

## Why

Live use of stage 2: Shop Again results did not feel aligned with what the owner loved unless a comment said
so; the chips mostly restated the typed request plus comment text; a style the parser guessed ("industrial")
was treated as a settled like; and Shop Again re-ran nearly the same Google search and removed what was seen,
leaving the ranker little room. The owner's two loved West Elm tables shared a seller and a premium finish,
not a shape.

## What changed

- **Learning from the items** (`prompts/shop_prefs.md` v2, v1 kept; `src/app/shop/prefs.py`, `models.py`).
  The call gets each rated item (id, title, price, seller, rating, comment) and up to 6 loved, 6 liked and
  4 disliked thumbnails through the existing guarded fetcher (`app.thumbnails`: https, public addresses, size
  and time limits); a failed fetch leaves that item to go by title and is recorded in the profile's
  `images` report (host and reason, never a URL). It returns inferred attributes with kind, polarity,
  evidence ids and confidence. Code decides what stands (`validate_inferred`): evidence must be rated
  results of the right polarity; two items of evidence unless a comment names the attribute or the one item
  is loved with a comment; a net weight (loved 3, liked 1, minus disliked titles that show it); a dislike is
  refused when a loved or liked title shows it unless a comment backs it; quality cues must be visible in a
  title, a sent image or the upper price band. **Seller affinity** (two loved or liked, no dislike) and the
  **price band** (the loved and liked prices among all shown prices: upper or lower third) are computed in
  code and the model's own versions are ignored; the band never changes the typed cap.
- **Guesses.** A parser style term the typed request does not contain is a guess: a dotted "guess" chip, not a
  like, not given to the ranker as one; dropped when the model reports a loved or liked item that contradicts
  it and no loved or liked title shows it, or when deleted. Inferred chips are dashed and tagged "inferred",
  show the evidence titles in a tooltip (text nodes only), and are deletable; deleted phrases never return.
- **Wider pool** (`search.run_query`, `again.py`). Up to three phrasings (adjusted query; product plus the
  strongest inferred material, finish and shape; seller name plus product) within
  `APP_SHOP_AGAIN_MAX_SERPER_CALLS` (4). Each phrasing's first page runs; more pages only while the pool is
  under 60; a phrasing already fetched this session continues after its last page. Ranker pool up to 80;
  seller cap 4, or 8 for the affinity seller.
- **Ranking** (`prompts/shop_rank.md` v3; v1 and v2 kept). Profile parts labelled by reliability, guesses
  excluded from likes, loved, liked and disliked titles as examples. Typed searches still use v1.
- **Lookups only on Open.** Loving no longer resolves; the page, Saved panel, footer text, `make shop-live`
  text, SPEC and ADR say so.
- **Summary line** from the profile in code ("Looking for more brushed brass and wood and metal pieces from
  West Elm in the upper price range, fewer ..."). **Alignment** (`preference_alignment`, `alignment_counts`):
  results matching at least one inferred like and no dislike; stored in each Shop Again record and shown in
  the footer ("matches your preferences: N of 24").
- **Mock.** Fixtures gained 16 items per topic from an invented premium seller (Hartwell & Co; four among the
  first 40 results, twelve past the 80th); the mock search engine surfaces them for queries that name the
  seller or its materials; the mock answers the v2 call (a "premium metal finish" cue only when two or more
  loved or liked items come with an image) and the coffee-table sample carries a parser guess.
- **Cost per Shop Again (design).** Up to 4 Serper calls (8 credits, about $0.008). Anthropic about $0.002 by
  estimate (preference call about 3,000 input tokens including up to 16 small photos and about 700 output;
  rank call about 4,500 input and 600 output; Haiku 5.5 prices); assumed token counts, not measured.

## Choices made without being asked

- A phrase the shopper said that equals an inferred phrase is shown once, as the shopper's.
- Evidence exception reading: a comment on an evidence item that names the attribute, or one loved item that
  has any comment.
- Price band edges are the top and bottom third of the shown prices; the middle says nothing.
- A repeated search phrasing starts after the pages already fetched, so the same page is not bought twice.
- The preference row always shows when there are results, with a hint, so the grid does not move.
- `StoredProfile`, `SearchRecord` and `Session` gained fields with defaults, so older session files load.

## Headless check (`make shop-check`)

48 checks pass, no console errors. Flow: empty page; a failed search with Retry; a held search to see the
loading state; the first batch (4 of 24 from the invented seller); two brushed brass items loved with no
comments, two liked, one disliked, no lookup; Shop Again (loading text, 24 new results, none seen before, 8
from the favoured seller, summary naming it, "inferred" chips with a dashed border, evidence tooltip,
footer "4 Serper calls (3 searches) = 8 credits", "matches your preferences: 8 of 24", 0 lookups); Open runs
exactly one lookup; delete an inferred chip; Saved tabs; reload restores all of it; a second session where
the guess chip stays (dotted, "guess"), is deleted and stays deleted. Screenshots (gitignored) in
`spike-output/shop-screenshots/`: `04-first-batch`, `05-chips-after-shop-again`, `06-tooltip`,
`07-grid-after`, `08-guess-chip`, `09-mobile` (plus `01` to `03`). Fixed after looking: the hint line
claimed "rate a few results" after a Shop Again that learned nothing.

## Status

- Tests: 689 passed (641 before; 48 in `tests/test_shop_learn.py`, plus updated stage 2 tests). `make lint`
  and `make typecheck` clean; mock `make eval-fast` pass_rate 1.0 (plumbing only).
- Not verified: anything live (Serper pages 3 and 4; how the model reads real photos and titles; whether
  "premium" cues and seller affinity hold on real results; how many fresh results the extra phrasings add;
  real image-fetch failures); Safari; dark mode visually.

## Questions for review

- Is two items of evidence the right bar, or should one loved item (no comment) be enough for a shape or
  finish when it is also in the price band's top third?
- Is 8 results for a favoured seller too many on a list of 24?

## Next command

`make shop`, then open the printed URL. Browser check: `make shop-check`.
