# 0009. Size hints and the fit check

- **Status:** proposed
- **Date:** 2026-10-22

## Context

The fit check (ADR 0006) was "unknown" on every pick in every live run, although sizes sit in the data:
the Article sofa's URL slug is `sanders-85-5-sofa`, the media unit's is `leif-54-media-unit`, the
console's is `leif-37-storage-console`, and other listings carry `79"W` or `63"L x 12"W x 16"H` in the
title. ADR 0006 deliberately excluded parsing titles; this ADR reverses that for sizes written in text
the provider already returned, with explicit confidence, while still not fetching product pages.

## Decisions

**Where a size can come from, in order of trust.** (1) The lookup's feature text (the existing
parser), (2) sizes written in a title, the store page's own title first and then the search title,
(3) a number in the URL slug. The first source that yields a plausible size wins; partial sizes are
fine (a width alone).

**Title patterns.** `79"W`, `63"L x 12"W x 16"H`, W x D x H groups with a unit (`79 x 18 x 30 in`,
`58 x 39 cm`), unlabeled sizes (`58"`, `85.5 in`, `214 cm`, `1200 mm`) and mixed fractions
(`12 5/8"`). Inches, centimetres and millimetres are converted to metres. A labeled axis maps to
width, depth, height or length; an unlabeled size is recorded as the product's longest side. Numbers
with neither a unit nor an axis letter are ignored, and "2 in 1" is not a size.

**Slug rule.** A slug number counts only when it sits next to a category word or a size word
(`leif-54-media-unit`, within one token before or three after), is at most three digits, and is
plausible for the category. A pair like `85-5` is read as 85.5 when that is plausible for the category;
otherwise the first number alone is tried (the 5 is then a stray digit), and a pair whose second
number is not a single digit is never merged (`36-12` is 36). Slug sizes are inches, longest side.

**Plausibility.** `categories.SIZE_RANGE_IN` gives, per category, the plausible range of the longest
side in inches (sofa 48 to 130, console table 28 to 80, rug 24 to 180, and so on; wide on purpose and
untuned). A value outside the range is discarded, a note is recorded (`size_note`, in the trace and
`result.json`) and the next source is tried. This also catches a real size on the wrong product: the
58 x 39 cm IKEA table in a coffee-table slot is discarded.

**Provenance.** Each candidate carries `size_source` (`feature`, `title`, `slug`, `none`) and
`size_confidence` (`high` for a feature or a title with units, `low` for a slug, `none`).

**Fit check.** With two horizontal sizes the existing rules apply (longer side over 60% of the room's
shorter side, footprint over 12% of the floor area). With only a longest side the footprint rule is
skipped. A low-confidence size can pass or fail, but the message says the size came from the listing URL
and is approximate; the low room-confidence note is kept. With no horizontal size, or a height alone,
the check stays "unknown", and flat goods stay "unknown" too (ADR 0006).

**Replacement size note.** When an item replaces a known piece, the new longest side is compared with
the old piece's width; a difference beyond 25% adds a note ("new piece is about 1.40 m long vs your old
sofa at 2.00 m (30% shorter)"), shown in the final list, never a failure. The 25% is an untuned guess.

## Alternatives considered

1. **Fetch product pages for sizes.** More complete, but it brings back page scraping and its
   problems; kept in the backlog if title and slug hints prove insufficient.
2. **An LLM reads titles.** Unnecessary for patterns this regular, and it would put numbers in front of
   the fit check that no code can verify.
3. **Trust slug numbers anywhere.** Rejected: ids, years and colour codes look like sizes.

## Consequences

- Fit is no longer "unknown" for most picks, but a slug size is a guess about the longest side. The
  message says so.
- Mis-parses are possible (a title that lists the box size, a pair of numbers that are not a size); the
  plausibility table is wide and untuned, so some wrong sizes will pass.
- Existing fixtures are unchanged; the mock run still shows mostly "unknown" because its fixture titles
  carry no sizes. Unit tests cover the patterns with the real examples.
- Not measured live: how often a size is found, and how often it is right.
