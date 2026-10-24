# Session 2026-10-24: Shop Mode, stage 1

Branch `shop-mode-stage-1`, from `main` at the photo-real spike PR (#13). **No live call was made**: mock
mode and fixtures only, `.env` not read, no network in any test (a socket guard allows loopback only,
for the one test that runs the real server). Nothing was committed, pushed or opened as a PR.

## What changed and why

The product now has two modes (ADR 0012, proposed). Shop Mode is a curated product search with no
room photo: type a request, browse a grid, rate each result, open one. Redesign Mode (room photo,
stripping furniture, photo-real render) comes after. The room pipeline is untouched and unused here.

- **Package `src/app/shop/`.**
  - `parse.py`: one small-model call (prompt `shop_parse.md` v1) turns the request into a `ShopQuery`.
    Code sanitizes it: prices must be sane numbers, min and max are swapped if reversed, term lists are
    lowercased, deduplicated and bounded, price words are stripped from the search text, and the known
    category key comes from `categories.py`, never from the model. An unusable model answer degrades to
    the typed words with a warning.
  - `search.py`: Serper page 1; page 2 only when fewer than 24 results could be shown after the
    filters and the per-seller cap. Hard filters: price range (a result with no price, or a non-USD
    one, goes only when a price filter is set) and category plausibility (a title that plainly names
    another known kind of product, or a cover or replacement part). Dedupe by product id and by
    normalized title plus seller. An id-only ranker (`shop_rank.md` v1) orders the pool against style,
    materials, must-haves and avoid; results it omits follow in search order. At most 4 per seller,
    applied after ranking; top 24. Size hints come from the existing title parser. No lookups.
  - `resolve.py`: the lazy lookup. Reuses the room pipeline's `_lookup_one` (link hygiene, shipping
    status, store choice). Only results shown in the session; cap `APP_SHOP_LOOKUPS_PER_SESSION` (12);
    cached per result, including a failed lookup (marked fallback to the Google link); past the cap the
    Google link comes back marked and is not cached. A 401 or 403 propagates and is not cached.
  - `store.py`: one JSON file per session in `shop-data/`, atomic writes, mode 0600, per-session lock,
    validated random id. Searches, every result shown, ratings with comments, resolved links; helpers
    `rated_results`, `seen_ids`, `disliked_sellers`.
  - `service.py` (behaviour without HTTP), `server.py` (standard library, 127.0.0.1 only),
    `providers.py` (fixture fakes in mock mode, the existing live providers otherwise), `static/`
    (HTML, CSS, one script).
- **Small shared edits** to existing code: a `page` argument on providers' `search` (Serper sends
  `page` only for page 2; SerpApi and the old fixtures return nothing for page 2),
  `known_category` and `category_hits` in `categories.py`, `Config.shop_lookups_per_session`, mock
  routing for the new parse schema and a Shop Mode ranking in `mock_llm.py`.
- **Page.** Search box, "how your request was understood" chips, a responsive card grid (thumbnail,
  title, price, seller, size hint, love / like / dislike toggles, a note saved on blur or Enter, Open),
  footer with search time, Serper calls, estimated cost and lookups used, a disabled "Shop Again" with
  the tooltip "Coming next". Reload restores the last search, ratings, notes and resolved links.
  Loving a result triggers a resolve; Open resolves, then opens the retailer link in a new tab.
- **Security.** Strict CSP (`script-src 'self'`, `img-src https:`, no inline script or style), every
  external string as a text node, https-only thumbnails (a failed one becomes a letter tile), no image
  proxying, `Referrer-Policy: no-referrer`, Host, Origin and `application/json` checks (a local server
  that spends money must not be callable from another web page), 8 KB body cap, input validation.
- **Commands.** `make shop` (mock), `make shop-live` (prints the expected cost per search first),
  `make shop-check` (headless browser check, below). Entry point `scripts/shop.py`.
- **Docs.** ADR 0012, SPEC.md (modes, shipped versus later, commands), `docs/todo.md` (stage 2 Shop
  Again, stage 3 eval, deployment, terms review, Redesign Mode), working notes, a one-line note on
  ADR 0011 (the 2D plan is no longer the first view).

## Choices made without being asked

- Prompts are in `prompts/` with the others, not `src/prompts/` (that folder does not exist; the
  loader reads `prompts/`).
- The 2D-plan reasoning in ADR 0012 is mine, from what the repo says; the owner decided it.
- Rating and comment are independent; clearing both removes the record. The session id is kept in the
  browser's local storage; an unknown id gives an empty page, not an error.
- Playwright is not installed in the project environment here (the brief said it was). The check
  runs it with `uv run --with playwright`, so no dependency was added to `pyproject.toml` or `uv.lock`;
  it used the Chromium headless shell already in `~/Library/Caches/ms-playwright`. A mypy override
  ignores missing Playwright imports.
- Mock data is synthetic Serper-shaped JSON (`tests/fixtures/shop/`), because the existing fixtures
  hold 8 oak side tables. Titles carry `[FIXTURE]`.
- Requests longer than 300 characters are rejected, a session allows 50 searches, bodies are capped at
  8 KB: all guesses.

## Headless check (`make shop-check`)

Drives the page in mock mode: the three example searches, three ratings (love, like, dislike), clearing
a rating, two notes (one saved with Enter, one on blur), Open in a new tab, a reload, and a restore of
all of it. 35 checks passed, no console errors. The page's CSP also blocked Playwright's own
string-evaluating wait, which was a useful accidental check; the script uses locator waits. Screenshots
(gitignored) in `spike-output/shop-screenshots/`: `01-empty`, `02-results-<topic>` (three),
`03-results-curtains-full`, `04-rated`, `05-restored`, `06-restored-mobile`. Fixed after looking: square
thumbnails that stretched cards (now 4:3, absolutely positioned image), a shifted row when a price was
missing (fixed-height price row).

## Status

- Tests: 603 passed (537 before; 66 in `tests/test_shop.py`). `make lint` and `make typecheck` clean;
  mock `make eval-fast` pass_rate 1.0 (a plumbing check, not a quality signal). No live eval was run.
- Not verified: anything live. Serper's `page` parameter on the shopping endpoint, the real page size,
  real parse and rank quality, real latency and cost, real thumbnails in the grid, the popup behaviour
  in Safari, dark mode visually, any browser but headless Chromium.

## Questions for review

- Is "category plausibility from the title" too strict or too loose on real titles? It only drops titles
  that name another known category or a cover; unknown categories are never filtered.
- Is the 4-per-seller cap right once real results show how concentrated they are?
- 12 lookups per session and about $0.025 each is up to about $0.30 a session; acceptable?

## Next command

`make shop`, then open the printed URL. To run the browser check again: `make shop-check`.
