# 0015. Clarifying questions: an offline spike

- **Status:** proposed (a spike; nothing here is wired into the shop). Round 2 (strict picture matching, vision verification, pruning) is ADR 0016.
- **Date:** 2026-11-04

## Context

A vague request ("dresser") returns a mixed first batch, and the pipeline learns taste only after the visitor rates results and presses Shop Again. The idea: when the visitor hits
Search, show up to five short questions about the traits that define the product (colour, material, shape, style, size, one feature), each with two to four options and, where we can,
a picture, using only a quick first fetch, and turn the answers into the typed constraints the pipeline already understands. Open questions: is it fast enough (the visitor waits for
the questions), are the questions and pictures good, and do the answers improve the results?

## Decision

Build and measure it offline, without touching the live path.

1. `src/app/shop/clarify.py` is not imported by the API, the service or the search pipeline (a test checks that). It has `analyze_facets` (one Haiku call, a forced tool call with a strict
   schema, thinking disabled, on the first 40 listings of one Serper call for the base query; prompt `shop_clarify` v1), `pick_option_images` (code only: a listing whose TITLE
   carries the option's value words and has an https thumbnail), `apply_answers` and `quality_flags` (code only).
2. `evals/shop_clarify/` is its own harness (plan, mock, live, report) in the style of `evals/shop`, reusing its providers, the Serper record/replay cache, the judges and the
   scorers without editing them. `evals/shop/thresholds.json` is untouched (SHA-256 c64fe35a...d237).
3. The live run is the owner's call: `EVAL_CLARIFY_CONFIRM=yes`, never from CI or tests, hard stops $0.30 Anthropic and 150 Serper credits, saved Serper responses reused.

## Design choices the brief did not fix

- **`dimension` on a question** (`color`, `material`, `shape`, `style`, `size`, `feature`): needed to map an answer onto an existing mechanism; the model sets it, code checks it.
- **Option counts and the five-question limit.** The schema cannot bound a list (`maxItems` is rejected), so code truncates to five questions (noted) and keeps an option count outside 2 to 4,
  flagging it, so the harness can see how often the model breaks the rule.
- **Short numbers for evidence.** The model cites listings by their row number; code turns them into product ids and drops unknown numbers. A cited listing is used for a picture only
  if its title confirms the value.
- **How answers feed the pipeline** (`map_answers`): colour to `color_terms` (a colour id; a typed quota), material to `material_terms` (a quota when the vocabulary knows it), shape to
  `shape_terms` (a quota for round, oval, rectangular, square), style to `style_terms`, size and feature to `must_have` (soft, read by the ranker); an unknown colour or shape degrades to a
  must-have. As the parse step does, the words of a colour, material, shape or style answer are added to `search_text` (at most two words) because that is how a typed "walnut dresser"
  reaches Serper. A "no preference" answer changes nothing.
- **Time to questions is parse + fetch + model, in sequence**, because the fetch needs the parse's search text; the report also gives the figure with the parse overlapped. A cached
  fetch reports the latency recorded when it was live (a sidecar file beside the results), so a re-run does not understate the wait.
- **Lift is measured on the top 24** of two normal pipeline runs (`run_query`, the first search, with the parse reused) judged by the existing persona judge (love or like share) and the
  attribute judge; the attribute judge checks the persona's answered traits in both arms.
- **Vague and specific.** The brief's twelve requests: the six single nouns are the vague ones, the six with a qualifier or a second word are the specific ones.

## Consequences

If the live run shows the targets are met (median time to questions at most 4 s and max 6 s, pictures on at least 70% of options, at most one flagged question per request on average, mean lift
above 0), the next step is an API route and a page; if not, the report says which target failed and why. Until then nothing about the shop changes.
