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
| CONVERGE_SHARE | 0.70 | one value's share of the like mass to converge |
| OPEN_SHARE | 0.30 | share each of two or more values needs for open |
| MIN_SUPPORT_ITEMS | 2 | items behind a converged value (a loved item with a naming comment may be the second) |
| KNOWN_COVERAGE_MIN | 0.70 | share of the like mass (and of the shown titles) that must have a known value before a dimension can converge or run the lift test |
| PRICE_BAND_MIN_MASS | 0.80 | share of the liked mass a price band must hold, or price is mixed |
| CONVERGED_MAX_SHARE | 0.80 | most of a batch a converged value may take |
| LIFT_MIN | 1.3 | like share over exposure share needed to call a value preferred |
| DISLIKE_MIN_ITEMS | 2 | disliked items sharing a value before a dislike chip |
| SELLER_DISLIKES_TO_DEPRIORITIZE | 2 | dislikes (and no love or like) before a seller is ranked last |
| MIN_AFFINITY_ITEMS | 2 | loved or liked items (and no dislike) for a seller affinity |
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
| SELLER_DEFAULT_CAP | 4 | results from any other seller |

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
