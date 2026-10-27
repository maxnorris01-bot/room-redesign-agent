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

None yet: no live run has been made.
