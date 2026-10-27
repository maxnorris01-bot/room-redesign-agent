# Shop Mode eval (stage 3)

Twelve requests, each with a hidden persona, scored on first-batch quality and on whether Shop Again
moves results toward what the persona likes. Built in mock mode; the live run is the owner's call.

```
make eval-shop-plan                                  # cases, personas, thresholds hash, estimated cost; spends nothing
make eval-shop-mock                                  # the whole harness on fixtures and deterministic judges (free, CI-safe)
make eval-shop-live EVAL_SHOP_CONFIRM=yes            # live providers: real Anthropic and Serper spend
make eval-shop-report RESULTS=path OUT=docs/evals/x.md   # Markdown report from a results file (default: newest)
```

## What a run does

For each case (`cases/case-NN.json`, listed in `cases/index.json`, versioned, see `cases/CHANGELOG.md`):
search through the service layer; judge every shown item (category and attribute judge, persona
judge); the persona rates the first 12 in display order (Love, Like or Dislike by its answer); apply
the case's refine step, if any; Shop Again; judge and rate again; Shop Again a third time and judge.
Automatic lookups, Open and the gallery are off (no SerpApi). After every step the running spend and
credits are checked against the hard stops; at a stop partial results are written with the reason.

The pipeline receives the request, the ratings and the refine text. Persona text goes to the persona
judge only. Judges see one request or persona and numbered listings (title, price, seller,
thumbnail), never tags, chips, ranker output or other items.

## Files

| File | What |
|------|------|
| `cases/` | 12 case files, index, changelog (personas live here, never shown to the pipeline) |
| `thresholds.json`, `THRESHOLDS.md` | the numbers and the reason for each; judgment values set before any live run; SHA-256 recorded in every results file |
| `spec.py`, `coverage.py` | loaders, the hash, category coverage findings |
| `scorers.py`, `evaluate.py` | code scorers and threshold evaluation |
| `judges.py` | the two model judges (prompts in `prompts/eval_shop_*_judge.md`) |
| `runner.py`, `plan.py`, `report.py`, `__main__.py` | the run, the estimate, the report, the CLI |
| `mockdata.py` | the generic mock catalogue for cases without a fixture |
| `known_instability.md` | what varies run to run and how to treat one failing case |
| `results/` | one JSON per run (gitignored: third-party titles and thumbnail URLs) |

## Conventions followed, and where the repo differs from the brief

The repo's existing evals use `evals/cases/*.jsonl`, `evals/thresholds.yaml`, gitignored
`evals/reports/*.json`, a `known-unstable/` folder and Makefile targets that force mock (plain) or
live (`-live`). The shop eval follows the repo for mock/live handling, results and the
known-unstable convention, and the brief for file shapes (one JSON per case, `thresholds.json`).
It is not wired into `make eval-fast`, which runs the template's placeholder pipeline.

## What this cannot tell you

Judges are Haiku, the pipeline's own model family, so blind spots may be shared; personas are texts,
not people; neither is calibrated against human labels (see `docs/todo.md`). A mock run shows that the
harness works, not that Shop Mode is good. Twelve requests are a small sample.
