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
dominant cost) runs only when the visitor opens a result (stage 2b removed "or loves it"), for a result actually shown in the
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

## Amendment, stage 2b (2026-10-24): learn from the items, not only the comments; lookups on Open only

Live use of stage 2 showed Shop Again results that did not feel aligned with what the owner loved unless a
comment said so. The preference chips mostly restated the typed request plus comment text; nothing was
learned from the rated items; a style the parser had guessed ("industrial") was treated as a settled like;
and Shop Again re-ran nearly the same Google search and removed what was seen, leaving the ranker little
to work with. Two loved West Elm tables shared a seller and a premium finish, not a shape.

- **Learn from the items** (`shop_prefs.md` v2; v1 kept). The preference call now gets every rated item
  (id, title, price, seller, rating, comment) and, for up to 6 loved, 6 liked and 4 disliked items, the
  thumbnail, fetched with the existing guarded fetcher (https only, public addresses, size and time
  limits; a failed fetch leaves that item to go by title and is recorded in the profile's image report,
  never with a URL). It returns inferred attributes (shape, material, color_finish, frame_or_legs, size,
  style, quality_cue, seller, price_band) each with polarity, evidence ids and confidence. Stated likes and
  dislikes are only the shopper's own words.
- **Rules held in code, not trusted to the model** (`app.shop.prefs.validate_inferred` and friends):
  evidence ids must be rated results of the right polarity; an attribute needs evidence from two items,
  unless a comment on an evidence item names it or the one item is loved and has a comment; a like needs a
  positive net weight (loved 3, liked 1, minus disliked results that show it); a dislike is refused when a
  loved or liked title shows it, unless a comment backs it; a quality cue must be visible in a title, in an
  image that was sent, or be about the price band; a seller affinity needs two loved or liked items from
  the seller and no dislike of it, and the price band is worked out from where the loved and liked prices
  sit among every price shown (upper or lower third), both computed from the ratings and never taken from
  the model; the band informs ranking and the summary line but never changes the typed price filter.
- **Guesses.** A style term the parser produced that the typed request does not contain is a guess. It is
  shown as a guess chip (dotted, not counted as a like, not given to the ranker as a like) and dropped
  automatically when the model reports a loved or liked item that contradicts it and no loved or liked
  title shows it, or when the visitor deletes it.
- **Chips.** Inferred chips are tagged "inferred", look different from the shopper's own words and from
  guesses, show the evidence titles in a tooltip (text nodes only) and are deletable; deleted phrases are
  never re-added (the removed list from stage 2). The summary line is written in code from the profile.
- **Wider pool.** Up to three phrasings per Shop Again (the adjusted query; the product plus the strongest
  inferred material, finish and shape; the seller's name plus the product, when a seller affinity exists)
  within `APP_SHOP_AGAIN_MAX_SERPER_CALLS` (4) Serper calls. Every phrasing's first page runs; further
  pages run only while the pool is under 60. A phrasing the session already fetched starts after the pages
  it took (a typed search that used pages 1 and 2 is continued at page 3), so the same results are not
  fetched again. Exclusions and filters are unchanged. The ranker sees up to 80 candidates (`shop_rank.md`
  v3; v1 and v2 kept) with the profile parts labelled by reliability and the loved, liked and disliked
  titles as examples. The seller cap is 4, or 8 for the affinity seller.
- **Lookups only on Open.** Loving a result no longer triggers a SerpApi lookup; only Open does (cached,
  capped as before). A loved item keeps its Google link until opened. This supersedes "or loves it" in
  decision 2.
- **Alignment measure.** `preference_alignment(results, profile)` is the fraction of results that match at
  least one inferred like (title shows the phrase, the affinity seller, or inside the price band) and none
  of the dislikes. It is logged in every Shop Again record and shown in the footer ("matches your
  preferences: N of 24") as a diagnostic for the stage 3 eval, not a gate.
- **Cost per Shop Again (design).** Up to 4 Serper calls (8 credits, about $0.008), one preference call
  (about 3,000 input tokens including up to 16 small photos, about 700 output) and one rank call (about
  4,500 input, 600 output): an Anthropic estimate of about $0.002 at Haiku 5.5 prices (an estimate from
  assumed token counts, not a measurement), no lookups.
- **Not verified live:** Serper pages 3 and 4, how well the model reads real photos and titles, whether
  "premium" cues and seller affinity hold on real results, and the share of fresh results the extra
  phrasings really add.

## Amendment, stage 2c (2026-10-24): the model describes, code decides

**The failure.** After two or more Shop Agains on the owner's real ratings, the results leaned round. The
owner's four loved items were all West Elm, $699 to $899, all with storage: a round walnut drum, a round
brushed brass drum, a rectangular wood and black metal lift-top, and a rectangular walnut with drawers.
The liked items (eight or more) spanned $140 to $800 across several sellers and both shapes. What they
share is the seller, warm wood with dark or brass metal, clean modern lines and a premium finish, not a
shape. The tool produced the like chip "round drum" and the dislike chip "rectangular with drawers", which
contradicts a loved item and a liked item, and it wrote learned values ("warm walnut", "drum") into the
request chips, which kept pushing round tables. Two causes: (1) the model decided what counted as a
preference and over-read mixed likes, picking one value of a dimension; (2) an exposure feedback loop: a
batch that is mostly round, rated, makes the next profile conclude "likes round" because round is what
was shown.

**The change.** The model now only describes each rated item along fixed dimensions (`shop_prefs.md` v3;
v1 and v2 kept): shape, material, color or finish, frame or legs, frame color, style, size class, and
quality cues, with the item's thumbnail when it can be fetched. It does not judge preferences. Its other
output is unchanged: the shopper's own words from comments (stated likes and dislikes, a price ask) and
parser guesses a loved item contradicts. `app.shop.decide` decides every inferred entry, from the tags of
ALL the session's ratings on every Shop Again (no incremental drift; the same ratings in any order give the
same profile; deleted chips stay deleted; a model failure still gives a profile, decided from titles alone):

- **Exposure baseline.** How common each value is among everything shown in the session, by a small
  title-keyword lexicon in code (no model call). A title with no keyword for a dimension is unknown and left
  out. Style has a short lexicon; size class has none, so it has no baseline.
- **Per dimension**, from the weighted like mass (loved 3, liked 1): *converged* when one value holds at
  least 70% of the mass, at least 2 items support it (a loved item whose comment names it can be the
  second) and its like share is at least 1.3 times its exposure share; *open* when two or more values each
  hold at least 30% (a neutral chip; no pressure on search or ranking); else *none*.
- **Dislikes** only when at least 2 disliked items share a value, no loved or liked item has it and the
  lift test passes. One dislike only excludes that item. A seller is deprioritized only after 2 dislikes and
  no love or like from it. Any inferred dislike chip is dropped when a loved or liked item has the value.
- **Seller affinity**: stage 2b's rule (2 loved or liked, no dislike) plus the lift test against the
  seller's share of what was shown.
- **Price as a spread.** Mixed when the loved prices cluster in the upper third of the shown prices while
  the liked prices span at least two thirds of the shown range: a neutral chip, no price pressure, loved
  prices only a quality hint. If loved and liked prices together cluster, stage 2b's band rule applies.
  The typed cap never changes.
- **Quality cues** (phrases such as "solid wood") need at least 2 items and half the loved and liked items,
  visible in a title or a sent photo, and are never features (storage, drawers, lift-top). Terms already in
  the typed request are never written back as learned chips.
- **Request chips.** The chips under the search box always show what the parser read from the owner's typed
  words. Learned values live only in the preference chips; the adjusted query that was searched is stored
  separately (`used_query`).
- **Search.** Phrasings come only from what converged: the typed query (never carrying a learned value or
  the seller's name, so look-alikes from anywhere appear); the seller's name plus the product; and, when a
  dimension is open with 2 or 3 values, one phrasing per value, each carrying the converged words and
  never the same open value in every phrasing (one open dimension is varied: the one with the most like
  mass, shape first on a tie). When the call cap (4) bites, the lowest priority goes first (open values as a
  group, then the standalone converged words) and the response records what was left out.
- **Ranking and guards.** `shop_rank.md` v4 (v1 to v3 kept) is told what converged, what is open and must
  be kept mixed, and that a mixed price is no signal. In code, after ranking: no known value of an open
  dimension (and of shape and material when they have not converged) may exceed 60% of a batch, the next
  best items of other values take the place; an affinity seller may take up to 50% of a batch and a seller
  the visitor pinned up to 80% (any other seller 4); if a guard cannot be met the pool's remainder is
  returned and the response says so. The largest single-value share per dimension and the largest seller
  share are logged in the response and the footer. The pin is a click on the seller chip, stored in the
  session.

**The thresholds, in one place** (`src/app/shop/thresholds.py`; all untuned starting points for the stage 3
eval to tune):

| Name | Value | Meaning |
|---|---|---|
| LOVE_WEIGHT | 3 | weight of a loved item in the like mass |
| LIKE_WEIGHT | 1 | weight of a liked item |
| SHAPE_CONVERGE_SHARE | 0.80 | one shape's share of the like mass to converge (stage 2e; was 0.70 for every dimension) |
| SOFT_CONVERGE_SHARE | 0.60 | the same for material, finish, frame color and style (stage 2e) |
| OPEN_SHARE | 0.30 | share each of two or more values needs for open |
| MIN_SUPPORT_ITEMS | 2 | items behind a converged value (a loved item with a naming comment may be the second) |
| KNOWN_COVERAGE_MIN | 0.60 | share of the like mass (and of the shown results, now tagged by the model) that must have a known value before a dimension can converge or run the lift test (stage 2e: 0.60, was 0.70) |
| PRICE_BAND_MIN_MASS | 0.80 | share of the liked mass a price band must hold, or price is mixed |
| CONVERGED_MAX_SHARE | 0.80 | most of a batch a converged value may take |
| LIFT_MIN | 1.3 | like share over exposure share needed to call a value preferred |
| DISLIKE_MIN_ITEMS | 2 | disliked items sharing a value before a dislike chip |
| SELLER_DISLIKES_TO_DEPRIORITIZE | 2 | dislikes (and no love or like) before a seller is ranked last |
| MIN_AFFINITY_ITEMS | 2 | priced loved or liked items before a price signal (stage 2i: no longer the seller rule; `SELLER_MIN_LIKE_SHARE`, `SELLER_NET_DISLIKE_RATIO` and `TIER_MIN_LIKE_SHARE` were removed, replaced by the group like-rate rows below) |
| GROUP_MIN_RATED | 4 | rated items a seller, tier or soft-dimension value needs before it earns a chip or a leaning (stage 2i; judgment from a handful of live sessions) |
| GROUP_MIN_LIKED | 2 | liked items (loved or liked, one each) it needs (stage 2i) |
| GROUP_RATE_RATIO | 1.5 | its like rate must be this many times the baseline like rate over all rated items (stage 2i; this replaces the exposure lift test for sellers and tiers) |
| GROUP_MIN_RATE | 0.35 | and its like rate must be at least this, so a low baseline cannot promote a weak group (stage 2i) |
| MAX_SELLER_CHIPS | 3 | seller chips at once, highest like rate first (stage 2i) |
| TABLE_MIN_RATED | 2 | rated items a group needs to appear in the like-rate evidence table (stage 2i) |
| LEAN_AWAY_MIN_RATED | 6 | rated items a value needs before the page leans away from it (stage 2i) |
| LEAN_AWAY_MAX_RATIO | 0.5 | a value is leaned away from when its like rate is at most this times the baseline (stage 2i) |
| LEAN_TOWARD_MIN_SHARE | 0.30 | a value leaned toward gets at least this share of a batch where the pool has it (stage 2i) |
| LEAN_AWAY_MAX_SHARE | 0.30 | a value leaned away from gets at most this share of a batch (stage 2i) |
| MARKET_MIN_RATED | 8 | marketplace and budget items rated before the "exclude marketplace retailers" chip can appear (stage 2i) |
| MARKET_MAX_RATIO | 0.5 | ... their like rate at most this times the baseline (stage 2i) |
| MARKET_MAX_RATE | 0.20 | ... and at most this (stage 2i) |
| MARKET_MIN_RESULTS | 12 | fewer results than this without marketplaces shows the "Only n results without marketplace retailers" banner (stage 2i) |
| MARKET_REDISMISS_RATED | 8 | a deleted exclusion chip returns only after this many more ratings, and only if the evidence still holds (stage 2i) |
| SELLER_SEARCHES | 3 | seller searches per Shop Again: liked sellers by like rate, then peers of the same tier (stage 2i) |
| QUERY_NOVELTY_JACCARD | 0.8 | a written query sharing at least this share of its words with a query already used in the topic is a repeat and is dropped (stage 2p; judgment) |
| TYPED_MATCH_MIN_SHARE | 0.70 | a typed product colour, material or finish must match at least this share of a batch where the pool has tagged supply (stage 2p; judgment: the pipeline can only enforce what it can tag, so not 100%) |
| PHOTO_TAG_BATCH | 12 | candidates per photo tagging call (stage 2p) |
| PHOTO_TAG_WORKERS | 3 | photo tagging calls at once (stage 2p) |
| PHOTO_TAG_BUDGET_S | 6.0 | seconds allowed for the photo tagging step; candidates not done keep their title tags and the trace says so (stage 2p; judgment) |
| PHOTO_CACHE_MAX | 200 | prepared thumbnails kept in memory per service, shared by the fingerprint and photo tagging steps (stage 2p) |
| THIN_VARIANT_SEARCHES | 2 | variant searches a typed search may add when fewer than 24 results survive, inside a Shop Again's normal Serper cap (stage 2p) |
| MODEL_QUERIES | 2 | model-written search queries per Shop Again (stage 2i) |
| QUERY_MAX_WORDS | 8 | words in a model-written query (stage 2i) |
| QUERY_LIKED_TITLES | 6 | liked titles shown to the query writer (stage 2i) |
| QUERY_DISLIKED_TITLES | 4 | disliked titles shown to the query writer (stage 2i) |
| BASE_PAGE_MAX | 2 | the base query is paged no deeper than this; page 3 only as a last resort, page 4 never (stage 2i) |
| COLOR_SUGGESTED_MIN_SHARE | 0.40 | a batch takes at least this share from the suggested colours (a colour that complements the room the request names), where the pool has them (stage 2k; judgment, not measured) |
| COLOR_AVOID_MAX_SHARE | 0.15 | and at most this share from the colours to avoid because the product would blend in or clash (stage 2k) |
| REFINE_FOCUS_MIN_SHARE | 0.50 | a "focus on" refine chip takes at least this share of a batch where the pool has it (stage 2l; judgment) |
| NEAR_FP_DISTANCE | 6 | the most two 64-bit difference hashes may differ and still be one image (stage 2n; judgment) |
| NEAR_FP_PRICE | 0.40 | prices within this share of each other, for an image match (stage 2n) |
| NEAR_TITLE_JACCARD | 0.8 | share of words two normalized titles must share to be one product (stage 2n) |
| NEAR_TITLE_PRICE | 0.25 | prices within this share of each other, for a title match (stage 2n) |
| NEAR_FP_TOP | 48 | best ranked candidates whose thumbnails are fingerprinted live (stage 2n) |
| NEAR_FP_WORKERS | 10 | parallel fingerprint fetches (stage 2n) |
| OPEN_UI_TIMEOUT_S | 12 | seconds the page waits for a store link before it shows the Google product page instead (stage 2o; judgment) |
| NEAR_FP_BUDGET_S | 2.0 | seconds allowed for the whole fingerprint step; the rest fall back to titles (stage 2n) |
| REFINE_MAX_PER_SUBMIT | 5 | interpretations kept from one refine instruction (stage 2l) |
| REFINE_MAX_CHIPS | 12 | active refine chips in a topic (stage 2l) |
| FREE_NOTES_MAX | 3 | active free-note refinements; each one is a match flag per item in the tagging calls (stage 2p) |
| NOTE_MAX_CHARS | 120 | characters in a free note's phrase (stage 2p) |
| REFINE_MAX_CHARS | 200 | characters in one refine instruction (stage 2l) |
| TIER_BATCH_SHARE | 0.50 | most of a batch the affinity tier may take, 0.80 when pinned (stage 2f) |
| MAX_PEER_SEARCHES | 3 | peer retailer searches per Shop Again (stage 2f) |
| QUALITY_CUE_MIN_SHARE | 0.50 | share of loved and liked items showing a quality cue |
| QUALITY_CUE_MIN_ITEMS | 2 | items showing a quality cue |
| PRICE_UPPER_PERCENTILE | 0.67 | loved prices at or above this position: an upper cluster |
| PRICE_LOWER_PERCENTILE | 0.33 | the matching lower edge |
| PRICE_MIXED_LIKED_SPAN | 0.6667 | share of the shown price range the liked prices must span for mixed |
| MIN_SHOWN_PRICES | 8 | fewest shown prices before any price signal |
| RESULT_COUNT | 24 | results in a batch (the base of the share caps) |
| DIVERSITY_MAX_SHARE | 0.60 | most of a batch one value of shape or of an open dimension may take (a converged value: 0.80; a dimension the typed request names: no cap) |
| SELLER_MAX_SHARE | 0.50 | most of a batch an affinity seller may take |
| SELLER_PINNED_SHARE | 0.80 | most of a batch a pinned seller may take |
| SELLER_CAP_SHARE | 0.30 | one ordinary seller's share of a batch; the cap is clamp(floor(0.30 x batch size), 2, 4) (stage 2p; judgment) |
| SELLER_CAP_MIN | 2 | fewest results per seller the cap formula allows (stage 2p) |
| SELLER_CAP_MAX | 4 | most results per seller the cap formula allows; the old fixed cap (stage 2p) |
| MINORITY_POOL_SHARE | 0.40 | an open dimension's value under this share of the pool triggers targeted searches for it (stage 2e) |
| OPEN_STRATUM_FLOOR | 0.25 | share of the batch slots a value with a loved item gets when shape is open (stage 2e) |
| CANDIDATE_TAG_BATCH | 40 | titles per candidate tagging call (stage 2e: 100; stage 2g: 40, run concurrently) |

Other constants that shape a Shop Again (2b, unchanged): 4 Serper calls, a pool target of 60 and a ranker
pool of 80, 16 thumbnails (6 loved, 6 liked, 4 disliked), 10 Shop Agains per session.

**Limits.** The lexicon is small and plain: a title it cannot read is unknown, and a free-text value the
model gives that the lexicon does not know has no exposure baseline (it passes the lift test untested).
Size class has no baseline. "Converged on a frame colour" groups dark and brass metal into one value
because the owner's loved items had one of each; that is a judgement call in the lexicon, not a finding.
The guards limit titles the lexicon can read; an item it cannot read passes. Cost per Shop Again is
unchanged: up to 4 Serper calls (8 credits), one preference call (about 1,000 more output tokens for the
per-item tags) and one rank call, an Anthropic estimate of about $0.002 to $0.003 (assumed token counts,
not measured). Not verified live: how well the model tags real photos, whether the lexicon's baseline is
close enough on real result pages, and whether the thresholds feel right on the owner's ratings.

## Amendment, stage 2d (2026-10-24): coverage, honest fallback, and a decision trace

**What failed (observed live with 2c).** The preference call hit max_tokens, so the profile was decided
from titles alone; the title-only lexicon then converged shape on "round" from four keyword titles while
four rectangular titles were silent, and "industrial" from two titles. The price band read $450 to $1,950
though the liked items cost $143.99 to $799; no seller chip showed though 8 of 10 liked items were West
Elm; a "could not keep material mixed" warning fired though the request said wood and metal.

**Changes.**
- *Call robustness.* max_tokens comes from the schema and the item count (twice the estimate, 3,000 to
  12,000), the answer is asked for compactly, and a failed call is retried once as two split calls (loved
  and liked, then disliked). The response and footer say items tagged N of M, photos used, and whether a
  retry or the fallback happened.
- *Honest fallback.* With no model tags, only the seller affinity, the price classification and what the
  comments state are decided. Shape, material, finish, frame and style never converge from titles alone.
  The page says "Could not read item details; only seller and price were learned."
- *Coverage.* Shares are over all like mass, so unknown items count against every value. A dimension
  converges only if known values cover at least 70% of that mass, then the 70% share, two items and lift
  1.3 apply. Open uses the same shares. The exposure baseline counts known values only, and a dimension
  with under 70% known coverage in what was shown skips the lift test and cannot converge.
- *Price.* A band must hold at least 80% of the liked mass (loved weighted) and 80% of the liked items, or
  price is mixed (a neutral chip with the loved and liked ranges, no pressure). The typed cap is untouched.
- *Guards.* The diversity guard covers shape and any open dimension at 60%; a converged value is capped at
  80%; material is no longer always guarded; a dimension the typed request names is exempt and gets no
  warning. Seller caps are unchanged. Warnings give the real share and the limit.
- *Trace.* "How I decided" (closed by default) lists, per dimension, the like mass by value, known
  coverage, exposure share by value, lift, the decision and why it did not converge; for seller and price,
  the numbers and the rule that fired. The same trace is in the Shop Again response and the session file.

**Replay.** The owner's recorded ratings are a fixture (`tests/fixtures/shop/owner_session_replay.json`)
run offline: shape open (57/43), material not converged (lift 1.286, and the request names it), price
mixed (a $450 to $800 band would hold 86% of the like mass but 75% of the liked items), West Elm affinity
chip (86% of the like mass, 50% of what was shown, lift 1.714), no "industrial" or "round" like. In
fallback the same session decides only seller and price.

**Limits.** Dimensions with no lexicon (frame or legs, size class) cannot converge. The 80% price rule on
both mass and items is a judgement call. Not verified live: model tagging on real photos, the lexicon
against real pages, and the thresholds on real ratings. A retry adds the failed call and two split calls
(still under about $0.01 of Anthropic cost by the estimate).

## Amendment, stage 2e (2026-10-24): the model tags what was shown; the pool is searched for the minority

**What failed (observed live with 2d, the owner's second session; tagging of the rated items worked,
11 of 11).**
- *Lexicon coverage.* The title keyword baseline for "what was shown" covered shape 46%, finish 40%,
  frame 8% and legs and size 0%, so the lift test could not run and almost nothing could converge. Warm
  wood held 75% of the like mass at lift 1.78 and was blocked only by coverage.
- *Synonym splits.* "metal base" and "metal frame" were two frame values, and "warm wood", "brown wood" and
  "rustic brown" were three finishes, so a real pattern was cut into pieces under the share threshold
  (style modern 69%, frame dark or brass 69%, shape rectangular 69%, all just short of 70%).
- *Seller overreach.* A Home Depot chip appeared with 2 items of 8 (25% of the like mass) and the summary
  said "Looking for more pieces from Home Depot."
- *Supply.* The batch was 91% rectangular though shape was open and the cap is 60%: the pool had too few
  round tables, and a guard can only reorder a pool.
- Size class said "medium" for everything.

**Changes.**
- *Candidate tagging by the model.* One small-model call (`shop_candidate_tags` v1, the rank model, titles,
  price and seller only, no image) tags every shown result and every candidate in the pool for shape,
  material, finish, frame color and style, at most 100 titles per call. Rows come back as text
  (`n|shape|material|finish|frame|style`) at about 14 tokens each, not a JSON object per candidate. Tags
  are cached by result id in the session file (`tag_cache`), so a result is never tagged twice. These
  tags are the exposure baseline for the lift test, the diversity guard, the stratified selection and the
  composition report. The keyword reader (`vocab.title_tags`) remains only as the fallback when the tagging
  call fails; then the baseline is marked "keywords" and no attribute converges (open can still be shown,
  seller and price are decided as before), and the page says the results could not be tagged.
- *Fixed vocabularies, one table* (`src/app/shop/vocab.py`, passed into both prompts; `shop_prefs` is v5,
  v1 to v4 kept): shape (round, oval, rectangular, square, other); material (wood, metal, glass, stone,
  upholstered, other); finish (light wood, warm wood, dark wood, painted, metallic, glass, other); frame
  color (black, brass or gold, silver or chrome, natural wood, white, other); style (modern, mid-century,
  industrial, farmhouse, traditional, boho, glam, other). A normalization map folds free text into them
  ("walnut", "brown wood" and "rustic brown" are warm wood; "black or brass metal" is black, the colour named
  first; a frame described only as metal is `other` for every wording). A phrase the map does not know is
  `other`: read, but never a value to learn, so `other` cannot converge or be open. Size class and legs are
  no longer asked for or decided; old session files still load.
- *Per-dimension thresholds.* Shape converges at 80% of the like mass; material, finish, frame color and
  style at 60% with two supporting items; known coverage 60% for every dimension, of the like mass and of
  what was shown; lift 1.3 unchanged, now on the model's tags of what was shown. Open is unchanged (30% for
  two or more values). A dislike needs the same per-dimension share of the disliked items.
- *Seller.* An affinity needs 2 loved or liked items AND 40% of the like mass, no dislike from the seller,
  and the lift. The summary only says "more pieces from <seller>" for a chip that passed. The trace lists
  every seller's like share and every rule it failed.
- *Supply.* When a dimension that has search words (shape first) is open and one of its liked values is
  under 40% of the tagged pool, targeted searches for that value run ("round coffee table ..."), their new
  candidates are tagged, and this repeats until the value reaches 40% or the call cap of
  `APP_SHOP_AGAIN_MAX_SERPER_CALLS_OPEN` (default 6; the normal cap stays 4) is reached. A phrasing that finds
  nothing new is not asked again. The extra credits are in the footer. The supplied candidates are kept
  first when the pool is cut to 80. Only after this may the response say "could not keep shape mixed",
  with the real counts.
- *Stratified selection.* When shape is open the 24 slots are allocated to each open value in proportion
  to its like mass, within the 60% open cap and with a floor of 25% (6 slots) for a value with a loved item;
  each value's best ranked candidates fill its slots, the rest fills in ranked order, and the batch is
  returned in ranked order. Every cap holds in both passes: converged 80%, open 60%, the seller caps, and
  a dimension the typed request names is exempt. The guard stays as a final safety net.
- *Trace.* "How I decided" shows the vocabulary tags of the rated items, the shown-result coverage, the
  pool by shape and finish and seller before and after supply, the planned slots, the final batch by shape,
  finish and seller with counts, the searches used and each seller's like share. The same trace is in the
  response and the session file.

**Replay.** The first session (`owner_session_replay.json`) still gives shape open, price mixed and the
West Elm affinity (86% of the like mass, lift 1.714), nothing from keyword guesses. The second
(`owner_session_replay_2.json`, an authored 26-title shown list around the owner's nine ratings) gives
shape open (rectangular 69%, round 31%), no Home Depot chip (25% of the like mass, under 40%), a warm wood
finish (81% of the like mass, lift 1.625), no material (wood lift 1.25), no frame color (black 44%, and
only 29% of shown titles tagged for it), price mixed.

**Limits.** The mock candidate tagger can only read what a title's keywords say, so its coverage is the
keyword coverage; a real model should read more, which is what the 60% coverage floor now assumes and
nothing has measured. The thresholds are starting points. Candidate tagging adds one call (up to 100
titles) for the shown results the first time (later only for results never tagged) and one for the pool
each Shop Again, plus one small call per supply step. At about 14 output tokens and 40 input tokens a row
on the default model (Haiku 5.5, $0.10 and $0.50 per million tokens) that is about $0.001 to $0.002 per 100
titles, inside the target of $0.001 to $0.003 per Shop Again; on Haiku 4.5 prices ($1 and $5) it would be
ten times that. The mock's token counts are assumed, not measured. Not verified live.

## Amendment, stage 2f (2026-10-24): diagnose tagging, nets for dislikes, retailers as a signal, style families

**What failed (observed live with 2e, the owner's third session; 9 of 9 rated items tagged with photos,
48 of 48 shown results tagged).**
- *Candidate tagging failed* ("candidates tagged 0 of 72 in 2 calls") and the trace did not say why. With no
  pool tags, 44 of 72 candidates had an unknown shape, the lift tests could not run, the supply step could not
  act and "matches your preferences" read 0 of 24.
- *The wrong kinds of retailers.* The owner loved or liked West Elm (4 items, 67% of the like mass, lift 3.6),
  World Market and Lowe's. The pool was Lowe's (12), Walmart sellers (about 20), Wayfair (10), Home Depot (3);
  nothing from Article, CB2, Crate and Barrel or Pottery Barn. Only 2 searches ran.
- *The West Elm chip was blocked* by "no dislike from that seller" (4 likes, 1 dislike), so no seller-targeted
  search ran.
- *Style split* across industrial, mid-century, glam and modern, which are one modern family, because a style
  was a single value per item.
- *"0 of 24"* counted only inferred likes and showed 0 when nothing was decided.

**Changes.**
- *Diagnosable, tolerant candidate tagging.* Each call is logged (items, token limit, stop reason, whether the
  answer was valid JSON, rows read, the first 200 characters of the raw answer) in the response, the session
  file and the trace. The parser reads a JSON answer, a truncated one, a code fence, an introduction plus a
  list, list markers, quotes, any case, "key: value" values, fewer or more than five values and rows in any
  order (matched by their leading number). A call that ends with `max_tokens` (its partial last row dropped)
  or reads under 80% of its rows is retried once as two calls of half the size, only for what is still
  untagged. The token limit is twice the estimate (16 tokens a row). A failure shows its reason on the page:
  "Candidate tagging failed: the answer was cut off at the 928-token limit after 0 of 24 rows." `llm.complete`
  gained `keep_truncated` (returns a partial answer with its stop reason). The cause of the live failure is
  not known; the log will show it.
- *Net dislike rule.* A seller (or tier) needs liked weighted mass (loved 3, liked 1) at least
  `SELLER_NET_DISLIKE_RATIO` (3) times its disliked items; the other conditions stay. 4 likes against 1
  dislike passes, 3 to 1 passes exactly, 2 to 2 and 5 to 2 do not. The dislike count is on the chip tooltip.
  Seller names are canonicalized ("Walmart - JerFit Life" is Walmart; "Crate and Barrel" is "Crate & Barrel").
- *Retailer tiers and peers* (`src/app/shop/retailers.py`, see "The retailer table" below). A tier with at
  least 50% of the like mass, 2 loved or liked items and the net rule gets a "retailers like West Elm" chip
  (deletable, pinnable). **My decision:** only design tiers can earn it (design premium and design
  mainstream); a marketplace and budget tier never does, because liking a Lowe's table is about the table, and
  the owner's own second session would otherwise have produced "retailers like Home Depot". When a seller or
  tier affinity exists a Shop Again searches up to 3 peers (one page each, "Article wood and metal coffee
  table with storage"), taken in order from peers not yet searched in this session (kept in the session
  file); later Shop Agains rotate on; a peer that found nothing new is never searched again, and once every
  peer has been searched one that found something is asked for its next page. The total Serper cap is 9
  (`APP_SHOP_AGAIN_MAX_SERPER_CALLS_TOTAL`, default 9; it replaces the 2e `..._OPEN` cap): up to 4 phrasings,
  3 peers, then the supply step. New candidates are tagged like the rest.
- *Batch allocation.* Results from the affinity tier may take up to 50% of a batch (80% if the tier chip is
  pinned; reserved up to the tier's like share). The seller caps still apply (4, 50% for an affinity seller,
  80% pinned). The tier pass runs first and respects the open shape plan, then the shape strata and the rest
  fill in ranked order; the batch is returned in ranked order.
- *Style as a family.* Per-item style is up to two values (prompts `shop_prefs` v6 and `shop_candidate_tags`
  v2, older versions kept). Decisions use two families: modern-leaning (modern, mid-century, industrial, glam,
  scandinavian) and traditional-leaning (traditional, farmhouse, boho, rustic). Converged at the soft 60%
  share on a family, open needs two families at 30%; leaves appear only in the trace and tooltips, and an
  item naming both families counts in both.
- *Alignment.* "Matches your preferences" is measured against every decided signal (converged likes, seller
  and tier affinity, a price band, the shopper's own stated likes) and reads "n/a (nothing decided yet)" when
  there are none. The footer also shows the affinity tier's share of the batch and the number of distinct
  sellers.

**The retailer table.** `src/app/shop/retailers.py` holds `RETAILERS`, rows of `Retailer(name, tier,
search_name, peers, aliases)`. It is owner-editable configuration and it is judgment, not data: check it and
change it. To add a retailer add a row (a canonical name, a tier from `TIERS`, the words to search with, peers
by canonical name, most similar first, and other spellings as aliases); to change a peer order edit the
`peers` tuple; a retailer not in the table has no tier and no peers. Design premium: West Elm, CB2, Crate &
Barrel, Pottery Barn, Article, Room & Board, Design Within Reach, Anthropologie, Rejuvenation, Joybird,
Burrow, Blu Dot, Herman Miller, RH, Williams Sonoma Home. Design mainstream: AllModern, World Market, Target,
Wayfair, IKEA. Marketplace and budget: Walmart, Amazon, Lowe's, Home Depot, Overstock, Temu, AliExpress,
Wish. `AFFINITY_TIERS` (the two design tiers) says which tiers can earn a chip and peer searches.

**Replays.** Replay 1 (West Elm) keeps shape open, price mixed and the West Elm chip, and gains the tier chip
and 3 peer searches (its summary now says "from West Elm and similar retailers"). Replay 2 (Home Depot) keeps
no seller or tier chip (the budget tier never earns one) and its warm wood finish. Replay 3
(`owner_session_replay_3.json`, an authored 48-title shown list and 72-candidate pool): West Elm seller chip
(8 of 12 like mass, 8 against 1 dislike, lift 3.56), tier chip "retailers like West Elm", style one converged
modern-leaning family (leaves industrial, glam, mid-century, modern), shape open (67/33), price mixed,
material and frame color not decided (coverage), warm wood finish just under the lift test (1.295); peers
Article, CB2 and Crate & Barrel found 8 new candidates each; the supply step ran for round; the batch holds 14
rectangular, 10 round, 12 design premium.

**Limits.** The retailer table and the 50% and 3:1 numbers are judgment. The mock candidate tagger reads only
title keywords and the mock peer catalogues are synthetic, so how many new candidates real peer searches find,
and whether a real model's tags reach 60% coverage, are not measured. A worst-case Shop Again is 9 Serper
calls (18 credits, $0.018) plus about $0.002 to $0.004 of Anthropic calls including tagging.

## Amendment, stage 2g (2026-10-24): bounded, reliable, fast, and no duplicates

**What failed (observed live with 2f, the owner's fourth session).**
- Shop Again took 98.9 seconds. 10 candidate-tagging calls ran in 3 rounds; 8 failed with "response has no
  text content" (stop reason none, 0 rows) and one (50 items, limit 1760 tokens) hit max_tokens after 18
  rows that were mostly "-". Every failure triggered a half-size retry, so failures cascaded. 33 of 265
  candidates were tagged; the failed calls still cost money (Anthropic $0.0156 against about $0.004).
- All 265 candidates, from 7 sequential Serper searches, were sent for tagging though 24 are shown.
- Duplicated results were seen (the same product twice in a batch, or something already shown or rated
  coming back), under differing product ids, seller spellings ("west elm", "Room&Board", "Joybird Furniture",
  marketplace sub-sellers) and several listings of one product.
- A long paragraph of diagnostics above the grid, and only 17 of 24 results with no explanation.

**Budgets (all in `Config`, all `APP_*` variables).**

| Setting | Default | Meaning |
|---|---|---|
| `APP_SHOP_TARGET_S` | 30 | what a Shop Again aims for (shown in the trace) |
| `APP_SHOP_HARD_STOP_S` | 45 | past this every remaining optional step is skipped and the response says so |
| `APP_SHOP_TAG_BUDGET_S` | 15 | all candidate tagging together; what is tagged by then is used, the rest stays unknown |
| `APP_SHOP_TAG_CALL_TIMEOUT_S` | 12 | one tagging request (shortened to the time left; no SDK retries) |
| `APP_SHOP_TAG_TOP_N` | 60 | candidates tagged by the model: the top N untagged by rank |
| `APP_SHOP_SEARCH_WORKERS` | 4 | concurrent Serper searches (1 to 8) |
| `APP_SHOP_AGAIN_MAX_SERPER_CALLS_TOTAL` | 9 | unchanged (phrasings, peers, supply and refill) |
| (code) `shop_max_steps` | 40 | model calls one Shop Again may make (the global default of 12 is too few for tagging batches) |

**Changes.**
- *Candidate tagging is a forced tool call.* `record_tags` with a strict JSON schema (an array of objects:
  index, shape, material, finish, frame_color, style as a list of up to two values), through the existing
  `llm.complete` helper (new: `tool_choice`, `temperature`, a per-request `timeout_s`, and `stop_reason`,
  `block_types` and the tool input on the result; an error now carries its stop reason, block types and
  cost). Prompt `shop_candidate_tags` v3 (v1 and v2 kept: v2 is the text-rows format). Temperature 0, no
  `effort`, no thinking. The text-rows parser stays as the fallback for an older or text answer and as the
  one retry format. Batches of at most 40 titles run concurrently (a thread pool; results are discarded if
  the budget has ended). At most ONE retry per batch (for what is still untagged; the rows format when the
  tool call errored or had no text), none for a call with no text content in under 2 seconds, none past
  the deadline. Every call's outcome is logged, with the block types and stop reason when there is no text.
  Failed calls are counted in the cost (`failed_usd`).
- *Tag only what matters.* Order of a Shop Again: searches, hard filters and de-duplication, the pool cut to
  80, ONE rank call, then the model tags the top 60 untagged candidates by rank. The title keyword reader
  stands in for the rest. The diversity guard, the supply step and the stratified selection act on any
  candidate that carries a tag, model or keyword (they cannot act on a candidate with none); the trace says
  how many of the pool and of the batch came from each. Supplied candidates are appended in search order (no
  second rank call) and tagged.
- *Speed.* The Serper searches of a Shop Again run concurrently (first pages of every phrasing and every peer
  at once, then further pages and the supply search one after another), only when the provider says it is
  thread safe. The preference call, the tagging of what was shown and the typed query's first page run at the
  same time. Phase timings (searches, preferences, tagging, rank, total) are in the footer and the trace.
- *Jobs.* `POST /api/search` and `POST /api/shop-again` validate, start a background job and answer 202 with
  `{"job_id"}`; `GET /api/job/<id>?session=<session id>` returns `{state, phase}` and, when done, the same
  view as before (or `{status, error}`). The page polls every second and shows the phase under the spinner.
  Ids are 128 random bits; a job started for a session is readable only with that session id (otherwise 404,
  as for an unknown id); one job runs per session and 8 in all; a finished job is forgotten after 10 minutes.
  Validation, the CSP, the rate and session limits are unchanged (checked before the job starts).
- *One de-duplication* (`app.shop.dedupe`), used for typed searches, Shop Again candidates, the seen check and
  the final batch. Duplicate = same product id, or same thumbnail, or same normalized title (lowercase,
  punctuation, sizes and plain colour words removed; the brand and a finish such as walnut stay) with a price
  within 1% whatever the seller, or same normalized title from the same seller. The best listing is kept (one
  with a retailer link, then the lowest price). Anything shown or rated earlier always wins, counted apart
  (shown before, rated before); the trace has the count each rule removed. Seller names have one
  canonicalization (`retailers.seller_key`: case, punctuation, "&" and "and", ".com", " - Seller" and
  " Furniture" suffixes), used by the retailer table, pins, caps and the rules. A pin stored with the old key
  of a name containing "&" no longer matches.
- *Fewer than 24.* One sentence says why ("81 seen or rated, 63 duplicates, 22 filtered out") and the trace
  adds the seller-cap and guard counts. One refill search (the converged value, else the first open value,
  with its search words) runs before giving up, inside the call cap and the time budget.
- *Banner.* The line above the grid is one short sentence ("24 new results."; why fewer; the tagging failure
  reason only when tagging failed; "Stopped early before ..." after a hard stop) and a "details" link that
  opens "How I decided". Every warning is listed in that panel.

**How to read the diagnostic.** `make shop-diagnose` (live, about $0.003) sends the candidate tagging call
three ways on the newest session's titles: A text rows with 100 titles (the old call), B text rows with 40,
C the forced tool call with 40. For each: the model id, `max_tokens`, latency, the outcome (ok or the
exception class), the content block types, the stop reason, usage, rows parsed and the first 300
characters. Read it like this: A and B failing with `block types: (none)` and `stop reason: end_turn` or
nothing is the "no text content" failure and says it is not the batch size; B fine and A failing says the
batch size is the problem (stay at 40); C fine with `block types: tool_use` and `stop reason: tool_use`
says the tool call is the reliable form; C failing with an HTTP 400 class means the tool schema or
`strict` was rejected (the call falls back to the rows format on its one retry, and tagging is then as
reliable as B); `max_tokens` as the stop reason says the limit is too low for that format; a latency near the
12 second timeout says the model is slow, not broken. Nothing in the output is a key or an environment
value.

**Replay 4.** `owner_session_replay_4.json` (46 ratings, a 265-candidate pool with repeated ids, shared
thumbnails, a title at another seller's price, lowercase and "&" and ".com" seller spellings, and copies of
shown and rated items): West Elm seller chip (5 of 12 like mass, no West Elm dislike), no tier chip (22
dislikes in the design premium tier against liked mass 10: under 3 to 1), shape open (round 8 of 12, 67%,
rectangular 33%), warm wood finish converged (83%, lift 1.417), modern-leaning style converged. With tagging
answers forced empty it finishes in about 15 fake seconds with the failure reason in one sentence.

**Limits.** The cause of the live failure ("no text content") is still not known: the tool call and the
diagnostic are the answer to find out. Concurrency is only exercised with fakes; the real Serper and
Anthropic latencies are unmeasured. Where tagging fails the lift tests cannot run, as before. The top-60
rule means candidates ranked below 60 are guarded only by keywords.

### Fix after stage 2g: the `temperature` TypeError (`make shop-diagnose`)

The first live `make shop-diagnose` printed "FAILED before a response: TypeError" for all three calls. Cause:
stage 2g passed `temperature=0` to `messages.stream()`, and the installed SDK (anthropic 1.8.0) has no such
parameter, so the SDK raised `TypeError: Messages.stream() got an unexpected keyword argument 'temperature'`
at `src/app/llm.py` (`_create_message`) before any request was sent. In a Shop Again every tagging call died
the same way (and the TypeError escaped the tagging handler, which only caught API and output errors).

Changes: `temperature` is gone from `llm.complete` and the tagging call; a client-side exception in a tagging
call is now recorded in the call log (class, redacted message of at most 300 characters, and the repository
`file:line` where it was raised) and the Shop Again goes on without tags (a budget error still propagates);
`tests/test_shop_sdk_shapes.py` runs every Shop Mode call shape (parse, rank, preferences, candidate tags in
the three diagnostic shapes) through the real SDK client over an httpx `MockTransport` and checks the JSON
body that would be sent.

Fields deliberately NOT sent: `temperature` (the SDK has no such argument; the newest models take no
sampling parameters), `thinking` (no extended thinking for tagging) and `output_config.effort` on the tagging
calls. Fields sent that the SDK documents but the API's support for the model is unverified: `strict` on the
`record_tags` tool, and `tool_choice`; if the API rejects the tool call (an HTTP 400) the one retry sends the
rows format instead, which sends neither.

`make shop-diagnose` now prints, for a failure before a response: `exception:` (the class and the message,
at most 300 characters, anything that looks like a key, token or Authorization value replaced by
`[redacted]`) and `raised at:` (the innermost frame in `src/` or `scripts/`); for a call that failed after
the request, `outcome: FAILED: <class and message>` and the same `raised at:` line. It never prints headers,
the environment or a request body.

### Fix after the live diagnostic: the model thinks by default (`make shop-diagnose`)

**What the diagnostic found (claude-haiku-5-5).** The rows call (structured output, no tool) came back with
block types "thinking, text": with `max_tokens` 1480 the thinking used the whole budget (stop reason
`max_tokens`, no text, usage reported as 0 tokens); with 3400 the thinking plus the answer hit the limit
(97 of 100 rows parsed). The forced `record_tags` tool call returned only a `tool_use` block, 40 of 40
items in 7.3 s, with no thinking. So the earlier "no text content" failures were most likely thinking
that spent the budget, and every Shop Mode call could have been paying for thinking.

**The thinking rule.** What the installed SDK (anthropic 1.8.0) documents: `thinking` is a field of
`messages.create` and `messages.stream` taking `{"type": "disabled"}`, `{"type": "enabled",
"budget_tokens": n}` or `{"type": "adaptive"}` (with an optional `display`); `output_config.effort` takes
`low`, `medium`, `high`, `xhigh` or `max`; a response may report `usage.output_tokens_details.thinking_tokens`.
The SDK does not say which model ids accept which: that is the API's, so a refusal is handled (below).
Every Shop Mode model call that returns structured data (parse, rank, preferences, candidate tagging) now
sends `thinking={"type": "disabled"}` through the one wrapper (`llm.complete(disable_thinking=True)`); the
calls that already sent `effort` low keep it. If the API refuses the field for a model (an HTTP 400 that
names thinking) the request is repeated once without it and later calls to that model omit it for the life
of the process. A response that is only thinking blocks is its own error: "the model used its whole budget
thinking (stop reason ..., max_tokens N)", with the thinking tokens when the API reports them, instead of
"no text content". The log of a call says whether a thinking block was present.

**Candidate tagging.** The forced tool call is the primary and the only format (the rows format is no longer
a retry; its parser stays for old logs and tests, and the diagnostic still runs it as variants A and B for
comparison). One retry round per batch remains: the same tool call for what is still untagged, in pieces of
half the batch (40 becomes 20), none when a call came back with no text in under 2 seconds, none past the
deadline.

**Usage of a failed call.** A response that reports no tokens at all (or zero) is a failed call that is
still billed. Its usage is recorded as unknown, its cost is estimated for the budget from the request size
(about 3.5 characters a token) and the WHOLE `max_tokens` as output, and it is labelled "estimated (failed
call)" in the footer ("failed tagging calls $X (estimated, failed call)"), the trace ("usage unknown (cost
estimated, failed call)") and the diagnostic. It is never booked as $0.

**The diagnostic** (`make shop-diagnose`) now prints for each call the block types, whether a thinking
block was present (and the thinking tokens when reported), usage or "unknown" with the estimated cost, and
for each call that parsed rows a "fields filled" count per field (shape, material, color_finish,
frame_color, style: how much a title-only tagger can read; `other` and null do not count). To read it: a
variant with `thinking: yes` and `stop reason: max_tokens` is the thinking-budget failure; with the field
disabled none of the three should show thinking; `fields filled` far below the batch size for a field says
titles rarely show it (frame_color is the usual one).


## Amendment, stage 2h (2026-10-25): prepare while the shopper browses, a shorter live path, a step bar

A live Shop Again took 34.6 s: preferences 8.5 s, tagging what was shown 4.4 s (from titles only, so the
lift test could not run for shape, finish, frame color or style), tagging 140 candidates 15.9 s (6 calls)
and rank 4.4 s. Nothing here changes a threshold or the decision rules; this amendment moves model work
earlier, runs the rest in parallel, and shows where the time goes.

**Background preparation (`app.shop.prep`).** When a batch is stored (a search or a Shop Again), a
background job tags every item from its title AND its thumbnail with prompt `shop_candidate_tags` v4
(v1 to v3 stay on disk): the same forced `record_tags` call (strict schema, thinking disabled), with the
photo fetched by the guarded fetcher (https only, the same size limits, EXIF stripped). Two calls of about
12 items for a batch of 24. Results are cached in the session file by result id with a source:
`photo` or `title`. A photo tag replaces a title or keyword tag and is never replaced by one; an item
already tagged from a photo is skipped. Each rating that has a comment is read as soon as it is written
(the preference call restricted to that one item) and cached under `item id | sha1(comment)[:12]`, so a
changed or deleted comment is a different key and the old reading is ignored (and purged). Limits: at most
4 model calls at once across ALL background jobs (a shared pool), 4 calls for one stored batch, a session's
preparation stops at `APP_SHOP_PREP_MAX_USD` (default $0.05), Start over cancels what is queued and
discards what is running (`POST /api/reset`), and every write is one transaction under the store's lock.
A failure is recorded (redacted, URLs included) and never raised. `APP_SHOP_PREP=0` turns it off.

**What the preferences step reuses.** `cached_readings` finds the rated items covered by a photo tag (or a
comment reading that carries tags) and, if they have a comment, a reading of that exact comment. If every
rated item is covered, the preference call is skipped: the answer is built from the cache (tags, likes,
dislikes, query adjustments from the comment readings, quality cues from the photo tags, contradicted
guesses computed in code from the tags). If some are missing, only those go to the model and the two parts
are merged. The trace says exactly what happened: "N of M rated items reused from the background
preparation; K sent to the model now (or: so no preferences call was needed)".

**The wait and the fallback.** A Shop Again waits for preparation still running for its session for at most
`APP_SHOP_PREP_WAIT_S` (default 8 s; a bounded timed wait on a condition, ending the moment the last task
does), showing "Finishing your last ratings (n of m)". After that it proceeds with the live path for
whatever is not cached. Preparation can never block it forever or make it fail.

**Cost.** Preparation spend is recorded per session (`prep.usd`), added to the session total and shown as
its own "background preparation $x" in the footer. It is the one place cost rises: every shown item is
tagged once with a photo, whether or not a Shop Again follows. It is estimated at under $0.005 a batch of 24
with Haiku, UNMEASURED live; `make shop-live` shows the real number.

**Live path.** (1) Each search's candidates are queued for tagging the moment that search returns, in
batches of at most 40, up to `APP_SHOP_TAG_WORKERS` (default 6) at once, while the other searches and the
rank call run. Page 1 of the original request starts with the job; searches that depend on learned
preferences start as soon as the chips are known. (2) The cause of "6 calls took 15.9 s against 6.5 s for
one": tagging shared the search worker count (4), so six batches ran in two waves of about 6.5 s, and
tagging began only after every search had finished. (3) Why 140 candidates were tagged with
`APP_SHOP_TAG_TOP_N` 60: the cap was applied once per step (initial, each supply search, refill) and again
for the shown items, not once per Shop Again. Now `TagPipeline` holds ONE cap for the run: the first-come
work may use the cap minus a 12-title reserve, supply and refill candidates use the rest, and the reserve
is spent on the next-ranked candidates when no supply is needed. A product id is never tagged twice
(cached tags are reused; a photo tag is only requested when preparation had none). The tradeoff: tagging
starts before the rank call, so the first-come titles are not exactly the top by rank; the reserve is
rank-ordered. (4) The trace carries a timeline: every call (start offset, duration, queue wait, retries)
and every phase (preferences, searching, ranking, tagging). The 30 s target and 45 s hard stop are
unchanged.

**Status bar.** `GET /api/job/<id>` returns `steps` (id, label, state pending, active, done or skipped,
`done_count` of `total`) and `elapsed_s`. Shop Again: "Finishing your last ratings", "Searching (n of m)",
"Tagging (n of m)", "Ranking"; a first search: Searching, Tagging, Ranking. A step that is not needed shows
as done at once. The page draws one segment per step (check on finished steps, an animated active segment
that stops under `prefers-reduced-motion`), the label with counts, an elapsed timer, no percentages and, after
30 s, "Taking longer than usual; this stops at 45 s". `role="progressbar"` with `aria-valuenow` (steps
done) and `aria-valuemax`, and the current step text in the polite live region. Strings go in with
`textContent`; the CSP is unchanged. `APP_SHOP_MOCK_DELAY` (mock only, seconds per mock step, default 0)
slows the mock so `make shop-check` can screenshot the bar mid-run.

**One seller canonicalization.** `retailers.seller_key` (case, punctuation, "&" and "and", "www.", ".com",
sub-seller suffixes) maps "west elm", "West Elm", "westelm.com", "Crate & Barrel", "crateandbarrel.com" and
the other table retailers to one key and one canonical display name; unknown sellers keep a cleaned name and
no tier. `count_sellers` groups by that key and feeds the seller caps, seller chips, peer exclusion,
de-duplication and every trace table, so the "pool after" count covers every candidate in the final batch
(the 2g trace showed Living Spaces at 0 after and 2 in the batch).

**Open.** Live numbers for preparation spend, how often the 8 s wait is used, and the new Shop Again time
are unmeasured. The mock tags photos by title keywords, so quality cues and photo-over-title overrides are
tested with fakes, not with real images.

## Amendment, stage 2i (2026-10-25): tailoring from like rates, leanings, no marketplaces, better searches

Speed was fixed (13.7 s live). The next live Shop Again learned nothing: every rule said "not enough
evidence", so the batch was pages 3 and 4 of the original query, 10 of 22 results came from Walmart and
Lowe's and none from West Elm, Article or CB2, although 7 of the 15 liked items (of 48 rated, a 31% like
rate) came from those three and 1 from Walmart and Lowe's. The premium tier had 7 liked of 12 rated, the
marketplace and budget tier 1 of 11. The seller and tier chips were blocked by "liked mass at least 3 times
disliked mass", which ignores hit rates. All the numbers below are judgment from a handful of live sessions,
not measurements; they are in `thresholds.py` and the table above.

**Group like-rate evidence (`app.shop.evidence`).** One function counts, for any group (a seller, a retailer
tier, or one value of shape, material, finish, frame color or style, at the label level), the items rated,
the items liked (loved or liked, one each), the group's like rate and the baseline like rate over all rated
items in the session. It does not depend on how often the group was shown, so it removes the exposure bias
without the lift test. Tags are the best available: photo, else the title model, else title keywords (the
rest of the decisions still use the model's tags only).

**Seller and tier chips** (replacing "3 times the disliked mass" and "40%/50% of the like mass"): rated at
least 4, liked at least 2, like rate at least 1.5 times the baseline and at least 35%. Marketplace and budget
retailers never earn a positive chip. Up to 3 seller chips (highest like rate first) and one tier chip; the
pin behaviour and the caps are unchanged (tier up to 50% of a batch, a seller 50%, 80% pinned, 4 otherwise).
`SELLER_MIN_LIKE_SHARE`, `SELLER_NET_DISLIKE_RATIO` and `TIER_MIN_LIKE_SHARE` were removed. Consequence worth
knowing: when almost everything was liked (baseline above 67%) no group can be 1.5 times the baseline, so
such a session (the owner's first three real sessions liked 10 of 10, 8 of 9 and 6 of 7) decides no seller
or tier chip; the replay tests dislike extra results to get one.

**Leaning layer.** A soft-dimension value is *leaning toward* with the same test (4 rated, 2 liked, 1.5 times
the baseline, 35%) unless its dimension already converged on it, and *leaning away* with at least 6 rated and
a like rate of at most half the baseline. Toward: a soft preference line for the ranker (`shop_rank` v5, v4
kept) and a soft share of at least 30% of the batch where the pool has the value (a pass after the tier and
the strata, never over a converged cap, skipped for the open dimension that owns the strata). Away: a soft
avoid line and at most 30% of the batch. A leaning is never written to the typed-request chips, never
overrides a hard filter, and is skipped for a value the request typed or the shopper deleted. The converged
and open logic is unchanged. On the evidence run: natural wood frame (7 of 10) leans toward, black frame
(4 of 31) leans away, rectangular and wood earn nothing.

**Marketplace exclusion.** At least 8 marketplace and budget items rated, a like rate at most half the
baseline and at most 20%: an "exclude marketplace retailers" chip (deletable, "liked 1 of 11"). While it
exists, marketplace sellers leave the candidate pool before ranking, tagging and the seller caps; fewer than
12 results shows the banner "Only n results without marketplace retailers. Remove the chip to include
them." and nothing is re-added silently. Deleting it dismisses it (`StoredProfile.dismissed`); it returns
only after 8 more ratings if the evidence still holds. Typed searches are unaffected.

**Seller searches and written queries.** After the preferences step, up to 3 sellers chosen by the evidence
(highest like rate, never a marketplace, not one whose chip was deleted or whose last search found nothing
new) are searched as "<seller> <request search text>", the next page not yet fetched, with no chip needed;
free slots go to peers of the same tier as before. A forced `record_queries` tool call (`shop_queries` v1,
strict schema, thinking disabled, about 300 tokens) writes one or two queries from the typed constraints,
the leanings, the settled values, the liked sellers and up to 6 liked and 4 disliked titles; it runs beside
the first search wave. Code validates each query (category word, every must-have and typed material, at most
8 words, no URL, no price word, no retailer that was not liked, not a repeat) and notes drops in the trace.
Accepted queries run as page-1 searches; the cost is in the footer's Anthropic figure. `APP_SHOP_MODEL_QUERIES=0`
turns it off. The Serper cap stays 9 per Shop Again (one call is always kept for the supply search).

**Page depth.** The base query is paged to page 2; page 3 only as a last resort when the filtered pool is
under a batch plus the reserve (36); page 4 never. The pool target dropped from 60 to 36. After the supply
step, whatever the tagging cap has left tags the next-ranked candidates (the cap of 60 is still one cap per
run).

**Display and trace.** Chip kinds "leaning toward", "leaning away" (dashed outlines) and the exclusion chip;
the footer "matches your preferences" counts converged attributes, seller or tier chips and leanings toward,
says what it counted, and never says "nothing decided yet" when any chip exists; How I decided has a table of
like-rate evidence (every seller, tier and soft value with at least 2 rated: rated, liked, rate, baseline,
verdict, reason), the written queries (accepted and dropped) and the excluded count; card sellers show the
canonical name. Each round keeps a compact pool snapshot (at most 100 items) in the session file, and
`scripts/shop_export_replay.py` turns a session into a redacted replay fixture; `tailoring_evidence.json`
is a synthetic fixture with the numbers above.

**Open.** Unmeasured live: how the batches feel, the cost of the query writer (a fraction of a cent), and
whether 1.5 times, 35% and 30% are right. A like-rate baseline over the session treats an unrated result as
unseen, not as disliked.

## Amendment, stage 2j (2026-10-25): topics

In one session a new search for a different product (couches after coffee tables) kept everything learned
from the earlier ratings. A session now has topics. A topic is identified by the canonical category of the
parsed request (`app.categories.canonical_category`: "couch" and "sofa" are one; a category the table does not
know, such as "curtains", is its own key; a missing category always starts a new topic). Ratings, searches,
comment readings (by their rating) and pool snapshots are stamped with their topic; session files from
before topics migrate on load (everything belongs to the topic of the last typed search; nothing is dropped).

A search of the same category is a refinement: the learned state stays ("Kept what I learned in this session
(same product type)."). Any other search starts a new topic: the profile (chips, leanings, seller and tier
chips, the marketplace exclusion and its dismissal, comment-derived chips), the peer rotation and the refine
chips reset, and the baseline like rate and every evidence table are computed from the new topic's ratings
only. The tag cache and the seen-results memory stay (a result already shown is never shown again). Saved
items stay in Saved with a small "From an earlier search" label and do not count toward the new topic;
re-rating one in the new topic counts it there. A Shop Again or a comment reading that finishes after a new
topic began writes nothing (HTTP 409 for the Shop Again); the page ignores a late answer for an earlier
topic. The notice under the search box says what happened, including when a missing category caused the
reset; How I decided shows the topic, the ratings counted and those left out. Start over still clears all.

## Amendment, stage 2k (2026-10-25): request understanding and colour

"boho curtains that pair well with a dark blue room" searched "boho curtains dark blue": the colour belongs
to the room, not the product. `shop_parse` v2 (v1 kept) separates product fields (search text, category, style,
materials, must-haves, price, `color_terms` for a colour the product itself should be) from `context` items
(room_color, wall_color, existing_item, room_type, room_size, mood, other). When the context names a colour
the parse also gives up to 4 suggested product colours and up to 3 to avoid, from the fixed colour list, with a
one-line reason. Code validates everything: kinds against the fixed list, colours against the vocabulary (no
overlap; suggestions only with a colour-bearing setting and no product colour), the room's colour is stripped
from the search text, and an unknown value becomes an approximate free context item (a soft hint only).
Suggestions are never hard filters. A typed product colour ("dark blue curtains", "navy sofa under $1500")
stays a product attribute and triggers no suggestions.

Colour is a new tagging dimension `color` (white, cream, beige, tan, brown, black, gray, blue, navy_dark_blue,
green, red, rust_terracotta, yellow_mustard, pink, purple, orange, metallic, multicolor_pattern; `other` is a
read value never learned). It is in the forced strict `record_tags` tool (required, nullable; prompt v5, v4
kept; photo for shown items, title for candidates; tokens per item 40 to 48, so a batch of 40 gets 4040 as its
limit) and is a soft dimension in the evidence and decision layers (group like rates, leanings, converged and
open logic; thresholds unchanged). Rated items take their colour from the cached photo or title tags.

The ranker gets the setting, the suggested colours and the colours to avoid as soft guidance (`shop_rank` v6,
v5 kept; never something the product must contain). Selection takes at least 40% of a batch from suggested
colours and at most 15% from colours to avoid where the pool has them (`COLOR_SUGGESTED_MIN_SHARE`,
`COLOR_AVOID_MAX_SHARE`: judgment, not measured), under every cap and filter. The first search runs page 1 of
the plain query and page 1 of a second query with the top two suggested colours at the same time instead of
page 2 (the same two Serper calls; page 2 of the plain query only if the pool is thin: three). Chips for the
setting, each suggestion and each colour to avoid are deletable (`POST /api/context/remove`; deleting the
setting removes the whole colour effect); they belong to the topic. The trace shows how the request was
split, the reasons and how many results came from suggested and avoided colours. The query writer
(`shop_queries` v2, v1 kept) also receives the setting and the suggestions. Parse tokens 600 to 900.

## Amendment, stage 2l (2026-10-25): refine search

A "Refine the next Shop Again" box (200 characters) turns plain instructions into deletable chips that change
every later Shop Again in the topic and outrank everything inferred from ratings. One forced tool call
(`record_refinements`, `shop_refine` v1, strict schema, thinking disabled, 500 tokens, the only tool in that
call) returns at most 5 items of the fixed kinds (exclude_seller, exclude_tier, exclude_value, focus_value,
focus_seller, price_max, price_min, must_have, free_note). Code validates each against the retailer table, the
tiers and the vocabularies; anything else becomes an approximate free note (a soft hint). The instruction is
untrusted: links and angle brackets are stripped, it sits between delimiters, it cannot select anything
outside the fixed kinds, and strength follows the kind. At most 12 chips are active; an opposite instruction
on the same target replaces the old chip and says so.

Effects: exclusions are hard filters on the pool before ranking and the seller caps (seller, tier, and values
by title keywords and then by the model's tags), and no search is spent on them; focus chips take at least 50%
of a batch where supplied (`REFINE_FOCUS_MIN_SHARE`, a pass before the tier pass) and go to the ranker as
must-favor; a seller focus is searched by name; prices replace the typed range (the price chip changes);
must-haves extend the request's; the query writer receives every chip as a hard constraint. An inferred chip
that contradicts a refine chip is suppressed and the trace says so. Fewer than 12 results names the
responsible chips in a banner; nothing excluded is re-added. Chips live in the topic until deleted, a new topic
(the notice says so) or Start over. Text left in the box when Shop Again is pressed is read first as a step
("Reading your refinement"); a refinement that cannot be read adds a warning and the Shop Again goes on. The
call's cost is in the session total and the footer.

## Amendment, stage 2m (2026-10-25): customer reviews and more photos

Ratings: Serper results carry `rating` and `ratingCount` (SerpApi shopping: `rating`, `reviews`); the
`google_product` lookup may carry `product_results.rating`, `reviews` and `media`. A result stores them only
as given (rating in (0, 5], count at least 0), the Open lookup fills a missing one and never overwrites, and
cards and Saved show "stars 4.6 (1,203)" with an accessible label and nothing when absent. Display only: not
in evidence, decisions or caps (the ranker's candidate line has shown the rating since stage 1; unchanged,
nothing new uses it). The trace says how many shown results carried a rating.

More photos: each card with a product id has a button that runs the same SerpApi product lookup as Open. One
lookup per result is cached in the session (`Session.product_cache`, failures too) and counted once against
the shared cap of 12; a gallery after Open, and Open after a gallery, cost nothing more. Up to 6 https images
appear inline with previous and next buttons and the arrow keys; past the cap the button is disabled with the
reason; a failed lookup shows a short message. No CSP change was needed (`img-src https:` already allows the
images); the trace shows lookups used.

## Amendment, stage 2n (2026-10-25): fixes from the first live run

Photo field: SerpApi's `google_product` documents gallery images under `product_results.thumbnails`; stage 2m read
only `media`, so the live gallery was empty. The gallery now reads `thumbnails` first, then `media`, keeping only
distinct https image links in order (6 at most); `reviews_images` (customer photos) is never part of it. The trace
and footer report, per lookup, which response fields held images and how many, so a future mismatch shows at once.
Fewer than 2 usable images are cached like any lookup and the button reads "No more photos available".

Same product from different vendors: two signals, either one enough. (1) Image: a 64-bit difference hash of the
thumbnail, at most 6 bits apart, with prices within 40%. (2) Title: normalized titles (seller names, sizes and
filler words removed) with Jaccard similarity of at least 0.8 and prices within 25%. A group keeps one
representative (a chip seller or tier or design premium first, then a rated one, then the lowest price) and the
card says "Also at: eBay $18.99, Lowe's $21.00" (three, then "and n more"; text only). Hidden members join the
seen memory with the shown results (every topic), do not count toward caps, shares, stats or batch size, and are
traced by reason with at most 5 examples. Fingerprints come from thumbnails the preparation already fetched, and
live for the top candidates (10 in parallel, a 2 second budget, never past the hard stop); a candidate without
one is compared by title only. Fetches are bounded: 500 KB and a 24 megapixel header limit (Pillow was already a
dependency; nothing was added). Thresholds: `NEAR_*` in `app.shop.thresholds`, all judgment values.

Comments about the listing: the preference reader (prompt `shop_prefs` version 7; v6 kept) also labels each item's
comment as taste (null) or one of duplicate, broken_image, wrong_product_type, wrong_item, listing_problem,
other_meta, and whether it also says something about taste. Code checks the label against that fixed list (an
unknown label is ignored). A flagged item makes no chip and its rating is left out of tags, evidence, baselines and
seller rules; the result stays in the seen memory. In a mixed comment ("duplicate and too dark") only the taste part
is read. The trace lists these items under their own heading, "flagged duplicate by you" for duplicates, and says
whether the automatic check would have caught each.

Small: when chips only narrow the search the footer says "matches your preferences: none yet (n chips only narrow
the search)"; the refine box's placeholder is "e.g. no Walmart" with the full examples in its title and label.

## Amendment, stage 2o (2026-10-26): the gallery goes dormant, Open is fixed, store links are resolved ahead

Gallery dormant. "More photos" needs the SerpApi product lookup, which takes a few seconds, and there is no faster
source: Serper results link only to google.com, and fetching retailer pages ourselves is blocked by many retailers,
brittle and a terms risk. So `APP_SHOP_GALLERY` (default 0) hides it: no button, no gallery, no photos state, no
`/api/photos` (it answers 404), and no image wording in the footer or trace. A lookup still parses the rating, the
review count and the retailer link; with the flag off its image lists are dropped before they are cached, so they
are never sent to the page. All gallery code and its tests stay; the tests run with the flag on. What would reopen
it: a faster provider or a paid plan that makes the lookup fast.

Blank tab, the cause. `openResult` opened `about:blank` inside the click and wrote nothing into it, then waited for
`/api/resolve`, which runs the lookup (seconds) with no limit on the page's side. The tab sat empty for the whole
wait; on a failed lookup it was closed without ever showing a page; and a result with an empty or non-https link was
closed too, with the reason only in the status line. (The check reproduced this before the fix: the tab was empty
while the lookup was held, and a failed lookup never reached any page.) The lookup also ran under the session's lock.

Open now. The tab is opened synchronously in the click (so a popup blocker allows it), its opener is cut, and it
shows "Finding the store page..." as text nodes. A store link is accepted only if it is https without credentials;
the tab then goes to it. On failure, on `OPEN_UI_TIMEOUT_S` (12 s, in `thresholds.py`) or with no usable store link
it goes to the item's Google link (https, google.com or a subdomain) and the card says "Could not find the store
page; showing Google's product page." With neither link usable the tab is closed and the card says so. If the
browser blocks the tab (`window.open` returns null) the card says so and, once the link is known, shows one inline
"Go to store" link (rel noopener); no repeated popups. The button reads "Finding store..." and is disabled while a
lookup is in flight (a second click reuses it). A result without a product id, or one whose lookup is cached, opens
at once with no loading page.

Resolve ahead. Loving or liking an item queues a background lookup (never a dislike) when it has a product id, is
not cached and the automatic budget is left: `APP_SHOP_AUTO_LOOKUPS_MAX` (8) of the session's 12, so Open keeps at
least 4; `APP_SHOP_AUTO_LOOKUPS=0` turns it off. At most 2 run at once, at most 16 wait, they never run on the
request's thread and never hold the session lock while the provider answers. One cache serves Open and the automatic
lookups (a per-product lock makes the second wait for the first); a failure is cached, counted once and never
retried. A lookup that finishes after Start over (queued ones are dropped) or a topic change may fill the cache but
writes nothing learned. The footer reads "link lookups n of 12 (auto m, by you k), median s", and the trace lists each
lookup as made ahead or by you with its seconds.

## Amendment, stage 3 (2026-10-26): the Shop Mode eval

What it is. `evals/shop/` holds twelve requests (the owner's list, unchanged), each with a hidden persona of two
to four sentences that does not restate the request. A run searches through the service layer (not the browser),
judges every shown item, has the persona rate the first twelve in display order, applies the case's refine step
("no Walmart" on the coffee tables, "focus on mesh backs" on the desk chairs) and runs Shop Again twice, judging and
rating each batch. Automatic lookups, Open and the gallery are off, so no SerpApi call is made. The pipeline gets the
request, the ratings and the refine text; persona text goes to the persona judge only.

What it measures. Code scorers (no model): price compliance against the parsed range in every batch, repeats across
rounds, seller diversity, near-duplicate groups (the pipeline's own `nearby.py` signals), context colours in the
search text, avoid-colour and product-colour shares, refine exclusions and focus share, latency and cost. Two Haiku
judges (forced tool call, strict schema, thinking off, at most 12 items per call, thumbnails through the guarded
fetch): a category and attribute judge, and a persona judge that answers love, like or dislike. Judges see numbered
listings only, never tags, chips, ranker output or other items; an invalid answer is retried once and then recorded
as "judge failed", never scored. Shop Again lift is the persona match rate (love or like) in round 2 minus round 1,
in points; round 3 minus round 1 is reported without a gate.

Thresholds. `evals/shop/thresholds.json`, with the reason for each number in `THRESHOLDS.md`: judgment values set
before any live run, never tuned on a result. Every results file records the SHA-256 of the file, and a test checks
that. Hard stops: $0.60 Anthropic and 500 Serper credits; at a stop the run writes partial results with the reason.

Conventions. The repo's existing evals use `evals/cases/*.jsonl`, `thresholds.yaml`, gitignored reports, a
`known-unstable/` folder and mock-forced Makefile targets with `-live` twins. The shop eval follows the repo for mock
and live handling and the brief for file shapes. It is not wired into `make eval-fast`, which runs the template's
placeholder pipeline: `make eval-shop-plan`, `eval-shop-mock`, `eval-shop-live` (refuses without
`EVAL_SHOP_CONFIRM=yes` and under CI or pytest) and `eval-shop-report` are its own targets. Results are JSON in the
gitignored `evals/shop/results/` (third-party titles and thumbnail URLs); the Markdown report has neither and cuts
titles, so it can be committed under `docs/evals/`.

Coverage findings. `categories.py` cannot represent four of the twelve requests exactly: curtains and bookshelves
are not in its synonym groups or size table, "dining chairs" and "dining chair" are different topic keys, and "desk
chairs" folds into "desk". They are kept as written and reported as findings.

What it cannot tell you. The judges are the pipeline's own model family, so blind spots may be shared; a persona is a
text, not a person; neither judge is calibrated against human labels (todo: the owner labels a sample and the harness
reports agreement). Twelve requests are a small sample, live results vary run to run (`known_instability.md`), and a
mock run shows that the harness works, not that Shop Mode is good. The fifth replay fixture
(`owner_session_replay_5.json`, the live curtains session) was added with this stage.

Baseline (first live run, 2026-10-26, `shop-live-20261027-041854-b00d9f`): verdict fail, 6 of 22 checks failed (attribute match
worst case 43.5%, product colour share 20.8%, top seller share 33.3%, refine focus share 41.7%, lift mean +6.2, lift worst
-19.6); the other 16 passed, including category (mean 99.6%), price (100% in all 36 batches), near-duplicates (0), repeats (0),
latency (median 6.5 s and 12.9 s) and cost ($0.144 Anthropic, 204 credits, a third of the estimate). The diagnosis in
`docs/evals/shop-eval-baseline-2026-10-26.md` reads two of the failures as measurement or threshold shape (attribute "cannot
tell", a count cap against a share threshold), one as partly both (colour from title keywords) and three as real (free-note
refinements reserve nothing, Shop Again can run out of queries, the lift is modest); per-case lifts are inside sampling noise.
Thresholds and pipeline were not changed in that stage, so the baseline stays comparable.

## Amendment, stage 2p (2026-10-27): fixes from the first live Shop eval

The diagnosis is `docs/evals/shop-eval-baseline-2026-10-26.md`; thresholds and the baseline were left as they were.

Categories and the seller cap. `categories.canonical_category` folds a plural into its singular ("dining chairs" and "dining
chair", "bookshelves" and "bookshelf" are one key), "desk chair" is its own category (the group comes before "desk"), curtains,
dining chairs and desk chairs have synonym groups, size ranges and plausibility keywords, rug sizes in feet ("8x10", "8' x 10'")
and curtain lengths ("84-inch") parse into size hints (Shop Mode has no size filter, only hints). Topic keys use the canonical
keys; a session file with an old key is migrated on load (derived keys only). The per-seller cap is `clamp(floor(0.30 x batch
size), 2, 4)`; the walk assumes the size the pool can fill and walks again with the smaller cap when the batch is shorter, so a
thin pool from few sellers gives a shorter batch rather than a third of it from one seller. Affinity, pinned and tier caps are
unchanged.

Never run dry. The model query writer (`shop_queries` v3) runs on every Shop Again, with or without ratings, is told the
queries already used in the topic and how much was seen, and must give two new variants; a variant sharing 80% of its words with
a used query is rejected (`QUERY_NOVELTY_JACCARD`), and deterministic modifier variants fill in what the model does not give.
A typed first search that cannot fill 24 results runs up to two variant searches inside a Shop Again's normal Serper cap and
says "Only N results matched; try broadening the request." A Shop Again with nothing unseen left says "I ran out of new results
for this search. Try changing the request or the refine chips." and records why; the eval marks the round "exhausted". The
Serper cap of 9 calls (18 credits) per Shop Again is unchanged.

Typed colour, material and finish are quotas. After the pool is ranked and de-duplicated, the top 36 candidates are tagged from
title and thumbnail (12 a call, 3 calls at once, 6 s in all); a photo tag overrides the title tag, the rest keep title tags and
the trace says so; one bounded cache serves the fingerprint and photo steps so no thumbnail is fetched twice. A typed attribute
must match at least 70% of a batch by best tag where tagged supply exists (untagged and `other` never match); "navy" and "dark
blue" are `navy_dark_blue`, "blue" is `blue` only; a shortfall is in the trace and a banner names the attribute. The selection
puts the quota after the shopper's focus chips and before everything inferred. A typed product colour overrides the setting's
suggested colours; the typed colour and material chips are deletable and the quota goes with the chip. The eval measures the
colour from the thumbnail (category judge v2, `color_match`) instead of from title keywords: the measurement changed after the
baseline because title keywords miss every title that names no colour; the threshold (70%) is unchanged and the report prints the
title figure beside it.

Refine terms outside the vocabulary. A free note carries a strength (focus, prefer, exclude) and a phrase of at most 120
characters (`shop_refine` v2, at most 3 active). The candidate and photo tagging calls return a true / false / unknown flag per
item for each active note. A focus note reserves 50% of the batch from items flagged true where supply exists, a prefer note goes
to the ranker only, an exclude note removes items flagged true (never unknown) before the caps and the selection (after the rank
call, because the flags need candidates to tag). The chip reads "matched by description" once flags exist. "mesh" is a material
value (a back type would add a column to every tagging row); "no leather" and any single fabric must be a note, because
`upholstered` covers every fabric and leather.

Not done and open: the attribute judge still counts "cannot tell" as a miss (a threshold decision for the owner); no live run has
checked the new calls (photo tagging time, the writer on every Shop Again, the flags).
