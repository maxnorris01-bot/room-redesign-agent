# Evals

## Tiers

| Tier | Runs | Cases |
|------|------|-------|
| fast | every PR | first 15 |
| standard | merges to main | first 50 |
| nightly | scheduled | all |

Case order matters: put the highest-signal, most representative cases first.

## Adding cases

Append to `cases/*.jsonl` (directly in `cases/`, not a subdirectory - the loader globs
non-recursively). Each line: `id`, `category`, `input`, and either `expected_contains`
(deterministic) or a `rubric` name (LLM-as-judge). When you find a real failure in the wild, add
it here as a regression case.

### Cases the model itself is unstable on

If a case's live-mode result turns out to flip between identical runs (same code, same input - not
a case-authoring mistake, genuine model instability on a judgment-heavy input), it doesn't belong
in the gating set: a fixed-string `expected_contains` match can't express "any of these outputs is
fine, this other one isn't," and gating CI on a coin flip isn't a useful signal. Move it to
`cases/known-unstable/<id>.jsonl` instead (a subdirectory - the non-recursive loader won't pick it
up) with a `note` field quoting the before/after results, and see that directory's `README.md` for
the full convention. Document the reproduction in the project README's Known Failures section too.
This is not a way to make an inconvenient case disappear - it's for a case you've watched genuinely
flip, with the evidence written down both places.

## Rules

- Case and threshold changes go in their own commits, never bundled with the change they'd excuse.
- A judge is trusted only after it agrees with a human-labeled gold set (`gold/`). Record its agreement rate in the README results table.
- Reports in `reports/` include prompt versions so results are attributable.
- Thresholds in `thresholds.yaml` are starting points; tune per project.
