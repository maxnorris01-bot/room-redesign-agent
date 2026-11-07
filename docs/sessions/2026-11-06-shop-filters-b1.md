# 2026-11-06 Shop filters, backend (B1)

Branch `shop-filters`, one PR, not merged. The clarifying questions are being replaced by filters on the finished results; this adds the data the front end needs. Ranking and result order are unchanged. `APP_SHOP_CLARIFY` stays off and the clarify code is untouched.

## What changed and why
- **`attrs` on every result** (`ShopResult.attrs`, six dimensions, vocabulary keys, `[]` = unknown) and **`facets` + `facets_unknown` on `search`** in `GET /session` and a done job's `result`. Contract and TypeScript shapes: `docs/api-contract.md` ("Filters (stage F1)"). Facets are computed in the view from the stored attrs, so they are always the latest batch's own and an old session (no attrs saved) still serialises: its attrs are read from the title.
- **`src/app/shop/facets.py`** (pure): word-boundary synonym matching from `clarify_vocab.json` (longest synonym wins and uses up its words, so "faux leather" is not also "leather" and "machine washable" is not also "washable"), the facet rules, option visuals from the existing `option_visual`.
- **`src/app/shop/colortags.py`** + `prompts/shop_result_colors.md` (v1): colour for the shown results. Order: a photo tag the search already made, else one vision call over the shown results that have none (12 per call, concurrent, 6 s cap in all, product colours only, multicolour = "multicolor" + up to two), else the title's colour words. A failure, a cap or a budget error never fails the search.
- **Why photo tagging looked skipped for "rug":** it was not skipped. The photo step ran (live: 36 thumbnails, 3 calls, 3.4 s, all 24 shown results tagged) but (1) `run_query` marked the job's `tagging` step as skipped for every typed search ("a typed search tags nothing"), so the progress list never showed it, and (2) a typed search threw the photo tags away after using them for typed-attribute quotas, so nothing exposed them. Fixed: the `tagging` step now shows while photos are read (skipped only when photos are off), and the shown results' photo colours are kept in `attrs`. The words "Checking photos" are not in this repo; I assume the front end labels step id `tagging`.

## Decisions
- Vision call is batched (up to 12 images per call), not one call per result: same 6 s cap, far fewer calls.
- `facets` and `facets_unknown` sit in `result.search` beside `results`, not at the top of `result`: they describe that batch. Easy to move if the front end wants otherwise.
- Dimension order is by coverage (how many shown results have a value), ties in the brief's order.
- No description text exists in the search results, so material, shape, style, size and feature read the title only.
- Vocabulary quirks kept as is (not mine to change here): "luxe" is glam, "marble" is a material even for a marble-look pattern, "high"/"long"/"low" are size words.

## Live run (1, "rug", pre-approved)
Estimated before: at most $0.05. Actual: Anthropic $0.0032, Serper $0.002, total $0.0052; 12.0 s wall. The new step added about 0 s here (all 24 shown results already had photo tags, so no colour call: `shown_attrs` seconds 0.0). The colour vision path was therefore NOT exercised live; it is covered by fakes only. Worst case it adds the 6 s cap. Sample JSON is in the PR description.

## Checks
- `tests/test_shop_filters.py`: 20 tests pass (title matching, boundaries, multi-value, colour from tag / vision / title fallback / timeout cap, facet rules, unknown counts, new batch recomputes, old result without attrs, result order identical with and without the step). Also ran `test_shop.py`, `test_shop_stage2p_typed.py`, `test_shop_clarify_q1.py`, `test_shop_clarify_fast.py`, `test_shop_fixes.py`, `test_shop_deploy_contract.py`: all pass.
- `ruff check`, `ruff format --check` clean; `mypy src` clean (the new test file's imports surface 5 pre-existing mypy errors in other test files).
- Not run (brief asked for small checks): full `make test`, `make eval-fast`, `make eval-fast-live`. Locked eval thresholds untouched; no existing prompt changed.

## Flag for review
- Deploy is not done: `fly deploy` is yours to run after merge (no new env var or secret is needed).
- The 6 s colour cap runs after ranking and is bounded by the hard stop; a search already at the hard stop gets title colours.
- `trace.shown_attrs` (typed search) / `composition.shown_attrs` (Shop Again) records how colours were obtained.

Next: `git diff main --stat`, then `git push` and `gh pr create` when reviewed.
