# Stage 2r: fixes from the first full 12-case run, plus free offline tools, progress

| Phase | State |
|-------|-------|
| 1 Fill loop for short batches | finished |
| 2 Stop searching early when the pool is plentiful | finished |
| 3 Refine-focus supply flag | finished |
| 4 Shape guard and round-black-metal | finished |
| 5 Tagging timeout | finished |
| 6 Free offline tools (Serper cache, replay, lint, lift diagnosis) | finished |
| Cross-cutting (docs, plan, browser check, ADR) | finished |

Precondition: main is at ff88439 (PR #32, stage 2q); read from `.git/refs/heads/main` and `.git/logs/HEAD`, no git command run.

## Phase 1
- `search.run_query`: the pre-rank "thin first search" block (pool size, fixed cap of 4 per seller) is replaced by `fill_batch()`, which runs
  after the first full selection for both a first search and a Shop Again. Trigger: fewer than 24 results actually selected (after
  filters, de-duplication, near-duplicate collapse, seller caps, guards and typed quotas). Order: the writer's variants (`shop_queries`
  v4), neutral fillers, then page 2 of a variant that found something. Stops: batch full, call cap (Shop Again 9; first search
  `FIRST_SEARCH_MAX_CALLS` = 4, a new constant in `thresholds.py`), `FILL_EMPTY_STREAK_MAX` (2) searches in a row that add no new
  candidate surviving the filters, hard stop, or no variants left. The old single refill search still runs after it if calls remain.
- Gated by `shop_thin_variants` (`APP_SHOP_THIN_VARIANTS`, off in a bare Config, on from the environment) for both a first search and a Shop
  Again, so no existing test changed. `THIN_VARIANT_SEARCHES` is replaced by the two constants above (ADR table updated).
- Trace: `trace.fill` (first search) and `trace.batch.fill` (Shop Again): every step (kind, query, page, found, new candidates that
  survived, shown before and after) and the stop reason; first searches keep the old `trace.thin` shape. Source kinds: `thin_variant` (first
  search), `writer` / `filler` (Shop Again).
- "N filtered out" is now broken down (`filter_breakdown`): price, category or size, implausible price, seen or rated, duplicate, seller cap,
  diversity guard, other (the remainder, so the parts sum to what the searches returned). `trace.batch.shortfall.breakdown`; the short-batch
  sentence keeps the old three numbers and adds the causes and what the fill loop did; "How I decided" lists the steps and the breakdown.
- Exhaustion unchanged (`exhausted` now also carries the breakdown). The eval results keep `fill` in each round's trace.
- Tests: 14 new (`tests/test_shop_stage2r_fill.py`), suite 1508. No existing test edited.

## Phase 2
- What the saved case-02 data shows (`results/shop-live-20261029-034000-3f9fa0.json`; the results file keeps no per-call timeline, so this is
  read from the trace and the code's call plan): rounds 2 and 3 each used 9 Serper calls (18 credits) for a pool that was already big. Round 2:
  the pool was cut to 80 candidates before ranking (`pool_before`), grew to 218 after the supply searches (`pool_after`), 43 were seen or rated,
  67 duplicates, the "no Walmart" chip removed 32; round 3: 77 to 205, 75 seen or rated, 33 duplicates, 44 removed by the chip. The 24 shown
  were picked from 218 and 205 candidates. By the call plan (cap 9, one call kept for supply): the typed query and up to three profile
  phrasings, up to three retailer searches, two writer variants, then supply searches for minority shapes. The calls that added nothing the
  batch needed are the supply searches and the optional phrasings and retailers: the pool already held 80 unseen candidates before them, with
  74% rectangular (the shape guard needs only 10 of another shape), the material quota met (23 of 24) and no focus chip.
- Change: with `shop_early_stop` (`APP_SHOP_EARLY_STOP`, off in a bare Config, on from the environment) wave one is the typed query and the
  sellers the shopper liked, with the writer beside them, then the writer's variants (all personalising, always run). The optional searches
  (the profile's other phrasings, retailers that are not liked sellers) and each supply search run only if `supply_is_plentiful` says no: at
  least `PLENTIFUL_UNSEEN` (60) unseen candidates pass the filters, the typed quotas and focus shares can be met from them (judged on the tags
  they carry now, so an unmeasurable quota keeps the search going), and each guarded dimension has enough candidates of another value. Each check
  is in `trace.batch.early_stops` (what was about to run, why it did or did not stop).
- Expected effect on case-02 (an estimate, not a measurement): the base query, the liked-seller search (if any) and two writer variants run (4
  to 5 calls); with 80 unseen candidates, the material quota met and 47 of 69 non-rectangular candidates, the rest is skipped: about 5 calls
  and 10 credits a Shop Again instead of 9 and 18, so case-02 about 22 credits instead of 38 and a pipeline cost near $0.036 instead of $0.053,
  clear of the 0.05 threshold. The 9-call cap is unchanged and the fill loop can still use it when the batch is short.
- Tests: 9 new (`tests/test_shop_stage2r_early.py`), suite 1517. No existing test edited.

## Phase 3
- What `supply` counted: `focus_share` (evals/shop/scorers.py) said supply exists when a title in "everything the run had already shown"
  (`seen_titles`) or in the batch carries the focus word. Case-10 round 3 had 9 results, 1 title hit (0.111); the whole unseen pool was those 9
  (`pool_after.size` 9, `seller_capped` 0, `guarded` 0), but round 2's 14 mesh titles are in `seen_titles`, so `supply` was True. It counted
  items that cannot be shown again: wrong.
- Fix (measurement change, threshold stays 0.5): the pipeline reports `trace.batch.focus_supply` (unseen candidates past every filter that
  match each focus chip, and how many are in the batch); the eval scores a focus round only when that count covers the quota the focus reserves,
  ceil(0.5 x the batch). Old results files (no count) keep the old reading, marked `measure: seen titles`. For the saved run: before, round 3
  scored 0.111 and the check failed at 0.111; after, round 3 is not scored (supply at most 1 of 5 reserved), round 2 (0.609) is the worst
  scored round and the check reads 0.609 (pass). The saved file cannot give the unseen count for round 2, so it keeps `supply` as recorded
  (14 hits already cover the 12 reserved).
- The fill loop would help round 3: the variants keep the focus words now (`RefineSet.focus_words()` into `QueryInput.focus_words`, a hard word
  of kind `focus`), so a short batch with a focus note ("mesh backs") searches "... mesh back ..." variants and pulls in more candidates that
  can be flagged true. Not verified live.
- Tests: 9 new (`tests/test_shop_stage2r_focus.py`), suite 1526. No existing test edited.

## Phase 4
- Guards: `search.effective_guards` runs before every selection. A guard is switched off for a dimension when fewer than `GUARD_ALT_SUPPLY_MIN` (6)
  unseen candidates carry another value than the pool's top one and the top value would exceed the cap (it would otherwise only shrink the batch). The
  trace has `guards_off` (dimension, top, how many carry another, cap, sentence) and the page says "Not mixing shape: only 0 of the 44 new candidates are
  not rectangular, so the shape limit is off for this batch." instead of "Could not keep shape mixed". With 6 or more alternatives the guard is fully
  active (20 results in the test: 14 plus the 6). Case-06 mock: 44 rectangular rugs give 14 with the guard and 24 without.
- Case-09 table (first batch, `shop-live-20261029-034000-3f9fa0.json`; the judge says yes / no / cannot_tell only, not which attribute broke; no tags saved):
  | item judged "no" | price | what the title says | violates (by title) |
  |---|---|---|---|
  | Pocassy Black All-Iron Round Side Table | $47.64 | round, black, iron | none by title: the thumbnail disagrees (judge) |
  | Costway 2 PCS 2-Tier Round Side Table with Storage Shelf Metal Frame ... Black | $49.99 | round, black, metal frame, 2-tier set with shelf | none by title; a set of two with a shelf (thumbnail) |
  | Wire Mesh Stackable Side Table, Black | $40.50 | black, wire mesh, stackable; no "round" | shape not stated (a stackable mesh table is rarely round) |
  | Threshold Sheet Metal Side Table with Teardrop Legs | $80.00 | sheet metal; no "round", no colour | shape and colour not stated |
  | Monarch Specialties Monarch Accent Table Metal With Tempered Glass | $75.99 | metal and glass; no "round", no colour | shape, colour and material (glass top) not stated |
  Of the 24 titles, 9 never say "round" (H&M Home, Hoffeorrad, Wire Mesh, Butler Nicholas, Threshold sheet metal, IKEA TRANERED, Fondi, Creative Co-Op,
  Monarch): the quota covered colour (24 of 24 black) and material (21 of 24 metal) but not shape. 3 of the 5 misses do not state the shape; 2 state round,
  black and metal and were judged from the thumbnail. So the evidence is partial: shape is the only typed attribute without a quota and 3 of 5 misses are
  unverified on it, but the file cannot say which attribute each item broke.
- Typed shape: `ShopQuery.shape_terms` (set by `build_query` from the request: round, oval, rectangular or square, plus circular and rectangle), a 70% quota in
  `typed_attributes` (a rule accepting that shape only), the same banner ("Only 4 of 24 results match your shape round: the pool had 4 that do."), a "Shape"
  chip that deletes through `remove_context(kind="shape_term")` and takes the quota with it. Old sessions have no `shape_terms` and so no quota.
- Existing tests edited (the guard now lets a single-valued pool fill the batch): `test_the_guard_that_cannot_be_met_says_so_and_returns_what_exists` (renamed
  `test_a_guard_with_no_alternative_value_is_off_and_says_so`; 24 results and the new sentence), `test_warnings_state_the_real_shares_and_the_limit`,
  `test_supply_stops_at_the_open_cap_of_six_calls_and_the_warning_has_real_counts`.
- Tests: 10 new (`tests/test_shop_stage2r_guards.py`), suite 1543 with phase 5.

## Phase 5
- Cause of "APITimeoutError after 1.0 s" (case-04 round 3, three-case run): `candidates.tag_batch` and its retry gave every call `max(1.0, min(call timeout,
  deadline - now))`; a call (or the retry in halves) that started with a second of the tagging budget left got a one second timeout and failed. The photo step
  had the same floor.
- Fix: `MIN_TAG_CALL_TIMEOUT_S` = 8 s. A call whose deadline leaves less is not started (counted as skipped); the timeout is `max(8, min(12, left))`; a
  timed-out call is retried once (in halves, as before) only when 8 s remain; the photo step does not start, start a later wave or retry without it.
- Existing tests edited: `test_the_tagging_budget_ends_the_work_and_the_rest_stays_unknown` (the call at 12 s of a 15 s budget is no longer started: 2 calls, 80
  tagged) and `test_the_hard_stop_shortens_the_tagging_deadline_and_the_request_timeout` (9.5 s left gives a 9.5 s timeout; 4.5 s starts nothing).
- Tests: 7 new (`tests/test_shop_stage2r_timeout.py`), suite 1543.

## Phase 6
- `evals/shop/cache.py`: `CachingSerper` (record, use, replay, off), keys by a hash of the request, files hold request, timestamp and response only, 14-day
  freshness, stats (hits, misses, recorded, stale, credits saved, what a replay lacked). The runner charges each round for the credits it actually spent; the results
  file has `serper_cache` and the report a "Serper cache" section with "cached pool, not a fresh-world run". `.gitignore` has `evals/shop/cache/`. The app's runtime
  never imports it (a test checks).
- `make eval-shop-replay` (`evals/shop/structural.py`): mock model and judges on saved responses; a miss is an error (exit 1 and the missing queries); an empty cache
  exits 2 with "run a live eval once with recording". `make eval-shop-lint RESULTS=...` (`evals/shop/lint.py`): 9 anomaly rules, severity order, at most 38 lines.
  `python -m evals.shop lift` (`evals/shop/diagnose_lift.py`) and `docs/evals/shop-lift-diagnosis-2026-10-29.md`.
- The saved responses are not in the repo (real Google data); tests use a synthetic cache recorded from the mock catalogue.
- Tests: 19 new (`tests/test_eval_shop_cache.py`), suite 1562; the two plan tests were edited for the new measured constants.

## Cross-cutting
- `plan.py` uses the full run's costs (pipeline $0.0325 a case incl. Serper, 19 credits, $0.00045 a judge call); the plan also prints the cached figures.
- Browser check: `run_fixes_2r` (fill stop reason, the causes, guard off, typed shape banner and chip) with scenarios `narrow`, `flat`, `roundshort`; the page's Shop Again
  status now shows the fill stop and the relaxed cap, price and guard notes.
- Docs: `docs/evals/shop-eval-full-run-2026-10-29.md`, the lift diagnosis, ADR 0012 amendment 2r, SPEC, todo, working notes, `evals/shop/README.md`, `known_instability.md`.
