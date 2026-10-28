# Shop eval: known sources of run-to-run variation

Read this before concluding anything from one failing case.

## What varies between identical runs

- **Live search results change.** Serper returns different listings, prices and ordering from day to
  day (stock, promotions, ranking). The same request can show a different first batch tomorrow.
  Seller and price spreads, near-duplicate groups and "supply exists" for a refine focus all move with
  it. The mock run is deterministic; the live one is not.
- **The judges are models.** Haiku answers the category, attribute and persona questions with some
  variation, and there are no sampling controls on these models, so two judgements of the same
  listing can differ. A persona match rate on 24 items moves by several points on judge noise alone.
- **The pipeline's own model calls vary.** Request parsing, tagging, ranking and preference reading
  are model calls too; a Shop Again after identical ratings can pick a different batch.
- **Thumbnails.** A thumbnail that fails to fetch leaves its listing to be judged on title alone
  (the judge stats count images used of offered), which shifts attribute and persona answers a little.
- **Timing and cost** depend on provider latency at the time.

## How to treat one failing case

1. Re-run that case once (`make eval-shop-live EVAL_SHOP_CONFIRM=yes ARGS='--case case-04'`) before concluding. A case that fails twice, with the same check, is a
   real finding.
2. A mean or lift threshold failing across many cases is a finding on its own; do not re-run to
   make it pass.
3. A case that flips between identical runs is not moved out of the set quietly: record the two
   results here with the run ids, then move it to `cases/known-unstable/` with a note (the repo's
   convention in `evals/cases/known-unstable/README.md`), in its own commit.
4. Never edit `thresholds.json` or a case to turn a failing run into a passing one. Threshold and
   case changes are their own commits, and the thresholds hash in every results file says which
   numbers a result was held to.

## Judge limits (see also the report footer)

Same model family as the pipeline: shared blind spots are possible. The persona is a text, not a
person. Neither judge has been calibrated against human labels; until then judge scores are a
relative signal (did Shop Again move toward the persona?) more than an absolute one.

## Recorded instability

First live run (`shop-live-20261027-041854-b00d9f`, baseline in `docs/evals/shop-eval-baseline-2026-10-26.md`): the spread of
the 12 per-case round-2 lifts has a standard deviation of 14.3 points, which is what two independent 24-item batches at a 55%
match rate give by sampling alone (14.4); one item is 4.2 points. The per-case lift threshold (-10) is therefore inside the
noise of one batch, and only the mean (+6.2, standard error 4.1) is informative. The judge's own repeatability has not been
measured yet: `make eval-shop-rejudge` does it (about $0.03 for that file). Cases that started at 65% or more (03, 06, 10, 11)
regressed by 0.7 points on average against +9.6 for the other eight, so a high first batch is a ceiling, not a defect.

Rejudge of the same run (35 batches, 781 items, $0.0256): the persona judge agreed with itself on 90% of three-way answers
and 93% of match / no-match answers; the mean absolute difference in a batch's match rate was 3.6 points, the mean difference
-0.3, the spread (sd) 6.1 points. Noisiest cases: case-12, case-04, case-08, case-09 and case-11 (85 to 88% three-way
agreement). Judge noise on one case's lift is therefore about 6 points, about 1.8 points on the 12-case mean, so the mean lift
shortfall against +15 is not judge noise. (The batch-to-batch sampling spread, 14.3 points, is the larger part of the
per-case noise.)
