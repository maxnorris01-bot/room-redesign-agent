# Stage 3 shop eval: progress

| Phase | State |
|-------|-------|
| 1 Cases, personas, thresholds | finished |
| 2 Judges and scoring | finished |
| 3 Runner and Makefile targets | finished |
| 4 Reports, docs, mock end-to-end | finished |

## Phase 1
- `evals/shop/cases/` (12 JSON cases, `index.json`, `CHANGELOG.md`), `evals/shop/thresholds.json` and `THRESHOLDS.md`,
  `evals/shop/spec.py` (loaders, SHA-256 of the thresholds file), `evals/shop/coverage.py` (category findings).
- Repo conventions: existing evals use `evals/cases/*.jsonl`, `thresholds.yaml`, gitignored `evals/reports/*.json`, a
  `known-unstable/` folder and mock-forced Makefile targets. The brief asks for per-case JSON and `thresholds.json` under
  `evals/shop/`, so the shop eval follows the brief for file shapes and the repo for mock/live/report handling.
- Category findings (kept as written, no substitution): curtains (not in `categories.py`), dining chairs (no synonym
  group), bookshelves (plural not folded), desk chairs (folds into "desk").
- `pyproject.toml`: pytest `pythonpath = ["."]` so tests can import the `evals` package.
- Tests: `tests/test_eval_shop_spec.py` (10). Suite 1185.

## Phase 2
- `evals/shop/scorers.py` (price, repeats, sellers, near-duplicates via `nearby.match_reason`, context leakage, colour shares,
  refine exclusion and focus, judge-derived rates), `evals/shop/evaluate.py` (per-case results to threshold checks and a
  verdict: pass, fail, incomplete), `evals/shop/judges.py` (category/attribute judge and persona judge: forced tool call,
  strict schema, thinking off, batches of at most 12, thumbnails through `fetch_images`, index-validated, one retry, then
  "judge failed" = `None`, never scored).
- Prompts follow the repo convention (`prompts/eval_shop_category_judge.md`, `eval_shop_persona_judge.md`, version 1).
- Mock: deterministic judge rules in `src/app/mock_llm.py` (routed by the forced tool name), so the real judge code path runs.
- Tests: `tests/test_eval_shop_scoring.py` (39). Suite 1224.

## Phase 3
- `evals/shop/runner.py` (rounds, persona ratings of the first 12, refine step, guards, partial results, results JSON),
  `evals/shop/mockdata.py` (generic mock catalogue and a router that keeps the three existing fixtures),
  `evals/shop/plan.py` (estimate from configuration), `evals/shop/__main__.py` (plan, mock, live, report; live refuses without
  `EVAL_SHOP_CONFIRM=yes`, under CI and under pytest), Makefile targets `eval-shop-plan`, `eval-shop-mock`, `eval-shop-live`,
  `eval-shop-report`; results go to the gitignored `evals/shop/results/`.
- Not wired into `eval-fast`: that target runs the template's placeholder pipeline (`evals/run.py`), so the shop eval has its own
  mock target instead.
- Tests: `tests/test_eval_shop_runner.py` (22). Suite 1246.

## Phase 4
- `evals/shop/report.py`, `known_instability.md`, `README.md`, `docs/evals/README.md`; ADR 0012 amendment (stage 3), SPEC.md,
  docs/todo.md, working notes, session summary `docs/sessions/2026-10-26-shop-eval.md`.
- Fifth replay fixture added: `tests/fixtures/shop/owner_session_replay_5.json` (the live curtains session, exported with
  `scripts/shop_export_replay.py`), `REPLAY_FILE_5`, `tests/test_shop_replay_5.py` (3).
- `--case` option on the mock and live commands (re-run one case).
- Tests: `tests/test_eval_shop_report.py` (10; includes one full 12-case mock run). Suite 1259.
