# Stage 2n: fixes from the first live run of stages 2j to 2m

Date: 2026-10-25. Mock mode and fixtures only; no live calls were made.

## What changed and why
1. **More photos did nothing live.** The gallery read `media`; SerpApi documents `product_results.thumbnails`. It now reads
   `thumbnails`, then `media`, keeps distinct https image links in order (6 at most) and never uses `reviews_images`.
   Fewer than 2 usable images: cached, button "No more photos available" (disabled). Footer and panel show, per lookup,
   which response fields held images and how many.
2. **Same product from different vendors.** New `src/app/shop/nearby.py`: image hash plus price, or normalized-title
   similarity plus price; one representative per group; "Also at: ..." on the card; hidden members join seen memory and do
   not count toward caps or shares; bounded fingerprinting (10 parallel, 2 s, never past the hard stop, title-only fallback).
3. **Comments about the listing are not taste.** Prompt `shop_prefs` v7 (v6 kept) labels each comment; code validates the
   label against a fixed list. A flagged item makes no chip, and its rating is out of tags, evidence, baselines and seller
   rules; it stays in seen memory. Mixed comments keep only the taste part. The trace lists these items, says "flagged
   duplicate by you", and whether the automatic check would have caught each.
4. **Small fixes.** Footer: "matches your preferences: none yet (n chips only narrow the search)". Refine placeholder
   "e.g. no Walmart" with the full examples in the title and accessible label.

## Choices made mid-session
No question was put to the user. Where the brief was silent: an unknown label from the reader is ignored (never removes a
rating from taste); Pillow was not added (already a dependency); two mock-only switches (`APP_SHOP_MOCK_DUPLICATES`,
`APP_SHOP_MOCK_ONE_PHOTO`) serve the browser check.

## Evals
`make eval-fast` (mock): 2 cases, pass rate 1.0, latency p95 0.0004 s, mean cost $0.00. A plumbing check only. No live eval
was run (not allowed in this session); the live behaviour of the v7 prompt and of fingerprinting is untested.

## Status
ruff clean, `ruff format --check` clean, `mypy --strict` clean, 1152 tests pass (was 1110 at the start of the brief),
`make shop-check` all checks passed. Screenshots in `spike-output/shop-screenshots/` (60 to 66, light, dark, 390 px).

## Existing tests edited
Prompt-version asserts in `tests/test_shop_stage2e.py` and `tests/test_shop_stage2f.py` (6 to 7, v6 added to the kept list);
the footer wording assert in `tests/test_shop_tailoring.py`; `comment_key` import moved in `tests/test_shop_speed_prep.py`.

## Flag before merging
- Fingerprinting adds up to 2 s of wall time to a typed search or Shop Again in the worst case and one image fetch per top
  candidate (no model credits). Not measured live.
- Candidates are tagged before the collapse, so the tagging cap can spend on items later collapsed.
- A comment read live (not by the background preparation) is flagged for that run only.
- Thresholds `NEAR_*` are judgment values.

## Next command
`git diff main`, then `make shop-check`
