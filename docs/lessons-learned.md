# Lessons learned (cross-project)

A living doc. Each project gets a dated section below as gaps in this template get discovered
while building it. When a fix actually lands in this template's own code, mark it done here and
note the commit - don't just delete the entry, since "we tried X and it didn't work" is as useful
as "we tried X and it worked."

This doc records what was *found*, and each gap notes whether it's been *applied* to this
template's own code yet - a found-but-not-applied gap is still real, just not acted on.

---

## From: Claim Verification Agent (v0), 2026-09-21 to 2026-09-23

### Gaps found in this template

1. **No mock/live LLM toggle. `[APPLIED 2026-09-23]`** This template had no `src/app/llm.py` at
   all - no client abstraction, mocked or real. Claim Verification built one from scratch:
   `APP_LLM_MODE=mock` (default) returns a `MockAnthropicClient` that generates schema-valid but
   arbitrary responses - free, no key required, good for checking routing/plumbing/schema on every
   change - while `APP_LLM_MODE=live` hits the real API and costs money. Ported into this template
   as `src/app/llm.py` (the budgeted, traced, streaming-capable `complete()` wrapper) and
   `src/app/mock_llm.py` - the latter generalized from Claim Verification's tier/verdict-specific
   heuristic router into a generic JSON-schema-driven placeholder generator, since this template
   has no domain schema of its own to route against.

2. **Eval scoring ran sequentially. `[APPLIED 2026-09-23]`** `evals/run.py`'s `main()` used a
   plain list comprehension. Since each case is dominated by network wait, not CPU, switched to
   `ThreadPoolExecutor(max_workers=min(5, len(cases)))` with `.map()` (preserves input order in
   results regardless of completion order, so reports stay reproducible) - the same shape Claim
   Verification proved out, ported as-is.

3. **`tracing.span`'s file write wasn't thread-safe - a prerequisite for #2, not optional.
   `[APPLIED 2026-09-23]`** Added the same module-level `threading.Lock` around the trace file
   write that Claim Verification added, landing in the same commit as #2 rather than after it.

4. **Eval cost was hardcoded to `0.0`. `[APPLIED 2026-09-23]`** `score_case()` now takes a
   `Config` and constructs a real `Budget` per case, reading `budget.cost_usd`/`budget.steps` back
   after `system_under_test` returns - the same pattern `app.pipeline.run(text, budget=budget)`
   uses. Cost only becomes real once a project's own pipeline actually calls `app.llm.complete`,
   but the wiring itself no longer needs to be invented per project.

5. **No aggregate, run-level cost cap. `[APPLIED 2026-09-23]`** Added an optional
   `max_total_cost_usd` threshold (`evals/thresholds.yaml`) and a check in `evals/run.py` that sums
   `cost_usd` across all results and fails the run if it's exceeded - a CI-time backstop on top of
   `Config.max_cost_usd`'s per-case cap. This was genuinely new, not just ported: Claim
   Verification didn't have it at first and relied on a manually-set Anthropic Console spend limit
   as the only aggregate backstop. **Claim Verification adopted it back on 2026-09-26**
   (`max_total_cost_usd: 2.50`, sized for its 13-case fast tier), so the pattern is now proven in a
   real project. One thing it surfaced: the value is a single number across all tiers, so a project
   with a much larger standard/nightly tier needs per-tier values or a higher number.

6. **Placeholder thresholds were very optimistic. `[APPLIED 2026-09-23]`** `max_latency_p95_s`
   raised from `2.0` to `30.0` as a more realistic (still explicitly a guess) starting point, and
   both `thresholds.yaml` and `config.py` now carry an explicit comment explaining that these
   numbers are guesses to be tuned after a real `make eval-fast-live` run, referencing this doc, so
   a future project doesn't mistake "I have to raise this a lot after the first live run" for a red
   flag the way it easily could without the comment.

7. **No documented convention for run-to-run LLM verdict instability. `[APPLIED 2026-09-23]`**
   Added a "Cases the model itself is unstable on" section to `evals/README.md` and a generic
   `evals/cases/known-unstable/README.md` (genericized from Claim Verification's version - same
   convention, no claim-specific case IDs or quoted claims).

### Things that worked well as-is - no change recommended

- Prompt versioning via frontmatter (`version: N`) plus `prompt_versions` in every report summary -
  used as designed, made the provenance_only prompt fix this session directly attributable to a
  specific report.
- `evals/README.md`'s "case and threshold changes go in their own commits, never bundled with the
  change they'd excuse" rule - held up as a real guardrail across every session on Claim
  Verification, not just words in a doc.
- The ADR convention (`docs/adr/`) - used for real, non-obvious decisions (classifier shipping
  ahead of full evaluator coverage; web search tool vs. a separate search API), not just checked off
  as a formality.

### A prompt-writing lesson worth carrying forward (not template code, but a pattern)

An evaluator prompt that outputs both a structured verdict label *and* free-text reasoning needs an
explicit instruction keeping the two consistent. The `provenance_only` prompt's first version let
the model hedge in its own reasoning ("cannot be confirmed or refuted directly") while still
selecting a verdict that contradicted that hedge (`not supported` instead of `provenance-only`).
Adding an explicit check - "if your own reasoning wouldn't confirm or refute the claim, the verdict
must be X" - fixed it. Worth building into any future evaluator prompt with multiple verdict labels
that have subtle boundaries between them (this will very likely come up again for Purchase Decision
Agent's buy/wait/skip verdict).

### Status

All seven gaps above are applied to this template's own code as of 2026-09-23 - see each item's
`[APPLIED]` tag. `make install && make lint && make typecheck && make test && make eval-fast` was
run against the ported code on 2026-09-23 and confirmed clean. One real issue turned up along the
way: `make lint` failed on a `ruff format` mismatch in `evals/run.py` (a long f-string the
formatter wanted collapsed to one line) - fixed by running `make format` before `make lint`, no
behavior change. No other issues found.

### Still open, not applied here (in scope for a future pass, not this one)

- `eval-standard`/`eval-nightly` still don't have mock-forced/live-forced variants - noted in the
  template's own `Makefile` and README, inherited as-is from Claim Verification's own unresolved
  version of the same gap.
- No adversarial/prompt-injection eval pattern documented - Claim Verification flagged this as its
  own open item too; hasn't been generalized into the template.
- The self-grading / outcome-tracking pattern the portfolio plan calls out as the differentiator
  for two of the three projects (Claim Verification, Purchase Decision Agent) isn't reflected in
  the template at all yet - it needs a persistence layer this template doesn't have an opinion on.
  Worth a dedicated pass once the first project actually builds that piece, rather than guessing at
  its shape now.

---

## Workspace note (resolved)

`~/Desktop/Projects/ai-project-template/` - the stale, untracked duplicate of this repo noted
above - was deleted on 2026-09-23.
