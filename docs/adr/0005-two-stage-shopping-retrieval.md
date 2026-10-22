# 0005. Two-stage shopping retrieval: structured Google Shopping data, a model that only picks ids

- **Status:** proposed
- **Date:** 2026-10-21

## Context

The first live run used Claude's web search tool for retrieval (SPEC.md configuration A). It returned
listing and keyword pages (`nathanjames.com/collections/...`, `wayfair.com/keyword.php?...`) that
passed the "URL was in the search results" guard, one empty slot, and one likely invented price
(133.33, exactly half the 266.67 slot budget). The model was reading web pages and reporting what it
thought it saw; prices and links were only as good as that reading. Max then tested two structured
Google Shopping providers directly. Those raw responses are on disk, gitignored, in `spike-output/`;
trimmed copies are committed as `tests/fixtures/shopping/`.

## Evidence (from Max's live provider tests)

- **Serper** `POST https://google.serper.dev/shopping`, header `X-API-KEY`, body
  `{"q":"oak side table","gl":"us"}`: 40 items and `"credits": 2` for one query, 2.1 s. Each item has
  title, source (seller), a price string such as `"$249.00"`, rating, ratingCount, productId, position.
  `link` is a `google.com/search?ibp=oshop...` product-panel URL, not a retailer URL, and is absent on
  5 of 40 items.
- **SerpApi** `GET https://serpapi.com/search.json?engine=google_shopping`: 40 items, 1.9 s. Same data
  plus `extracted_price` (float), old price, delivery and tag.
- **Overlap:** 30 of 40 titles are common to both providers, and the product ids (Serper `productId`,
  SerpApi `product_id`) are identical for all 30.
- **Lookup:** SerpApi `engine=google_product&product_id=<id>` works with a Serper `productId`, 2.1 s.
  `product_results.stores[]` gives each seller's direct retailer URL (with tracking parameters such as
  `srsltid` or `utm_*`), price, shipping, total and availability text ("In stock online"), plus
  `product_attributes`. `about_the_product` may be empty.
- **Dimensions:** `google_immersive_product` (needs a SerpApi page token, so unusable with Serper
  results) returned `about_the_product.features` with Width "58 cm", Depth "39 cm", Height
  `12 5/8 "` for an IKEA item. The `google_product` output for another item had no dimensions, so
  dimensions are optional.

## Options considered

1. **Claude web search, as before.** One credential, no integration. But the model reads pages and
   reports prices and URLs itself, so it can return listing pages and unverifiable numbers (the
   evidence above). A URL pattern guard (added 2026-10-21) removes the worst URLs but does not make
   prices trustworthy. Kept selectable as `APP_RETRIEVAL_MODE=web_search`.
2. **SerpApi alone.** Search and lookup from one vendor and one key. Its search is the more expensive
   one per call (assumed $0.025), and we would pay that for every slot.
3. **Serper alone.** Cheapest search (2 credits a query, about $0.0006 assumed), but it returns only
   the Google panel URL, never a direct retailer URL, and has no per-product lookup that we know of.
   Not enough on its own.
4. **Serper for search, SerpApi for lookup (chosen).** Product ids are shared, so the cheap search
   feeds the lookup that has the retailer URLs. Two vendors and two keys.
5. **Retailer APIs.** Direct, authoritative, but one integration, one approval and one set of terms
   per retailer, and coverage would be narrow for a portfolio project.
6. **Affiliate feeds.** Broad catalogs, but they require commercial agreements and affiliate links;
   the portfolio is strictly non-commercial (SPEC.md).

## Decision

Make "shopping" the default retrieval mode, in three stages per slot:

1. **Search** (Serper by default, SerpApi selectable): query is the slot category plus style words.
   Code keeps the top 25, drops unpriced and duplicate results and anything over the slot budget.
2. **Rank**: a small model (Haiku by default, `APP_MODEL_RANK`) sees a compact list and returns up to
   `APP_FINALISTS_PER_SLOT` ids with a reason. No web search tool. Every id is validated against the
   list; the model never supplies a price, seller or URL.
3. **Lookup**: each finalist is looked up by product id (SerpApi `google_product`), concurrently. The
   store matching the seller (else the lowest total) supplies the retailer URL (tracking parameters
   stripped, product-page guard applied), price, shipping, total and dimensions. If the lookup fails or
   the URL fails the guard, the candidate keeps the Google panel URL (`url_kind: "google_panel"`), or no
   URL (`"none"`). A URL is never invented.

Prices and URLs therefore always come from provider data. `price_source` says whether a price is from
the search or the lookup.

## Consequences

- **Cost is dominated by lookups.** At the assumed $0.025 per SerpApi call, three slots with two
  finalists is six lookups, about $0.15 per run, more than the web-search run it replaces ($0.04 for
  searches). Levers: fewer finalists, a cheaper lookup source, free-tier usage (real cost $0).
- **Both USD defaults are assumptions, not measurements:** `APP_SERPER_USD_PER_QUERY=0.0006` and
  `APP_SERPAPI_USD_PER_SEARCH=0.025`. Two things need confirming from the real dashboards: that Serper
  really bills 2 credits per shopping query (the response says so) and what a credit costs on Max's
  plan, and what a `google_product` lookup costs on SerpApi. The estimates count toward
  `APP_MAX_COST_USD` even when real usage is free.
- Two new keys (`SERPER_API_KEY`, `SERPAPI_API_KEY`); live shopping runs need SerpApi even when Serper
  searches. Keys are read from the environment, never logged, and never in errors, traces, tables or
  results (tested).
- Both vendors serve data derived from Google Shopping. Their terms and Google's for a public demo are
  not reviewed; this is a spike decision for private runs. Review before any public live upload.
- Data freshness is the provider's: prices are Google Shopping's at crawl time, not a live page read.
  Stock and price-history fields are returned but unused.
- Latency per slot becomes search + rank + the slowest lookup (three sequential hops, lookups in
  parallel); the stage table shows each call, and `critical_path_seconds` follows that chain.
- Mock mode uses trimmed real responses as fixtures (titles prefixed `[FIXTURE]`); no network.
- Revisit if the exploratory shopping run shows the cost or the three-hop latency is worse than web
  search, or if provider terms rule out the demo.
