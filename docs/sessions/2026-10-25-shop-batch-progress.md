# Shop batch progress (stages 2j to 2m)

Reread this file after a context reset and continue from the first unfinished phase. Mock mode only.

| Phase | Stage | Status |
|---|---|---|
| 1 Topics | 2j | finished |
| 2 Request understanding and color | 2k | finished |
| 3 Refine search | 2l | finished |
| 4 Customer reviews | 2m part 1 | finished |
| 5 More photos | 2m part 2 | finished |
| 6 Cross-cutting checks and docs | | finished |

## Notes per phase

(each phase: tests added, decisions, open questions)

### Phase 1 (2j topics): finished
- New file `tests/test_shop_topics.py` (16 tests). Total suite 1035 passed; ruff, mypy, `make shop-check` pass.
- Topic id = `canonical_category(query.category)` (couch and sofa are one); an unknown-to-the-table category such as
  "curtains" is its own key; a missing category always starts a new topic with its own notice.
- Stored: `Session.topics`, `current_topic`, `RatingRecord.topic`, `SearchRecord.topic/topic_notice`, pool snapshots
  carry `topic`. Old files migrate on load (`ensure_topics`), stamping everything with the topic of the last search.
- Learned state comes from `rated_results`, `rated_count`, `sellers_to_deprioritize`, `cached_readings` (all
  topic-limited via `in_topic`); a new topic sets `profile=None` and clears `searched_peers`. Tag cache and seen memory stay.
- A Shop Again that finishes after a new topic is dropped (409); a comment reading likewise writes nothing.
- Page: `#topic-notice`, saved label "From an earlier search", late answers ignored, topic line in How I decided.
- Existing tests edited: none (only a mock parse sample for "walnut coffee table with storage" was added).
- Browser-check coverage for topics is added in phase 6.

### Phase 2 (2k request understanding and color): finished
- New file `tests/test_shop_request_understanding.py` (24 tests). Suite 1059 passed; ruff, mypy, `make shop-check` pass.
- Color vocabulary (dimension `color`): white, cream, beige, tan, brown, black, gray, blue, navy_dark_blue, green, red,
  rust_terracotta, yellow_mustard, pink, purple, orange, metallic, multicolor_pattern, other. Short labels: navy, rust,
  mustard, multicolor. No families (none needed by the evidence layer). Tagging tool gets a required nullable `color`;
  tokens per item 40 to 48 (batch of 40 limit 3400 to 4040); prompt `shop_candidate_tags` v5 (v4 kept).
- `shop_parse` v2 (v1 kept; PARSE_MAX_TOKENS 600 to 900): product fields, `color_terms`, `context`, suggestions, avoid,
  reason. Code validates kinds, colours (fixed list, caps 4 and 3, no overlap, suggestions only with a colour-bearing
  setting and no product colour), strips the room's colour from the search text; unknown values become approximate
  context text.
- Ranking: `shop_rank` v6 (v5 kept) with setting lines; typed search uses v6 only when the request has a setting.
  Selection: >= 40% suggested, <= 15% avoid (`COLOR_SUGGESTED_MIN_SHARE`, `COLOR_AVOID_MAX_SHARE`).
- First search: page 1 of the plain query and page 1 of "<search> <top two suggested colors>" at once (2 calls, same credits
  as before); page 2 of the plain query only if the pool is thin (3 calls).
- Chips: setting, suggested, avoid color chips, deletable via `POST /api/context/remove`; deleting the setting removes the
  suggestions too; chips belong to the typed request of the topic. `How I read your request` shows the split, reasons and
  counts. Query writer (`shop_queries` v2, v1 kept) gets the setting lines.
- Fixture: `serper_curtains.json` gained Navy, Dark Blue and Blue titles (22 of 116); the mock search surfaces colors named
  in a query first.
- Existing tests edited: color key and version bumps (stage2e/2f/2g/decide/replay/thinking/tailoring), listed in the report.

### Phase 3 (2l refine search): finished
- New file `tests/test_shop_refine.py` (30 tests). Suite 1089 passed; ruff, mypy, `make shop-check` pass.
- `src/app/shop/refine.py`: forced `record_refinements` call (`shop_refine` v1, 500 tokens, thinking off, only that tool),
  validation against retailer table, tiers and vocab (anything else is an approximate free note; free notes are always
  soft), conflict replacement with notice, caps 5 per submit and 12 active chips, 200 characters, links stripped.
- Chips live in `Topic.refinements` (end with the topic; the topic notice says so). Endpoints `/api/refine`,
  `/api/refine/remove`; `/api/shop-again` accepts `refine_text` and the job gets a first step "Reading your refinement"
  (only then; other jobs unchanged). A refinement that cannot be read adds a warning and the Shop Again goes on.
- Effects: exclusions are hard filters on the pool (seller, tier, value by title keywords then by model tags), focus chips
  take 50% (`REFINE_FOCUS_MIN_SHARE`) in a pass before the tier pass, a seller focus is searched by name, prices replace
  the typed range (display price chip changes), must-haves extend the request, ranker and query writer get every chip,
  inferred chips contradicting a refine chip are suppressed and listed in the trace; banner under 12 names the chips.
- Cost: `Session.refine_usd`, in the session total and the footer ("refinements $x").
- Existing tests edited: `test_shop_thinking.py` (8 call sites now).

### Phase 4 (2m reviews): finished
- Tests in `tests/test_shop_reviews_photos.py` (7 for reviews). Serper fixtures carry `rating` and `ratingCount` for most
  results (a few have none on purpose); SerpApi shopping uses `reviews`; the `google_product` lookup may carry
  `product_results.rating`, `reviews` and `media`. `ShopResult.rating/rating_count` are set only from the source
  (rating must be in (0, 5], count >= 0), the Open lookup fills missing ones (never overwrites). Display only: not in
  evidence/decide/caps. NOTE: the ranker's candidate line has shown the rating since stage 1 (unchanged); no new use.
- Cards and the Saved panel show "stars 4.6 (1,203)" with an aria-label; trace line "Customer ratings: n of N".

### Phase 5 (2m more photos): finished
- 8 more tests in the same file (15 total). One shared lookup per result (`Session.product_cache`, whole `ProductDetails`
  cached, failures cached too); `resolve` and `/api/photos` use `product_lookup`; counted once against the cap of 12;
  a gallery after Open is free and so is Open after a gallery. Up to 6 https images, previous/next buttons and arrow keys,
  no CSP change (`img-src https:` already allows them). Mock gallery: the Article fixture now has 6 `media` images, rating 4.6
  and 1,203 reviews; ids ending in 9 have no product_results (message "No more photos...").

### Phase 6: finished
- `scripts/shop_browser_check.py` covers topics, the curtains request, refine, stars and the gallery; screenshots 43 to 57
  (light, dark, 390 px) were opened and checked. Docs written: ADR 0012 amendments 2j to 2m, SPEC.md, todo, working notes,
  fixtures README, `2026-10-25-shop-batch.md`. Final: 1104 tests, ruff, mypy, `make shop-check`, `make eval-fast` pass.
