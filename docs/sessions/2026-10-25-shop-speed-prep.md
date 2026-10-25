# 2026-10-25: Shop Again speed, background preparation, step bar, seller canonicalization

Written for someone with no other context. Mock mode only: nothing here ran live, and no live number is
claimed.

## What changed and why

The last live Shop Again took 34.6 s (searches 2.5 s, preferences 8.5 s, tagging the shown items 4.4 s,
tagging 140 candidates 15.9 s in 6 calls, rank 4.4 s). The shown items were tagged from titles only, so the
lift test could not run for shape, finish, frame color or style. This session moves model work earlier,
runs the rest in parallel and shows progress.

1. **Background preparation** (`src/app/shop/prep.py`). Each stored batch is tagged from title and photo in
   two calls of about 12 items (prompt `shop_candidate_tags` v4; v1 to v3 kept). Tags are cached in the
   session by result id with a source (`photo` or `title`); a photo tag wins and is never replaced. Each
   comment is read as it is written and cached by item id plus the comment text (a changed or deleted
   comment is a different key). The preferences step reuses the cache and skips its model call when
   everything is cached; otherwise only the missing items go to the model. A Shop Again waits at most
   `APP_SHOP_PREP_WAIT_S` (8 s) with "Finishing your last ratings (n of m)", then carries on. Limits: 4
   concurrent model calls across all background jobs, 4 calls per batch, `APP_SHOP_PREP_MAX_USD` (0.05)
   per session, cancel on Start over (`POST /api/reset`), atomic session writes. The spend is its own
   "background preparation $x" in the footer and part of the session total.
2. **Live path.** Why 6 concurrent tagging calls took 15.9 s: tagging used the search worker count (4), so
   6 batches ran in two waves, and it began only after all searches had finished. Tagging now has its own
   worker count (`APP_SHOP_TAG_WORKERS`, 6), batches of up to 40, and each search's candidates are queued
   the moment it returns (a `TagPipeline`). Why 140 candidates were tagged with a cap of 60: the cap was
   applied per step (initial, each supply search, refill) and again for the shown items. There is now one
   cap per Shop Again, with a 12-title reserve for supply and refill. The trace has a timeline: every call
   (start, duration, queue wait, retries) and every phase.
3. **Step bar** replaces the spinner (first search: Searching, Tagging, Ranking; Shop Again: Finishing your
   last ratings, Searching, Tagging, Ranking). `GET /api/job/<id>` returns `steps` and `elapsed_s`.
   `APP_SHOP_MOCK_DELAY` (mock only) slows each mock step so the bar can be photographed.
4. **Seller canonicalization.** `seller_key` and the new `count_sellers` give one row per seller in every
   table, so "pool after" counts include every candidate in the batch.

Decisions and tradeoffs worth reviewing: first-come tagging happens before the rank call, so those titles
are not exactly the top by rank (a 12-title reserve is rank-ordered); preparation is off in a bare `Config`
and on from the environment; `redact` now also replaces URLs (an exception message could echo a request URL
with a key in its query).

## Asked mid-session

Nothing. The fifth replay fixture was not built: a saved session holds the shown items, not the candidate
pool a replay needs, and a fixture also needs authored attributes; there is no existing export tool. It is
on `docs/todo.md`.

## Eval and checks

- `make eval-fast` (mock): 2 cases, pass rate 1.0, p95 0.0003 s, mean cost $0. A plumbing check only.
- No live eval was run; the live numbers (Shop Again time, preparation cost, wait usage) are unmeasured.
- Tests: 970 passed (new file `tests/test_shop_speed_prep.py`, 34 tests). `ruff check` and `ruff format
  --check` clean, `mypy --strict` clean (64 files). `make shop-check` passes, including the new mid-run
  step-bar check.
- Existing tests updated for the design: prompt version 4, job status now carries `steps` and `elapsed_s`,
  one slow-tagging test pins `shop_tag_workers=1`, the thinking test expects 6 call sites, and the
  `RouterClient` double now joins the text blocks of a photo call.

## Mock timeline (a mock Shop Again with 0.3 s per mock search or call)

Preferences 0.0 to 0.31 s while the original request's page 3 searched; then four searches 0.32 to 0.63 s,
with a tagging call starting as each returned (0.32, 0.63, 0.64, 0.64 s, queue wait 0 to 0.01 s); rank at
1.0 s; two more searches 1.0 to 1.66 s; 48 of a cap of 60 titles queued. Total 1.7 s. These are mock times,
not predictions.

## Screenshots checked (`spike-output/shop-screenshots/33` to `36`)

The step bar was opened mid-run for a search and a Shop Again, in light, dark and at 390 px. First pass
problems, fixed: the active step's text was repeated beside the bar and wrapped onto two lines, and at
390 px the step names were cut to "Finishing ..." and the timer was pushed against the edge. Now the
repeated text is hidden from sight but kept in the live region, and names wrap on narrow screens.

## Open questions before this merges

- Is a first-come tag before rank acceptable, or should tagging wait for the rank call (slower)?
- The mock tags photos from title keywords, so quality cues and photo-over-title overrides are covered by
  fakes only.
- The preparation spend ($0.05 cap, estimated about half a cent or less a batch) is a guess.

## Next command

From the repository root: `git diff main`. Then, when you choose to spend, `make shop-live` on a real session; read the footer and How I decided.
