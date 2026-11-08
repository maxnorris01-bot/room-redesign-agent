# Price min/max as a refine filter (2026-11-07)

Branch `shop-price`, one PR, not merged, not deployed. Backend only.

## What changed and why

Shop Again can now take a typed price range. It is a refine chip (kind `price`, id `price`) set through
`POST /refine` with `"price": {"min": number|null, "max": number|null}`, next to `filters` and `text`.

- **Contract** (`docs/api-contract.md`, "Price range"): the payload replaces the stored price; `{}` or both null clears
  it. Negatives, non-numbers and `min > max` are `400` and store nothing (not even the filters sent in the same
  call). Chip targets are `price:50-200`, `price:-200`, `price:50-`; labels "Price: $50-$200", "Under $200", "Over $50".
  Removable with `/refine/remove` (id `price`), cleared by a category change, applied to every later Shop Again.
  `search.price_filter` is `{min, max, removed}`, or `null` without a typed price.
- **The drop reuses the rule that was already there.** `search.apply_filters` already dropped out-of-range and unpriced
  candidates for the query's price (before ranking). The typed price is fed through the query (`refine.apply_to_query`),
  replacing both ends of the parsed budget and of any text refinement, so there is no second price filter to keep in
  step. `removed` is read from the same counts. Filter tiers run after it, as before.
- **Fetch help.** With typed filters, the price words ("under $200", "over $50", "$50 to $200") go first in the
  targeted search, then the filter words, 4 added words in all (`refine_filters.targeted_text`). Serper's shopping
  endpoint has no price parameter that we use, so there is nothing more to add.
- **One extra page, only when short.** If the strict drop leaves fewer than 24 candidates, one extra page is fetched
  (the targeted words plus the price words, the next page for them), merged and re-filtered. It counts in
  `serper_calls`/credits/`total_usd` and takes the place of the usual refill search (so a typed price never costs
  more than one extra fetch). A failed page is a warning. The batch is never padded with out-of-range items.
- A first search and a Shop Again without a typed price run the same code as before; a test sets and clears a price and
  compares titles, prices and the fetch calls with an untouched session.

Decision: a price-only Shop Again (no filters) has no targeted fetch of its own, because that would add a fetch to every
priced Shop Again; the extra page (price words in the text) happens only when the pool is short.

## Choices mid-session

None asked.

## Eval numbers

Not run: nothing in `prompts/` or the model logic changed (no `make eval-fast` needed for a plumbing and filter change).
One live Shop Again, pre-approved in the brief ("rug", then price max 100):

- results shown 24, above $100: **0**, candidates dropped by the price rule: **51** (`price_filter.removed`)
- 3 Serper calls (no extra price page: the pool was full), `total_usd` $0.0111, 11.7 s
- estimate printed first: worst case about $0.024 Serper plus about $0.02-0.05 Anthropic; one extra page is $0.0020

## Test, lint, typecheck

- `tests/test_shop_refine_price.py`: 30 pass (clear/replace/validation, chip forms, remove and category reset, parsed
  budget and text overridden, strict drop with missing price and inclusive bounds, extra page only when short and when
  it fails, with filters, `removed`, no-price identical).
- ruff check and format clean on `src` and the new test; mypy clean for the new code (the other mypy errors are in
  existing test files on `main`).
- `tests/test_shop_again.py::test_rank_v4_is_used_for_shop_again_and_v1_for_a_typed_search` fails on `main` too (same
  with this change stashed); it is one of the tests already failing since PR #41.
- Only the tests I added or touched and the neighbouring refine/filter/again tests were run, as asked.

## Open questions

- The page needs a price control that posts `price` and shows the chip. Not built here.
- The extra page is keyed to "fewer than 24 after the drop"; if a very narrow range often needs more, a second page
  would be a separate decision.

## Next

`git diff main`, then review the PR; deploy with `fly deploy` (nothing else to configure: no new setting or secret).
