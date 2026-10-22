# Session 2026-10-21: two-stage shopping retrieval

Branch `two-stage-shopping-retrieval`. Mock mode only: no live call, no `make spike-live`, `.env` not
read, no real key used anywhere (tests use obviously fake key strings).

## Why

The first live run's web-search retrieval returned listing-page URLs and one likely invented price.
Max tested two structured Google Shopping providers by hand; the findings are in
`docs/adr/0005-two-stage-shopping-retrieval.md`. This change makes that approach the default and keeps
web search selectable.

## What changed

- **Settings** (`config.py`, `.env.example`): `APP_RETRIEVAL_MODE` (shopping | web_search),
  `APP_SEARCH_PROVIDER` (serper | serpapi), `APP_FINALISTS_PER_SLOT` (2), `APP_MODEL_RANK` (defaults to
  the retrieval model), `SERPER_API_KEY`, `SERPAPI_API_KEY` (env only, deliberately not Config fields),
  `APP_SERPER_USD_PER_QUERY` (0.0006), `APP_SERPAPI_USD_PER_SEARCH` (0.025). `.env.example` says the two
  prices are assumptions and free-tier usage costs $0 in reality.
- **Providers** (`src/app/shopping.py`, new): `SearchProvider` / `ProductLookup` protocols;
  `SerperSearch`, `SerpApiSearch`, `SerpApiProductLookup` over `httpx2` (the HTTP client the Anthropic
  SDK already brings; now an explicit dependency); 15 s timeout, one immediate retry on timeout or 5xx;
  401, 402, 429 and other statuses become `ProviderError` messages that name the provider and status
  only. Parsers for all three responses, `parse_price`, `parse_dimensions` (cm, mm, m, inches with
  fractions), `strip_tracking`, `choose_store`. Fixture-backed fakes for mock mode.
- **Pipeline** (`src/app/shopping_retrieval.py`, new; `retrieval.retrieve` dispatches by mode):
  1. search, then top 25, drop unpriced, dedupe by product id, drop above the slot budget;
  2. rank with `prompts/rank.md` v1 (structured output, no tools, ids validated, capped);
  3. lookups in parallel per slot, store chosen by seller match else lowest total, tracking parameters
     stripped, `is_product_page_url` applied, fallback to the Google panel URL (or none).
  New candidate type `ProductCandidate` (extends `Candidate`, which stays the web-search API shape)
  with `url_kind`, `shipping`, `total`, `dimensions_m`, `rating`, `rating_count`, `reason`,
  `product_id`, `price_source`.
- **Cost and timing:** one stage-table row per search, rank and lookup (provider in the model column,
  `*` marks estimated dollars, with a footnote). Provider dollars go into the shared `Budget` (cost cap)
  but are not LLM steps. `critical_path_seconds` is prepare_image + perception + the slowest slot chain
  (search, rank, slowest lookup); `result.json` totals follow.
- **CLI:** `--retrieval-mode`, `--search-provider`, `--finalists`. `make spike` with no `ARGS` now runs
  a generated image (mock only; `spike-live` has no default).
- **Slot coverage:** default slots now count `art_and_decor` kinds and `fixed_items` categories, with
  synonym folding. Test uses the last live room's categories (`tests/fixtures/perception_live_run_categories.json`,
  free text replaced by "-"): no rug or floor lamp default; only "bookshelf" remains.
- **Fixtures** (`tests/fixtures/shopping/`): first 8 items from each of the four source files, trimmed.
  All four source files were present. The IKEA lookup fixture comes from the
  `google_immersive_product` response (same `stores` / `about_the_product.features` keys). Scanned: no
  `api_key` or key-like string (a test enforces it).

## Tests

193 passed (89 before). New: `test_shopping.py` (60: both providers' parsing including items with no
link, price parsing, dimensions incl. the IKEA values, tracking stripping, store selection, HTTP
behaviour over a mock transport incl. 401/402/429, retry, timeout, no key in errors, fixtures free of
keys), `test_shopping_retrieval.py` (stage 1 filtering, id validation, lookup fallbacks, URL guard,
cost and budget, concurrency with barriers, fake keys absent from trace, table and result.json even
when a real HTTP lookup class fails, slot coverage, config, CLI). Existing tests that assumed web
search now select it explicitly. `make lint`, `make typecheck`, `make test`, mock `make eval-fast` pass.

## Surprising or worth knowing

- **Cost.** At the assumed prices a default mock-shaped run is about $0.16 (six lookups at $0.025 =
  $0.15). Real cost depends on the plan; the cap `APP_MAX_COST_USD=0.25` has little headroom. Try
  `--finalists 1` for the first live run.
- A test caught a real bug in my store matching: one-letter seller names matched by containment
  ("A" inside "Unrelated"). Containment now needs 4+ characters.
- Mock results are the same fixture for every slot (keyed by query, with one default), and titles are
  prefixed `[FIXTURE]`. The mock ranker also returns one invented id on purpose so the validation shows
  up in every run. IKEA dimensions only appear with `--search-provider serpapi` (that fixture's product
  is in SerpApi's list but not in the first 8 Serper items).
- A lookup that fails with a billing or auth error (401, 402, 429) degrades to a Google panel URL
  silently except for `summary.lookup_errors` in `result.json`; a search failure is raised loudly
  (exit code 4).
- Nothing here was measured live. Whether Serper's `link`-less items, SerpApi lookups for Serper ids,
  and the cost assumptions behave as the hand tests suggested is the first thing a live run checks.

## Next

```bash
make spike                                  # mock, shopping mode, generated image
make spike ARGS='spike-photos/room1.png --budget 800 --style "warm minimalist" --retrieval-mode web_search'
```
Then, Max's call with keys in `.env`: the live shopping run in `docs/todo.md`.
