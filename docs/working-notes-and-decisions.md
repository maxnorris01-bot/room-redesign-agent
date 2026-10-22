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

**2026-10-21 - Retrieval default is now two-stage shopping data (ADR 0005, proposed).** Why: web-search retrieval had the model read pages and report prices and URLs itself (listing pages, one invented price). Now a shopping provider returns structured results, a small model only picks ids, and a per-product lookup supplies the retailer URL, price, shipping, total and dimensions; the model never supplies a price, seller or URL. Decisions inside it: Serper searches (cheap) and SerpApi looks up (shared product ids, direct retailer URLs); a lookup that fails or yields a non-product URL degrades to the Google panel URL or no URL, never an invented one (`url_kind`); provider calls are costed from two USD settings that are assumptions and count toward the cost cap; slots and lookups run concurrently, so the critical path is search + rank + slowest lookup. `web_search` mode stays selectable for comparison. Cost watch: six lookups per run at the assumed $0.025 is about $0.15, which dwarfs the old search cost; check the real prices before reading anything into the mock table. Details: `docs/adr/0005-two-stage-shopping-retrieval.md`, `docs/sessions/2026-10-21-two-stage-shopping-retrieval.md`.

**2026-10-21 - Default-slot coverage counts decor and fixed items, with synonyms.** The last live room already had a rug and a floor lamp (as decor), yet the default slots would have proposed both. "Already covered" now also checks `art_and_decor` kinds and `fixed_items` categories, and folds synonyms (area rug = rug; floor lamp = lamp = torchiere). Side effect to remember: any lamp covers a "floor lamp" slot, by design.

**2026-10-21 - "coolpale" in style_descriptors came from the model, not our code.** Evidence: the string appears only in `result.json` of run 20261021T032130Z-4a360a (the parsed output); the trace holds no raw text. Between the model's JSON and `result.json` the data goes `json.loads` -> pydantic -> `model_dump` -> `json.dumps`, and no string operation in `src/` touches those fields (the only joins are downstream in the retrieval prompt or on our own strings). The v1 prompt contains neither "cool" nor "pale", so templating is not a source; image prep changes pixels, and schema stripping changes only the request schema. A test pins the passthrough. Not provable from that run's artifacts because the raw text was not saved; `result.json` now stores `perception_raw_text`. Mitigation: perception prompt v2 requires separate lowercase phrases and says never to run words together.

**2026-10-21 - What changed in the latency and retrieval hygiene session.** Retrieval slots now run concurrently (one thread per slot) behind a lock-protected `Budget`: the step cap stays exact, but the cost cap can overshoot by the calls already in flight, since they cannot be cancelled and still bill. A candidate must now be a product-page URL as well as present in the search results; listing, collection, category and search URLs are dropped with a reason. Perception prompt v2 asks for shorter output (notes of 12 words or fewer, at most 6 decor entries, at most 4 uncertainties, 3 to 5 separate lowercase style phrases, gaps only when clearly absent and not already covered); no `maxItems` was added, and the run records output sizes so we can see whether v2 is obeyed. CLI flags make perception experiments one-liners. Streaming perception now records time to first token. See `docs/sessions/2026-10-21-latency-and-retrieval-hygiene.md`.

**2026-10-21 - Latency limit: first 20 s, then "judge the wait by feel".** The owner first set a 20 s limit, then relaxed it: no hard limit for now; the owner judges the wait by feel on real runs. So this session reduced wall-clock time where cheap and safe and made timing measurable per stage, rather than chasing a number. ADR 0004 targets still wait until the owner has felt a real run.

**2026-10-21 - Live runs: perception v2, concurrent slots, shopping retrieval.** Perception v2 cut output tokens 30% (1,838 to 1,288) and time by about 4 s, inside the run-to-run noise (same v1 prompt: 17.3 s and 12.9 s). Concurrent slots worked (critical path 22.6 s against a 41.5 s sum). The web-search run still returned only listing pages and one apparently invented price, so retrieval moved to structured shopping data (ADR 0005: Serper search, Haiku picks ids, SerpApi lookup by product id). The first shopping run took 14.8 s with a real product page and a price and shipping from the lookup; real Anthropic cost about $0.025, provider calls on free tiers. The owner decided: keep the visitor-questions idea in the backlog and judge the wait by feel. Details: `docs/sessions/2026-10-21-live-runs-latency-and-shopping.md`, `docs/research/2026-10-21-shopping-data-sources.md`. ADR 0004 (targets) is drafted as proposed; the owner decides the numbers before the measured run.

**2026-10-20 - Exploratory live run (one room): $0.0762 and 35.9 s for perception plus 2 retrieval slots.** Searches were 52% of cost, perception output 63% of perception cost; perception was 48% of time. Perception was accurate on what is visible but plan geometry assumed a frontal view; retrieval returned listing-page URLs (not product pages) and one empty slot. No targets locked yet; ADR 0004 comes before the measured 3-room run, with a stage-allocation proposal in the session doc. Details: `docs/sessions/2026-10-20-exploratory-live-run.md`.

**2026-10-18 - Perception exploration (one photo): labelling works; plan-view and furniture removal do not yet.** Segmentation and Claude's own read label a room well; classical CV fails on white rooms; plan-view dimensions from one photo are unreliable (a corner shot breaks the "frontal, north = far wall" convention in schema v1); LaMa removal gave a recognisable but smeared empty room. Consequence: treat the canvas as a proposal the user confirms. ADR 0003 is NOT yet amended and photo-real is NOT in v1; both await the project owner. Schema v1 stays the baseline for the exploratory live run. Full write-up: `docs/research/2026-10-18-perception-exploration.md`.

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
