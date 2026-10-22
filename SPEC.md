# Room Redesign Agent - Spec

*Scoped 2026-10-18 in a Cowork chat. Decisions and reasoning: `docs/working-notes-and-decisions.md` and `docs/adr/`. This file is the contract for implementation sessions.*

## Product (v1 target)

A tab on the portfolio site where a visitor uploads a room photo and gives budget and style goals. The system:

1. **Perceives** the room: a multimodal call returns structured data (rough dimensions, walls, windows, doors, fixed/structural items, movable furniture, art, living things such as pets or people, style descriptors).
2. **Builds a blank 2D room canvas** from that structured data (top-down, editable later). Photo-real empty-room generation is a gated experiment, not v1 (ADR 0003).
3. **Asks the user for preferences** (style, must-keep items, priorities) before proposing. For now the keep-or-replace choice arrives as CLI flags (`--keep`, `--replace`), defaulting from perception's `keepable` flags; slots are requested replacements, gaps, suggested replacements, then accent pieces, up to 3 (ADR 0007, proposed). Each slot gets a per-item price ceiling (budget x item cap / (1 + shipping buffer)) instead of an equal share of the budget.
4. **Retrieves** real furniture options. Default: structured Google Shopping data (search, a model that only picks ids, then a per-product lookup for the direct retailer URL; ADR 0005, proposed). Claude's web search tool stays selectable. Prices and URLs always come from provider data, never from a model.
5. **Proposes** a furniture set with a running budget total: one structured model call picks from each slot's finalists; code builds every item from the real candidate data (the model never supplies a price, seller or URL; a slot may replace an existing item and the final item records which). Layout is not part of the proposal yet (it needs the canvas). ADR 0006, proposed.
6. **Self-checks** the proposal: budget (total and per-item cap), fit against room dimensions (code, coarse; unknown when a product states no size), style match (one text-only model call), and grounded links and prices (code, including a check that the store page does not contradict the search title on size or material). One automatic revision on failure, then the list is shown with any remaining failures flagged as warnings. Never hidden. Must-keep items wait for the preference questions.
7. **Outputs** a final furniture list with links, prices, shipping, a written rationale per item, the totals against the budget, the four check results and any warnings.

An evaluation harness of 10-15 room/constraint cases scores the pipeline after every prompt or logic change.

## Out of scope for v1

- Manual drag-and-drop redesign editor (v1.1, after the agent clears its targets).
- Photo-real empty-room or redesigned-room image generation (gated experiment, ADR 0003).
- Accounts, saved projects, payments, affiliate links. The portfolio is strictly non-commercial.
- Public live upload until the spike numbers, rate limits and a workspace spend limit exist (curated and recorded demo first; mock mode always available).

## Session 1: cost and latency spike (the first work session)

Goal: exercise the real perception and retrieval call pattern on real rooms and record cost and latency per stage, before any self-check loop or UI exists. This is the Claim Verification lesson (`Engineering_Standards.md`, "Validate performance/cost economics before building out scope").

In scope:
- `scripts/spike.py`: photo path in, structured perception out, then retrieval for a small set of furniture slots, printing a per-stage table (model, input/output tokens, searches, dollars, seconds) and writing a trace via `app.tracing`.
- Perception as one multimodal call with schema-constrained structured output, via `app.llm.complete`, prompt as a versioned file in `prompts/`.
- Retrieval has two modes, `APP_RETRIEVAL_MODE`. Configuration A, `web_search`: Claude's web search tool (see claim-verification-agent ADR 0003 for the tool-loop pattern); the first live run showed listing-page URLs and an invented price. Configuration B, `shopping` (now the default, ADR 0005, proposed): stage 1 searches a shopping provider (Serper by default, SerpApi selectable), stage 2 has a small model pick finalist ids from that data (no web search tool, ids validated), stage 3 looks each finalist up for its direct retailer URL, shipping, total and dimensions. Candidates carry `url_kind` (`retailer_product`, `google_panel` or `none`) and `price_source`; a URL is never invented. The two USD-per-call settings are assumptions until confirmed from the real dashboards.
- Mock mode (default) works end to end with no API key. Live mode only via an explicit `make` target or env override.
- Models are configuration, never hardcoded. Record the model used per stage so a cheaper tier for retrieval vs a stronger one for perception can be compared.
- Photos live in a gitignored `spike-photos/` folder. Strip EXIF (GPS) before any photo is used or committed anywhere. No photo is committed until its rights are decided.

Targets (proposal, not yet locked): perception plus retrieval for one room at or under $0.08 and 30 s. Full run at or under $0.20 mean and p95 at or under 60 s with streamed stage progress. Process: one exploratory room is run first and recorded, ungraded; targets are then locked in an ADR before the measured 3-room run (ADR 0004, drafted 2026-10-21 as proposed; the numbers there supersede this paragraph once accepted). Targets are never loosened after seeing a result.

Measured run (after targets lock): 3 rooms of different difficulty (simple, cluttered with a pet in frame, poor lighting), retrieval configurations compared on price accuracy, link validity, style and budget match, latency and cost.

Spike definition of done:
- `make lint`, `make typecheck`, `make test`, `make eval-fast` (mock) pass.
- Mock run verified with `ANTHROPIC_API_KEY` unset.
- Per-stage table from the exploratory live room recorded in a session doc.
- A written go / no-go / redesign recommendation against the proposed targets.

## Eval design (built after the spike, proposal)

- 12 versioned room and constraint cases: budget extremes, "keep the sofa" must-keeps, a pet or non-furniture in frame, and one impossible-constraints case whose correct behavior is to say so.
- Deterministic scoring first: total at or under budget, must-keeps respected, links resolve and price matches the page, no fixed item treated as movable, output matches schema.
- Mock runs are scored on structure only; live runs on quality. The report carries an `llm_mode` field and never blends the two.
- No LLM judge in v1. Style match is the one subjective criterion and gets a judge later, validated against labeled examples.
- Every eval tier has explicit mock-forced and live-forced Makefile targets (the template's `eval-standard` and `eval-nightly` do not yet).

## Explicit non-goals for the spike session

Do not build the self-check loop, the canvas UI, the site page, the Vercel proxy, the Fly deployment or the eval cases. If something looks tempting to add, note it in `docs/todo.md` instead.
