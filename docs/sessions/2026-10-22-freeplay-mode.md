# Session 2026-10-22: freeplay mode

Branch `freeplay-mode`, from `main` at PR #8. Mock mode only: no live call, no `make spike-live`,
`.env` not read; tests use obviously fake key strings.

## Why

A flat per-item ceiling of 436.36 left one cheap sofa (199.99) when the median sofa in the search was
1519.20. Budget logic is the weak part, so a no-budget mode ships first; budget modes return later and
reuse the pieces built here.

## Part A: mode and CLI

- `--budget` is optional; none means freeplay. `--price-tier {value,mid,premium}` (default mid) is
  freeplay only: with `--budget` it exits 2. `--items N` (2 to `APP_MAX_ITEMS`) exits 2 outside the
  range; defaults 5 (freeplay) and 3 (budget). `--slots` stays as a legacy alias with no range check.
- Config: `price_tier`, `items`, `default_items_freeplay`, `max_items`, `lookups_per_slot`,
  `lookup_alternates`, per-mode prompt versions; env `APP_PRICE_TIER`, `APP_DEFAULT_ITEMS_FREEPLAY`,
  `APP_MAX_ITEMS`, `APP_LOOKUPS_PER_SLOT` (all in `.env.example`).
- `result.json`: `mode`, `price_tier`, `items`, `budget_usd` null in freeplay; the run header says
  `freeplay tier=mid items=5` or `budget=$800 items=3`.
- `make spike` with no `ARGS` is now a freeplay mock run (it was a budget run).

## Part B: slot list scaling

Accent lists reach 10 per room type (living, bedroom, office, dining, generic), keeping existing entries
and order; synonyms were added in `categories.py` (ottoman, console table, bed frame, dresser, wardrobe,
desk, filing cabinet, sideboard, cabinet, bar cart, bench). A shortfall prints a note and never pads. One
deviation: office "desk lamp" is now "task lamp", because the substring coverage rule counted "desk lamp"
as covered by "desk" and could never choose it.

## Part C: price tiers

`price_stats` gained p33 and p67. `tier_pool` filters the rank pool in code (value, mid, premium with a
3x-median outlier guard, widening below 3 results, and a last-resort "all" for tiny pools), records the
band in the slot summary, `slot_plan.tier_bands` and the trace. Rank prompt v4 and proposal prompt v3 (no
budget wording; a CLI version flag overrides both modes). Self-check: budget checks report "n/a" with "no
budget set" and never fail; revisions trigger only on fails. Over-budget options are not used. Freeplay
totals have no budget or remaining.

## Part D: lookup economy

One lookup per slot in freeplay (`APP_LOOKUPS_PER_SLOT`), with a second attempt if the first gives no
retailer page; each attempt is a table row and counts in the cap. Other finalists become unverified
alternates (`url_kind: google_panel`, search price, Google link) shown as "alternate (retailer link on
request)"; links-less ones are dropped; the pick must be a retailer page and unverified alternates never
reach the proposal as candidates. `lookup_alternate()` does one on demand; `--lookup-alternates` does them
eagerly. Freeplay scales the step cap (items + 6) and the provider-dollar cap ($0.06 per slot) with the
item count, never below the configured values, because 10 slots exceed the defaults (untuned guesses).

## Part E: the shipping count

In the live result, the sofa and media unit picks had `shipping: null` and `total` equal to `price`, the
side table had 19.0, yet `shipping_known_count` was 3. In the code, `item_shipping` returned `max(total -
price, 0)` whenever a total existed, so a provider total equal to the price (what it reports when it has
no shipping data) became a "known" shipping of 0. In the lookup parsing, `null` meant only that
`shipping_extracted` was absent from the store entry: unknown, not free; there was no representation of
free at all. (The fixtures contain only stores with a stated amount.) Fix: `shipping_status` (`known`,
`free`, `unknown`) on stores and candidates. `parse_shipping` treats a stated amount (0 is free), a
"shipping" text containing "free", and a total above the price as known, and everything else as unknown;
`item_shipping` no longer derives from a total unless the status is not unknown; totals count only known
or free items; the final list prints "free shipping" or "shipping unknown". The regression test uses the
live values (null, null, 19.0): 1 of 3 known.

## Tests

367 passed (310 before). New `tests/test_freeplay.py` (57): percentiles and tier bands (tiny, empty,
widening, outlier guard), the rank pool filtered in code, CLI bounds and mode exits, slot lists reaching
10 for every room type with skips and the shortfall note, n/a checks, the freeplay proposal and totals
without budget wording, one lookup with fallback, unverified alternates and the on-demand lookup, cap
counting, cap scaling, the shipping regression and parsing, and end-to-end mock runs. Existing tests were
updated for the longer accent lists and the new stats fields. Ran the suite three times. `make lint`,
`make typecheck`, `make test`, mock `make eval-fast` pass; thresholds untouched.

## Worth knowing

- The mock sofa slot is still the "scaled x8" fixture (for the budget over-limit scenario); in freeplay
  it simply picks a pricey sofa. All scaled ids resolve to the scaled Article store except one per
  fixture, so the budget scenario still drops one option.
- Nothing was measured live: tier quality, first-lookup failure rate, and the guess constants.

## Next

```bash
make spike
make spike ARGS='--synthetic --style "warm minimalist" --price-tier premium --items 8'
make spike ARGS='spike-photos/room1.png --budget 800 --style "warm minimalist" --replace sofa --keep desk'
```
