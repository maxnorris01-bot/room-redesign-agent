# Shop fixes progress (live-run fixes after stages 2j to 2m)

Reread this file after a context reset and continue from the first unfinished part. Mock mode only.

| Part | Status |
|---|---|
| 1 More photos read the wrong field | finished |
| 2 Same product from different vendors | finished |
| 3 Meta comments are not taste | finished |
| 4 Small fixes | finished |
| Tests, browser check, docs, report | finished |

## Notes per part

### Part 1: finished
- `shopping._gallery` reads `thumbnails` then `media` (non-image, duplicate and non-https entries dropped, order kept, 6 max);
  `reviews_images` is counted in `ProductDetails.image_fields` but never in the gallery. Fixtures: the Article fixture now has
  `thumbnails` and `reviews_images` (live shape), `serpapi_product_article_media.json` has the `media` shape.
- Fewer than 2 usable images: cached (one lookup, counted once), button "No more photos available" and disabled
  (`photos_none` in the view); failure and cap messages unchanged. Per-lookup field names and counts in `lookup_trace`
  (view), the footer ("link lookups 1 of 12 (thumbnails 6, reviews_images 3)") and How I decided.
- Tests: new file `tests/test_shop_fixes.py` (part 1: 6 tests). Suite 1110.

### Part 2: finished
- `src/app/shop/nearby.py`: dHash (Pillow was already a dependency, `pillow>=13`; nothing added), title normalization,
  Jaccard, price windows, collapse with representative rules, bounded fingerprint step (10 workers, 2 s, top 48, never past
  the hard stop). Guarded fetch gained `max_bytes` (500 KB for fingerprints) and a header pixel limit (24 M) with a JPEG
  draft decode. Seen memory = shown/rated results (all topics) plus `Session.hidden`; fingerprints in `Session.fingerprints`
  (prep fingerprints shown items from the thumbnails it already fetches). Config `shop_near_duplicates` off bare, on from env
  (`APP_SHOP_NEAR_DUPLICATES`). Mock: `APP_SHOP_MOCK_DUPLICATES=1` adds same-product listings from eBay, Lowe's, Overstock; the
  mock fingerprint reads the image id from the URL path; mock peer catalogues start at their own offsets (so two retailers
  are not the same products).
- Cards show "Also at: ..." (textContent). Trace: reasons, exact-rule counts, 5 examples, fingerprint report.
- Tests: part 2 in `tests/test_shop_fixes.py` (21 tests). Suite 1131.
- Side effect to know: candidates are tagged as searches return, before the collapse, so the tagging cap of 60 can include
  items collapsed afterwards (browser check allows it).

### Part 3: finished
- Prompt `shop_prefs` is now version 7 (v6 kept as `shop_prefs.v6.md`). `ItemTags` gained `meta_kind` (null for taste or no
  comment) and `has_taste`. Code checks the class against `META_KINDS` (duplicate, broken_image, wrong_product_type,
  wrong_item, listing_problem, other_meta); an unknown value is ignored, so a made-up class never removes a rating from taste.
- `reading_entry` stores `meta`; a meta comment drops stated likes/dislikes that are about the listing, and drops all of them
  and the query adjustments when `has_taste` is false. `store.meta_kind_of` is the one switch: `rated_results`,
  `topic_ratings`/`rated_count`, `disliked_sellers`, `sellers_to_deprioritize` and `cached_readings` skip such an item, so it
  makes no chip, no tags, no like-rate or baseline evidence. The result stays in `session.results` (seen memory).
- `comment_key` moved to `store.py` (re-exported use sites updated). Live path: a comment read at Shop Again time is flagged
  the same way (`tagging.meta_flagged`).
- Trace: `meta_comments` (title, class, "flagged duplicate by you", whether the automatic check would have caught it and by
  which signal), shown under "Comments about the listing, not your taste".
- Tests: 12 new in `tests/test_shop_fixes.py`; two existing prompt-version asserts bumped 6 to 7 (and v6 added to the kept list).
  Suite 1149.

### Part 4: finished
- Footer: "matches your preferences: none yet (n chips only narrow the search)"; the counted wording is unchanged when taste chips or leanings exist. Refine placeholder "e.g. no Walmart", full examples in `title` and `aria-label`.
- Browser check: new section `run_fixes` on a seventh server (mock duplicates, one-photo products). Screenshots 60 to 66.
