# 0006. Proposal and self-check: one structured call, four checks, one revision

- **Status:** proposed
- **Date:** 2026-10-21

## Context

After retrieval (ADR 0005) the pipeline has, for each furniture slot, a few real finalists with
provider-sourced prices, shipping, links and sometimes dimensions. SPEC.md's last two stages turn that
into a final furniture list and check it. The owner scoped the design (below); this ADR records it and
what it can and cannot guarantee.

## Owner decisions (scoping answers)

- The proposal is **one model call** with structured output. Not rule-based, not an agent loop.
- The self-check verifies **four things**: budget total and per-item caps, fit against room
  dimensions, style match, grounded links and prices.
- On failure: **one automatic revision**, then show the list with the remaining failures flagged as
  warnings. A failed check is never hidden.
- Missing dimensions: **fit is unknown, and says so.** No guessed sizes; no parsing sizes from titles;
  no fetching product pages in this change.
- **The model never supplies prices, sellers or URLs.** Code attaches them from the candidate data.

## Decision

**Proposal** (`src/app/proposal.py`, `prompts/proposal.md` v1, `APP_MODEL_PROPOSAL` default
`claude-sonnet-5-5`, low effort). One call sees the room summary, the total budget and style, and each
slot's finalists (id, title, retailer, price, shipping, total, rating, link kind, stated size). It
returns per slot a pick id (empty to leave the slot empty), alternate ids, a rationale and a fit note,
plus a summary. Code validates every id against that slot's own finalists, drops invented ones, and
builds each item from the real `ProductCandidate`. A slot with no finalists is carried through with the
retrieval note, and if no slot has finalists the model is not called.

**Self-check** (`src/app/selfcheck.py`). Each check returns pass, fail or unknown with a message and the
item it concerns. The style call runs in a worker thread while the code checks run.

| Check | How | Catches | Cannot catch |
|---|---|---|---|
| budget | code: sum of totals (price plus shipping; unknown shipping counts as 0 and warns) within the budget; no item over `APP_ITEM_CAP_FRACTION` (0.6) of it unless it is the only item | an over-budget list, one item eating the budget | real shipping when it is unknown; tax; price changes after the crawl |
| fit | code: when width and depth are stated, fail if the longer side is over 60% of the room's shorter side or the footprint over 12% of the floor area; otherwise unknown ("no size found; check the product page") | an obviously too-big piece | everything else: it is coarse; it uses the room size at perception's stated confidence (a note is added below 0.5); it cannot see existing furniture (that needs the canvas); flat goods (rug, runner, mat, carpet) skip the footprint rule and report "unknown" |
| style | one Haiku call (`prompts/style_check.md` v1), text only: style goal, room style, each pick's title, retailer and category; "poor" fails, "ok" passes | a title that clearly contradicts the goal | anything visual: it cannot see images; "ok" is also its answer when the text says too little |
| grounded | code: the pick is among the finalists; `url_kind` is a retailer product page (a Google panel link warns, no link fails); `price_source` is search or lookup; the URL passes the product-page guard; the search title does not clearly contradict the store page (its title plus URL slug) on size or material | invented ids, listing pages, model-supplied prices, a link that lands on a different product | whether the page still shows that price or is in stock |

The three fit constants and the item cap are untuned guesses in `config.py`.

**Revision.** If any check fails (unknown is not a failure), the proposal model is called once more
(`APP_MAX_REVISIONS`, default 1, 0 disables) with the previous picks and the failed checks, its output
is validated the same way, and all four checks rerun. If the revision has more failures than the
original, the original is kept and the output says so. A revision that cannot be parsed keeps the
previous proposal with a warning. Whatever still fails is kept as a warning on the item (or on the
proposal for a whole-list failure such as the total), and the output records whether a revision
happened and which picks changed.

**Separate cost caps.** `APP_MAX_COST_USD` (0.25) now bounds Anthropic dollars only; the new
`APP_MAX_PROVIDER_COST_USD` (0.30) bounds estimated provider dollars. Both are enforced, and the table
and `result.json` show the two subtotals. Provider prices are assumptions and free-tier usage is $0 in
reality, so they should not eat the model budget (or the reverse). Under concurrency a cap can be
overshot by calls already in flight.

## Alternatives considered

1. **Rule-based allocation** (choose by price, rating and style keywords). Cheap and deterministic, but
   it cannot weigh "does this suit the room and the style" or trade slots against each other under a
   budget. The model call is one cheap call; the checks keep it honest.
2. **Agent loop with re-search** (let the proposer search again when nothing fits). More flexible, but
   unbounded cost and latency, the lesson of Claim Verification. One revision over fixed finalists is
   bounded; "nothing fits" is shown honestly as an empty slot.
3. **Parsing sizes from titles or fetching product pages** for the fit check. Would fill many unknowns,
   but titles are unreliable and page reads reopen the web-scraping problem ADR 0005 removed. Deferred;
   fit stays unknown instead of guessed.
4. **A vision style check** (send product images). Better signal, more cost and a new data path.
   Deferred.

## Amendments (2026-10-21, ADR 0007)

- **Title/page mismatch.** The lookup's own store title is kept (`store_title`). The grounded check
  fails with "store page title disagrees with search title (size or material)" when both texts state
  sizes (numbers with ", in, inch, cm, mm or W, cm and mm converted to inches, within about 1 inch;
  a bare 2 to 3 digit number in the URL slug counts as inches) and none match, or both name materials
  (oak, walnut, ... ) and the sets are disjoint ("oak" matches "smoked oak"). It is silent when either
  side says nothing. Sizes are used only to compare two titles, never for fit. Real example: search
  title `Article Oak Wood Media Unit 79"W ...` against slug `judith-63-low-media-unit-walnut` fails.
- **Flat goods** (rug, runner, mat, carpet, with synonyms) skip the fit rule: status unknown, "flat
  item; footprint rule not applied". This removes the rug false-fail noted below.
- The proposal prompt is v2 (v1 kept): a slot may replace an existing item, the budget is for the
  whole list, kept items are not duplicated.

## Consequences

- **Latency.** Two more model hops (proposal, then style check in parallel with code checks), and a
  third and fourth if a revision runs. `critical_path_seconds` is perception, the slowest retrieval
  slot, the proposal, the slower of style and code checks, then the revision and its re-check. The
  full-run target in ADR 0004 (35 s) was a guess made before these stages existed and may need revising
  before the measured run.
- **Fit will mostly say "unknown"** until sizes are available (1 of 3 looked-up products so far), and
  (before the 2026-10-21 amendment) falsely failed large flat items. Unknown fit is accepted for
  now and visible in the output.
- **Web-search retrieval mode** produces candidates whose price came from a model
  (`price_source: model`), so the grounded check fails them by design.
- **Steps.** A three-slot run with a revision uses 8 model requests (perception, 3 ranks, proposal,
  style, revision, style), inside `max_steps` 12.
- Everything here was verified in mock mode only. No live numbers yet: the proposal and style prompts,
  their latency and cost, and how often the revision fires are unmeasured.
- Revisit when the canvas exists (fit against existing furniture), when a size fallback exists, or if
  the live revision rate shows the first proposal is usually wrong.
