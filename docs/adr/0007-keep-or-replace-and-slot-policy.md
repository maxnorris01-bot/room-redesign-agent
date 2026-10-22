# 0007. Keep-or-replace and the slot policy

- **Status:** proposed
- **Date:** 2026-10-21

## Context

The first full live run proposed one item: with one gap in a furnished room there was one slot, and the
budget was split equally across slots, so that one slot received the whole budget. A furnished room
should get more than one proposal, and the visitor knows which of their furniture they want to keep.

## Decisions (owner)

- The choice reaches the agent as **CLI flags** `--replace` and `--keep` (comma separated), with
  defaults from perception's `keepable` flag. No preferences file, no interactive prompt.
- With nothing named and few gaps, the slot list is gaps, replaced items, plus accent slots, **up to 3
  slots** in total (`APP_DEFAULT_SLOTS`, `--slots`).

## Decision

**Flags.** `--replace` / `--keep` take item ids (`f1`) or category names (`sofa`, `coffee table`);
category names match through the existing synonym folding ("couch" is a sofa). A name that matches no
existing furniture, or an item named in both flags (even by different names), is an error: exit code 2,
printing the valid ids and categories. Items not named: `keepable: unlikely` is suggested-replace,
`likely` and `unclear` are kept. Ids come from perception, so an unknown name is reported after
perception ran (a literal name in both flags is caught before anything runs).

**Slot order**, stopping at `max_slots`: requested replacements, gaps, suggested replacements, accents.
- Accent slots come from the room-type list and skip any category already covered by kept furniture,
  fixed items or decor (with synonyms); a replaced item's category is never an accent.
- A gap already covered by something that stays (for example "side table beside sofa" beside a kept
  nesting side table) is skipped and noted.
- Requested replacements or gaps beyond the cap are printed as dropped; accents beyond it are filler
  and not reported.
- `Slot.source` is `replace_requested`, `gap`, `replace_suggested` or `accent` (it was `replaceable`,
  `gap`, `default`). A replacement slot carries `replaces_id` and a size hint with the old item's width
  and depth, and the final item records `replaces` (id, category, width, depth).

**Budget ceiling, not an equal split.** Each slot's `budget_usd` is now a per-item price ceiling:
`total budget x APP_ITEM_CAP_FRACTION / (1 + APP_SHIPPING_BUFFER)` (0.6 and 0.10 by default, so 436.36
for an $800 budget), the same for every slot. Search keeps results priced at or under it. After lookup,
any finalist whose total (price plus shipping) is over `budget x APP_ITEM_CAP_FRACTION` is dropped with
a reason; a slot can end with no finalists and carries through with a note. The equal split was
dropped because it ties a slot's allowance to how many other slots there are (one slot got the whole
budget, six would get a sixth each) and says nothing about which item is worth more. The ceiling is
the same cap the self-check applies, so search and check agree; how the money is shared across slots
is the proposal's job (prompt v2: the sum of totals must stay within the whole budget).

**Output.** `result.json` gains `preferences` (keep, replace, and per existing item the decision and
whether it was requested, suggested or default), `kept_items` and `slot_plan` (slots, dropped by cap,
skipped). The printed list shows "Replaces: sofa (f1)" per replaced item and a "Keeping:" line before
the totals.

## Amendment (2026-10-22): empty slots, over-budget options, the link rule

Found in the first live run with `--budget 800 --replace sofa --keep desk`: every one of 25 sofa
results was over the $436.36 ceiling, the slot came back empty, and the output said only "25 over
budget", so the tool looked broken. Also an alternate had no retailer URL and the total line treated
unknown shipping as $0.

- **Price stats.** Search records per slot, from parsed numeric prices of the top 25: considered,
  priced, unpriced, over_ceiling, min, median, max. They are in the slot summary, the trace and
  `slot_plan.price_stats` in `result.json`.
- **Over-budget options.** When a slot has no in-budget finalist, up to 2 options are built so the
  buyer sees what exists. After the ceiling filter: the 8 cheapest priced results over the ceiling
  are ranked (same rank stage; prompt v3 says to pick the closest matches anyway, v2 stays
  selectable with `--rank-prompt-version`), the top 2 are looked up, and an option needs a verified
  retailer URL. After the post-lookup total-cap drop: the dropped finalists become options directly,
  with no extra lookup. Each carries a reason (over per-item limit, or over total cap after
  shipping) and a flag if it also exceeds the whole budget. A filled slot never gets options.
- **Options are never picks.** Not proposal candidates, not in the totals, not budget-checked, not
  mentioned by the model (the proposal sees an empty slot as data and is told not to recommend
  anything for it). They appear only in their own block, written by code: "<slot>: nothing found
  within your $<ceiling> per-item limit", "Replaces: ..." if it replaces something, "Cheapest <slot>
  seen: $X (median $Y, N results). Raise the budget or loosen the style to fit one.", then "Over your
  limit, not counted in the total:" with name, price, shipping (or "shipping unknown"), URL and
  "also over your total budget". If no option survives, only the stats line is shown.
- **Cost.** At most one Haiku rank call and two lookups per such slot, counted in the caps and shown
  as "(over limit)" rows; the slot chain stays one concurrent chain.
- **Link rule.** A finalist with no link at all (`url_kind: none`) is dropped with a reason; if that
  empties the slot, a WARNING says "lookup returned no store for <title>". A finalist that only has
  a Google page link stays (as before, flagged by the grounded check). Alternates face the same
  grounded rules as picks (link, product page, title agreement, price source) and are dropped if
  they fail. When a lookup returns nothing the reason (http error with status, no product_results,
  no stores) goes into the trace, the slot summary and a warning.
- **Accents.** "media unit" joins the living-room accent list after bookshelf; it is skipped when
  kept furniture, fixed items, decor or a planned slot (a gap) already covers it.
- **Shipping wording.** Unknown shipping is never shown as $0: the total line says how many items
  have known shipping and that unknown is excluded (`result.json` carries the counts and a note).
  The budget check still counts unknown shipping as 0 and warns, unchanged.

## Amendment (2026-10-22, ADR 0008): slot lists scale to 10

The accent lists now reach 10 distinct furniture categories per room type (living, bedroom, office,
dining and the generic list), keeping every earlier entry in its order and adding sensible furniture
only (armchair, side table, console table and ottoman for living; wardrobe, bench, armchair, desk and
bookshelf for bedroom; filing cabinet, storage cabinet, armchair, side table and console table for
office; bench, bar cart, display cabinet, console table and side table for dining), with synonyms in
`categories.py`. The office entry "desk lamp" became "task lamp": the substring coverage rule treats
"desk lamp" as covered by "desk", so it could never be chosen. The slot order is unchanged (requested
replacements, gaps, suggested replacements, accents); covered categories are still skipped. When fewer
than N slots exist the plan has what exists and a note ("this room type has K standard slots"); it
never pads with duplicates. The default count is 3 in budget mode and 5 in freeplay; `--items`
overrides it (2 to `APP_MAX_ITEMS`). The alternates rule was relaxed for freeplay in ADR 0008.

## Alternatives considered

1. **A preferences file.** Reproducible, but a second artifact to maintain for a single-user spike and
   a poor fit for the eventual UI, which will collect the choice directly.
2. **An interactive prompt.** Breaks scripted and mock runs and the "no interaction" property of the
   evals; flags are enough until there is a UI.
3. **Always 3 to 5 items** regardless of the room. Simple, but proposes furniture the visitor does not
   need; filling with accents only when there are few gaps keeps the list tied to the room.
4. **Keep the equal split.** Rejected above.

## Consequences

- A furnished room now gets up to 3 proposed items by default (typically a gap, a suggested
  replacement and an accent), so a run costs more: more searches, ranks and lookups (about $0.05 of
  provider dollars per slot at the assumed prices).
- Accents are generic per room type; they are the weakest slots and exist to avoid a one-item answer.
- Unknown-name errors cost a perception call before they are reported. A UI will make this moot.
- The ceiling and shipping buffer are untuned guesses, like the item cap.
- `Slot.source` renamed; consumers, tests and docs updated.
- Over-budget options add up to 3 calls per empty slot (about $0.05 estimated provider dollars) and
  rely on the rank stage to filter off-category junk from the cheapest results, which is unmeasured.
- Mock-verified only. How well suggested replacements (perception's `keepable`) match what a visitor
  would choose is unmeasured, and `keepable` flipped between runs on the same photo before.
