# Stage 2r: fixes from the first full 12-case run, plus free offline tools

Date: 2026-10-29. Mock mode, fixtures and saved results only; no live call, no live eval, no live rejudge. `evals/shop/thresholds.json` and `THRESHOLDS.md`
are unchanged (a test asserts SHA-256 `c64fe35a...d237`). Evidence: `docs/evals/shop-eval-full-run-2026-10-29.md` (run `20261029-034000-3f9fa0`, one run,
nothing replaces the baseline) and `docs/evals/shop-lift-diagnosis-2026-10-29.md`. Precondition: main is at ff88439 (PR #32), read from `.git/refs`, no git
command run for it. The per-phase notes are in `2026-10-29-shop-fixes-2r-progress.md`.

## What changed and why
1. **Fill loop.** A batch short of 24 after every filter, cap and guard now runs on-request variant searches until it is full (a Shop Again's 9 calls, a first
   search's 4, two searches in a row adding nothing, or the hard stop), with each step and the stop reason in the trace and the page, and "N filtered out" broken
   into causes. Replaces the thin-first-search block that looked at the pool size.
2. **Early stop.** Optional searches (profile phrasings, retailers that are not liked sellers, supply) are skipped when 60 unseen candidates pass the filters and the
   quotas, focus and guards can be met; the liked-seller and writer searches always run. Case-02 (9 calls twice, pools of 218 and 205) should drop to about 5.
3. **Refine focus supply** counts unseen candidates (a measurement change; threshold unchanged); variants keep the focus words.
4. **Guards and typed shape.** A guard is off when fewer than 6 unseen candidates carry another value; a typed shape is a 70% quota with a deletable chip.
5. **Tagging timeout.** No tagging call gets less than 8 s; one that cannot is not started; a timeout is retried once when that much remains.
6. **Eval tools, free.** Serper record and replay cache (gitignored), `make eval-shop-replay`, `make eval-shop-lint`, the lift diagnosis, measured plan costs.

## Choices where the brief was silent
- New behaviours are off in a bare Config and on from the environment (`APP_SHOP_THIN_VARIANTS` now also gates the Shop Again fill loop; new `APP_SHOP_EARLY_STOP`), the
  repo's pattern, so older tests did not move. The guard rule, the timeout floor and the typed shape apply always.
- Liked-seller searches and writer variants are "personalising" and always run; profile phrasings and non-liked retailers are "optional".
- Supply for the focus check means covering the reserved quota, ceil(0.5 x the batch), not merely one candidate.
- The case-09 diagnosis could not isolate the violated attribute (the judge gives yes/no/cannot_tell); I implemented the shape quota on thinner evidence than for colour
  (see the progress notes) and say so.
- The lift diagnosis has a fifth verdict, "learned something the titles cannot confirm", because the saved file has no tags and most chips are not named in titles.
- The replay is structural: the mock writer's variants may not be in the saved responses; misses are listed, not hidden.

## Estimates (from `make eval-shop-plan`, built from the full run's measured costs)
Full 12-case run about $0.46 (Anthropic $0.23, 228 credits expected; measured last time $0.45); with every Serper call a cache hit about $0.23 and 0 credits. A 5-case
re-run about $0.19 and 95 credits, about $0.09 cached. Cases 04, 06 and 12 about $0.11, about $0.06 cached. One Shop Again about $0.011 (6.3 credits). The worst
case, 528 credits for 12 cases, is over the 500-credit hard stop (the expected 228 is not). The estimate does not include the early stop's savings.

## Tests and checks
1567 tests pass (1494 before): fill 14, early stop 9, focus 9, guards and typed shape 10, timeout 7, eval cache/replay/lint/diagnosis 19, docs 5. ruff, `ruff format --check`, `mypy --strict`, `make shop-check` and `make eval-shop-mock` pass.

## Existing tests edited (a design change forced each)
- Guard off: `test_the_guard_that_cannot_be_met_says_so_and_returns_what_exists` (renamed `test_a_guard_with_no_alternative_value_is_off_and_says_so`),
  `test_warnings_state_the_real_shares_and_the_limit`, `test_supply_stops_at_the_open_cap_of_six_calls_and_the_warning_has_real_counts`.
- Tagging minimum: `test_the_tagging_budget_ends_the_work_and_the_rest_stays_unknown`, `test_the_hard_stop_shortens_the_tagging_deadline_and_the_request_timeout`.
- Measured plan costs: `test_the_estimate_uses_the_measured_full_run` (renamed from `..._post_2p_rerun`), `test_the_estimate_gives_one_shop_again_and_a_five_case_rerun`.

## Flag before merging
- Nothing here was checked live: the fill loop's variants, the early stop's effect on quality, the typed shape quota, the 8 s floor.
- The eval's focus check changed its measure; the baseline document stays the record of the old one.
- The fill loop appends new candidates unranked (no extra rank call), like the old refill search.
- A first search that fills can take longer (a writer call and up to two searches); the first-search cap is 4 calls.

## Next command
`make eval-shop-plan` and, if you agree, `make eval-shop-live EVAL_SHOP_CONFIRM=yes` (it records the Serper responses; `make eval-shop-replay` is free afterwards).
