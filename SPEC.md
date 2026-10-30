# Room Redesign Agent - Spec

*Scoped 2026-10-18 in a Cowork chat. Decisions and reasoning: `docs/working-notes-and-decisions.md` and `docs/adr/`. This file is the contract for implementation sessions.*

## Modes (ADR 0012, proposed)

The product has two modes. **Shop Mode** comes first; **Redesign Mode** follows.

| | Shop Mode | Redesign Mode |
|---|---|---|
| Input | A text request ("modern couches under $1000") | A room photo, preferences |
| Output | A browsable grid of real products; the visitor rates each love, like or dislike with an optional comment | A furniture list and a photo-real render of the picks in the room |
| Status | Stages 1 and 2 shipped (below) | Not started as a mode; the pipeline in "Product" below exists and is kept |

**Shipped in Shop Mode stage 1 (ADR 0012):** request parsing (one small-model call into a typed query: search text,
category, price range, style, material, must-have and avoid terms); Serper shopping search (page 2 only
when fewer than 24 results survive); hard filters for price and category, dedupe, at most 4 results per
seller; an id-only ranking against the soft preferences; the top 24 with size hints; lazy retailer-link
lookups (only on Open, at most 12 per session, never twice); JSON sessions under `shop-data/`
holding every search, every result shown, every rating and comment, and every resolved link; a local web
page (127.0.0.1 only).

**Shipped in stage 2:** Shop Again (one small-model call builds a preference profile from the ratings and
comments, shown as chips the visitor can delete; a fresh set of up to 24 results that excludes everything
already seen, ranked with the profile, from up to 3 Serper pages, 10 per session); a Saved panel (loved and
liked results from every search, re-ratable, remembered open or closed); Start over; Serper cost counted
in credits; a loading state and a retry on errors.

**Shipped in stage 2b:** Shop Again learns from the rated items themselves (titles, prices, sellers and, when they can be fetched, thumbnails), not only from comments: inferred preference chips with evidence tooltips, a seller affinity and a price band worked out in code, parser-guessed styles shown as guesses, up to three search phrasings in a wider pool (up to 4 Serper calls), a ranker that sees the profile, a summary line, an alignment diagnostic, and lookups only on Open.

**Shipped in stage 2c:** the model describes, code decides. The per-item tags come from the model; whether a shape, material, finish, seller or price is a preference is decided in code from all the ratings, against what was shown (so liking round and rectangular makes shape *open*, not round, and a mostly-round batch does not teach "likes round"). Open and mixed-price chips exert no pressure; the request chips never change; search phrasings come only from converged values (one per value of an open dimension); the final 24 are held to a 60% single-value cap on guarded dimensions and a 50% seller cap (80% when pinned). Every threshold is in `src/app/shop/thresholds.py` and the table in ADR 0012.

**Shipped in stage 2d:** the preference decisions are honest about what was read. The preference call sizes its own token budget and retries once in two parts; with no item details only seller and price are learned (and the page says so); a dimension converges only when known values cover at least 70% of the like mass; a price band must hold 80% of the liked mass or price is mixed; the diversity guard covers shape and open dimensions (60%), a converged value is capped at 80%, and a dimension the typed request names is exempt. A "How I decided" panel shows the numbers behind each decision. The owner's real session is an offline replay fixture. Thresholds: ADR 0012.

**Shipped in stage 2e:** the model tags what was shown, and the pool is searched for what the shopper likes but the pool lacks. One small call tags every shown result and every candidate (titles, price and seller only; cached by result id in the session) from fixed vocabularies, and those tags are the baseline for the lift test, the guards and the batch report; the title keyword reader is only the fallback if that call fails (then no attribute converges). Shape converges at 80% of the like mass, material, finish, frame color and style at 60%, known coverage 60% everywhere; synonyms fold into one value. A seller chip needs 2 items and 40% of the like mass. When an open dimension's value is under 40% of the pool, targeted searches fetch it (up to 6 Serper calls, `APP_SHOP_AGAIN_MAX_SERPER_CALLS_OPEN`), and when shape is open the 24 slots are allocated per value by like mass with a 25% floor for a loved value. "How I decided" also shows the pool and batch makeup. Size class and legs no longer decide anything. Thresholds: ADR 0012.

**Shipped in stage 2f:** candidate tagging is diagnosable and tolerant (a per-call log, a reader that accepts fenced, prefaced, reordered and short answers, a retry in halves, the real reason on the page). A seller needs liked mass of at least 3 times its dislikes instead of no dislike. Retailers are a signal: a table of tiers and peers (`src/app/shop/retailers.py`, owner-editable judgment) gives a "retailers like West Elm" chip for design tiers, up to 3 peer retailer searches per Shop Again that rotate across the session, a 9-call total cap, and up to half a batch for the affinity tier. Style is decided on two families (modern-leaning, traditional-leaning) from up to two labels per item. "Matches your preferences" counts every decided signal and says n/a when nothing is decided. Thresholds: ADR 0012.

**Shipped in stage 2g:** a Shop Again is bounded and faster. Candidate tagging is a forced tool call with a strict schema (batches of 40 run concurrently, 12 seconds a call, 15 seconds in all, one retry, no cascade), only the top 60 candidates by rank are tagged by the model, searches and the preference call run concurrently, and a latency budget aims for 30 seconds and stops at 45. A search or a Shop Again is a background job the page polls for its phase. One de-duplication (id, thumbnail, title and price, title and seller, with one seller canonicalization) covers typed searches, candidates, what was shown or rated, and the final batch; a short batch says why and tries one refill search; the banner is one sentence with a details link; `make shop-diagnose` tests the tagging call live. Budgets: ADR 0012.

**Later:** an eval of about 12 prompts (stage 3), deployment as a portfolio-site tab, then Redesign Mode. The **2D room plan is dropped from the product**
(ADR 0012); step 2 of "Product" below is superseded, and the plan code survives only in the developer run
report.

## Product (v1 target)

*This section describes the room pipeline, which becomes Redesign Mode. See "Modes" above for what is shipped and what is later.*

A tab on the portfolio site where a visitor uploads a room photo and gives budget and style goals. The system:

1. **Perceives** the room: a multimodal call returns structured data (rough dimensions, walls, windows, doors, fixed/structural items, movable furniture, art, living things such as pets or people, style descriptors).
2. ~~**Builds a blank 2D room canvas** from that structured data~~ *Superseded: the 2D plan is dropped from the product (ADR 0012); the photo-real render is the main output (ADR 0011).*
3. **Asks the user for preferences** (style, must-keep items, priorities) before proposing. For now the keep-or-replace choice arrives as CLI flags (`--keep`, `--replace`), defaulting from perception's `keepable` flags; slots are requested replacements, gaps, suggested replacements, then accent pieces, up to 3 (ADR 0007, proposed). With `--budget` each slot gets a per-item price ceiling (budget x item cap / (1 + shipping buffer)) instead of an equal share of the budget. Without `--budget` (freeplay, ADR 0008) there is no ceiling: each slot is limited to a coarse price tier (value, mid or premium: thirds of that slot's own search prices), and `--items` sets how many pieces to shop for (2 to 10; 5 by default). A slot with nothing in budget is shown as empty with what was seen (cheapest, median, count) and up to two real over-limit options with verified retailer links, clearly outside the total and never picks.
4. **Retrieves** real furniture options. Default: structured Google Shopping data (search, a model that only picks ids, then a per-product lookup for the direct retailer URL; ADR 0005, proposed). Claude's web search tool stays selectable. Prices and URLs always come from provider data, never from a model.
5. **Proposes** a furniture set (with a running budget total in budget mode, none in freeplay): one structured model call picks from each slot's finalists; code builds every item from the real candidate data (the model never supplies a price, seller or URL; a slot may replace an existing item and the final item records which; a kept item that is part of a set with a replaced one is flagged, and `--replace-matching` replaces it too). Layout is not part of the proposal yet (it needs the canvas). ADR 0006, proposed.
6. **Self-checks** the proposal: budget (total and per-item cap), fit against room dimensions (code, coarse; sizes from the lookup, titles or the URL slug with a confidence, unknown when none is found; ADR 0009), style match (one model call that sees the room photo and product thumbnails and reports clashes with furniture that stays; ADR 0010), and grounded links and prices (code, including a check that the store page does not contradict the search title on size or material). One automatic revision on failure, then the list is shown with any remaining failures flagged as warnings. Never hidden. Must-keep items wait for the preference questions.
7. **Outputs** a final furniture list with links, prices, shipping (never shown as $0 when unknown), a written rationale per item, the totals against the budget, the four check results and any warnings.

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

## Viewing a run (developer report)

Every `scripts/spike.py` run writes `report.html` (and `room.jpg`, the prepared room image) into its
`spike-output/<run id>/` folder; `--no-report` turns that off, and a report problem only prints a
warning. `make report` rebuilds the latest run, `make report RUN=spike-output/<run id>` a chosen one,
and `make reports` every run plus `spike-output/index.html`. The commands print a file path and the
macOS `open <path>` command; nothing opens by itself. The page is static HTML and CSS (no script, no
external fonts), escapes every provider string, and loads product thumbnails from their hosts. It is
a developer tool, not the product UI.

## Shop Mode (developer commands)

`make shop` starts the page in mock mode (fixture results, no network, no key) on
`http://127.0.0.1:8765/`; stop it with Ctrl-C. `make shop-live` does the same with real Anthropic,
Serper and SerpApi calls and prints the expected cost first: a search is 1 or 2 Serper calls (2 credits
each, about $0.001 a credit) plus about $0.01 or less of Anthropic; a Shop Again is up to 4 Serper calls
(8 credits) plus a preference call (which also reads up to 16 small product photos) and a rank call; a SerpApi lookup runs only on Open (loving a result looks nothing up), at most 12 per
session. Sessions are saved
in `shop-data/` (gitignored). `make shop-check` drives the page in a headless browser in mock mode and
writes screenshots to `spike-output/shop-screenshots/`. The server listens on 127.0.0.1 only and
sends a strict Content Security Policy.

Stage 2h: while you browse, each stored batch is tagged in the background from titles and product photos
and each comment is read as it is written, so a Shop Again reuses them (it waits at most
`APP_SHOP_PREP_WAIT_S`, 8 s by default, for what is still running). The cost appears in the footer as
"background preparation". A slow action shows a step bar (Finishing your last ratings, Searching, Tagging,
Ranking) instead of a spinner; `APP_SHOP_MOCK_DELAY` slows the mock for screenshots. See ADR 0012, stage 2h.

Stage 2i: Shop Again tailors from like rates. A seller, retailer tier or soft-dimension value that was
liked at least 1.5 times as often as everything else (and at least 35%, from 4 rated and 2 liked) earns a
chip or a "leaning"; marketplace and budget retailers are left out when they were rated at least 8 times and
almost never liked (a deletable chip, with a banner under 12 results); liked sellers are searched by name,
the model writes one or two validated queries, and the base query is paged no deeper than page 2 (page 3 as
a last resort, never page 4). See ADR 0012, stage 2i.

Stages 2j to 2m: a new search for a different product starts a new topic (what was learned stops shaping it;
Saved stays). The request is split into product words and setting words ("pair well with a dark blue room"
suggests cream and rust, it does not search dark blue), with a colour dimension. A "Refine the next Shop
Again" box turns plain instructions ("no Walmart", "focus on cream", "under $400") into chips that outrank
what was inferred. Cards show star ratings and review counts where the source gave them, and a "More photos"
gallery shares the product lookup (and the cap of 12) with Open. See ADR 0012, stages 2j to 2m.

Stage 2n: the gallery reads the product lookup's `thumbnails` field first; the same product listed by several vendors
is shown once with an "Also at" line (image hash and title similarity); a comment that reports a duplicate, a broken
image or a wrong item is not taste and teaches nothing. See ADR 0012, stage 2n.

Stage 2o: "More photos" is off unless `APP_SHOP_GALLERY=1` (its lookup takes seconds; no faster source exists). Open opens
its tab at once with a loading message and always ends on the store page or Google's product page, never a blank
tab; loved and liked items have their store link looked up in the background (8 of the 12 lookups at most). See
ADR 0012, stage 2o.

Stage 3: a Shop Mode eval under `evals/shop/` (12 requests, hidden personas, code scorers, two Haiku judges,
thresholds in `thresholds.json` with their hash recorded in every result). `make eval-shop-plan` and
`make eval-shop-mock` are free; `make eval-shop-live` needs `EVAL_SHOP_CONFIRM=yes`. See ADR 0012, stage 3.

Stage 2p (fixes from the first live Shop eval): singular and plural are one category key and desk chairs are not desks; the
per-seller cap follows the batch size; every Shop Again asks the query writer for two new queries and says plainly when it ran
out of results; a typed colour, material or finish is a 70% quota backed by photo tags of the top candidates; a written refine
note ("fluted legs", "no leather") has a strength and is matched by description. See ADR 0012, stage 2p.

Stage 2q (fixes from the post-2p live re-run): the photo tagging budget starts at the first call and the first wave always
starts; a search variant keeps every hard word of the request (category, typed style, type, size, colour, material) and adds no
style, material or colour word, checked in code; every shown item records which search found it; a short batch raises the
per-seller cap to 5, 6, 7 before shrinking, never past 30% of the batch shown; a furniture listing priced far below the pool is
left out. See ADR 0012, stage 2q.

Stage 2r (fixes from the first full 12-case run): a batch short of 24 after every filter, cap and guard runs variant searches until it is full, a
call cap is reached or two searches in a row add nothing, and says why; a Shop Again stops its optional searches when the unseen pool is plentiful
and the quotas can be met; a diversity guard is off when the pool has no alternative value; a typed shape is a quota; a tagging call is never given
less than 8 s; the eval can record and replay Serper responses and lint a saved results file. See ADR 0012, stage 2r.

Stage 2s (seller affinity that checks its own bet): a learned seller gets one allowance (chip, named search and pin alike) that starts at 4 slots of 24, grows by 2 only when
its boosted items were liked, and is revoked at once when at least 3 of them are rated and mostly disliked; the seller can return after 8 further ratings. See ADR 0012, stage 2s.

## Deployment (Shop Mode, stage D1, ADR 0013)
Shop Mode can run as an invite-only JSON API (`python -m app.shop.serve`, `APP_DEPLOY_MODE=1`) behind the portfolio site's `/api/shop/*` rewrite. Contract: `docs/api-contract.md`; runbook: `docs/deploy.md`.
Local use is unchanged. Caps ($0.50 session, $1.00 invite/day, $3.00 day, 8 searches, 25 Shop Agains) are proposed defaults, not locked. Out of scope: the React page, accounts, payments, Redesign Mode hosting.
