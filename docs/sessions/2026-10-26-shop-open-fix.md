# Stage 2o: gallery dormant, Open fixed, store links resolved ahead

Date: 2026-10-26. Mock mode and fixtures only; no live calls were made.

## What changed and why
1. **Gallery off by default.** `APP_SHOP_GALLERY` (default 0): no More photos button, no gallery, `/api/photos` answers
   404, no image wording in the footer or trace. A lookup still gives the rating, review count and retailer link; its
   image lists are dropped before caching. All gallery code stays; its tests run with the flag on. The ADR says why
   (a few seconds per lookup, no faster source) and what would reopen it.
2. **Open never leaves a blank tab.** Cause: the old handler opened `about:blank` in the click, wrote nothing into it,
   and waited for a lookup with no page-side limit; the tab sat empty, was closed without a page on failure, and was
   closed on an empty or non-https link with the reason only in the status line. Now: the tab opens in the click with
   its opener cut and "Finding the store page..."; an https store link is followed; failure, a 12 s timeout
   (`OPEN_UI_TIMEOUT_S`) or no store link goes to the Google product link with a one-line card message; neither link
   usable closes the tab with a message; a blocked tab gives a message and one inline "Go to store" link; the button
   reads "Finding store..." and one lookup is in flight per product; no product id or a cached lookup opens at once.
3. **Resolve ahead.** Love or Like queues a background lookup (never Dislike), at most 8 of the 12 (`APP_SHOP_AUTO_LOOKUPS_MAX`),
   2 at a time, one cache shared with Open, failures cached and never retried, nothing learned written by a late
   lookup. `APP_SHOP_AUTO_LOOKUPS=0` turns it off. Footer: "link lookups n of 12 (auto m, by you k), median s"; the trace
   lists each lookup as made ahead or by you, with seconds.

## Choices made mid-session
No question was put to the user. My choices: automatic lookups are off in a bare `Config` and on from the environment (like
the other stage toggles); the manual lookup keeps running under the session lock as before (only the automatic ones run
outside it); a per-product lock makes Open wait for an automatic lookup of the same product instead of calling twice.

## Evals
`make eval-fast` (mock): 2 cases, pass rate 1.0. A plumbing check only; no live run was made.

## Status
ruff, `ruff format --check` and `mypy --strict` clean; 1175 tests pass (1152 before); `make shop-check` all checks passed
(new Open scenarios: success, failure, timeout, blocked popup, plus the flag-off page). Screenshots 70 to 74.

## Existing tests edited
- Gallery tests in `tests/test_shop_fixes.py` and `tests/test_shop_reviews_photos.py` now build their service with
  `shop_gallery=True` (a helper `gallery_service`; `sparse_service` and the cap test config).
- `test_each_lookup_reports_the_field_names...`: the trace entry gained `by` and `seconds`; the assert checks the old keys.
- Two page-text asserts: "images found in" is now "; images in ", and `if (r.product_id)` is now
  `if (r.product_id && state.galleryOn)`.

## Flag before merging
- The blank-tab cause is read from the code and reproduced in the browser check against the old handler; I have not seen
  the owner's real blank tab, so a different trigger on his browser is possible.
- A lookup the page did not wait for (timeout) still finishes and caches on the server.
- Open is instant for loved items only after the page has seen the resolved link (next view load); before that it shows
  a loading tab for one fast round trip.

## Next command
`git diff main`, then `make shop-check`
