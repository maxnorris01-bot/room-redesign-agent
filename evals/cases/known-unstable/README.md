# Known-unstable cases

Cases here are **not** loaded by `evals/run.py::load_cases` - it globs `evals/cases/*.jsonl`
non-recursively, which does not descend into this subdirectory. (Confirm this empirically for your
own project before relying on it - write a throwaway test against a scratch directory rather than
just trusting this comment, the way the first project built from this template did.) They don't
count toward pass_rate, don't gate CI, and aren't part of any tier's case set.

## What belongs here

A case moves here when a live run against **identical code and input** produces a **different
result** on a second run - genuine model instability on a judgment-heavy input, not a
case-authoring mistake. This tends to show up on inputs that legitimately straddle a boundary
between two valid labels (a real fact reported with dropped caveats; evidence that's suggestive but
not conclusive). `evals/run.py`'s live-mode scoring is a single fixed-string `expected_contains`
match; it cannot express "either of these two outputs is acceptable, this third one isn't," which
is what a genuinely ambiguous case would actually need. Gating CI on a coin flip isn't a useful
signal, so park the case here instead of either force-fitting it to one label or silently dropping
it.

When you move a case here, give it a `note` field quoting the specific before/after results you
saw, and cross-reference the reproduction in the project README's Known Failures section and the
relevant session doc - the evidence should be readable from either place.

## What retires a case from here

A gold-set-validated LLM-as-judge (`evals/README.md`'s "a judge is trusted only after it agrees
with a human-labeled gold set" rule) - not a tighter `expected_contains` string. Once that judge
exists and is validated for this project, move the case's file back up into `evals/cases/` (or wire
this directory into a dedicated judged-case loader) rather than gating it on exact-string matching
again.
