# Stage 2p: fixes from the first live Shop eval

Date: 2026-10-27. Mock mode and fixtures only; no live call, no live eval, no live rejudge. `evals/shop/thresholds.json` and
`THRESHOLDS.md` are unchanged (a test asserts the SHA-256 `c64fe35a...d237`). The diagnosis these fixes follow is
`docs/evals/shop-eval-baseline-2026-10-26.md`.

Precondition: the baseline document and the `rejudge` command are in the working tree. I cannot run git, so I could not check that
they are merged into main.

## What changed, by phase
1. **Categories and the seller cap.** Singular and plural are one canonical key ("dining chairs" = "dining chair", "bookshelves" =
   "bookshelf"); "desk chair" is its own category (it used to fold into "desk"); curtains, dining chairs and desk chairs got synonym
   groups, size ranges and plausibility keywords; rug sizes in feet and curtain lengths parse into size hints (Shop Mode has no size
   filter, only hints). Topic keys use the canonical keys and old session files migrate on load. The per-seller cap is
   clamp(floor(0.30 x batch size), 2, 4).
2. **Never run dry.** The query writer (prompt v3) runs on every Shop Again with the queries already used; variants that repeat a used
   query (80% shared words) are rejected and deterministic variants fill in; a thin first search runs up to two variant searches inside
   a Shop Again's normal Serper cap; an exhausted Shop Again says so, records why, and the eval marks the round "exhausted".
3. **Typed colour and material are enforced.** Photo tags for the top 36 candidates (12 a call, 3 at once, 6 s; one shared thumbnail
   cache), a 70% quota for typed colour, material and finish by best tag, match rules in `vocab.py`, a banner naming a shortfall,
   deletable typed chips, a ranker line. The eval scores colour from the thumbnail (category judge v2).
4. **Refine terms outside the vocabulary.** Free notes (prompt v2) carry focus, prefer or exclude; the tagging calls return a
   true / false / unknown flag per note; focus reserves 50% from true flags, exclude removes true (never unknown), prefer is
   ranker-only; the chip says "matched by description" once flags exist; `mesh` is a material value.
5. **Cross-cutting.** `eval-shop-plan` uses the measured baseline plus an estimate of the new calls; `CASES=` on the plan, mock, live
   and rejudge targets; browser checks and screenshots for the new banners; the rejudge result recorded in the baseline document and
   in `known_instability.md`; ADR 0012 amendment 2p, SPEC, todo, working notes, `evals/shop/README.md`.

## Choices where the brief was silent
- New switches are off in a bare `Config` and on from the environment (the repo's pattern): `APP_SHOP_PHOTO_TAG_TOP_N` (36),
  `APP_SHOP_THIN_VARIANTS` (1). The query writer stays behind the existing `APP_SHOP_MODEL_QUERIES`.
- "The queries already used, stored in the session": derived from the session's own search records (`queries` and `pages`), no new
  field and no migration.
- "Within the existing Serper budget" for the thin first search: at most a Shop Again's normal cap (4 calls), so up to two variant
  searches on top of the two pages.
- The seller cap shortens a thin batch (a walk with the smaller cap) rather than allow a third of it from one seller.
- The typed quota comes after the shopper's focus chips and before everything inferred; a "blue" typed means `blue` only, "navy" and
  "dark blue" mean `navy_dark_blue`.
- Exclude notes act after tagging, before the caps (the rank call comes first because the flags need candidates to tag): not
  literally "before ranking".
- "no leather" must be a note: the vocabulary's `upholstered` covers every fabric and leather (refine prompt v2 says so).
- "mesh" is a `material` value, not a back-type dimension (a new dimension widens every tagging row).
- Mock changes so the eval exercises the new code: the request parser reads colour and material words as typed attributes and the
  longest known category phrase; a titled-colourless item with a photo gets a deterministic pseudo-photo colour; mock refine readers
  for notes; mock scenario servers (`APP_SHOP_MOCK_SCENARIO=thin|dry|navy`) for the browser check.

## Estimates (from `make eval-shop-plan`, an estimate, not a measurement)
One Shop Again about $0.014 (Anthropic about $0.0046, 9.7 Serper credits). A 5-case re-run about $0.22 and 132 Serper credits. The
whole 12-case run about $0.54 (Anthropic $0.22, 316 credits expected); the worst case (528 credits) is over the 500-credit hard stop,
which the plan says. Mock timeline of a Shop Again with the photo step: 0.035 s total, the photo tagging 0.01 s (mock calls have no
latency); with a 1 s mock delay per search it is 2.1 s, the photo calls running as three parallel calls after the rank call. Live the
photo step is bounded by its 6 s budget and the 45 s hard stop; the baseline's Shop Again median was 12.9 s.

## Tests and checks
ruff, `ruff format --check` and `mypy --strict` clean; 1429 tests pass (1275 before this stage): categories 49, dry 23, typed 44,
notes 32, docs 3, plus edits to existing ones. `make shop-check` all checks passed (new sections for the banners, the deletable chips
and the chip wording); `make eval-shop-mock` completes (informational verdict); `make eval-fast` 1.0.

## Existing tests and checks edited (a design change forced each)
- Seller cap: `test_seller_cap_counts_sellers_not_positions`, `test_cap_keeps_the_rankers_favourites` (test_shop.py);
  `test_a_batch_cannot_exceed_the_seller_cap_by_mixing_name_variants` (speed_prep); `test_sellers_are_deprioritized_only_with_two_dislikes_and_no_like`
  (test_shop_again.py, more sellers so BadShop can show three items); the caps test in test_shop_decide.py (renamed);
  `test_stratified_selection_still_holds_every_cap` (stage2e).
- Curtains are now known: `test_parse_boho_curtains_*`, `test_known_category_uses_categories_py` (test_shop.py), the topic key assert in
  test_shop_topics.py; the eval spec and report tests (no category findings any more).
- Exhaustion message: `test_an_empty_page_stops_the_paging_and_zero_results_say_so`; prompt versions (`shop_queries` 3, `shop_refine` 2,
  `eval_shop_category_judge` 2) in test_shop_request_understanding, the eval scoring and runner tests; the vocabulary assert (mesh) in
  test_shop_stage2e.
- The estimate test and the Makefile asserts in the eval runner and rejudge tests (CASES=); the browser check's curtains threshold
  (`warm >= 8`, was 10: photo tags pick some untitled colours).

## Flag before merging
- No live run has checked the photo tagging time, the writer on every Shop Again, or the note flags; the worst-case credits exceed
  the hard stop of 500 for a 12-case run (the expected figure does not).
- A thin batch from few sellers is now shorter (3 sellers give 6 results) instead of 12 with a third from one seller.
- The product-colour figure in later reports is not comparable with the baseline's title figure; the report prints both.
- The attribute judge still counts "cannot tell" as a miss (open, a threshold decision).

## Next command
`make eval-shop-plan CASES=case-04,case-06,case-10,case-12`
