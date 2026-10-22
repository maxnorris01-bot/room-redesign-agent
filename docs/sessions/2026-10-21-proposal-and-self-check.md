# Session 2026-10-21: proposal and self-check

Branch `proposal-and-self-check`, from `main` after the two-stage shopping PR (#5) merged. Mock mode
only: no live call, no `make spike-live`, `.env` not read; tests use obviously fake key strings.

## What was added

- **Proposal** (`src/app/proposal.py`, `prompts/proposal.md` v1): one structured call to
  `APP_MODEL_PROPOSAL` (default `claude-sonnet-5-5`, `APP_EFFORT_PROPOSAL=low`). Per slot it returns a
  pick id (empty = leave the slot empty), alternate ids, a rationale and a fit note, plus a summary.
  Code validates every id against that slot's own finalists, drops invented ones, and builds each item
  from the real `ProductCandidate` (price, shipping, total, url, url_kind, dimensions). Extra fields
  the model writes (a price, a URL) fail schema validation. A slot with no finalists is carried with
  the retrieval note; if no slot has finalists the model is not called.
- **Self-check** (`src/app/selfcheck.py`): budget (total; per-item cap `APP_ITEM_CAP_FRACTION`=0.6 unless
  the only item; unknown shipping counts as 0 and warns), fit (code only, coarse; unknown with the text
  "no size found; check the product page"; "room size is a low-confidence estimate" below 0.5), style
  (one text-only Haiku call, `prompts/style_check.md` v1, `APP_MODEL_STYLECHECK`; poor fails, ok passes),
  grounded (code: id among finalists, link kind, price source, product-page URL). The style call runs
  in a worker thread while the code checks run.
- **Revision**: any failure (unknown does not count) triggers up to `APP_MAX_REVISIONS` (default 1; 0
  disables) calls to the proposal model with the previous picks and the failed checks; all four checks
  rerun. Failures that remain become warnings on the item (or on the proposal for the total). Output
  records `revised` and `revision_changes`.
- **Output**: `result.json` has `proposal` (items with real pick and alternates, fit and style outcome,
  warnings; totals; summary; the check results; `check_summary`; `revised`; `revision_changes`;
  proposal-level warnings), `caps`, and `proposal_dropped` (invented ids). After the stage table the
  script prints the final list and the totals line.
- **Rows and timing**: the stage table gains `proposal`, `self-check / code`, `self-check / style` and,
  if it ran, `revision` and `revision / self-check / ...`. `critical_path_seconds` is prepare_image +
  perception + slowest retrieval slot chain + proposal + slower of the self-check pair + revision +
  slower of its re-check pair. A new `perception_retrieval_seconds` keeps the informational comparison
  to the old proposed 30 s target meaningful.
- **Cost caps**: `APP_MAX_COST_USD` (0.25) is Anthropic dollars only; new `APP_MAX_PROVIDER_COST_USD`
  (0.30) bounds estimated provider dollars. `Budget` keeps the two totals apart
  (`add_provider_cost`); `check_budget` has a third argument. The table and `result.json` totals show
  both subtotals. ADR 0004's consequence line is updated.
- **CLI**: `--no-proposal` (previous behaviour), `--revisions N`, `--proposal-model`,
  `--proposal-effort`.
- **Mock mode**: deterministic proposal (first retailer-linked finalist, the next as alternate, one
  invented alternate id that validation visibly drops; on revision, the lowest total) and style
  responses (everything "good" unless a title contains `POOR-STYLE`). `mock_llm.proposal_payload` and
  `style_payload` build scripted responses for tests (over-budget first proposal, poor style match,
  missing dimensions).
- Docs: ADR 0006 (proposed), SPEC.md stages 5 to 7, ADR 0004 consequence, `.env.example`, todo,
  working notes.

## The default mock run

`make spike` exercises the whole path, including the revision: the first proposal picks the first
retailer-linked finalist per slot (Article, $308 each, $924 against $800), the budget check fails, the
revision picks the lowest totals ($136.31 each, fixture Google-panel links), and the final list shows
the remaining warnings (shipping unknown, link is a Google Shopping page) and "fit: unknown". That is
fixture behaviour, not a quality signal.

## Tests

231 passed (193 before). New `tests/test_proposal.py` (38): id validation and scoping, items from real
data, the model cannot supply prices or URLs, empty and no-finalist slots, every check's pass, fail and
unknown paths (including the low-confidence note and inclusive boundaries), style fail triggers a
revision, a revision fixes an over-budget list, a still-failing list is shown with warnings, revisions
0, a worse revision discarded, a garbled revision and a failed style call degrade safely, text-only
style request, critical path arithmetic, separate caps tripping independently, the step count for a
three-slot run with a revision (8 of 12), `result.json` shape, the printed final list, CLI flags, and
fake keys absent from every output. Existing shopping tests were adjusted for the split caps. Ran the
suite three times. `make lint`, `make typecheck`, `make test`, mock `make eval-fast` pass; thresholds
untouched.

## Decided differently or worth knowing

- **A revision that makes things worse is discarded** (more failed checks than the original): not in the
  brief, added so a revision cannot degrade the list; the output says so.
- **A failed style call degrades to "unknown"** and a garbled revision keeps the previous proposal with a
  warning, so one bad optional call does not lose a good proposal. A bad first proposal still raises.
- **Fit will falsely fail rugs and other large flat items** (rule applied literally), which can waste a
  revision. Flagged in the ADR and todo; not special-cased.
- **Web-search mode always fails the grounded check** (its prices come from the model). By design.
- **Dimensions**: only width and depth are used, per the brief; a lookup with only a height gives
  "unknown".
- Nothing was measured live. Whether the proposal and style prompts behave, what they cost and how long
  they take (ADR 0004's 35 s full-run guess), and how often the revision fires, are all unknown.

## Next

```bash
make spike
make spike ARGS='spike-photos/room1.png --budget 800 --style "warm minimalist" --no-proposal'
```
Then, Max's call with keys in `.env`: a live full run, compared against ADR 0004.
