# To-do - Room Redesign Agent

A running checklist, not a narrative. Check items off as they're done and add a short note (what
happened, any surprises) rather than deleting the line - that note is often more useful later than
the checkmark itself. Add new items as they come up; don't wait for a session's end. This is
*not* where decisions and reasoning go - that's `docs/working-notes-and-decisions.md`. This is just
"what's next."

## Up next (in priority order)

<!-- The most important items, in the order you'd actually tackle them. -->

- [ ] Workspace spend limit and project-named API key in the Anthropic Console (`portfolio-projects` workspace), before any live run. Walk through one step at a time.
- [ ] **Exploratory live run on one room (Max's call, ungraded):** put a photo in `spike-photos/`, then `make spike-live ARGS='spike-photos/<photo> --budget 800 --style "warm minimalist"'` (rough guess $0.05-0.15, unmeasured; the step/cost caps in `Config` will stop it). Record the per-stage table in a session doc. Check first that the models accept the tools: Models API `capabilities.server_tools.web_search.supported` for `claude-haiku-5-5`.
- [ ] Cowork: research shopping API candidates (pricing, terms, coverage) for retrieval configuration B.
- [ ] Lock cost and latency targets in ADR 0004 (after the exploratory run, before the measured 3-room run).
- [ ] Measured 3-room spike; go / no-go / redesign recommendation.

## Later

<!-- Real, but not next. -->

- [ ] Spike follow-ups, only if the exploratory run says they matter: run retrieval slots in parallel (latency; `Budget` is not thread-safe yet), compare `web_search_20260209` dynamic filtering vs the basic tool, sweep thinking effort per stage, prompt caching for the shared prompt prefix, `response_inclusion: excluded` on `web_search_20260318`.
- [ ] CI: `eval-standard` / `eval-nightly` jobs now run the mock-forced targets; point the scheduled nightly job at `eval-nightly-live` only once there are cases and a spend limit.
- [ ] Port the mock/live-forced `eval-standard` / `eval-nightly` Makefile split back to `portfolio-project-template` and update its `docs/lessons-learned.md`.
- [ ] Eval cases (12), mock/live-aware scoring, thresholds set in their own commit.
- [ ] Self-check loop, canvas UI, site tab in `portfolio-site`, Vercel proxy, Fly deployment.
- [ ] Manual drag-and-drop editor (v1.1).
- [ ] Photo-real empty-room experiment (gated, ADR 0003).


## Lower priority / opportunistic

<!-- Worth doing if the moment presents itself; not worth planning around. -->

## Completed (most recent first)

<!-- Move items here when done, newest first, with a one-line note on what happened. -->

- [x] 2026-10-18 Claude Code session 1: repo setup (placeholders replaced, `CLAUDE.md` filled, mock/live-forced targets for every eval tier, `.env.example`) and the spike skeleton (`scripts/spike.py`, perception, retrieval config A, pricing table, mock mode, tests). No live calls were made; see `docs/sessions/2026-10-18-spike-skeleton.md`.
