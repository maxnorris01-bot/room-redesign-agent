# Session 2026-10-22: pick quality

Branch `pick-quality`, from `main` at PR #9 (freeplay). Mock mode only: no live call, no
`make spike-live`, `.env` not read, no real network in any test (a test fixture makes any connection
attempt fail the test). I read the local provider responses in `spike-output/` for field names only.

## Why

The latest live freeplay run worked and showed five problems: fit was "unknown" on every pick although
sizes sit in titles and URL slugs; a new warm ivory sofa was proposed next to a kept pale blue armchair
that matched the old sofa; the visitor asked for 5 items and got 3 (and the console table slot paid for
a search, rank and lookup before the proposal dropped it as a duplicate of the media unit); the
text-only style check passed everything; and Article supplied 3 of 4 picks and every alternate.

## Part A: size hints (ADR 0009)

- `app/sizes.py`: title patterns (79"W, 63"L x 12"W x 16"H, W x D x H, 58", 85.5 in, 214 cm, mm,
  fractions), slug numbers next to a category or size word with the 85-5 rule, and `resolve_size`
  (features, then titles, then slug).
- `categories.SIZE_RANGE_IN` (longest side, inches, untuned) discards implausible values with a note.
- `ProductCandidate`: `size_source`, `size_confidence`, `size_note`; partial sizes allowed.
- Fit: longest-side rule when only one horizontal size exists (footprint skipped); a low-confidence size
  says it came from the listing URL and is approximate; unknown when nothing was found.
- Replacement note (25%, untuned) in the final list. Existing fixtures were not modified.

## Part B: sets, messaging, families (ADR 0010)

- `FurnitureItem.matches_id` (optional, additive: `schema_version` stays 1; the structured-output schema
  now lists every property as required, with this one nullable). Perception prompt v3 is the default;
  v1 and v2 stay selectable. A text fallback ("matches ...") is marked inferred.
- Warning when a kept item is in a set with a replaced one; `--replace-matching` converts it (not when
  the visitor explicitly kept it); the set goes to the proposal as a "Set note" line.
- `categories.FAMILIES` and `family_of`; the planner allows one slot per family unless the replacement
  was explicitly requested, records why, and refills with the next uncovered category.
- Item-count message and `item_count_report` in `result.json` (requested, standard categories, covered by
  what stays with names, duplicates skipped, found, empty slots, hint).

## Part C: seller variety

Seller cap of 3 in the freeplay rank pool (after the tier band; the removed count is recorded); rank
prompt v5 (freeplay default; budget mode keeps v3); seller concentration in `result.json` and a footer
line. No cross-slot cap, by design (see ADR 0010).

## Part D: image-based style check

- Both providers carry a thumbnail (Serper `imageUrl`, SerpApi `thumbnail`); `thumbnail_url` is carried
  on the candidate. The mock fixtures were stripped of thumbnails, so mock results get a fake URL that
  is never fetched.
- `app/thumbnails.py`: https only (redirects too, by hand, max 3), public addresses only, 3 s, 1 MB,
  image types (not SVG), Pillow decode and re-encode at 256 px, no query in logs. Any failure falls back
  to text for that item and is counted in the trace (`thumbnail.fetch` spans and the slot summary).
- Prefetch runs in a worker thread right after the rank step, in parallel with the lookup.
- Style prompt v2 (default; v1 text-only stays): room photo, style, kept items with colours, set notes
  and up to 10 thumbnails; per item match, note, clash (none/mild/strong) and the kept item named; the
  code records `basis`. A strong clash or poor match fails the check (warning + the existing single
  revision). Both modes use it. `APP_THUMBNAILS=0` turns the fetches off.
- Mock mode serves a generated image for any URL.

## Tests

453 passed (367 before). New `tests/test_pick_quality.py` (86): every size pattern and unit, slug rules
and the 85-5 case, plausibility rejection, order of trust, partial-size fit, the replacement note,
matches_id and the text heuristic, the clash warning and `--replace-matching`, item-count messaging,
family skipping and refill, seller cap and concentration, thumbnail rules (scheme, redirects, private
addresses, size, content type, timeout and deadline, no query in traces, fallback), prefetch overlap with
the lookup, style v2 with and without images, v1, clash-triggered revision, and cost. Existing tests were
updated for the new defaults (perception v3, rank v5 in freeplay, style v2, family limits, set warnings).
Ran the suite three times. `make lint`, `make typecheck`, `make test` and mock `make eval-fast` pass;
thresholds untouched.

## Deviations and worth knowing

- **Families lower the per-room maximum** (living 8, bedroom 9, office 7, dining 8, generic 8); the ADR 0007
  "10 per room type" no longer holds when asked for 10, which now prints the shortfall message.
- **The seller cap applies in freeplay only**, so budget mode's pool is unchanged.
- **The mock's IKEA side-table sizes are now discarded** under a coffee-table slot (58 cm is implausible
  for one); the test now asserts the discard and its note.
- A slug of `oslo-36-12-coffee-table` reads as 36 in (the 12 is ignored); ambiguous, but harmless.
- Not measured live: size hit rate and accuracy, whether the image check beats the title check, thumbnail
  fetch failures, `matches_id` quality, and any effect of the seller cap on picks.

## Next

```bash
make spike
make spike ARGS='spike-photos/room1.png --style "warm minimalist" --items 5 --replace sofa --keep desk --replace-matching'
make spike ARGS='spike-photos/room1.png --budget 800 --style "warm minimalist" --replace sofa --keep desk'
```
