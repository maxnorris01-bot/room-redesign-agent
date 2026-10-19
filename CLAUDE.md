# CLAUDE.md

## Session startup (read these in full before doing anything else)
1. `docs/collaboration-conventions.md` - the commit/PR convention (no AI attribution),
   branch/merge sequence, and division-of-labor rules mirrored from the Cowork Project's
   `Chat_Instructions.md` / `Engineering_Standards.md` (those two live only in that Claude
   Project, not on disk, so this local copy is what a Claude Code session actually reads). Read
   once; it doesn't change per repo unless the Cowork Project's copy changes.
2. This file, in full.
3. `docs/lessons-learned.md` - gaps found building real projects against this template, and
   whether each has been applied back here yet.
4. If cloning this template to start a new project: also read the target project's own
   `docs/working-notes-and-decisions.md` and `docs/todo.md` once they exist (this template's
   copies are blank stubs, not live content).

## Commands
- Install: `make install`
- Lint + format check: `make lint`
- Tests: `make test` (single test: `uv run pytest tests/test_x.py::test_name`)
- Fast evals (mock, free): `make eval-fast` (run after ANY change to prompts or agent logic)
- Fast evals (live, real API cost): `make eval-fast-live` - run deliberately, not routinely

## Rules
- IMPORTANT: Prompts live in `prompts/` as versioned files. Never inline prompt strings in code.
- Any change to a prompt or agent logic must be followed by `make eval-fast`. Show the output before calling the work done. `make eval-fast` defaults to a mock client (`APP_LLM_MODE=mock`, free, no key required) - it's a routing/shape/plumbing check, not a quality gate. For a change that could affect output quality (a prompt's content, an agent logic, a model choice), also run `make eval-fast-live` deliberately and report its real numbers - it costs real API money, so don't run it as part of routine iteration.
- Eval cases and thresholds (`evals/`) change only through explicit, separate commits. Never edit them to make a failing eval pass. If a case turns out to be genuinely unstable across identical live runs, see `evals/README.md`'s "Cases the model itself is unstable on" - move it, don't force-fit or drop it.
- Never commit secrets. Config comes from environment variables; `.env.example` lists what's needed.
- Every agent loop must have a step cap and a cost cap, both set in `src/app/config.py`. Both defaults there are guesses - see `evals/thresholds.yaml`'s comment - validate them against a real `make eval-fast-live` run before trusting them, and expect to raise them at least once.
- Log every LLM/tool call through `src/app/tracing.py` so runs are inspectable.
- Record non-obvious design decisions as a short ADR in `docs/adr/`.

## Definition of done
Lint clean, tests pass, `make eval-fast` (mock) passes `evals/thresholds.yaml` as a plumbing check. For anything touching output quality (prompts, agent logic, model choice), `make eval-fast-live`'s real numbers vs. `evals/thresholds.yaml` are the actual quality gate - report them, don't just cite the mock pass. README results table updated if live numbers changed.

## Session summary (required at the end of every implementation session)
Write `docs/sessions/<YYYY-MM-DD>-<short-slug>.md` before handing off, even if not asked. It's read by someone outside this session with no other context, so write it plainly, not as agent notes to self. Include:
- What changed and why (the key decisions, not a diff narration)
- Anything you asked the user to choose between mid-session, and what was picked
- Eval numbers: tier run, pass rate, latency p95, mean cost, vs. thresholds
- Test/lint/typecheck status
- Open questions or things you'd flag for review before this merges
- Exact next command the user should run (e.g. `git diff main`, `make eval-fast`)

## Working notes and decisions log
`docs/working-notes-and-decisions.md` is a different, longer-lived record than the session docs
above. Session docs narrate one sitting's diff; the working notes log is a short, durable list of
decisions (with the reasoning, not just the choice) and open items worth remembering - meant to be
skimmed months later, not read start to end. Update it whenever a real decision actually gets made
(scope, sequencing, what to defer and why, a tradeoff picked between options) - not just as an
end-of-session ritual. If something is already fully written up elsewhere (a technical fix in a
session doc, a bug in the README's Known Failures, a design tradeoff in an ADR), link to it here
rather than duplicating the detail. A blank template lives at `docs/working-notes-and-decisions.md`
in this repo - projects cloned from this template start with it and should fill it in as they go.

## To-do list
`docs/todo.md` is the running checklist - what's next, in priority order, checked off with a short
note as items get done. Different from the working notes log above: that's decisions and reasoning,
this is just task state. Keep it current as priorities shift, not just when items complete - it's
meant to answer "what do I work on next" after time away without having to reconstruct it from
session docs. A blank template lives at `docs/todo.md` in this repo - projects cloned from this
template start with it.

## Conventions
- Conventional Commits (`feat:`, `fix:`, `eval:`, `docs:`, `chore:`).
- One logical change per commit; small PRs.
