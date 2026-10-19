# Working notes and decisions log - Room Redesign Agent

This is a different kind of record from `docs/sessions/*.md`. Session docs narrate what changed in
one sitting and are written for someone with zero context picking up the diff. This file is the
opposite shape: short, durable entries - a decision plus *why*, or an open item worth remembering -
meant to be skimmed months later when the reasoning behind something has otherwise been forgotten.
Append to it whenever a real decision gets made, not just at the end of a session. If something is
already fully documented elsewhere (a technical fix in a session doc, a known failure in the
README, a design tradeoff in an ADR), point to it here rather than duplicating it.

## Decisions

<!--
One entry per decision, newest first. Format:

**YYYY-MM-DD - Short title.** What was decided, and the reasoning - not just the choice but why
this choice over the alternatives. Note what's still open or deferred within it, if anything.
-->

**2026-10-18 - Form factor: site tab backed by a Python service.** Modeled on the Satellite Point-of-View Tool: a new tab on `portfolio-site`, with the agent in this repo (from `portfolio-project-template`) and a thin Vercel function between them so keys and rate limits stay server-side. Proposed, to confirm after the spike. See ADR 0002.

**2026-10-18 - Blank canvas is 2D, from perception data.** Claude does not generate images, so a photo-real empty room means a second vendor. v1 renders a 2D top-down canvas from structured perception output; photo-real is a gated experiment. See ADR 0003.

**2026-10-18 - v1 sequencing: agent first, manual editor v1.1.** v1 = perception, canvas, preference questions, retrieval, proposal with running budget, self-check loop, final list with links and rationale, plus the 10-15 case eval harness. Drag-and-drop manual redesign follows once the agent clears its cost and latency targets. Avoids repeating Claim Verification's pattern of building scope before the economics were known.

**2026-10-18 - Cost and latency targets: explore one room, then lock.** Proposed targets (perception plus retrieval for one room at or under $0.08 and 30 s; full run at or under $0.20 mean and p95 at or under 60 s with streamed progress) are not locked. One exploratory live room is run and recorded ungraded; targets are then locked in an ADR before the measured 3-room run and never loosened afterward.

**2026-10-18 - Demo and retrieval deferred to spike results.** Demo plan: mock mode and recorded replays first; live public upload only after spike numbers, rate limits and a workspace spend limit exist. Retrieval: web search tool vs a shopping API, decided on measured price accuracy, link validity, latency and cost. The shopping API vendor is not chosen; Cowork researches candidates and terms first.

**2026-10-18 - Naming.** Tool: Room Redesign Agent. Repo: `room-redesign-agent`. Planned route `/room-redesign-agent`, single-source data file `src/data/roomRedesignTool.ts` in `portfolio-site`.

## Open items / parking lot

<!-- Things to revisit later - not urgent, not forgotten. -->

- Where the 12 eval photos and the curated demo rooms come from (own photos, CC-licensed, or both), and how faces, addresses and EXIF are scrubbed. Needed before cases are written, not before the spike.
- Shopping API vendor candidates, pricing and terms of use (non-commercial portfolio). Cowork to research before retrieval configuration B.
- Which model per stage (perception vs retrieval vs self-check); decided by the spike.
- Workspace spend limit and a project-named API key in the `portfolio-projects` Console workspace, before the first live run.
- Template gaps: mock/live-forced `eval-standard` and `eval-nightly` targets, adversarial eval pattern.


## See also

- `docs/sessions/*.md` - what changed and why, per work session.
- `README.md`'s Known Failures section (if present) - the detailed, evidence-quoted record of real
  bugs found and their status.
- `docs/adr/` - specific architectural decisions with fuller reasoning than fits here.
