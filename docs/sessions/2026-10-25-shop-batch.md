# 2026-10-25: topics, request understanding, refine, reviews and photos (stages 2j to 2m)

Written for someone with no other context. Mock mode only; nothing ran live and no live number is claimed. The
phase-by-phase log is `docs/sessions/2026-10-25-shop-batch-progress.md`.

## What changed, per phase

1. **Topics (2j).** A topic is the canonical category of the parsed request. Same category: learned state kept
   with a notice. Different or missing: a fresh profile, evidence and refine chips from that topic's ratings only;
   Saved, the tag cache and the seen memory stay; old session files migrate on load; late jobs and comment
   readings of an old topic write nothing; the page ignores late answers and labels old saved items.
2. **Request understanding and colour (2k).** `shop_parse` v2 splits product words from setting words and may
   suggest and avoid colours (fixed list, soft only); a `color` tagging dimension (tool field, prompt v5) flows
   through evidence, leanings and decisions; `shop_rank` v6 and `shop_queries` v2 get the setting; batches take
   40% suggested and at most 15% avoided colours; the first search runs a coloured second query instead of
   page 2; deletable setting, suggested and avoid chips and a "How I read your request" trace.
3. **Refine (2l).** A refine box and `record_refinements` (`shop_refine` v1) make validated, deletable chips that
   filter, favour, price and constrain every later Shop Again of the topic and outrank inferred chips.
4. **Reviews (2m).** Star rating and review count on cards and in Saved, only when the source gave them.
5. **More photos (2m).** A gallery of up to 6 https photos from one shared, cached product lookup that Open also uses.

## Tests

Total 1104 passed (was 1019 at the start of this batch). New files: `test_shop_topics.py` 16,
`test_shop_request_understanding.py` 24, `test_shop_refine.py` 30, `test_shop_reviews_photos.py` 15.
Ruff, ruff format, `mypy --strict` and `make shop-check` pass; `make eval-fast` (mock) passes as a plumbing check.

## Existing tests edited because the design changed

- `test_shop_stage2e.py`, `_stage2f.py`, `_stage2g.py`, `test_shop_decide.py`, `test_shop_replay.py`: the new
  `color` dimension (extra `"color"` key in tag dicts and in the dimension lists, token limit 4040, candidate
  tags prompt version 5 with v4 kept).
- `test_shop_tailoring.py`: rank prompt version 6 (v5 kept); "Leaning away from black" without a full stop.
- `test_shop_thinking.py`: 8 model call sites (query writer and refine call), the diagnostic's colour column.
- `test_shop_replay.py`: the trace keys excluded from one comparison (`alignment_counted`, `model_queries`,
  `reviews`).
- `tests/test_shop.py` (the shared double) answers `record_queries` and `record_refinements`.
- `scripts/shop_browser_check.py`: new sections for topics, curtains, refine, stars and the gallery.

## Decisions and tradeoffs to review

- The mock model reads the three new calls with small deterministic rules; their quality says nothing about a
  real model.
- The colour of rated items comes from their cached photo or title tags, not the preference call, so the
  preference prompt did not change.
- Colour suggestions and the focus and avoid shares are judgment (40%, 15%, 50%), unmeasured.
- A refinement that cannot be read adds a warning and the Shop Again continues.
- The ranker's candidate line has shown the rating since stage 1; reviews added no new use of it.
- The mobile refine placeholder is cut off at 390 px.

## Not done

Nothing live was run: the colour tags on real photos, the quality of the real parse, refine and query-writer
calls, the real `media` of a SerpApi product lookup and the real costs are unmeasured. The fifth replay
fixture from a real session is still waiting for a session with a pool snapshot (see `docs/todo.md`).

## Next command

From the repository root: `git diff main`. Then, when you choose to spend, `make shop-live` and follow the
checklist at the top of `docs/todo.md`.
