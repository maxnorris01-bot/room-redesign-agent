# 2026-11-06 - Clarify B2: first question in ~2 s, the rest while the visitor answers

Branch `shop-clarify-fast` (from `main`). Backend only; the flag `APP_SHOP_CLARIFY` is unchanged and still off by default.

## Why
The questions exist to hide the 15-20 s search wait. They appeared at 6-10 s, pictures added 4 s more, and the rank gate (idle grace counted from "ready to rank") often fired before the visitor could answer, so results replaced the questions.

## What changed
- **Question 1 from the request text alone** (`clarify_first.py`), started in its own thread before the parse and the fetch. A category template table (rug, couch/sofa/sectional, loveseat, chair/armchair, dresser, nightstand, bed/bed frame, headboard, desk, coffee table, dining table, side table/end table, TV stand, bookshelf/bookcase/shelf, cabinet, mirror, lamp, curtain(s), pillow, blanket, vase, planter) gives a first and a second question; the first whose dimension the request does not state is used. No match or all stated: one Haiku call (`prompts/shop_clarify_first.md`, forced tool call, 250 tokens, thinking off) on the raw text. Shown at once with `more_coming: true`.
- **Questions 2..5 progressive.** When the fetch is in, the existing listing call runs on the first 25 listings told which dimensions are already asked and how many more (max 4). Code then drops asked or stated dimensions, repeated dimensions, and feature words inside another dimension's question (the "Washable" in a rug material question), and numbers the ids q2..q5. A failure keeps question 1 alone. `more_coming` flips to false when it ends.
- **Pictures off the critical path.** `clarify_vocab.json` (copy of the shared vocabulary, no URLs) and `option_visual(dimension, label)` in `clarify_visual.py` give each option `icon` and `swatches` (computed in the snapshot). Such options get no picture search and no vision check. Only options with neither go through the existing strict title match + vision check, after publish. `picture_eligible` per question.
- **Gate and clock.** Idle grace and max wait run from question 1 being shown. A pipeline that is ready first waits. The gate also waits up to 10 s for `more_coming=false`. Any answer or `touched: true` (new, optional, counts toward the 60-post limit) switches to the max-wait mode. Defaults 30 s / 60 s in `config.py`. The extra fetch after answers was removed from `search.py`: answers filter and reorder the candidates already fetched.
- **Timing.** `clarify.timing` = `{q1_ms, rest_ms, first_pictures_ms, source}` and one stderr line per job: `clarify_timing job=<id> q1_ms=.. rest_ms=.. source=.. questions=..`.
- The existing listing prompt (`shop_clarify.md`, version 1) is unchanged on purpose (the offline eval harness pins it); the "already asked / at most N / one dimension" instructions are added to the user text instead.

## Decisions to know about
- No `temperature` is sent: the installed SDK/models take none (noted in `llm.py`); the call is a forced tool call with thinking off. Prompt caching is not used: `complete` sends the system prompt as a plain string and this one is far below Haiku's minimum cacheable size anyway.
- In a non-colour question a colour word gives no swatch, and in a colour question a material word gives no icon (dimension-aware).
- A picture needs at least two eligible options in one question (the existing verifier drops a question with fewer); a lone unknown option stays text.
- If the session already holds answers for a possible repeat topic, question 1 waits for the parse (so it cannot appear and vanish).

## Estimated cost per search (Haiku 5.5 rates in `pricing.py`)
Template hit (most common categories): $0 for question 1. No template: about 450 in + 80 out tokens, roughly $0.00009. Listing call: about 2,300 in + 400 out, roughly $0.0004 (smaller than before: max 4 questions). Vision checks: mostly none now (drawn options skip them). Estimate only; not measured.

## Tests and checks
- `tests/test_shop_clarify_fast.py` (new, fakes only): template match, skip-when-stated, fall-through to the model; question 1 shown while the fetch is pending; rest excludes asked/stated; `more_coming` flips; gate waits when the pipeline is first; clock from question 1; `touched`; answers with no refetch; `option_visual` cases; pictures never block ready; timing fields and log line; mixed-dimension option dropped.
- Changed existing tests: default numbers (30/60), snapshot shape, failure test (now patches both question paths), spend and trace tests, import allow-list.
- `pytest -k "clarify or gate"`: 184 passed. `ruff check`, `ruff format --check`, `mypy src`: clean. No live runs, no eval-fast (no prompt used by the evals changed; locked thresholds untouched).

## Open questions
- Page work (stage Q2): render `icon`/`swatches`, poll for `more_coming`, post `touched` on first interaction. Contract updated in `docs/api-contract.md`.
- Template wording and option sets are my picks; tune from real use.
- Real timings are unmeasured by design; read them from the log.

## Next command
`git diff main`, then after review `fly deploy` (Max deploys), then `fly logs -a room-redesign-shop | grep clarify_timing`.
