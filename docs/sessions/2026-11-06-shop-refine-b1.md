# Refine B1: Shop Again always available, and refine filters shape the next search (2026-11-06)

Branch `shop-refine`, one PR, not merged, not deployed. Backend only.

## What changed and why

Max found two things in production: the Filters modal hid results (2-3 filters left nothing), and Shop Again was
disabled until something was rated. Filters now are preferences for the NEXT search, and Shop Again is always
available.

- **Shop Again without ratings.** `can_shop_again` is true with a finished search and the visitor under the cap
  (`GET /session` also says false while a job runs for the session). The "Rate a few results first" refusal is gone.
  With nothing rated the run skips the preference call and the tagging of everything shown (nothing to learn, so it is
  cheaper than a rated run); the batch is new because every shown id and same-title listing is excluded and the
  existing fetches reach past them (next page, written variants). Rated runs are unchanged.
- **Typed filters stored as refine chips.** `POST /refine` also takes `filters: [{dimension, values}]`
  (vocabulary keys). Chips: kind `filter`, id `filter:<dimension>`, label "Colour: gray or blue". Each payload
  replaces the whole set (`[]` clears), `POST /refine/remove` removes one, a new category starts a new topic and clears
  them. `filter` is not in `REFINE_KINDS`, so the instruction interpreter can never produce one.
- **Filters shape the fetch.** One targeted search (typed search text plus at most 4 filter words, colour first, no
  price words), made beside the preference call, merged into the pool, de-duplicated and cut against what was shown.
  Interpretation of "at most 2 fetches": one targeted fetch, and a second page only if the first added nothing new
  (never a third; none after a provider error). It counts in `serper_calls`, credits and `total_usd`, and the Shop
  Again still counts once.
- **Tiers.** Candidates are ordered by how well their attrs (the first search's function: photo colour tag first, the
  title for the rest) satisfy the filters: all dimensions (OR within, AND across); then a fitting colour before an
  unread one before a known-wrong one, so a known wrong colour never outranks a right one; then most dimensions,
  colour > style > material > rest. The order is applied before the rank call, before tagging (so the photo step, capped
  at 24 candidates, looks at the likeliest first) and before each batch selection (a stable sort, so the LLM order holds
  inside a tier). The page still fills from lower tiers.
- **`search.filters_match`** `{satisfied_all, shown, filters}` (null without filters), computed from the shown results'
  published `attrs`.

## Decisions to look at

- The 2-fetch reading above. If the intent was "2 fetches in the whole Shop Again", say so; the existing mechanism
  makes up to 4 to 9 Serper calls on a rated run.
- The ranking tiers run after the LLM rank (a stable sort), not inside it; the rank call only sees the pool in tier order
  and the filter chips in its "shopper asked to favour" lines.
- Not done: no front-end change (`static/` still gates the button on a rating).

## Tests and checks

- New: `tests/test_shop_refine_filters.py` (29 tests, fakes). Changed three old tests that asserted the removed
  "rate first" refusal (`test_shop_again.py` x2, `test_shop_topics.py` x2).
- `ruff check`, `ruff format --check`, `mypy src` clean. Whole suite: 1974 passed. Four tests fail on `main` already
  (they came in with PR #41, not this change): `test_rank_v4_is_used_for_shop_again_and_v1_for_a_typed_search`,
  `test_replay_two_candidate_tags_are_cached_in_the_session_and_not_repeated` (both read a schema `title` the colour
  call does not have), `test_every_model_call_in_shop_mode_asks_for_no_thinking` (counts 9 `complete(` calls, not 8),
  `test_only_the_clarify_flow_and_its_callers_import_the_spike` (`facets.py`, `colortags.py` import it). Left alone.
- `make eval-fast` (mock): pass rate 1.0, latency p95 0.0003 s, cost $0.00 (a plumbing check only).

## Live runs: NOT done

Max pre-approved two live runs, but the permission classifier in this session denied the command that starts them, so
nothing live ran and there are no before/after numbers. The script is at
`/private/tmp/claude-501/-Users-maxnorris-Desktop-Projects-room-redesign-agent/6c184cdc-6382-457c-85d9-3d1e119763bc/scratchpad/live_refine.py`
(run: `APP_LLM_MODE=live uv run python <script> <empty output dir>`). It does one first search on "rug", clones the session,
and runs Shop Again with `color=gray,blue + style=vintage` on one and with no filters on the other, nothing rated.
It prints the share of gray/blue in the top 12, seconds, cost and calls, never a key.

Estimated extra per filtered Shop Again (not measured): +1 Serper call ($0.002, 2 credits; $0.004 if the retry runs),
and up to 24 candidate photos read for colour (roughly $0.01 to $0.02, a guess), about 0 to 2 s more because the fetch
runs beside the preference call. A Shop Again without ratings skips the preference and shown-tagging calls, so it costs
less than a rated one.

## Next

`git diff main`, then `gh pr merge --merge --delete-branch` after review, then `fly deploy`.
