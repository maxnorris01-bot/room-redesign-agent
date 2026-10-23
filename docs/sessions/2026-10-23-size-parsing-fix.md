# Session 2026-10-23: size parsing fix

Branch `size-parsing-fix`, from `main` at the pick-quality PR (#10). Mock and fixtures only: no live
call, `.env` not read, no network in any test.

## Why

A live freeplay run showed two size misreads. The sofa title `... 80w 34d 30h` produced only a longest
side of 80 in. The bookcase title `... 21"x79" ...` was read as 0.53 m wide by 2.01 m deep (79 in is
really the height), and the fit check passed it as a footprint by luck.

## What changed

- **Parsing (`sizes.py`, `categories.py`).** Labeled forms are case-insensitive: `80w 34d 30h`,
  `80W x 34D x 30H`, `80"W 34"D 30"H`, `W80 D34 H30`, with a trailing unit word. Unlabeled two-number
  groups use `categories.axis_rule`: tall = width x height (depth unknown), flat = width x length,
  other = longest side only; the rule is written into the size note. Unlabeled three-number groups
  are W x D x H only if plausible for the category, else longest side only. `parse_title_sizes(text)`
  without a category keeps the old reading.
- **Fit (`selfcheck.py`).** The footprint needs two floor sizes (a height never counts). New height
  rule: height over ceiling minus `HEIGHT_CLEARANCE_M` (0.1 m, untuned) fails; unknown height or
  ceiling skips it. Messages name the axes used.
- **Replacement note (`proposal.py`).** Depth is compared too when both depths are known (25%).
- **Cosmetic.** The style note no longer reads `understated.; mild clash ...`.
- **Docs.** ADR 0009 amended (still proposed), `docs/todo.md`, `docs/working-notes-and-decisions.md`.

## Decisions

- A width plus a length still counts as a footprint (a table's `63"L x 12"W`); only an unlabeled
  group no longer manufactures a width/depth pair.
- The prefix form `W80 D34 H30` needs two axes in the title to count, so a model code like `H30` is
  ignored. The axis letter must not be followed by a digit, so `80 D34` is not read as 80 deep.

## Status

- Tests: 474 passed (453 before; 21 new in `tests/test_pick_quality.py`). `make lint` and
  `make typecheck` clean. Mock `make eval-fast` pass_rate 1.0. Existing tests and fixtures unchanged.
- Not measured live: axis accuracy on real titles, the 2.5x three-number test, the 0.1 m clearance.

## Open questions

- The tall-category list is a guess; a "tall" product outside it is read as longest side only.

## Next command

`git diff main`
