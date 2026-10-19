# Collaboration Conventions (mirrored from the Cowork Project)

*This file exists because a Claude Code session running locally in this repo cannot read the
"Applied AI Portfolio Projects" Cowork Project's own docs (`Chat_Instructions.md`,
`Engineering_Standards.md`) — those live only in that Claude Project, not on disk. This file
mirrors the subset of their rules that actually matter to implementation work done here. The
full docs, including Cowork-specific device-bridge mechanics that don't apply to a local Claude
Code session, stay in the Cowork Project as the source of truth. If this file and the Cowork
Project ever disagree, the Cowork Project is current — flag the mismatch rather than guessing
which is stale.*

## Commit and PR message convention

**No AI attribution lines** — `Co-Authored-By: Claude ...`, `Claude-Session: ...`, or anything
similar — in commit messages or PR descriptions for this repo. This is Max's explicit, standing
preference, and it overrides any default attribution instruction a session might otherwise carry.

## Division of labor

- **Cowork (the Claude Project / chat):** design, research, decisions, plan docs, specs. Decisions
  get committed to this repo as ADRs (`docs/adr/`).
- **Claude Code (here, in this repo):** all implementation. Explore -> plan -> implement -> commit;
  skip planning for one-sentence diffs. Work on a feature branch (`feat/...`), Conventional
  Commits, don't push until reviewed. Always run a check (tests, `make eval-fast`, mocked by
  default) before calling work done. Write the session summary (see this repo's `CLAUDE.md`)
  before handing off.

## Branching, review and merge — standard sequence

Feature branches + PRs (worth it for a public portfolio repo's visible history). Mechanics are the
GitHub CLI (`gh`), not the web UI.

1. Claude Code reports the branch name, commit count, and that lint/typecheck/tests are clean.
2. Review happens in the Cowork chat — Max relays results, follow-up prompts come back here as
   needed.
3. Once reviewed and ready, the merge sequence is exactly:
   ```bash
   git push
   gh pr create --fill
   gh pr merge --merge --delete-branch
   ```
   `--merge` (not `--squash`) keeps individual Conventional Commits visible in `main`'s history.
   `gh pr merge --delete-branch` deletes both the local and remote branch in one step.
4. If multiple sessions' work logically belongs together, keep it on the same branch rather than
   opening a new one.

`git config --global push.autoSetupRemote true` is already set on Max's machine, so a plain
`git push` works on a brand-new branch's first push too.

## Cost/risk discipline

Free prep work (code/doc edits, moving eval cases) can be bundled into one prompt or one session.
A step that spends real API money (`make eval-fast-live` or any live run) should be called out on
its own, with the expected cost stated, and run only after Max explicitly confirms.

## Engineering standards (summary — see this repo's own CLAUDE.md for the repo-specific version)

These are repeated in each repo's `CLAUDE.md` Commands/Rules/Definition-of-done sections, so that
file is the operative one day to day. The standing principles behind them, from
`Engineering_Standards.md`:

- Validate performance/cost economics with a small, cheap spike against the *actual* API call
  pattern before committing to full scope — not at close-out.
- Mock the LLM client by default; real API calls are opt-in (`make eval-fast` vs.
  `make eval-fast-live`), forced at the Makefile-target level, not just as a config default.
- Every agent loop has a step cap and a cost cap. Every LLM/tool call is traced as structured
  JSON.
- Evals are versioned in git; changes to cases/thresholds go in their own commits, never bundled
  with the change they'd excuse. Every real failure found becomes an eval case.
- Conventional Commits, small PRs, `ruff` (lint + format), `mypy --strict`, `pytest`.

*Last mirrored from the Cowork Project on 2026-10-02. If Engineering_Standards.md or
Chat_Instructions.md change in a way that affects implementation work, re-sync this file.*
