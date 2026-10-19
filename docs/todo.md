# To-do - Room Redesign Agent

A running checklist, not a narrative. Check items off as they're done and add a short note (what
happened, any surprises) rather than deleting the line - that note is often more useful later than
the checkmark itself. Add new items as they come up; don't wait for a session's end. This is
*not* where decisions and reasoning go - that's `docs/working-notes-and-decisions.md`. This is just
"what's next."

## Up next (in priority order)

<!-- The most important items, in the order you'd actually tackle them. -->

- [ ] Workspace spend limit and project-named API key in the Anthropic Console (`portfolio-projects` workspace), before any live run. Walk through one step at a time.
- [ ] Cowork: research shopping API candidates (pricing, terms, coverage) for retrieval configuration B.
- [ ] Claude Code session 1: repo setup (rename `PROJECT_NAME` and `OWNER/REPO` placeholders, fill `CLAUDE.md` for this project, mock-forced and live-forced targets for every eval tier, `.env.example`), then the spike skeleton per `SPEC.md`.
- [ ] Exploratory live run on one room (ungraded); record the per-stage table in a session doc.
- [ ] Lock cost and latency targets in ADR 0004 before the measured 3-room run.
- [ ] Measured 3-room spike; go / no-go / redesign recommendation.


## Later

<!-- Real, but not next. -->

- [ ] Eval cases (12), mock/live-aware scoring, thresholds set in their own commit.
- [ ] Self-check loop, canvas UI, site tab in `portfolio-site`, Vercel proxy, Fly deployment.
- [ ] Manual drag-and-drop editor (v1.1).
- [ ] Photo-real empty-room experiment (gated, ADR 0003).


## Lower priority / opportunistic

<!-- Worth doing if the moment presents itself; not worth planning around. -->

## Completed (most recent first)

<!-- Move items here when done, newest first, with a one-line note on what happened. -->
