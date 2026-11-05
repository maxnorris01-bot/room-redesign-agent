# 0017. Clarifying questions in the search job (stage Q1, flag off by default)

- **Status:** accepted for the backend only; behind `APP_SHOP_CLARIFY` (default off). No page uses it yet. Builds on ADR 0015 (the spike) and ADR 0016 (picture verification).
- **Date:** 2026-11-04

## Context

The spike showed that one question call on the first listings, with title-matched and vision-verified option pictures, can produce useful questions, and that answers map onto constraints
the pipeline already has. This stage puts that into the live search job so a page can show the questions while the search runs, without making a search slower when nothing is answered,
and without any change when the flag is off.

## Decision

1. **One flow in the same job** (`src/app/shop/clarify_flow.py`). While the request is parsed, page 1 of the raw request is fetched. If the parsed search text is materially the same
   (word overlap of 0.75 or more) the pipeline uses that page as its own page 1; if not, the page still feeds the question call and is billed. A side thread reads the first 25 listings,
   writes the questions (text only, shown at once: `clarify.state = "ready"`), then checks the option pictures and shows the verified https thumbnail URLs on a later poll
   (`pictures_state = "done"`). The pipeline goes on in the job's own thread.
2. **The apply point** is a `rank_gate` hook in `run_query`, called once the searches are done and right before the pool is cut and ranked (a typed search only). It blocks by the wait policy,
   freezes the answers, applies them with `apply_answers` and returns the new query. If the answers add search words, one more page-1 search runs and is merged into the pool (it counts as a
   Serper call and is kept first when the pool is cut). The seconds spent waiting are added to the hard stop, so the shopper's time is never the pipeline's.
3. **Wait policy** (`APP_CLARIFY_IDLE_GRACE_S` 6, `APP_CLARIFY_MAX_WAIT_S` 40; the bare `CLARIFY_*` names are read too): never wait when the questions failed or were skipped; questions still
   being written wait at most the grace, then are skipped; questions shown with no interaction wait the grace past ready-to-rank (past the moment they appeared, if later); with at least one
   answer the search waits for the submit, but never longer than the max wait after the questions appeared. A failure while waiting ends the wait. So no failure path waits longer than it would
   have without the feature.
4. **Answers** go to `POST /job/{id}/answers` (`docs/api-contract.md`): validated against the stored questions (unknown question ids ignored, an unknown option value is a 400 and records nothing),
   idempotent, `409` past the apply point (a repeat of what was applied is still `200`), `429` after 60 posts to one job. It is not a search and does not touch the search counters.
5. **Failure is silent to the shopper.** A failure in the question call, the picture checks or the wait marks the run `failed` or `skipped`, logs one `clarify:` line (class names only) and the
   search proceeds as it does today. A picture-check failure keeps the text questions.
6. **Spend.** The question call, the vision calls and the unused prefetch go through the search's own `Budget`, so they are inside the first search record's `anthropic_usd` / `provider_usd`, and
   the session, per-address and global caps count them like any other spend. Expected extra: about $0.0075 per first search (a live estimate, not measured here).
7. **Storage.** `Session.clarify` (questions as shown, answers, canonical category key) lives inside the session JSON, in the files locally and in the `shop_sessions.data` column in Postgres:
   no migration, and the 30-day purge, which deletes whole session rows, covers it. The field is left out of the saved JSON while empty, so a session saved with the flag off is still
   readable by the previous release (the model forbids unknown fields). A different canonical category clears it; another typed search in the same category keeps it, is not asked again and
   applies the stored answers.
8. **Shop Again** reuses the stored answers: `base_query(session, config)` returns the stored plain parse with the answers applied while the flag is on. The search record keeps the plain parse
   as `query` and the answered one as `used_query`, so request chips show what the shopper typed and a deleted answer chip (`POST /refine/remove`, id `answer:<question id>`) simply removes the
   stored answer.
9. **Pictures** are the providers' https thumbnails, shown by URL: no proxy, no stored images. No thumbnail URL or result is committed; the fixtures use placeholders.

## Choices the brief did not fix

- The text phase prunes by title only and instantly (an option no listing's title shows, a question left with fewer than two options). Vision only adds pictures: an option a picture check
  contradicted stays as text, because the label is still true of a listing's title; the count is in the trace.
- Questions are written from the first 25 listings of the raw request (`APP_CLARIFY_CANDIDATES`); the pictures are chosen from the whole first page.
- A synchronous `search()` call (no job) never asks: there is nobody to answer.
- The grace is counted from the later of ready-to-rank and the moment the questions appeared.
- The question call uses the same retrieval model as the parse, thinking off, and no retry (the visitor is waiting).
- In mock mode the service uses the canned callers from the spike harness (`app.shop.clarify_mock`, priced from their token counts) so the whole flow, and the spend counters, run for free.

## Consequences

Off: nothing changes (tests compare the status payload, the view keys and the results with the flag off). On: a first search of a category adds one model call and a few vision calls, and waits up to
6 s (40 s with an answer) for the shopper. The rollback is `fly secrets unset APP_SHOP_CLARIFY`. Quality of questions and lift are the spike's findings, not re-measured here.
