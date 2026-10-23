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

## Amendment 2026-10-23: labeled forms, category-aware axes and the height rule (still proposed)

A live freeplay run showed two misreads. A sofa title ending `80w 34d 30h` gave only a longest side
of 80 in (the compact labels were not recognised), and a bookcase title with `21"x79"` was read as
0.53 m wide by 2.01 m deep, when 79 in is the height of a tall narrow bookcase; the fit check then
treated 2.01 x 0.53 m as a footprint and passed by luck.

- **Labeled forms** are case-insensitive, with or without quote marks and spaces: `80w 34d 30h`,
  `80W x 34D x 30H`, `80"W 34"D 30"H`, `W80 D34 H30` (prefix form only trusted when two or more
  axes appear), each with an optional trailing unit word (in, inch, cm, mm) that applies to the run.
- **Unlabeled two-number groups** (`21"x79"`) take their axes from a named table in `categories.py`
  (`axis_rule`). Tall categories (bookshelf, bookcase, shelving, wardrobe, armoire, floor lamp, tall
  cabinet): larger = height, smaller = width, depth unknown. Flat categories (rug, runner, mat): width
  by length. Anything else: only the longest side, the other axis unknown rather than guessed. The
  rule used is written into the size note.
- **Unlabeled three-number groups** are W x D x H only when a plausibility check passes (in range for
  the category; for tall categories the third number must be the largest; otherwise the third may not
  exceed 2.5x the larger of the first two). Otherwise: longest side only.
- **Height counts toward plausibility** for tall categories (the range is on the largest side).
- **Fit.** The footprint rule needs two floor sizes (width, depth or length); a height never makes
  one. New height rule: a known height above the ceiling minus 0.1 m fails the check
  (`HEIGHT_CLEARANCE_M`, untuned); an unknown height or ceiling means no height check. Messages show
  the axes used (`2.01 m tall, 0.53 m wide`).
- **Replacement note** also compares depth when both depths are known, same 25% tolerance.
- Not measured live: how often the category table picks the right axes, and whether the 2.5x
  three-number test and the 0.1 m clearance are right.

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
