# 0012. Two modes: Shop Mode first, Redesign Mode later

- **Status:** proposed
- **Date:** 2026-10-24

## Context

The product so far is one pipeline: a room photo goes in, a furniture list comes out (perception,
slots, retrieval, proposal, self-check; ADR 0003 to 0010), with a photo-real render as the main
output still under experiment (ADR 0011). The owner has split the product in two:

- **Shop Mode** (this ADR, stage 1): a curated web product search with no room photo. The visitor
  types a request ("modern couches under $1000", "coffee tables with a mix of wood and metal with
  storage", "boho curtains"), gets a browsable grid, and rates each result love, like or dislike with
  an optional comment. A later "Shop Again" uses the ratings.
- **Redesign Mode** (later): the room photo, stripping furniture, and the photo-real render.

Shop Mode ships first because it needs no perception or render, reuses the retrieval pieces that
already work (Serper search, the id-only ranker, SerpApi lookups, size hints, link hygiene), and gives
the visitor something to react to, which produces the ratings both modes will learn from. The room
pipeline stays in the repo, unchanged and unused by Shop Mode.

## Decision (proposed)

**1. The 2D plan is dropped from the product.** The owner judged the 2D plan useless to visitors: the
photo is the output. Shop Mode has no room at all, and the photo-real render (ADR 0011) is what a visitor
wants to see in Redesign Mode. ADR 0011's "2D plan as the instant first
view" no longer applies. The plan code (`plan_shapes` in `src/app/report.py`) stays as a developer
tool in the run report; nothing in the product depends on it.

**2. Lazy lookups to control SerpApi cost.** A search makes no lookup. Each result carries the Google
product link from the search. A lookup (SerpApi `google_product`, about $0.025 at the assumed rate, the
dominant cost) runs only when the visitor opens a result or loves it, for a result actually shown in the
session, at most `APP_SHOP_LOOKUPS_PER_SESSION` (default 12) per session, and never twice for the same
result (the outcome is cached in the session, including "no retailer link"). Past the cap the Google
link is returned and marked as such. For scale: looking up all 24 results of a search would be about
$0.60 per search; the lazy rule bounds a session at 12 lookups (about $0.30 at most) and a typical one at
a handful. A search itself costs 1 or 2 Serper calls (page 2 only when fewer than 24 results survive the
filters and the per-seller cap) plus two small-model calls (parse, rank), an estimated $0.01 or less.
The cap of 12 is a guess.

**3. Hard versus soft constraints.** Price range and category are hard filters applied in code before
the ranker sees anything: a result outside the range is dropped; a result with no price (or a non-USD
price, which cannot be compared) is dropped only when a price filter is set; when the category is one
`categories.py` knows (a synonym group or a size-range entry), a result whose title plainly names
another known kind of product, or is a cover or replacement part, is dropped. Titles that name no known
category stay (they cannot be judged), and an unknown category such as "curtains" is never filtered.
Style, materials and must-haves are soft: the ranker orders the filtered pool against them and the
"avoid" list, ids only, so the model never supplies a price, seller or link (ADR 0005). A result the
ranker leaves out follows in search order, so the grid still fills. At most 4 results per seller reach
the final 24, applied after ranking so each seller's best ones survive. Limits: the filters and the
ranker read titles only; the category check is keyword matching, not a classifier; the seller cap and
the plausibility word list are untuned guesses.

**4. Session data model.** One JSON file per session under `shop-data/` (gitignored), written atomically
(temp file in the same folder, fsync, `os.replace`), mode 0600, every read-modify-write under a
per-session lock, random 16-character id validated before any path is built. A session holds: each
search (raw request, parsed `ShopQuery`, timestamp, result ids, Serper calls, timings, Anthropic and
provider dollars, filter counts, warnings); every result ever shown (so Shop Again can exclude seen
ones); each rating (love, like or dislike, an optional comment of at most 200 characters, timestamps,
the search it was made in); and every resolved link with its price, shipping and cost. Read-only
helpers return the rated results with their comments, the set of seen ids and the disliked sellers.
Shop Again logic is not built here.

**5. Data-source and terms-of-use risk (needs review before any public deployment).** Serper and SerpApi
return Google Shopping results scraped from Google. Showing those results, and their Google-hosted
thumbnails (gstatic), on a public page raises questions that have not been reviewed: Google's own terms
on scraping and reuse of Shopping data; the provider terms (Serper's are silent on display and caching,
and forbid mirroring as-is; see `docs/research/2026-10-21-shopping-data-sources.md`); retailers' rights in
product images; and whether a portfolio page counts as the non-commercial use the provider plans allow.
Shop Mode as built is a local tool: the server listens on 127.0.0.1 only, images are loaded by the
visitor's browser straight from their hosts (never proxied or stored), and no data leaves the machine
except the provider calls. Before any public deployment decide: keep it a recorded or local demo, show
links and text without thumbnails, or move to a source with display rights (a merchant feed or an
affiliate API). This is not legal advice.

## Options considered

1. **One pipeline with a mode flag.** Rejected: Shop Mode has no room, so almost nothing in the
   pipeline applies, and the flag would touch every stage.
2. **Eager lookups for every result.** Rejected on cost (about $0.60 a search) and latency (24 lookups).
3. **Rank before filtering.** Rejected: price and category are not preferences, and the ranker would
   spend tokens and sometimes disagree with the filter.

## Consequences

- Shop Mode lives in `src/app/shop/` and reuses `app.shopping`, `app.shopping_retrieval`, `app.sizes`,
  `app.categories` and `app.llm`. The shared edits are small: a `page` argument on providers' `search`
  (Serper only; SerpApi returns nothing for page 2), `known_category` and `category_hits` in
  `categories.py`, and `Config.shop_lookups_per_session`.
- Prompts `shop_parse.md` and `shop_rank.md` (v1) are new. Mock mode answers the parse for eight sample
  requests and orders ranks by title words, so the page works offline; mock quality says nothing about
  the real prompts.
- The page is plain HTML, CSS and one script, served by the standard library with a strict CSP; no
  dependency was added.
- Stage 2 (Shop Again), stage 3 (an eval of about 12 prompts) and deployment come next; Redesign Mode
  after (`docs/todo.md`).

## Open risks

- Serper's `page` parameter for the shopping endpoint, and the real shape and size of a shopping page,
  were not verified here (no live call).
- Search results change between runs, so "seen" ids only approximate "do not show this again".
- A per-session lookup cap of 12 and a per-session search cap of 50 are guesses.

## Amendment, stage 2 (2026-10-24): Shop Again, Saved, cost accounting

- **Preference profile.** After ratings exist, one small-model call (`shop_prefs.md` v1) reads the typed
  request, every rated result with its comment, and the existing profile with its deleted entries, and
  returns the complete updated profile: short likes and dislikes, and query adjustments. Comments are
  read by the model, not matched to fixed tags. Code sanitizes the answer and holds the rules the model
  is not trusted with: entries the visitor deleted are never added back (in likes, dislikes or
  adjustment terms, by phrase or close variant); category and price floor never change; `price_max` may
  only fall, by at most 20% of the typed cap, and only when the model gives a reason; lists are bounded.
  A failed call keeps the earlier profile and still searches.
- **Shop Again.** Adjusts the last typed query (never an earlier Shop Again's, so adjustments do not
  stack), runs the stage 1 pipeline with exclusions (every result id shown in the session, and the
  same listing under another id), fetches up to 3 Serper pages while fewer than 24 unseen results
  survive (and stops at an empty page), ranks with `shop_rank.md` v2 (v1 kept for typed searches) which
  also sees the profile and the loved, liked and disliked titles, and says "Only N new results found. Try
  changing the request." when short. A seller with at least 2 dislikes and no love or like is ranked
  last, not excluded. Cap: `APP_SHOP_AGAINS_PER_SESSION` (10, a guess).
- **Saved** is derived from the ratings (loved and liked, all searches and Shop Agains) and the stored
  results; nothing extra is stored. A re-rate to dislike, or a cleared rating, removes the result.
- **Serper cost** is `calls x APP_SERPER_CREDITS_PER_CALL (2) x APP_SERPER_USD_PER_CREDIT (0.001)`: a
  Shop Mode search of 2 calls is 4 credits, about $0.004. The stage 1 footer used a per-query figure
  ($0.0006) that showed $0.0012 for 2 calls while the live search used 4 credits. The USD per credit is the owner's figure, not a
  documented price. The room pipeline still uses `APP_SERPER_USD_PER_QUERY`.
- **Per Shop Again (design):** up to 3 Serper calls (6 credits, about $0.006), one preference call and
  one rank call (Anthropic about $0.01 or less; the mock bills fractions of a cent), no lookups.
- **Not verified live:** that Serper accepts `page` 3, what the model does with real comments, and
  whether "unseen" holds up when result sets are small or repeat the same listings.
