# Four failing tests on main (2026-11-08)

Branch: fix/failing-tests, from main at e5ee560 (PR #43). The four tests had failed since PR #41 (noted in `2026-11-06-shop-refine-b1.md`). PR #41 added a result-colour vision call and facet code on purpose; the tests had not caught up. All four are test fixes; no source file changed, and the shop pipeline behaves as before.

| Test | What was wrong | Fix |
|---|---|---|
| `test_rank_v4_is_used_for_shop_again_and_v1_for_a_typed_search` (`test_shop_again.py`) | It read `schema["title"]` of every model call. The colour call (`colortags.py`) uses a hand-written JSON schema, not a pydantic model, so it has no `title` (KeyError). | Test fix. Read the title with `.get`, and skip the colour call (None) when checking that the last two calls are PreferenceTags then RankedFinalists. Other hand-written schemas in the repo (the clarify calls) also carry no title, so the call was not changed. |
| `test_replay_two_candidate_tags_are_cached_in_the_session_and_not_repeated` (`test_shop_stage2e.py`) | Same KeyError, in the `tag_calls` helper that filters calls by schema title. | Test fix: `.get("title")`. The assertions about caching are unchanged and pass. |
| `test_every_model_call_in_shop_mode_asks_for_no_thinking` (`test_shop_thinking.py`) | It counts `complete(` calls under `src/app/shop` and expected 8; there are 9. | Test fix. The ninth is the colour vision call in `colortags.py`. It passes `disable_thinking=True` (the test's per-call check passes), so the rule still holds. Expected count is now 9, with a comment naming the call. |
| `test_only_the_clarify_flow_and_its_callers_import_the_spike` (`test_shop_clarify.py`) | `facets.py` and `colortags.py` import `app.shop.clarify_visual`, so the importer list was too short. | Test fix: allow the two modules. `clarify_visual` is a small pure helper (option icons and colour vocabulary), not the experimental spike (`clarify.py`, `clarify_verify.py`), and moving it would mean renaming a module and touching other importers for no gain. To keep the rule's intent, the test now also asserts that `facets.py` and `colortags.py` import nothing but `app.shop.clarify_visual`. |

## Results
- The four tests pass. Full suite: 2008 passed, 0 failed.
- `ruff check`, `ruff format --check`, `mypy` (the `make typecheck` scope: src, evals, scripts) and `make eval-fast` (mock, 2 cases, pass rate 1.0) are clean.
- Locked eval thresholds (`evals/shop/thresholds.json`, SHA-256 c64fe35a…d237) and all eval cases are untouched.

## CI
`.github/workflows/ci.yml` runs `uv sync`, `make lint`, `make typecheck` and `make test` on every push and pull request, and `make eval-fast` on pull requests. These are the commands run here, so the badge should go green. `make eval-fast` there gets a real `ANTHROPIC_API_KEY` secret, but the target forces mock mode. The workflow's push-to-main `make eval-standard` and the nightly `make eval-nightly` were not run (they are mock-forced targets that the brief did not list).
