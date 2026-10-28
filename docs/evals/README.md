# Eval reports

Markdown reports rendered from Shop eval results files (`make eval-shop-report RESULTS=... OUT=docs/evals/shop-<run>.md`).
A report holds no thumbnails or links and truncates titles; the results JSON it came from stays in the
gitignored `evals/shop/results/`. Commit a report here only for a run you want to keep; a mock run's report
says so at the top and is not a quality result.

## Reports kept here

| File | What |
|------|------|
| `shop-eval-baseline-2026-10-26.md` | The first live Shop eval (run `shop-live-20261027-041854-b00d9f`): the rendered report, then a diagnosis of the six failed checks. Thresholds and pipeline were left as they were so later runs compare with it. |
