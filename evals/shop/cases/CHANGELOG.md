# Shop eval cases: changelog

Case files are versioned (`version` in each file and in `index.json`). A change to a case, a
persona or the index is its own commit and is recorded here, never bundled with the code change it
would excuse.

## Version 1 (2026-10-26)

Twelve cases, taken from the owner's list without substitution. `categories.py` does not represent
four of them exactly; they were kept as written because a coverage gap is a result, not noise (the
plan and the report list them as findings):

- case-03 "curtains": no synonym group or size range in `categories.py` (Shop Mode reads it as an
  unknown category on purpose, stage 2k).
- case-04 "dining chairs": only in the size table; no synonym group, so "dining chair" and "dining
  chairs" are different topic keys.
- case-05 "bookshelves": the plural does not fold into "bookshelf" (the keyword is "bookshelf").
- case-10 "desk chairs": folds into "desk" (the desk keyword matches inside "desk chair"), so it
  shares a topic key and a size range with desks.
