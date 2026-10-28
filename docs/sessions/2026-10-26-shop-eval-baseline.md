# Stage 3, baseline: diagnosing the first live Shop eval

Date: 2026-10-27 (the run is dated 2026-10-26 in the file names). No live call, no rejudge, no change to the pipeline,
`thresholds.json` or `THRESHOLDS.md`.

## What was done
Read the saved results file `shop-live-20261027-041854-b00d9f.json` and the code behind each failed check, and wrote the
baseline document `docs/evals/shop-eval-baseline-2026-10-26.md`: the rendered report followed by the diagnosis (seven
questions), a ranked fix list, and what was deliberately not changed. Added the `rejudge` command and a richer saved trace.

## Key findings (details and evidence in the baseline document)
1. Case 12 colour 20.8%: the scorer reads colour from titles only and 18 of 24 titles name none; the pipeline treats a typed
   product colour as a ranker line, and the ranker and candidate tagger never see an image. Partly measurement, probably also a real
   gap; undecidable without a photo-based colour measure.
2. Case 10 focus 41.7%: round 2 was 54%, round 3 42%. "mesh" is not in the vocabulary, so the refine became a free note, which
   is two lines of text and reserves nothing. A real defect.
3. Case 06: 12 of the 13 attribute misses are "cannot tell" (titles omit size and colour), one is a wrong size. Round 3 returned
   nothing: the single phrasing was paged to its limit (page 3 is a last resort, page 4 never) and nothing new was learned.
   Round 1 was at the ceiling (70%) and the pool is deeper pages afterwards.
4. Case 04: Lowe's 4 of 12; the pipeline caps a count (4), the threshold a share (30%); no naming problem.
5. Lift: the 12 per-case lifts spread by 14.3 points, exactly what two 24-item batches predict by sampling (14.4); the mean is
   +6.2 with a standard error of 4.1. Cases starting at 65% or more regress by 0.7 on average, the rest gain 9.6. At least 5 of 12
   cases produced a new chip or leaning in round 2 (a lower bound).
6. `rejudge` estimate for this file: 35 batches, 781 items, 67 judge calls, about $0.031. Not run.
7. Real defects: Shop Again can run out of queries, typed colour not enforced, free notes reserve nothing, category folding;
   threshold shape: the count cap vs share, the per-case lift worst; measurement: "cannot tell" counted as a miss.

## Changes (eval code and docs only)
- `evals/shop/rejudge.py`, the `rejudge` subcommand in `evals/shop/__main__.py`, Makefile target `eval-shop-rejudge` (needs
  `EVAL_SHOP_CONFIRM=yes`, refuses under CI and pytest; a mock results file uses the mock judge and needs no flag);
  `plan.judge_call_usd` factored out of the estimate.
- `runner.trace_summary` keeps the request colours, chips, alignment signals, per-dimension decisions and pool counters (no titles
  or links); `runner.footer_line` says "none yet" when nothing was decided, like the page.
- Docs: the baseline document, `docs/evals/README.md`, `evals/shop/README.md`, `evals/shop/known_instability.md` (the noise numbers),
  `docs/todo.md` (the fix list), the working notes, an ADR 0012 note.

## Estimate vs actual (from the saved totals)
Anthropic $0.144 (estimated $0.43), 204 Serper credits (estimated 240). The plan's pipeline constant ($0.01 per round) was about
four times too high; worth correcting in `plan.py` in a later change (not done here, to keep this stage to diagnosis).

## Tests and checks
ruff, `ruff format --check` and `mypy --strict` clean; 1275 tests pass (1259 before): rejudge 10, runner +2, baseline document 4.
`make shop-check` all checks passed; `make eval-shop-mock` completes (informational verdict); `make eval-fast` pass rate 1.0.
Existing tests edited: none.

## Flag before merging
- The round-3 cause of case 06 and the per-round chips are inferences (the file did not keep the query plan or the profile); the
  next live run keeps them.
- The leanings in the lift table are recomputed from title keywords (a lower bound); seller, tier and marketplace are exact.
- The lift conclusions should wait for the rejudge numbers.

## Next command
`make eval-shop-rejudge EVAL_SHOP_CONFIRM=yes RESULTS=evals/shop/results/shop-live-20261027-041854-b00d9f.json`
