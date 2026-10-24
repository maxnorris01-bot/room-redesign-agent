# To-do - Room Redesign Agent

A running checklist, not a narrative. Check items off as they're done and add a short note (what
happened, any surprises) rather than deleting the line - that note is often more useful later than
the checkmark itself. Add new items as they come up; don't wait for a session's end. This is
*not* where decisions and reasoning go - that's `docs/working-notes-and-decisions.md`. This is just
"what's next."

## Up next (in priority order)

<!-- The most important items, in the order you'd actually tackle them. -->

- [ ] **Max: live test of Shop Mode stage 2** (`make shop-live`): search, rate a few results with comments ("too dark", "love the legs"), press Shop Again. Check: does Serper accept page 3, how many credits a Shop Again really uses (expect 2 to 6), what the profile chips say about real comments, whether the new set is really unseen and moves toward what was loved, and what a session costs. Record the numbers in a session doc.
- [ ] **Shop Mode stage 3: eval (next).** About 12 prompts scoring price compliance, category correctness, style match, and whether Shop Again moves results toward loved items and away from disliked ones. Own commit for cases and thresholds, per CLAUDE.md.
- [ ] **Shop Mode deployment as a portfolio site tab.** Needs the terms review below, a spend gate (searches and lookups per visitor), and a decision on where sessions live (the local JSON files are single-machine).
- [ ] **Terms review for Shop Mode** (ADR 0012): showing Google Shopping results and Google-hosted thumbnails publicly, provider terms, retailer image rights. Decide before any public deployment: local or recorded demo only, links without thumbnails, or a source with display rights.
- [ ] **Redesign Mode after Shop Mode works:** room photo, stripping furniture, photo-real render (ADR 0011). The 2D plan is dropped from the product (ADR 0012).
- [ ] Tune the Shop Mode guesses from live use: 24 results, 4 per seller, 12 lookups per session, 50 searches per session, the accessory word list, the 60-result rank pool.
- [ ] Shop Mode: tune from live use: 10 Shop Agains per session, 3 pages, the 20% price-cut limit, 12 profile phrases, 8 examples shown to the ranker.
- [ ] Shop Mode: sessions are local files; a Start over leaves the old file on disk (add a cleanup or a list of past sessions if that matters).
- [ ] Shop Mode: show the ranker's one-line reason on a card (it is stored as `rank_reason`) if it helps in live use; a "no results" state could offer to loosen the price.
- [ ] Shop Mode: make the rank prompt and the parse prompt measurable (a fixed request set and a score), as part of stage 3.

- [ ] Workspace spend limit and project-named API key in the Anthropic Console (`portfolio-projects` workspace), before any live run. Walk through one step at a time.
- [x] **Exploratory live run on one room (Max's call, ungraded):** put a photo in `spike-photos/`, then `make spike-live ARGS='spike-photos/<photo> --budget 800 --style "warm minimalist"'` (rough guess $0.05-0.15, unmeasured; the step/cost caps in `Config` will stop it). Record the per-stage table in a session doc. Check first that the models accept the tools: Models API `capabilities.server_tools.web_search.supported` for `claude-haiku-5-5`. Done 2026-10-20: $0.0762, 35.9 s; see `docs/sessions/2026-10-20-exploratory-live-run.md`.
- [x] **Exploratory live shopping run (Max's call) - done 2026-10-21 (one slot, `--finalists 1`, 14.8 s, about $0.025 Anthropic; see `docs/sessions/2026-10-21-live-runs-latency-and-shopping.md`).** Original note: set `SERPER_API_KEY` and `SERPAPI_API_KEY` in `.env`, then `make spike-live ARGS='spike-photos/room1.png --budget 800 --style "warm minimalist"'` (shopping mode is the default). Expect six SerpApi lookups per run (3 slots x 2 finalists); at the assumed $0.025 each that is about $0.15, so check the real cost, or try `--finalists 1`. Record the table in a session doc.
- [ ] Max: decide the two open questions from `docs/research/2026-10-18-perception-exploration.md` (amend ADR 0003 to a user-confirmed canvas; photo-real empty room in v1 or stay gated).
- [x] Cowork: research shopping API candidates (pricing, terms, coverage) for retrieval configuration B. Done 2026-10-21: `docs/research/2026-10-21-shopping-data-sources.md`.
- [ ] Lock cost and latency targets: ADR 0004 drafted 2026-10-21 (proposed, three open questions for the owner); accept it and write `thresholds.yaml` in its own commit before the measured 3-room run.
- [ ] Measured 3-room spike; go / no-go / redesign recommendation.

## Later

- [x] Photo-real spike built (2026-10-23, mock only): `make render`, `make render-live`; see `docs/sessions/2026-10-23-photo-real-spike.md` and ADR 0011 (proposed).
- [ ] Max: run the photo-real comparison live, one approach at a time (command lines are in the session doc), and judge the renders by eye. gpt needs `RENDER_OPENAI_MODEL` and `RENDER_OPENAI_USD_PER_IMAGE` set by you first; Decor8 stays a stub.
- [ ] Progressive display in the UI: list and plan first, the render swapped in when ready.
- [ ] Mask or segmentation options for the render (the box mask is a soft guide; try real segmentation).
- [ ] A render check in the eval harness (only after the spike shows which signals mean anything).
- [ ] Terms review for using retailer and Google-hosted product images as model inputs and in a public page.

- [x] Run report (2026-10-23): `make report`, `make reports`, `report.html` per run and `spike-output/index.html`; see `docs/sessions/2026-10-23-run-report.md`.
- [ ] The canvas UI should reuse the plan logic in `src/app/report.py` (`plan_shapes`: x west to east as a fraction of width, y north to south as a fraction of length) instead of redrawing it.
- [ ] Product thumbnails shown in a public page need a terms review (the report shows remote Google-hosted images in a local developer file only).
- [ ] The report is a developer tool and not the product UI; do not grow it into one.

- [x] Size parsing fix (2026-10-23): compact labeled sizes, category-aware axes for unlabeled groups, height rule, depth in the replacement note, style note join. Mock-verified only; see `docs/sessions/2026-10-23-size-parsing-fix.md`.
- [ ] Live check of the size fixes: re-run the room that showed the `80w 34d 30h` and `21"x79"` misreads and confirm the axes; tune the category table, the 2.5x three-number test and the 0.1 m height clearance.

- [ ] A product-page size source, only if title and slug hints turn out not to be enough (measure how often `size_source` is `none` and how often a hint is wrong in live runs).
- [ ] Stream slot results to the UI as they finish (each slot already completes independently).
- [ ] A hedged second lookup when the first is slow (start the fallback finalist's lookup after a delay instead of after a failure).
- [ ] Perception variance across runs: the same photo gave different room sizes, `keepable` flips and now `matches_id` may vary; measure with repeats and decide on tolerances or averaging.
- [ ] Measure picks that sit low in their tier band (does the cheapest third of a tier hide the best products, and does the seller cap or variety prompt help or hurt quality?).
- [ ] Live check of the image style check: is it better than the title check on real products, how often do thumbnails fail to fetch, how well does perception fill `matches_id`?
- [ ] Revisit the family groupings and the per-room-type maximum (living 8, bedroom 9, office 7, dining 8) against live runs.
- [ ] Spike to find a size source for the fit check: fit has been "unknown" in every live run (provider stores rarely state dimensions). Candidates: parse sizes from titles and URL slugs, read product pages, a different lookup source.
- [ ] Total and per-item budget modes, reusing the tier stats (p33, p67, median) so a ceiling is relative to the category instead of a flat number.
- [ ] UI: decor, and a minimalist-to-maximalist slider (maps to the item count and to decor slots).
- [ ] Gate the demo's item cap and lookup spend (a public run can cost up to 10 slots x about $0.026 plus fallbacks); decide limits before any public live upload.
- [ ] Live check of freeplay: do tier-filtered pools beat the old ceiling on a real sofa search, how often does the first lookup fail, and are the 3x-median guard and the 3-result minimum band sensible?
- [ ] Live check of over-budget options: does the rank stage keep accessories and off-category items out of the 8 cheapest over-ceiling results, and is 2 options per empty slot the right amount?
- [ ] Decide whether a Google-page-only finalist should also be dropped (the strict reading of "verified retailer URL"); watch how often lookups fall back in live runs.
- [ ] Consider a smarter empty-slot next step: suggest a budget that would fit the cheapest seen, or offer to relax the style.
- [ ] UI for keep or replace (the flags are a stand-in) and visitor preference notes (free text such as "must keep the oak desk", budget priorities).
- [ ] Tune the new untuned values from live runs: shipping buffer 0.10, mismatch tolerance, accent lists per room type, how often perception's `keepable` suggestion matches the visitor's choice.
- [ ] Measure a live multi-slot run (up to 3 slots, 2 finalists): cost at the assumed provider prices (about $0.05 per slot), the revision rate and the critical path.
- [ ] Fit against existing furniture, once the canvas exists (the self-check's fit is room-size only).
- [ ] Size fallback for the fit check (many products state no size): parse sizes from titles or read product pages. Deliberately excluded from ADR 0006 and 0007 (the mismatch check compares two titles' sizes only); decide with evidence of how often fit is "unknown".
- [ ] Image-based style check (the current one is text only).
- [ ] User preference questions (style, must-keeps, priorities) before the proposal; must-keep items are not yet checked.
- [ ] Tune the untuned self-check constants (item cap 0.6, fit 0.6 side / 12% footprint / 0.5 confidence) from live runs; (rugs and other flat goods are now exempt from the footprint rule).
- [ ] Measure the proposal and self-check live (latency, cost, how often the revision fires) and revisit ADR 0004's 35 s full-run guess before the measured run.
- [ ] Layout is not part of the proposal yet (needs the canvas).
<!-- Real, but not next. -->

- [x] Retrieval: require a product-page URL. Done 2026-10-21 (`retrieval.is_product_page_url`, URL-pattern rules). Still open: how prices get verified from product pages (fetch tool vs shopping API), and the guard is URL-only, so it cannot prove a page is a product page.
- [x] Trim perception output tokens: prompt v2 written 2026-10-21 (not yet measured live). Still to do: Max runs `--perception-only` on v1 and v2 and compares output tokens and seconds (flags: `--perception-prompt-version`, `--perception-effort`, `--perception-model`, `--image-long-edge`).
- [x] "coolpale": checked 2026-10-21; it came from the model (see working notes). v2 prompt forbids fused words.
- [x] Gap detection made conservative in prompt v2 (only clearly absent and not already covered; reason must name what is visible). Not verified live. A code-side guard (skip a gap whose category matches an owned item) was considered and left out: simple substring matching would also drop legitimate gaps.
- [x] Compare the Console usage with the run's $0.0762 estimate: Console showed $0.08 on 2026-10-20, so the cost code is validated. Re-check against the later runs' expected cumulative $0.25.
- [x] Concurrent retrieval slots with a thread-safe `Budget`: done 2026-10-21 (always concurrent, one thread per slot; no `--parallel` flag). Not yet measured live.
- [ ] Only if the owner finds the wait too long: ask the visitor setup questions (style, budget, auto-populate, room type) while perception runs in the background, so the wait overlaps with user input.
- [x] Serper provider spike (retrieval configuration B): Max's live tests informed ADR 0005; implemented 2026-10-21 as the default `shopping` mode (mock-verified only).
- [ ] Confirm the two provider cost assumptions from the real dashboards (Serper credits per shopping query and price per credit; SerpApi price per `google_product` lookup) and set `APP_SERPER_USD_PER_QUERY` / `APP_SERPAPI_USD_PER_SEARCH` from the plan. ADR 0005 stays `proposed` until then.
- [ ] Review the provider and Google terms of use before any public live upload (ADR 0005 consequence).
- [ ] Price history and stock fields come back from `google_product` but are unused for now; decide whether stock should gate a candidate.
- [ ] Fit-check candidates against the room using `dimensions_m` (needs the canvas data model and a clearance rule).
- [ ] Optional second lookup provider (a cheaper or free source for stores and dimensions), if lookup cost dominates in the live run.
- [ ] Retrieval prompt/rank: tune finalist count and ranking criteria once a live shopping run exists; compare with `web_search` mode on the same room.
- [ ] Time the proposal and self-check stages once they exist (the timing fields are generic: rows, ttft, critical path).
- [x] ADR 0004 drafted after real runs existed (2026-10-21); the owner still has to accept the numbers.
- [ ] Perception schema v2 candidates (image-space boxes, camera view type, per-item confidence, unknown positions, unseen regions): decide after the exploratory live run.
- [ ] Add one tape-measured room as an eval case to score plan-view dimension error.
- [ ] Spike follow-ups, only if the exploratory run says they matter: run retrieval slots in parallel (latency; `Budget` is not thread-safe yet), compare `web_search_20260209` dynamic filtering vs the basic tool, sweep thinking effort per stage, prompt caching for the shared prompt prefix, `response_inclusion: excluded` on `web_search_20260318`.
- [ ] CI: `eval-standard` / `eval-nightly` jobs now run the mock-forced targets; point the scheduled nightly job at `eval-nightly-live` only once there are cases and a spend limit.
- [ ] Port the mock/live-forced `eval-standard` / `eval-nightly` Makefile split back to `portfolio-project-template` and update its `docs/lessons-learned.md`.
- [ ] Eval cases (12), mock/live-aware scoring, thresholds set in their own commit.
- [ ] Self-check loop, canvas UI, site tab in `portfolio-site`, Vercel proxy, Fly deployment.
- [ ] Manual drag-and-drop editor (v1.1).
- [ ] Photo-real empty-room experiment (gated, ADR 0003).


- [ ] Print a visible warning when a shopping lookup fails (401, 402, 429 fall back to the Google panel URL and show only in `result.json` `summary.lookup_errors`).
- [ ] Budget allocation across slots belongs to the proposal stage; the equal split gave one slot the whole budget and the pick took 75% of it.
- [ ] Dimensions fallback when the lookup has none (parse sizes from titles or URL slugs, or read the product page); only 1 of 3 looked-up products had dimensions.
- [ ] Run several slots and `--finalists 2` live once in shopping mode, and `web_search` mode once after the change.
- [ ] Repeat perception runs per room to measure the run-to-run spread (dimensions, `keepable`, descriptors) before setting eval tolerances.

## Lower priority / opportunistic

<!-- Worth doing if the moment presents itself; not worth planning around. -->

- [ ] `schemas.to_output_schema` strips any key named `title` or `default` at every level; a future field with either name would vanish from the API schema (noted by the spike session).

## Completed (most recent first)

<!-- Move items here when done, newest first, with a one-line note on what happened. -->

- [x] 2026-10-24 Shop Mode stage 2 (ADR 0012 amended): preference profile (`shop_prefs.md` v1), Shop Again with exclusions, seller deprioritization and up to 3 pages (`shop_rank.md` v2), preference chips with delete, Saved panel, Start over, Serper credits in the cost, loading state and retry, 100-item fixtures. Mock-verified only; see `docs/sessions/2026-10-24-shop-mode-stage-2.md`.
- [x] 2026-10-24 Shop Mode stage 1 (ADR 0012, proposed): request parsing, Serper search with hard filters, dedupe, seller cap and an id-only ranker, lazy link resolve with a per-session cap and cache, JSON sessions with ratings and comments, a local page, `make shop`, `make shop-live`, `make shop-check`. Mock-verified only; see `docs/sessions/2026-10-24-shop-mode-stage-1.md`.
- [x] 2026-10-22 Pick quality (ADR 0009 and 0010, proposed): size hints from titles and slugs with confidence and plausibility ranges, partial-size fit and a replacement size note, matching-set warnings and `--replace-matching`, category families and item-count messaging, seller cap and rank v5, image-based style check v2 with a guarded thumbnail prefetch. Mock-verified only; see `docs/sessions/2026-10-22-pick-quality.md`.
- [x] 2026-10-22 Freeplay mode (ADR 0008, proposed): optional `--budget`, `--price-tier`, `--items` 2 to 10, accent lists to 10, price tiers from search stats, rank v4 and proposal v3, lookup economy with unverified alternates, shipping status fix. Mock-verified only; see `docs/sessions/2026-10-22-freeplay-mode.md`.
- [x] 2026-10-22 Over-budget options and link hygiene (ADR 0007 amended): price stats per slot, explained empty slots with up to 2 over-limit options (never picks), link-less finalists and bad alternates dropped, lookup failure reasons, media unit accent, shipping wording. Mock-verified only; see `docs/sessions/2026-10-22-over-budget-options-and-link-hygiene.md`.
- [x] 2026-10-21 Keep-or-replace and cleanup (ADR 0007, proposed): `--keep` / `--replace`, slot policy (requested replacements, gaps, suggested, accents; up to 3), per-item budget ceiling, proposal prompt v2, title/page mismatch check, flat-goods fit exemption, lookup failure warnings (401 exits 4). Mock-verified only; see `docs/sessions/2026-10-21-keep-or-replace-and-cleanup.md`.
- [x] 2026-10-21 Proposal and self-check (ADR 0006, proposed): one structured proposal call, four checks, one revision, separate Anthropic and provider cost caps, final list printed, `--no-proposal` / `--revisions` / `--proposal-model` / `--proposal-effort`. Mock-verified only; see `docs/sessions/2026-10-21-proposal-and-self-check.md`.
- [x] 2026-10-21 Two-stage shopping retrieval (ADR 0005, proposed): provider layer (Serper/SerpApi + SerpApi lookup), rank prompt, product lookup with dimensions, slot-coverage fix, per-call stage rows and critical path, fixtures, CLI flags. Mock-verified only; see `docs/sessions/2026-10-21-two-stage-shopping-retrieval.md`.
- [x] 2026-10-21 Latency and retrieval hygiene (concurrent slots, product-page URL guard, perception prompt v2, timing fields, CLI flags). Mock-verified only; see `docs/sessions/2026-10-21-latency-and-retrieval-hygiene.md`.
- [x] 2026-10-18 Claude Code session 1: repo setup (placeholders replaced, `CLAUDE.md` filled, mock/live-forced targets for every eval tier, `.env.example`) and the spike skeleton (`scripts/spike.py`, perception, retrieval config A, pricing table, mock mode, tests). No live calls were made; see `docs/sessions/2026-10-18-spike-skeleton.md`.
