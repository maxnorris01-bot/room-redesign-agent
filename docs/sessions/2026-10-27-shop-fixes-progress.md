# Stage 2p: fixes from the first live Shop eval, progress

| Phase | State |
|-------|-------|
| 1 Category and seller-cap cleanup | finished |
| 2 Never run dry | finished |
| 3 Enforce typed colour and material | finished |
| 4 Refine terms outside the vocabulary | finished |
| 5 Cross-cutting (plan costs, CASES=, browser check, docs, rejudge note) | finished |

Precondition: `docs/evals/shop-eval-baseline-2026-10-26.md` and `evals/shop/rejudge.py` are in the working tree. I cannot run git, so I could not check
that they are merged into main; flagged in the report.

## Phase 1
- `categories.py`: `singular()`, plurals fold in `canonical_category` ("dining chairs" = "dining chair", "bookshelves" = "bookshelf"); new groups
  desk chair (before desk), dining chair, curtain; size ranges renamed/added (desk chair, dining chair, curtain); curtains are flat items;
  `category_phrase` (longest known phrase, used by the mock parser); plural-aware `category_hits`.
- `sizes.py`: rug sizes in feet ("8x10", "8' x 10'", "8 ft x 10 ft"), curtain lengths ("84-inch"); `search.format_size_hint` reads rugs in feet.
  Shop Mode has no size filter (hints only), so there was no filtering mechanism to reuse.
- `store.migrate_category_keys` (run from `ensure_topics` on load): topic keys from labels, stored queries' `category_key`; idempotent.
- `thresholds.seller_cap(n)` = clamp(floor(0.30 x n), 2, 4) with `SELLER_CAP_SHARE/MIN/MAX` (replaces `SELLER_DEFAULT_CAP`; ADR table updated);
  `build_results` assumes the size the pool can fill and walks again with the smaller cap when the batch comes out shorter.
- Eval: `coverage.category_findings` compares canonical forms; the twelve cases now have no findings (the baseline keeps the old four).
- Existing tests edited (design change): `test_seller_cap_counts_sellers_not_positions`, `test_cap_keeps_the_rankers_favourites` (test_shop.py);
  `test_a_batch_cannot_exceed_the_seller_cap_by_mixing_name_variants` (speed_prep); `test_sellers_are_deprioritized_...` (more sellers so BadShop can show 3);
  `test_the_seller_caps_are_...` (decide, renamed); `test_stratified_selection_still_holds_every_cap` (stage2e); the curtains category tests in
  test_shop.py and test_shop_topics.py (curtains are now known); eval spec and report tests (no findings).
- Tests: `tests/test_shop_stage2p_categories.py` (49). Suite 1325.

## Phase 2
- Query writer (`queries.py`, prompt `shop_queries` v3, v1 and v2 kept): runs on every Shop Again when `shop_model_queries` is on, even with no
  liked item or leaning (`again._query_input` no longer returns None); it is told the queries already used in the topic
  (`again.used_queries`, from the session's search records: `queries` and `pages`), how many results were seen and rated, and must give two NEW
  variants; `too_close_to_used` rejects a variant with Jaccard >= 0.8 of its words against any used query (`QUERY_NOVELTY_JACCARD`);
  `variant_queries` = model first, then deterministic `fallback_variants` (modifier words, every typed word kept) for what is missing.
- Thin first search: `run_query` (typed, Serper, `shop_thin_variants` on from env, off bare) runs up to `THIN_VARIANT_SEARCHES` = 2 variant searches when
  the pool cannot fill 24, never above a Shop Again's normal cap (`shop_again_max_serper_calls`); trace key `thin`; banner "Only N results matched (...);
  try broadening the request."
- Exhaustion: a Shop Again with no unseen result returns `EXHAUSTED_MESSAGE` as headline and warning, with `trace.batch.exhausted` (reason, seen/duplicates/
  filtered counts, queries used before, what the writer did). The eval runner marks the round `exhausted` and the report prints "exhausted" instead of "n/a".
- The Serper cap of 9 calls per Shop Again is unchanged (tests assert it); the writer's cost is in the footer's Anthropic figure as before.
- Existing tests edited: `test_an_empty_page_stops_the_paging_and_zero_results_say_so` (new message), the prompt-version assert in
  test_shop_request_understanding (v3 current, v1 and v2 kept).
- Tests: `tests/test_shop_stage2p_dry.py` (23). Suite 1348.

## Phase 3
- `phototags.py`: `photo_tag_top` tags the top `APP_SHOP_PHOTO_TAG_TOP_N` (36; 0 bare, env default 36) candidates from title + thumbnail after rank and
  near-duplicate collapse: 12 per call, 3 calls at once, 6 s budget (`PHOTO_TAG_*` in thresholds.py + ADR rows), photo tag overrides title and keyword tags
  (an empty photo dimension keeps the earlier value), unfinished candidates keep title tags and the trace says so (`title_tags_for_the_rest`).
  Runs in typed searches and Shop Agains. `ThumbCache` (bounded, 200, failures remembered) is the one thumbnail fetch path: `HttpFingerprinter` uses it,
  so no image is fetched twice.
- `typed.py` + `vocab.typed_values`: typed colour / material / finish are quotas of `TYPED_MATCH_MIN_SHARE` = 70% of a batch by best tag; unknown and `other`
  never match; "navy" and "dark blue" = navy_dark_blue, "blue" = blue only; a typed colour overrides suggested colours (already so in `build_query`).
  `build_results(typed=...)`: after the focus chips, first items matching every typed attribute, then per attribute. Shortfall in the trace
  (`trace.typed` / `trace.batch.typed`) and a banner naming the attribute. The ranker gets a "Typed product attributes (must favor...)" line.
- Typed colour and material chips are deletable (`/api/context/remove` kinds `color_term`, `material`); the quota goes with the chip.
- Eval: category judge prompt v2 (v1 kept) adds `color_match`; the product-colour share is the judge's answer (cannot tell is not a match), the title figure is
  kept as `title_share`; the report prints a "Product colour" section and says the measurement changed after the baseline.
- Mock: `_heuristic_shop_parse` now reads colour and material words as typed attributes; the mock photo reader gives a deterministic pseudo-photo colour to a
  titled-colourless item.
- Existing tests / checks edited: eval judge version asserts (category v2), judge fakes include `color_match`; browser check curtains threshold `warm >= 8`
  (was 10: photo tags pick some untitled colours).
- Tests: `tests/test_shop_stage2p_typed.py` (44). Suite 1392.

## Phase 4
- Refine prompt v2 (v1 kept): a `free_note` carries `note_mode` focus, prefer or exclude and a phrase of at most `NOTE_MAX_CHARS` = 120 (letters, digits, apostrophes,
  spaces; URLs and `<>` stripped, always sent between `<note n>` tags as data); at most `FREE_NOTES_MAX` = 3 active notes. "No leather" / "no velvet" must be a note: the
  vocabulary's `upholstered` covers every fabric and leather.
- Tagging: candidate and photo calls return `notes`, a true / false / null per active note (strict schema, token limit +4 per note per item, 2,360 for 18 rows and 3
  notes), stored as `_note:<chip id>` = true / false / unknown in the tag dict (cached in the session). A candidate tagged before a note existed is asked again (title-only
  when it has no thumbnail, or when `APP_SHOP_PHOTO_TAG_TOP_N=0`).
- Selection: a focus note is a pseudo focus pair ("note", chip id) with the 50% reservation (`REFINE_FOCUS_MIN_SHARE`) from items flagged true; a prefer note reaches the ranker only;
  an exclude note removes items flagged true (never unknown), at the same point as `exclude_value` chips (after tagging, before the caps and the selection; the rank call comes
  first because the flags need candidates to tag). Trace per note chip: mode, `matched_by_description`, flagged counts, `in_batch` and `reserved`, `removed`. The page says
  "matched by description" once flags exist, "approximate" before.
- Vocabulary: `mesh` is a `material` value (a mesh back or seat is the material of that part; a back-type dimension would add a column to every tagging row); it comes first in the
  keyword table so "mesh back with a metal frame" reads as mesh. "focus on mesh backs" is now a plain `focus_value material:mesh` in the mock reader.
- Mock reader: "no leather" (exclude note), "must look vintage" (focus note), "focus on fluted legs" (focus note), "ideally with arms" (prefer note), gibberish (nothing), injection
  (a harmless prefer note). Mock tagging flags: true when a significant word of the note is in the title.
- Existing tests edited: the vocabulary assert in test_shop_stage2e (mesh).
- Tests: `tests/test_shop_stage2p_notes.py` (32). Suite 1424.

## Phase 5
- `evals/shop/plan.py`: measured baseline (pipeline $0.0074 a case, 17 credits, judge call $0.0004) plus estimated writer and photo calls; per Shop Again and 5-case re-run lines.
- `CASES=` on the plan, mock, live and rejudge targets (`--case a,b`). Browser check: thin, exhausted and colour-shortfall banners, deletable typed chips, "matched by description"
  (mock scenario servers). Rejudge result recorded in the baseline document and `known_instability.md`. ADR 0012 amendment 2p, SPEC, todo, working notes, evals/shop/README, session doc.
