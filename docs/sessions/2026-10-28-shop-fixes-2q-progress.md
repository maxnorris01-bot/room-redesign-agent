# Stage 2q: fixes from the post-2p live re-run, progress

| Phase | State |
|-------|-------|
| 1 Photo budget bug | finished |
| 2 Keep variants on-request, track item sources | finished |
| 3 Seller cap relaxes before the batch shrinks | finished |
| 4 Price outlier guard | finished |
| 5 Decidable attribute figure, re-run record, cross-cutting | finished |

Precondition: main includes PR #31 (merge commit 5bcd1de, shown in the session's git snapshot; I did not run git).

## Phase 1
- Root cause (reproduced in mock first, `tests/test_shop_stage2q_photo.py`): the case-04 round-1 trace (`wanted 36, tagged 0, calls 0,
  skipped 36, thumbnails cached 36, seconds 6.01 of 6.0`) did not mean "no call started". All 36 thumbnails were cached, the three calls
  started at once, and they were still running when the 6 s deadline passed. `wait()` timed out, the still-running futures were
  abandoned and counted as `skipped`, and `calls` counted only calls that had finished. A stub model slower than the budget reproduces the
  exact signature with 3 calls actually started.
- A second, latent path: the budget clock started before the thumbnail wait, so uncached thumbnails (a thin first search) could spend it
  before any call began.
- Fix (`phototags.py`): thumbnails get their own short wait (`PHOTO_IMAGE_WAIT_S` 2 s); the 6 s clock starts when the first call starts; the
  first wave (3 calls) always starts unless the hard stop is here and may finish up to `PHOTO_FIRST_WAVE_GRACE_S` (4 s) after the budget;
  later batches start only while budget remains; a failed call is retried once when `PHOTO_RETRY_MIN_LEFT_S` (2 s) of budget remains. The trace
  has `calls_started`, `retries`, `abandoned_calls` and `reason`. Budget (6 s) and the 36-candidate default are unchanged.
- No existing test edited. The ADR table gained the three new constants. Tests: 11 new, suite 1440. Lint, format, mypy, `make shop-check`,
  `make eval-shop-mock` pass.

## Phase 2
- `app/shop/hardwords.py` (new): the request's hard words (category, parsed styles, materials, colours, must-haves and refine chips, sizes
  such as 8x10 / 84 inch / queen) with their accepted forms; a hard word may be replaced only by a listed synonym from `vocab.SYNONYMS`,
  `vocab.COLOR_KEYWORDS` or `categories` and only from a group of at most 3 keywords (a bigger group is a tagging bucket: "upholstered"
  holds velvet, linen and leather). `added_attribute_word` finds a style, material or colour word the request did not contain.
- `queries.validate_query` runs both checks, so the model's variants and the deterministic ones are held to the same rules. The deterministic
  fillers lost "modern", "classic" and "contemporary" (style words); "popular" and "best" were added. A hard word the search words lack is
  added back as typed. Prompt `shop_queries` v4 (v3 kept as `shop_queries.v3.md`).
- When fewer than two variants survive, fewer searches run and `QueryResult.note` says so; it is in `trace.thin.note` (first search) and
  `trace.batch.model_queries.note` (Shop Again).
- Source tracking: `ShopResult.source = {kind, query}` (kinds: base, writer, seller, peer, thin_variant, supply, refill), set from the first
  search that found a product; the eval writes it on every item; the report has a secondary table "Where shown items came from" (per case and
  round: shown, attribute rate, persona rate). Old results files without `source` render "No source data".
- The writer can no longer add a "leaning toward" word to a variant (that is an attribute word the request did not contain). Leanings still
  act in the ranker and the selection. Flagged for review.
- Existing tests edited (forced by the new rule): `test_the_writer_drops_a_query_that_repeats_a_used_one_and_keeps_new_ones` and
  `test_variant_queries_use_the_model_first_and_the_fallback_for_the_rest` (the example "premium oak dining chair" adds a wood word; now
  "premium solid walnut dining chair"), `test_the_writer_prompt_is_version_three_and_the_old_ones_are_kept` (loads v3 explicitly now that v4
  is current), and the prompt version assert in `test_shop_request_understanding.py`.
- Tests: 29 new (`tests/test_shop_stage2q_variants.py`), suite 1469. Lint, format, mypy, `make shop-check`, `make eval-shop-mock` pass.

## Phase 3
- `build_results`: the first walk keeps clamp(floor(0.30 x batch size), 2, 4). When it fills fewer than 24 from a pool of at least 24, the
  cap is raised to 5, 6, 7 (`seller_cap_ceiling(24)` = floor(0.30 x 24)); a relaxed batch is kept only if it is longer and no seller holds
  more than 0.30 of the batch actually shown; a relaxed walk is never tightened again. Steps go to `trace.seller_cap` (`base`, `steps`,
  `final`, `message`: "A few sellers dominate this category; allowed up to N per seller to fill the batch."). Otherwise the existing
  tightening runs and the existing "Only N new results" message is shown.
- Finding that changes what this phase can fix: in the re-run's case-06 round 2 and 3 the batch stopped at 14 because of the **diversity
  guard**, not the seller cap. The trace says `guarded 20, seller_capped 10`, the pool was 100% rectangular, and the guard allows
  int(0.60 x 24) = 14 of one shape; the "Could not keep shape mixed" warning is the same thing. A mock reproduction (44-item rug pool, shape
  guard 0.6, every item rectangular) gives 14 with the guard and 24 without it. Relaxing the seller cap cannot lift that batch. I did not
  change the guard (out of scope, and a ranking decision); it is the first item in `docs/todo.md`.
- Tests: 10 new (`tests/test_shop_stage2q_cap.py`), suite 1479. No existing test edited. Gates pass.

## Phase 4
- `shop/pricecheck.py` and named constants in `thresholds.py` (`PRICE_OUTLIER_MEDIAN_SHARE` 0.10, `PRICE_OUTLIER_FLOOR_USD` 15,
  `PRICE_OUTLIER_MIN_POOL` 8, `PRICE_GUARD_CATEGORIES`; ADR table rows added): a furniture candidate under 10% of the pool median AND under $15
  is dropped inside `apply_filters`; rows (title, price, median, reason) are in `FilterCounts` and `trace.price_outliers`; the count joins
  "filtered out" in the shortfall sentence. Unpriced items keep today's handling; rugs, curtains and lamps are not checked.
- Tests: 10 new (`tests/test_shop_stage2q_price.py`), suite 1489. No existing test edited.

## Phase 5
- Report: "Attribute match among decidable items" (per case and overall, round 1) and the source table; README and `known_instability.md` say
  it is secondary and why. `docs/evals/shop-eval-rerun-2026-10-28.md` records the re-run. `plan.py` uses the measured figures.
- Plan edits forced: the two estimate tests in `test_eval_shop_runner.py` (new constants). The plan also compared an Anthropic-plus-Serper
  figure with the threshold, which is what the threshold counts; kept that and stopped double-counting credits.
- Page and browser check: the relaxed-cap sentence and the dropped-price note show in the status line; "How I decided" gained the cap steps,
  dropped listings, the variant note and the photo reason (the panel lines are not exercised by the browser check). New mock scenario
  `heavy`; screenshots `85-relaxed-cap*.png` (light, dark, 390 px).
- ADR 0012 amendment 2q, SPEC, todo, working notes, `2026-10-28-shop-fixes-2q.md`. Tests: 5 new (`test_shop_stage2q_report.py`), suite 1494.
