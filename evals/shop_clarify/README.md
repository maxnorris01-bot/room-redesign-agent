# Clarifying questions spike (offline harness)

Can we show up to five useful clarifying questions, with option pictures, within about 2 to 4 seconds of Search, from a quick first fetch, and do the answers improve the results? See
`docs/adr/0015-clarifying-questions-spike.md`. This harness measures it. It adds no UI and no API route and does not change the shop pipeline or `evals/shop/thresholds.json`.

```
make clarify-plan                                    # free: the requests, expected calls, expected and worst-case cost
make clarify-mock                                    # free: the whole harness on canned candidates and canned model output
make clarify-live EVAL_CLARIFY_CONFIRM=yes           # REAL spend; the owner's call. Round 2 (default): $0.15 stop, saved Serper only
make clarify-live EVAL_CLARIFY_CONFIRM=yes ARGS="--round 1"   # round 1: hard stops $0.30 and 150 Serper credits
make clarify-report                                  # report.md and contact-sheet.html from the newest results file
```

Round 2 (ADR 0016) adds, to each request: strict title matching for the option pictures, a Haiku vision check of every chosen
picture (yes, no or unclear; a no tries the next title match, up to three, then the option is text only), pruning of options and
questions that cannot be shown, an independent audit of the shown pictures (a second prompt that is not told the option), the
timing variants (parse and fetch overlapped; 25 listings), and the with/without-answers arms re-run on the saved round 1 questions.
`CLARIFY_VERIFY_MODE=single` makes one vision call per picture instead of up to 12 per call (compare costs with `CASES=cl-01`).

## What a request does

parse (Haiku, as the pipeline does) -> ONE Serper call for the base query (through the Shop eval's record/replay cache, so a saved response is free) -> `analyze_facets` (one Haiku call, forced
tool call, thinking off, the first 40 listings) -> `pick_option_images` (code) -> `quality_flags` (code) -> the hidden persona's scripted answers -> the normal pipeline twice (baseline, and
with `apply_answers`) -> the existing persona and attribute judges on the top 24 of each.

## Metrics (per request, then a summary)

| | |
|---|---|
| time to questions | seconds, with parse, fetch and model separately (median and max); a cached fetch reports its recorded live latency; round 2: time to text, verification latency, time to pictures, and the overlap and 25-listing variants |
| questions and options | counts |
| picture coverage | round 1: options with a title-evidenced thumbnail over all options; round 2: options with a VERIFIED picture over the options that remain |
| picture accuracy | round 2: the independent audit's agreement with the option over the shown pictures it can read (colour, material, shape) |
| pruning | round 2: questions that survive; options dropped or made text-only, with the reason |
| cost | Anthropic dollars and Serper credits |
| quality flags (code) | restates the request, duplicate or overlapping questions, option count outside 2 to 4, asks about price or brand |
| effect of answers | persona-match and attribute-match rate of the top 24, baseline against answers, and the lift in points |

Round 2 targets: independent picture accuracy >= 90%; coverage after verification >= 60%; added verification latency (median) <= 3 s; >= 80% of questions surviving; median time to questions <= 4 s and max <= 6 s for the best variant.

Round 1 targets (the spike's question, not locked thresholds): median time to questions <= 4 s and max <= 6 s; >= 70% of options with a picture; <= 1 flagged question per request on average; mean lift
with answers > 0. `report.md` ends its verdict with each of the four.

## Files

| File | What |
|------|------|
| `cases.json`, `spec.py` | the twelve requests (six vague, six specific), each with a persona text for the judge and one scripted answer per trait; never shown to the pipeline or the question writer |
| `persona.py` | matches a question to a persona trait and picks the option (by words, then closest, then a size range, else "unanswerable by persona") |
| `runner.py` | the run, the timing wrapper, the spend guard |
| `mock.py` | the canned question writer and canned vision calls for mock mode |
| `plan.py`, `report.py`, `__main__.py` | the estimate, `report.md` and `contact-sheet.html`, the CLI |
| `results/` | results, report and contact sheet: GITIGNORED (third-party titles and thumbnail URLs; the repo is public) |

`contact-sheet.html` shows, per request, the question cards as a visitor would see them (question text, option labels, the chosen thumbnail for each option, a "no preference" choice). Open it in a
browser; thumbnails are the listings' own https image URLs, loaded by your browser.

## What this cannot tell you

The persona is a text, the judges are the pipeline's own model family, twelve requests are a small sample, and a mock run shows that the harness works, not that the questions are good.
