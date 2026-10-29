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

## Judge noise: rejudge

```
make eval-shop-rejudge EVAL_SHOP_CONFIRM=yes RESULTS=evals/shop/results/shop-live-<run>.json [ARGS='--rounds 1,2 --cases case-06']
python -m evals.shop rejudge --results <file> [--rounds 1,2,3] [--cases case-01,case-02]
```

Re-runs the persona judge only (no search, no pipeline call) on the batches saved in a results file and
reports, per case and overall, how often the two judgements agree (the raw love/like/dislike answer, and the
match answer love-or-like against dislike that the lift uses) and how far the match rate moves. It prints its
estimated cost first (judge calls only: `ceil(items / 12)` per batch), refuses a live file without
`EVAL_SHOP_CONFIRM=yes` and under CI or pytest, writes its own gitignored `rejudge-*.json` beside the results
and never changes the source file. A mock results file is rejudged with the mock judge and needs no flag.

## Baseline

The first live run and its diagnosis are in `docs/evals/shop-eval-baseline-2026-10-26.md`. The results file keeps a compact trace
per batch (the parsed request's colour terms, the profile's chips, the alignment signals, the per-dimension decisions, the pool
counters; no titles or links) so a diagnosis does not have to guess what each Shop Again ran with.

## After the baseline (stage 2p)

- `CASES=case-04,case-06` narrows `make eval-shop-plan`, `eval-shop-mock`, `eval-shop-live` and `eval-shop-rejudge` (the CLI takes
  `--case a,b`; rejudge takes `--cases a,b`). The plan prints the cost of one Shop Again and of a 5-case re-run, from the measured
  baseline plus an estimate of the new calls.
- **The measurement changed.** The product-colour share is now what the category judge (prompt v2, `color_match`) says of each
  thumbnail; the baseline measured title keywords, which miss every title that names no colour. The threshold (70%) is unchanged;
  each report prints both figures (`## Product colour`), and the baseline document stays the record of the old measure. Judge
  versions are recorded in every results file. Cases and thresholds were not touched.
- A round with nothing unseen left is recorded as `exhausted` (the report says so), not `n/a`.
- The saved trace per batch keeps the typed-attribute quota and the photo tagging step (`trace.typed`), the thin-search variants
  (`trace.thin`) and why a Shop Again ran dry (`trace.exhausted`).



## Attribute match among decidable items (secondary)

The report prints, per case and overall, `good / (good + no)` for the attribute judge in round 1, with the count of `cannot_tell`
beside it (stage 2q). It is **secondary**: the verdict number, its threshold and its checks still count `cannot_tell` as a miss.
`cannot_tell` is common when listings state no size (case-06 on the 2026-10-28 re-run: 10 yes, 2 no, 12 cannot_tell of 24, which
is 42% in the verdict and 83% among the items the judge could decide), so the second figure shows how much of a low number is the
judge declining to decide rather than a mismatch. Whether `cannot_tell` should count is a threshold decision for the owner.

## Where shown items came from (secondary)

Each item in a results file carries `source` (`kind` and the search `query` that first found it: base, writer, seller, peer,
thin_variant, supply, refill; stage 2q). The report prints, per case and round, the shown items by source with their attribute and
persona match rates. It has no threshold. Results files from before stage 2q have no `source` and print "No source data".


## Serper cache, replay and lint (stage 2r)

`EVAL_SHOP_SERPER_CACHE` = `record` (default for `make eval-shop-live`: every live response is saved), `use` (a saved response younger than 14 days is
used and costs no credits; a miss goes live and is saved), `replay` (only saved responses; a miss is an error) or `off`. Files live under
`evals/shop/cache/serper/` (gitignored: they are real Google Shopping data), keyed by a hash of the request, holding the request, a timestamp and the
response body, never a header or a key. The app never reads them. A run with cache hits says "cached pool, not a fresh-world run" in the results file and
the report; the verdict logic is unchanged. `make eval-shop-replay` runs all 12 cases (or `CASES=...`) on the saved responses with the mock model, persona and
judges: zero spend, no network, structural checks only (batch sizes, sellers, price, repeats, calls and credits a live run would spend, fill-loop stop reasons,
guards off, what removed candidates): "structural only; not a quality result". It fails clearly when nothing is saved (run a live eval once). `make eval-shop-lint
RESULTS=<file>` lists anomalies in a saved live results file, most severe first. `python -m evals.shop lift --results <file>` is the offline lift diagnosis.

## Measurement changes after the baseline (stage 2r)

The refine focus check counts supply as unseen candidates that matched the focus and passed every filter (the pipeline's `trace.batch.focus_supply`) and scores a
round only when that count covers the quota the focus reserves; the baseline counted any title already shown. The threshold (0.5) is unchanged; the baseline
document is the record of the old measure, and the report footer says which measure a file used. (The product-colour measure changed in stage 2p.)
