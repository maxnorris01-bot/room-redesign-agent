# 2026-11-04: clarifying questions, an offline spike

## What changed and why

An offline spike that answers, with measurements, whether we can show up to five useful clarifying questions (with option pictures) within about 2 to 4 seconds of Search from one quick first fetch,
and whether answering them improves the results. No UI, no API route, no change to the live shop pipeline, no change to `evals/shop/thresholds.json` (SHA-256 `c64fe35a...d237`, checked).
Decisions and the reasoning are in `docs/adr/0015-clarifying-questions-spike.md`; the harness is described in `evals/shop_clarify/README.md`.

- `src/app/shop/clarify.py` (not imported by anything else; a test checks): `analyze_facets` (one Haiku forced tool call on the first 40 listings, prompt `shop_clarify` v1), `pick_option_images`
  (title matching, no model call), `apply_answers` / `map_answers`, `quality_flags`.
- `prompts/shop_clarify.md` and `.v1.md`.
- `evals/shop_clarify/`: twelve cases with hidden personas, the persona answerer, the runner (timing wrapper, spend guard, two pipeline arms scored by the existing judges), the plan, the report and
  contact-sheet writer, a canned question writer for mock mode, a README. Make targets `clarify-plan`, `clarify-mock`, `clarify-live`, `clarify-report`.
- `.gitignore`: `evals/shop_clarify/results/` (results, report, contact sheet, fetch-latency sidecar). `pyproject.toml`: `E501` ignored for the new eval and test files, as for `evals/shop`.

## Choices not fixed by the brief

A `dimension` field on questions; option counts outside 2 to 4 are kept and flagged (the schema cannot bound a list); evidence given as row numbers and mapped to product ids; time to questions is
parse + fetch + model in sequence (the overlapped figure is reported too); a cached fetch reports its recorded live latency; lift is the persona-judge love/like share and the attribute-judge share
on the top 24 of two `run_query` runs (the parse reused), with the persona's answered traits added to the attribute judge's list in both arms; the six one-noun requests are the "vague" ones.
How answers feed the pipeline: colour to `color_terms`, material to `material_terms`, shape to `shape_terms` (typed quotas), style to `style_terms`, size and feature to `must_have`, plus up to two
words in `search_text`.

## Numbers

Mock only (nothing live was run). `make clarify-mock`: 12 of 12 requests ok, about $0.02 of mock judge cost, 0 Serper credits. Mock timings, coverage and lift are canned and say only that the harness
works. Tests: 1784 passed (1747 before, +37 new). `ruff check`, `ruff format --check` and `mypy` clean. `make eval-fast`: pass rate 1.0 (mock). Thresholds hash unchanged.

## Open questions / flags for review

- The live cost estimate: expected about $0.16 Anthropic and 96 Serper credits, worst case about $0.28 and 144 credits against the stops of $0.30 and 150. The estimate rests on the Shop eval's measured
  per-search cost and on assumed token counts for the two new calls; the first live run will replace those assumptions with measurements.
- The Serper cache is shared with the Shop eval (`evals/shop/cache/serper`, gitignored); the 14-day age limit applies, and a stale entry is a live call.
- Picture coverage depends on titles carrying the option words; a size question's pictures depend on titles stating sizes, so coverage will likely be lowest there.
- The persona answerer is a heuristic (words, then spelling, then ranges): a request whose questions it cannot answer gets no "with answers" arm, which lowers the number of lift data points.

## Next command

`make clarify-plan`, then (Max's call) `EVAL_CLARIFY_CONFIRM=yes make clarify-live`, then `make clarify-report` and open `evals/shop_clarify/results/contact-sheet.html`.
