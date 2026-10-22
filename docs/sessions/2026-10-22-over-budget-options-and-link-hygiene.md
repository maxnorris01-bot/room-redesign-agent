# Session 2026-10-22: over-budget options and link hygiene

Branch `over-budget-options-and-link-hygiene`, from `main` at PR #7 (396e172). Mock mode only: no live
call, no `make spike-live`, `.env` not read; tests use obviously fake key strings.

## Why

The last live run (`--budget 800 --replace sofa --keep desk`) ended with the sofa slot empty because
all 25 results were over the $436.36 per-item ceiling. The output said "25 over budget" with no prices
and no next step, so the tool looked broken. The same run showed an alternate with no retailer URL and
no lookup data, and a total line that treated unknown shipping as $0.

## Part A: explain empty or ceiling-limited slots

- **Price stats** (`shopping_retrieval.price_stats`): considered, priced, unpriced, over_ceiling, min,
  median, max, from the parsed numeric prices of the top 25 (deduplicated). In the slot summary, the
  trace (`stage.retrieval.per_slot`) and `result.json` `slot_plan.price_stats`.
- **Over-budget options** (`OverBudgetOption`, `SlotResult.over_budget_options`):
  - no result at or under the ceiling: the 8 cheapest priced results over it go through the rank stage
    (so off-category junk is filtered), the top 2 are looked up, options need a verified retailer URL;
  - every finalist dropped by the post-lookup total cap: the dropped finalists (already looked up)
    become options directly, cheapest 2;
  - a filled slot gets none. Each option has a reason (`over_item_limit` or `over_total_cap`) and
    `also_over_total_budget`.
- **Rank prompt v3** (v2 kept as `rank.v2.md`, `--rank-prompt-version`, `Config.rank_prompt_version`):
  adds that when the request says every candidate is above the ceiling, the "never above it" rule does
  not apply and the closest matches should be picked.
- **Never picks.** Options are not proposal candidates, not in totals, not budget-checked, not seen by
  the model. The proposal gets "Finalists: none (empty slot: ...; do not recommend or mention any
  product for it)" as data, so no prompt bump was needed for that.
- **Rendering by code** (`spike._empty_slot_lines`): header with the ceiling, `Replaces:` when it
  replaces something, "Cheapest <slot> seen: ... Raise the budget or loosen the style to fit one.",
  "Over your limit, not counted in the total:" with name, price, shipping or "shipping unknown", URL,
  reason and "also over your total budget". No surviving option: stats line only. A filled slot prints
  nothing extra.
- **Cost and concurrency**: each such slot adds at most one Haiku rank call and two lookups, counted in
  both caps and shown as `(over limit)` rows; the work stays inside the slot's own thread, so slots
  are still concurrent and the critical path still takes the slowest slot chain.

## Part B: link hygiene

- A finalist with `url_kind: none` is dropped (`reason: no_link` in the slot's dropped list and trace).
  If that empties a slot the note and a WARNING say "lookup returned no store for <title>".
- Alternates face the same grounded rules as picks (`selfcheck.grounded_problems`); failing alternates
  are dropped with `alternate_failed_grounded: ...`.
- Empty lookups record why: `http error <status>`, `no product_results`, `no stores`, or `store url is
  not a product page` (`ProductDetails.note`, slot `summary.lookup_issues`, the provider span detail,
  and a run warning except for http errors, which the existing "N lookup(s) failed" warning covers).

## Part C and D

- "media unit" is the sixth living-room accent (after bookshelf); synonyms add "entertainment center".
  Accents are also skipped when a planned slot covers them (a media-unit gap), besides kept furniture,
  fixed items and decor. The cap of 3 and requested-replacements-first are unchanged.
- The final list's total line says "shipping known for K of N items, unknown excluded" when any item's
  shipping is unknown; `result.json` totals gained `item_count`, `shipping_known_count` and
  `shipping_note`. The budget check logic is unchanged.

## Mock scenario

Fixture search results for queries starting with "sofa" are the real Serper/SerpApi fixture results
with prices multiplied by 8 and ids prefixed `x`, so every one is over the ceiling; only the cheapest
scaled id has lookup data (the Article lookup fixture, scaled). The default `make spike` therefore
shows an empty sofa slot with one surviving option (also over the whole budget) and a warning for the
option whose lookup found nothing. Committed fixtures are unchanged.

## Tests

310 passed (274 before). New `tests/test_over_budget_options.py` (36): price stats (unpriced, duplicates,
top 25, empty), option selection (two options, a failed lookup dropped, no survivor, Google-page fallback
not enough, nothing priced over the ceiling), total-cap drops becoming options without extra lookups,
a filled slot without options, exclusion from the proposal text, totals and checks, rendering for
replace_requested and gap slots, option lines, the mock scenario, over-limit rows and cost, rank prompt
v3/v2 and the flag, link-less finalists, alternates, lookup failure reasons with no key in the trace,
media unit ordering and skip rules, and the shipping wording. Existing tests were updated for the new
default mock run (no revision, sofa slot over the limit), rank prompt v3, and dropped link-less finalists.
Ran the suite three times. `make lint`, `make typecheck`, `make test`, mock `make eval-fast` pass;
thresholds untouched.

## Worth knowing

- **Strict reading of "verified retailer URL".** I dropped `url_kind: none` (the rule's explicit case)
  and kept Google-page-only finalists (flagged by the grounded check), so a quota error on lookups does
  not empty every slot. Dropping those too is a one-line change if you want it.
- **A pre-existing bug fixed:** a single picked item lost its "shipping unknown" warning in the budget
  check (an earlier edit never applied). Found by a new test.
- The default mock run no longer revises (its two picks fit the budget); the revision paths are
  covered by scripted tests and a $600 budget test.
- Option prices and shipping come from the lookup, so a search price over the ceiling can turn out
  lower after lookup; the option keeps its reason from how it was found.
- Nothing was measured live: whether the rank stage keeps junk out of the cheapest over-ceiling
  results, and whether 2 options is the right number, are open.

## Next

```bash
make spike
make spike ARGS='spike-photos/room1.png --budget 800 --style "warm minimalist" --replace sofa --keep desk'
```
Then, Max's call with keys in `.env`: the same live run that showed the empty sofa slot.
