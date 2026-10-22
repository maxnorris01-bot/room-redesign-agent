# Shopping data sources for furniture retrieval (2026-10-21)

Desk research plus four small live provider tests, done in a Cowork chat. Purpose: pick a source of real furniture (price, direct link, size) for retrieval configuration B (SPEC.md, ADR 0005). Prices and terms were read from vendor pages on 2026-10-20 and 2026-10-21 unless marked otherwise; they change. This is not legal advice, and no terms were reviewed for a public live demo.

## Requirements that shaped the comparison

- A real product page URL and a price that comes from data, not from a model reading a page. The first web-search run returned listing pages and a probably invented price (`docs/sessions/2026-10-20-exploratory-live-run.md`, `docs/sessions/2026-10-21-live-runs-latency-and-shopping.md`).
- Seconds, not minutes: the owner asked to keep the visitor's wait short.
- Free tier for testing; cost per live run small enough for a portfolio demo.
- Non-commercial public demo; no affiliate agreement, no sales history, no contract.
- Size (width, depth, height) would help a fit check, but nothing was a hard requirement.

## Comparison

"Verified" means read from the vendor's own page or seen in a response; "second-hand" means a comparison article, several of which are published by vendors that rank themselves first and were not trusted for rankings.

| Source | How it finds products | Cost (as read) | Restrictions and terms | Known issues | Verdict |
|---|---|---|---|---|---|
| Serper | Scrapes Google Shopping, JSON | From $0.30 per 1,000 queries; 2,500 free credits; a shopping query billed 2 credits (seen in the response) | B2B; no mirroring as-is; terms silent on display and caching; UK law; no accuracy warranty; can change without notice | Signup failed for a while ("not possible to register at this moment"), worked later; shopping `link` is a Google panel URL, not a retailer URL; no lookup endpoint found | Chosen for stage 1 search |
| SerpApi | Same, plus lookup engines | About $25 for 1,000 searches on the entry plan (second-hand); 250 free per month; real plan price not yet checked | Google's DMCA claims were reported dismissed in 2026 (headlines only; other claims and appeal not verified) | Most expensive per call; shopping `product_link` is a Google panel URL; `google_product` by product id returns direct store URLs | Chosen for stage 3 lookup, and a selectable search provider |
| SearchApi.io | Same | $40 for 10,000 ($4 per 1,000) down to about $1; 100 free; failed requests not billed | Legal protection guarantee from the $100 plan | Plan page does not confirm Shopping is included | Backup, not tested |
| DataForSEO | Same, queued | $1 per 1,000 standard, $2 priority | None noted | Priority about a minute, standard up to 45 minutes, no live mode for shopping products (second-hand) | Rejected: too slow |
| Bright Data | Same, live or async | About $1.50 per 1,000; 5,000 free per month (second-hand) | Not read | Setup through proxy zones; response shape per docs includes `link`, `price`, `shop`; not tested (owner skipped signup) | Reserve, untested |
| Oxylabs | Same | About $0.80 to $1.00 per 1,000 plus rendering; 2,000 free results (second-hand) | Not read | Price varies by target | Reserve, untested |
| Apify actors (Wayfair, etc.) | Third-party scrapers of one retailer | $0.04 per result (about $40 per 1,000 results); $5 free credit | Unofficial; the user is responsible for the retailer's terms | The actor read was marked deprecated; listing-page data only; retailer can block | Poor fit |
| Claude web search | Model reads search pages | $10 per 1,000 searches | Part of the API | Listing-page URLs, an apparently invented price, empty slots | Kept as `web_search` mode for comparison |
| Brave Search API | Independent web index | $5 per 1,000; $5 free credit a month | Terms not read | No shopping endpoint | Not for products |
| Tavily, Exa, Firecrawl | Search and extraction for AI | Not verified | Not read | Page-level, not product-level, as far as seen | Not for products |
| Amazon Creators API (replaces PA-API v5, shut down 2026-04-30) | Official | Free | Needs Associates status and 10 qualifying sales in 30 days | Not available to a new site | Out |
| eBay Browse API | Official | Free | Production needs approval and a contract | Mostly used and auction items | Impractical |
| Etsy Open API v3 | Official | Free | Approval of the application purpose; bans use for analytics, machine learning or AI model training and scraping without written authorization; trademark notice required | Handmade goods, weak furniture fit | Risky |
| Awin product feeds | Retailer feeds you apply for | Free to publishers | Approval per retailer; bulk files, not search | Needs a site and indexing before it works as search | Later option |
| Wayfair, IKEA, Target, Home Depot | No public product API found | n/a | n/a | Only third-party scrapers | Not an option |

Not found: a reliable source for Walmart's affiliate API access requirements (search returned nothing useful). Awin's publisher FAQ could not be read (redirect).

## What the live tests showed

All with the query "oak side table", US, run by Max with free-tier keys; raw responses are gitignored in `spike-output/` and trimmed copies are committed as `tests/fixtures/shopping/`.

- Serper shopping: 200 in 2.1 s, 40 results, `credits: 2`. Fields: title, source (seller), price string, rating, ratingCount, productId, position, and a `link` that is always a Google product-panel URL (absent on 5 of 40). No shipping, no numeric price.
- SerpApi `google_shopping`: 200 in 1.9 s, 40 results, all priced, 28 distinct sellers (IKEA, Target, Home Depot, Wayfair, Walmart, West Elm, Crate & Barrel, Article and others). Same Google panel URLs. Extra fields: numeric price, old price, delivery text, a page token.
- Overlap: 30 of 40 titles shared; the product ids are identical for all 30.
- SerpApi `google_immersive_product` (page token from a SerpApi result): 200 in 1.7 s. Direct IKEA URL with tracking parameters, price $109.99, shipping $10, total $119.99, and `about_the_product.features` with width 58 cm, depth 39 cm, height 12 5/8".
- SerpApi `google_product` with a Serper productId: 200 in 2.1 s. Direct Article URL (with a `srsltid` parameter), $249 + $59 delivery = $308 total, "In stock online", 30-day returns, price history. No dimensions for that product.
- No response contained an API key (checked).

## Reading the results

1. Neither search endpoint returns a retailer URL; a lookup by product id does. So a two-stage design (cheap search, then lookup for finalists) is needed, and it is feasible because the ids are shared across providers.
2. Cost is dominated by lookups at SerpApi's price. Serper's search is cheap.
3. Dimensions come back sometimes, not always.
4. These providers serve data derived from Google Shopping; prices are as of Google's last crawl, not a live page read.

## Caveats and open items

- Cost figures for the two providers are assumptions until confirmed from the Serper and SerpApi dashboards (ADR 0005).
- Terms for a public demo (provider terms and Google's) are not reviewed. Review before any public live upload.
- Comparison-article claims (Bright Data, Oxylabs, Scrapingdog, DataForSEO latency) are second-hand and from interested parties.
- Bright Data and a second lookup provider remain untested options if SerpApi's lookup cost bites.

## Sources

- Serper: https://serper.dev and https://serper.dev/terms
- SerpApi Immersive Product API: https://serpapi.com/google-immersive-product-api
- SearchApi.io pricing: https://www.searchapi.io/pricing
- Brave Search API pricing: https://api-dashboard.search.brave.com/documentation/pricing
- Etsy API terms: https://www.etsy.com/legal/api; rate limits: https://developers.etsy.com/documentation/essentials/rate-limits
- Amazon PA-API shutdown and Creators API: https://dev.to/th3nate/amazon-pa-api-v5-is-shutting-down-april-30-2026-here-is-what-changes-at-the-auth-layer-22ek
- Apify Wayfair scraper: https://apify.com/parseforge/wayfair-scraper
- Bright Data shopping search docs: https://docs.brightdata.com/fast-shopping-search.md
- Comparison articles (self-ranked, second-hand): https://www.prismcrawl.com/blog/best-google-shopping-api and https://apiserpent.com/blog/best-google-shopping-api
- Google v. SerpApi DMCA dismissal (headlines only): https://www.searchenginejournal.com/court-dismisses-googles-dmca-claims-against-serpapi/583033/ and https://serpapi.com/blog/google-v-serpapi-the-court-granted-our-motion-to-dismiss/
