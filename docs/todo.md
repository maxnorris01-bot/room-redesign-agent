# To-do - Room Redesign Agent

A running checklist, not a narrative. Check items off as they're done and add a short note (what
happened, any surprises) rather than deleting the line - that note is often more useful later than
the checkmark itself. Add new items as they come up; don't wait for a session's end. This is
*not* where decisions and reasoning go - that's `docs/working-notes-and-decisions.md`. This is just
"what's next."

## Up next (in priority order)

<!-- The most important items, in the order you'd actually tackle them. -->

- [ ] Workspace spend limit and project-named API key in the Anthropic Console (`portfolio-projects` workspace), before any live run. Walk through one step at a time.
- [x] **Exploratory live run on one room (Max's call, ungraded):** put a photo in `spike-photos/`, then `make spike-live ARGS='spike-photos/<photo> --budget 800 --style "warm minimalist"'` (rough guess $0.05-0.15, unmeasured; the step/cost caps in `Config` will stop it). Record the per-stage table in a session doc. Check first that the models accept the tools: Models API `capabilities.server_tools.web_search.supported` for `claude-haiku-5-5`. Done 2026-10-20: $0.0762, 35.9 s; see `docs/sessions/2026-10-20-exploratory-live-run.md`.
- [ ] Max: decide the two open questions from `docs/research/2026-10-18-perception-exploration.md` (amend ADR 0003 to a user-confirmed canvas; photo-real empty room in v1 or stay gated).
- [ ] Cowork: research shopping API candidates (pricing, terms, coverage) for retrieval configuration B.
- [ ] Lock cost and latency targets in ADR 0004 (after the exploratory run, before the measured 3-room run).
- [ ] Measured 3-room spike; go / no-go / redesign recommendation.

## Later

<!-- Real, but not next. -->

- [x] Retrieval: require a product-page URL. Done 2026-10-21 (`retrieval.is_product_page_url`, URL-pattern rules). Still open: how prices get verified from product pages (fetch tool vs shopping API), and the guard is URL-only, so it cannot prove a page is a product page.
- [x] Trim perception output tokens: prompt v2 written 2026-10-21 (not yet measured live). Still to do: Max runs `--perception-only` on v1 and v2 and compares output tokens and seconds (flags: `--perception-prompt-version`, `--perception-effort`, `--perception-model`, `--image-long-edge`).
- [x] "coolpale": checked 2026-10-21; it came from the model (see working notes). v2 prompt forbids fused words.
- [x] Gap detection made conservative in prompt v2 (only clearly absent and not already covered; reason must name what is visible). Not verified live. A code-side guard (skip a gap whose category matches an owned item) was considered and left out: simple substring matching would also drop legitimate gaps.
- [ ] Compare the Console usage for the `room-redesign-agent` key with the run's $0.0762 estimate to validate the cost code.
- [x] Concurrent retrieval slots with a thread-safe `Budget`: done 2026-10-21 (always concurrent, one thread per slot; no `--parallel` flag). Not yet measured live.
- [ ] Only if the owner finds the wait too long: ask the visitor setup questions (style, budget, auto-populate, room type) while perception runs in the background, so the wait overlaps with user input.
- [ ] Serper provider spike (retrieval configuration B), alongside the shopping-API research.
- [ ] Time the proposal and self-check stages once they exist (the timing fields are generic: rows, ttft, critical path).
- [ ] ADR 0004 targets, only after the owner has felt a real run (the 20 s limit was relaxed to "judge by feel"; see working notes).
- [ ] Perception schema v2 candidates (image-space boxes, camera view type, per-item confidence, unknown positions, unseen regions): decide after the exploratory live run.
- [ ] Add one tape-measured room as an eval case to score plan-view dimension error.
- [ ] Spike follow-ups, only if the exploratory run says they matter: run retrieval slots in parallel (latency; `Budget` is not thread-safe yet), compare `web_search_20260209` dynamic filtering vs the basic tool, sweep thinking effort per stage, prompt caching for the shared prompt prefix, `response_inclusion: excluded` on `web_search_20260318`.
- [ ] CI: `eval-standard` / `eval-nightly` jobs now run the mock-forced targets; point the scheduled nightly job at `eval-nightly-live` only once there are cases and a spend limit.
- [ ] Port the mock/live-forced `eval-standard` / `eval-nightly` Makefile split back to `portfolio-project-template` and update its `docs/lessons-learned.md`.
- [ ] Eval cases (12), mock/live-aware scoring, thresholds set in their own commit.
- [ ] Self-check loop, canvas UI, site tab in `portfolio-site`, Vercel proxy, Fly deployment.
- [ ] Manual drag-and-drop editor (v1.1).
- [ ] Photo-real empty-room experiment (gated, ADR 0003).


## Lower priority / opportunistic

<!-- Worth doing if the moment presents itself; not worth planning around. -->

- [ ] `schemas.to_output_schema` strips any key named `title` or `default` at every level; a future field with either name would vanish from the API schema (noted by the spike session).

## Completed (most recent first)

<!-- Move items here when done, newest first, with a one-line note on what happened. -->

- [x] 2026-10-21 Latency and retrieval hygiene (concurrent slots, product-page URL guard, perception prompt v2, timing fields, CLI flags). Mock-verified only; see `docs/sessions/2026-10-21-latency-and-retrieval-hygiene.md`.
- [x] 2026-10-18 Claude Code session 1: repo setup (placeholders replaced, `CLAUDE.md` filled, mock/live-forced targets for every eval tier, `.env.example`) and the spike skeleton (`scripts/spike.py`, perception, retrieval config A, pricing table, mock mode, tests). No live calls were made; see `docs/sessions/2026-10-18-spike-skeleton.md`.
