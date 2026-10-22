# Session 2026-10-21: keep-or-replace and cleanup

Branch `keep-or-replace-and-cleanup`, from `main` after the proposal and self-check PR (#6). Mock mode
only: no live call, no `make spike-live`, `.env` not read; tests use obviously fake key strings.

## Why

The first full live run proposed a single item for a furnished room and gave that one slot the entire
budget. It also surfaced a link that landed on a different product than the search result (Article
"Judith" vs "Leif"), rugs failing the fit rule, and lookup failures that were visible only inside
`result.json`.

## Part A: keep or replace (ADR 0007, proposed)

- **Flags** `--replace` / `--keep` (comma separated ids or categories; `app/preferences.py`). Category
  names go through the existing synonym folding, now in `app/categories.py` (re-exported from
  `app.retrieval`). Unknown names and keep/replace conflicts exit 2 and print the valid ids and
  categories. Defaults come from perception's `keepable` (`unlikely` is suggested-replace).
- **Slot policy** (`retrieval.plan_slots`, `derive_slots` keeps returning the list): requested
  replacements, gaps, suggested replacements, then accents, up to `max_slots` (3). Accents skip
  categories covered by kept furniture, fixed items and decor, and a replaced item's category. Dropped
  requests and gaps are printed. `Slot.source` is now `replace_requested | gap | replace_suggested |
  accent`; `Slot` gained `replaces_id` and `total_cap_usd`, and `size_hint` carries the old item's size.
- **Budget ceiling** replaces the equal split: per-item price ceiling = budget x `APP_ITEM_CAP_FRACTION`
  / (1 + `APP_SHIPPING_BUFFER`) (436.36 for $800), identical for every slot. After lookup, a finalist
  whose total is over budget x item cap is dropped with a reason; a slot may end empty with a note.
  Rank prompt v2 (v1 kept) says "ceiling", not "share".
- **Proposal prompt v2** (v1 kept as `proposal.v1.md`, `--proposal-prompt-version`, default 2): slots may
  replace an existing item with a similar size, the budget covers the whole list, kept items are not
  duplicated. Items record `replaces` (id, category, width, depth). The model still returns ids only.
- **Output**: `result.json` gains `preferences`, `kept_items`, `slot_plan`, `warnings`; the printed
  list shows "Replaces: sofa (f1)" per replaced item and a "Keeping:" line before the totals.
- **Mock**: perception gained a desk (so `--keep desk` works) and one gap instead of two. The default
  mock run is now: coffee table (gap), sofa (suggested replacement), area rug (accent); with
  `--replace armchair` the first slot is the armchair.

## Part B: cleanup

- **Title/page mismatch** (`selfcheck.title_mismatch`, part of the grounded check): the store's own
  title is kept (`store_title`) and compared, with the URL slug, to the search title on sizes (", in,
  inch, cm, mm, W; cm and mm converted; a bare 2-3 digit slug number counts as inches; 1 inch or 3%
  tolerance) and materials (oak, walnut, ...). Fails when both sides have sizes and none match, or both
  name materials and the sets are disjoint. The Judith example fails, the Leif example passes.
- **Flat goods** (rug, runner, mat, carpet, plus synonyms; whole words, so "mattress" is still checked)
  skip the fit rule: unknown, "flat item; footprint rule not applied".
- **Lookup failures**: a WARNING line after the table and a marker in the final list header; 402 and 429
  degrade to the Google page link; 401 or 403 from a lookup is a configuration error and exits 4
  (message names provider and status, never a key). A 429 now gets one immediate retry (the brief said
  the retry "stays"; only 5xx and timeouts had one before).
- Dimensions are still not parsed from titles for fit.

## Tests

274 passed (231 before). New `tests/test_keep_replace.py` (40): flag and preference resolution
(ids, categories, synonyms, defaults, conflicts, unknown names with the valid list), slot order and cap
with truncation, accents beside the last live room (media unit gap then bookshelf only), replaces_id and
size hint, the ceiling formula and post-lookup drop, proposal v2 with `replaces`, v1 selectable,
mismatch and unit-conversion cases, flat goods, lookup warnings, 401 exit 4 with no key in output, CLI
exit codes, `result.json` shape, end-to-end with and without flags, and fake keys absent from outputs.
Existing tests were updated for the new source names, the ceiling and the mock perception. Ran the
suite three times. `make lint`, `make typecheck`, `make test`, mock `make eval-fast` pass; thresholds
untouched.

## Decided differently or worth knowing

- **Gaps already covered by something that stays are skipped** (not in the brief): it removes the
  doubtful "side table beside sofa" next to a kept nesting side table.
- **Unknown names are reported after perception ran**, because ids come from perception; only a literal
  name in both flags is caught up front. A UI removes this.
- **A category matching several items applies to all of them**; a partial word ("chair") matches
  nothing and is an error.
- **403 is treated like 401** for lookups (same "check the key and plan" meaning).
- A default mock run now proposes three items and still exercises a revision; that is fixture
  behaviour.
- Nothing was measured live. More slots cost more (about $0.05 of provider dollars per slot at the
  assumed prices), and whether `keepable` suggestions match what a visitor would choose is unknown.

## Next

```bash
make spike
make spike ARGS='spike-photos/room1.png --budget 800 --style "warm minimalist" --replace sofa --keep desk'
```
Then, Max's call with keys in `.env`: a live multi-slot run.
