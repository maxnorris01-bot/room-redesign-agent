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

**2026-10-18 - Spike skeleton decisions (Claude Code session 1).** Details in `docs/sessions/2026-10-18-spike-skeleton.md`; the reasoning worth keeping:

- *Images:* every photo goes through `app.images.prepare_image`: orientation applied, then the image is rebuilt from raw pixels (so EXIF/GPS/XMP/ICC cannot ride along), downscaled to a 1568 px long edge (configurable, `APP_IMAGE_MAX_EDGE`; image tokens scale with area, so this is also the cost lever), re-encoded as JPEG q85. Never upscaled. Tests and the mock spike use a generated image carrying fake GPS, so no real photo is needed anywhere.
- *Pricing:* one table, `src/app/pricing.py`, with source URL and date (checked 2026-10-18). Costs are always *estimates* from API-reported usage plus a configurable per-search estimate (`$0.01`, from "$10 per 1,000 searches"). Haiku 5.5's prompt-length tier is modeled; an unpriced model is costed at the highest known rate so the cap errs conservative and the table prints a warning.
- *Models:* defaults `claude-sonnet-5-5` (perception) and `claude-haiku-5-5` (retrieval), both confirmed on the models overview page. Config only; recorded per stage in trace and table.
- *Web search tool version:* default is the basic `web_search_20250305` (works on every model, results go straight into context) rather than the `_20260209`+ dynamic-filtering variants, because those run code execution under the hood and I could not verify Haiku 5.5 support or the token trade-off without a live call. `APP_WEB_SEARCH_TOOL` switches it so the two can be compared live.
- *Thinking effort:* `APP_EFFORT_PERCEPTION=medium`, `APP_EFFORT_RETRIEVAL=low` are guesses (thinking tokens bill as output). Explore live; they may be the biggest cost lever.
- *Perception schema (`schema_version` 1):* top-down plan in normalized coordinates (north = wall farthest from camera). Added a `furniture_gaps` list (categories the room visibly lacks) so retrieval slots have something to derive from. Living things are a separate list, never in `furniture`. Bounds (0..1 positions, confidence, positive sizes) are validated by pydantic *after* parsing; the JSON Schema sent to the API strips `maxItems`/`maxLength`/numeric bounds rather than pretending they work.
- *Slot derivation:* done in code, not by an LLM call, so the number of searching calls is bounded and inspectable. Order: furniture marked replaceable (`keepable: unlikely`), then gaps, then room-type defaults only if neither exists. Keepable items are never slots. Total budget is split equally across slots (spike heuristic). Slots run sequentially under one shared `Budget` (steps and dollars cap the whole run); parallelizing is a latency lever to test later (todo).
- *URL guard:* a candidate is kept only if its URL appeared in that call's search results (ignoring only a `#fragment` and trailing slash). The mock deliberately returns one unverified URL per slot so the guard visibly fires in every mock run.

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
- Template gaps: adversarial eval pattern. (Mock/live-forced `eval-standard` and `eval-nightly` targets are done in this repo as of 2026-10-18; not yet ported back to the template.)
- Unverified until a live call: whether Haiku 5.5 accepts structured output + `web_search_20250305` + `effort` together (check `capabilities.server_tools` via the Models API first), and how many thinking tokens each stage burns.
- `Config.max_cost_usd` (0.25) and `max_steps` (12) are still template guesses. They bound a whole spike run now; the proposed full-run target ($0.20) sits below the cap on purpose, but validate both against the exploratory run.


## See also

- `docs/sessions/*.md` - what changed and why, per work session.
- `README.md`'s Known Failures section (if present) - the detailed, evidence-quoted record of real
  bugs found and their status.
- `docs/adr/` - specific architectural decisions with fuller reasoning than fits here.
