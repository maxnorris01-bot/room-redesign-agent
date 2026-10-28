# Shop eval baseline: the first live run (2026-10-26)

This is the baseline for the Shop Mode eval: the report text for the first live run, `shop-live-20261027-041854-b00d9f`
(results file in the gitignored `evals/shop/results/`), followed by a diagnosis of the six failed checks. The report
section is exactly what `make eval-shop-report` renders for that file. The diagnosis reads the saved results and the
code; nothing was re-run live, and neither the pipeline nor `thresholds.json` was changed, so later runs stay comparable
with this one.

---

# Shop eval report: live run 20261027-041854-b00d9f

- Verdict: **fail**
- Started 2026-10-27T04:18:54+00:00, finished 2026-10-27T04:30:59+00:00
- thresholds.json SHA-256: `c64fe35a51c389603c507ed0a31e4388d08a1e96ee25f2d0bf9288f1fb75d237`
- Prompt versions: eval_shop_category_judge v1, eval_shop_persona_judge v1, shop_candidate_tags v5, shop_parse v2, shop_prefs v7, shop_queries v2, shop_rank v6, shop_refine v1
- Models: pipeline claude-haiku-5-5, rank claude-haiku-5-5, judges claude-haiku-5-5

## Thresholds

| Check | Result | Limit | Cases | |
|---|---|---|---|---|
| price compliance (worst batch) | 1.000 | >= 1.0 | 36 | pass |
| category match, mean of first batches | 0.996 | >= 0.9 | 12 | pass |
| category match, worst case | 0.957 | >= 0.75 | 12 | pass |
| attributes match, mean of first batches | 0.835 | >= 0.7 | 12 | pass |
| attributes match, worst case | 0.435 | >= 0.5 | 12 | FAIL |
| avoid-colour share of the first batch (worst) | 0.000 | <= 0.15 | 1 | pass |
| context colours in the search text | 0 | <= 0 | 1 | pass |
| product-colour share of the first batch | 0.208 | >= 0.7 | 1 | FAIL |
| near-duplicate groups, mean | 0.000 | <= 0.5 | 12 | pass |
| near-duplicate groups, worst case | 0 | <= 1 | 12 | pass |
| distinct sellers, fewest in a first batch | 6 | >= 6 | 12 | pass |
| top seller share, highest in a first batch | 0.333 | <= 0.3 | 12 | FAIL |
| items repeated across rounds | 0 | <= 0 | 12 | pass |
| refine exclusions respected (worst batch) | 1.000 | >= 1.0 | 2 | pass |
| refine focus share where supply exists | 0.417 | >= 0.5 | 2 | FAIL |
| Shop Again lift, round 2 minus round 1 (mean) | 6.186 | >= 15.0 | 12 | FAIL |
| cases where round 2 beat round 1 | 8 | >= 8 | 12 | pass |
| Shop Again lift, worst case | -19.570 | >= -10.0 | 12 | FAIL |
| first search, median seconds | 6.509 | <= 30.0 | 12 | pass |
| Shop Again, median seconds | 12.861 | <= 20.0 | 24 | pass |
| Shop Again, slowest seconds | 21.574 | <= 45.0 | 24 | pass |
| pipeline cost of one case, highest (USD) | 0.039 | <= 0.05 | 12 | pass |

## Cases

| Case | Request | Status | Category | Attributes | Persona r1 / r2 / r3 | Lift r2 | Sellers | Near-dups | Price | Search s | Again s | Pipeline $ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| case-01 | modern couches under $1000 | ok | 100% | 91% | 27% / 67% / 67% | 39.4 | 9 (top 18%) | 0 | 100% | 6.0 | 13.2 | 0.0221 |
| case-02 | coffee tables with a mix of wood and metal with storage | ok | 100% | 75% | 21% / 21% / 42% | 0.0 | 11 (top 17%) | 0 | 100% | 7.3 | 17.4 | 0.0390 |
| case-03 | boho curtains that pair well with a dark blue room | ok | 100% | 96% | 67% / 75% / 100% | 8.3 | 13 (top 17%) | 0 | 100% | 7.4 | 12.3 | 0.0209 |
| case-04 | mid-century dining chairs under $150 each | ok | 100% | 100% | 50% / 54% / 75% | 4.2 | 8 (top 33%) | 0 | 100% | 13.8 | 17.4 | 0.0355 |
| case-05 | small bookshelves for a studio apartment under $200 | ok | 100% | 96% | 33% / 46% / 47% | 12.5 | 10 (top 17%) | 0 | 100% | 8.3 | 13.2 | 0.0303 |
| case-06 | 8x10 neutral area rugs under $400 | ok | 96% | 43% | 70% / 50% / n/a | -19.6 | 9 (top 17%) | 0 | 100% | 6.3 | 7.1 | 0.0100 |
| case-07 | brass floor lamps | ok | 100% | 88% | 58% / 67% / 67% | 8.3 | 13 (top 17%) | 0 | 100% | 4.4 | 10.7 | 0.0200 |
| case-08 | queen bed frames with storage in wood | ok | 100% | 75% | 50% / 62% / 75% | 12.5 | 12 (top 17%) | 0 | 100% | 6.2 | 11.5 | 0.0181 |
| case-09 | round black metal side tables | ok | 100% | 67% | 58% / 50% / 54% | -8.3 | 11 (top 17%) | 0 | 100% | 6.0 | 10.0 | 0.0166 |
| case-10 | ergonomic desk chairs under $300 | ok | 100% | 88% | 71% / 88% / 50% | 16.9 | 6 (top 24%) | 0 | 100% | 6.8 | 13.1 | 0.0355 |
| case-11 | sectional sofas for a small living room | ok | 100% | 100% | 71% / 62% / 75% | -8.3 | 19 (top 12%) | 0 | 100% | 7.6 | 11.1 | 0.0202 |
| case-12 | navy velvet accent chairs | ok | 100% | 83% | 46% / 54% / 42% | 8.3 | 16 (top 17%) | 0 | 100% | 5.2 | 13.8 | 0.0247 |

## Shop Again lift (persona match rate, points)

- Round 2 minus round 1: mean +6.2, min -19.6, max +39.4; improved in 8 of 12 cases
- Round 3 minus round 1 (no gate): mean +12.8, min -20.6, max +39.4
- Per case (r2, r3): case-01 39.4/39.4, case-02 0.0/20.8, case-03 8.3/33.3, case-04 4.2/25.0, case-05 12.5/14.0, case-06 -19.6/n/a, case-07 8.3/8.3, case-08 12.5/25.0, case-09 -8.3/-4.2, case-10 16.9/-20.6, case-11 -8.3/4.2, case-12 8.3/-4.2

## Category coverage findings

Where `categories.py` cannot represent a request; kept as written.

| Case | Noun | Finding | categories.py says | Expected |
|---|---|---|---|---|
| case-03 | curtains | not in the synonym groups or size table | curtains | curtains |
| case-04 | dining chairs | singular and plural are different topic keys | dining chairs | dining chair |
| case-05 | bookshelves | not in the synonym groups or size table | bookshelves | bookshelf |
| case-10 | desk chairs | folds into another category | desk | desk chair |

## Refine steps

- case-02: "no Walmart" -> no Walmart (exclude_seller)
- case-10: "focus on mesh backs" -> focus on mesh backs (free_note, approximate)

## Cost and time

- Anthropic $0.1442 (pipeline and judges), Serper 204 credits
- Judges: 134 calls, 0 retries, 0 items judge-failed, $0.0553, thumbnails used 1562 of 1562
- First search: median 6.5 s; Shop Again: median 12.9 s, max 21.6 s

## Limits of this eval

The judges are Haiku, the same model family as the pipeline's tagger and ranker, so blind spots may be shared. The persona is a text description, not a person, and neither judge has been calibrated against human labels yet (docs/todo.md). Treat one failing case as a prompt to re-run it once (evals/shop/known_instability.md), not as a verdict.

---

## Diagnosis

**Method, and what the saved file cannot show.** The results file keeps, per batch, every shown item with its judge
answers and the persona's rating, plus the footer, the duplicate counts, the warnings and the refine chips. It does not
keep the chips, converged values, leanings or the parsed request that each Shop Again ran with (the runner saved a trimmed
trace; it now keeps those, see "Harness changes"). Where a question needed them I say so, and where I could rebuild a
number I say how: the seller, tier and marketplace decisions depend only on seller names and ratings, so they were
recomputed exactly with `evidence.py` from the saved ratings; leanings and converged values depend on item tags that were
not saved (the live run used model and photo tags), so I recomputed them from title keywords as a lower bound. Judge
agreement needs a second judgement of the same items; the `rejudge` command below produces it. Nothing here quotes a
product title.

Cost, from the saved totals: Anthropic $0.144 (pipeline $0.089, judges $0.055 over 134 calls) and 204 Serper credits,
against the plan's estimate of $0.43 and 240 credits. The estimate was about three times too high on dollars (it assumed
$0.01 per round for the pipeline; the run averaged about $0.0025). The hard stops were never near.

### 1. Case 12, navy velvet accent chairs: 20.8% navy

- **What the scorer measures.** `scorers.product_color_share` counts a result as navy only when `vocab.title_value("color",
  title)` finds `navy_dark_blue` in the **title**. A title that names no colour is "unknown" and counts as not navy.
- **First batch.** Of 24 items, 5 titles name navy, 1 names blue, 18 name no colour (round 2: 6 / 2 / 12 of 24 plus 4
  metallic; round 3: 5 / 0 / 17). 20 of 24 titles say velvet, 18 say accent. The pipeline's own colour tags for these items
  (photo tags for shown items) were not saved.
- **How the parse and pipeline treat the colour.** The request parser puts "navy" in `ShopQuery.color_terms`
  (`parse.py`, `own_colors`; "velvet" stays a free-text material term). `color_terms` is used in exactly three places: one
  ranker line, "Colors the product itself must be: navy" (`search.py:setting_lines`, `shop_rank` v6); the typed-dimension set
  that makes the diversity guard leave colour alone (`decide.py:typed_dimensions`); and the request trace. It is not a
  filter, not a second query (the colour query exists only for context colours), and not a reserved share of the batch.
  Typed materials and must-haves are treated the same way: a ranker line and nothing else.
- **The ranker cannot see the colour.** `rank_user_text` sends titles, prices and sellers, never images. Candidate tags
  (`candidates.py`) read titles only ("never an image"); only already-shown items are tagged from photos, in the background
  preparation. So when a title names no colour, neither the candidate tag nor the ranker knows it.
- **Verdict.** Partly a measurement artifact, partly a real gap, and I cannot say how much of each. The 21% is a floor
  set by title keywords: 75% of titles are silent, and the photos of those items may well be navy (the persona, which sees
  photos, loved 4 of the 24 and liked 7; weak evidence only). It is also a real defect in design terms: a typed product
  colour is a soft hint to a ranker that cannot see colour, so nothing in the pipeline can raise the share. Retrieval versus
  selection cannot be separated because the 40-candidate pool was not saved (the runner now keeps the pool's composition counts).
- **What would fix it.** Measurement (eval only, small): add a `color_match` field (yes, no, cannot tell, against the
  case's product colour, from the photo) to the category judge (`evals/shop/judges.py`, prompt `eval_shop_category_judge`
  v2) and score the share from that; also add navy to case-12's stated attributes. Pipeline (separate stage, medium): tag
  the colour of the top candidates from their thumbnails before selection (`candidates.py` already supports images for
  preparation; one extra image call per 40 candidates, about 12 thumbnails of 150 tokens per 12 items, a fraction of a
  cent), and give a typed product colour a minimum share of the batch the way a focus chip has one (`search.py` selection,
  reusing `REFINE_FOCUS_MIN_SHARE`), plus a colour-first second query as for suggested colours.

### 2. Case 10, "focus on mesh backs": 41.7%

- **Which batch failed.** The check takes the worst batch after the refine: round 2 was 54.2% (13 of 24 titles say
  mesh), round 3 was 41.7% (10 of 24). Round 1, before the refine, was already 41.2% (7 of 17). So the refine moved the
  share up 13 points once and not again; supply was never the limit.
- **Why a free note.** `refine.validate_item` accepts a `focus_value` only as `dimension:value` with a value in the fixed
  vocabulary (`refine._dimension_value`). The material vocabulary is wood, metal, glass, stone, upholstered
  (`vocab.py`); there is no "mesh" and no back-type dimension, so the interpretation became an approximate `free_note`
  (the saved chip says `free_note`, approximate).
- **How a free note is used.** Only as text: one ranker line, "Notes from the shopper (soft hints, approximate)"
  (`refine.ranker_lines`), and one query-writer line (`refine.writer_lines`). The focus reservation (`REFINE_FOCUS_MIN_SHARE`,
  `again.refine_trace`) applies to `focus_value` and `focus_seller` chips only, and the ranker sees titles only, so a
  note about a back type reaches a model that cannot see the back and reserves nothing.
- **How 41.7% was measured.** `scorers.focus_share`: titles containing "mesh", over the batch. A title that says nothing
  about the back counts as a miss, so the measure is a floor.
- **General fix (separate stage).** Per-item match flags for each active free note: the candidate tagging call
  (`candidates.py`, `record_tags`) gets the note text and returns one extra boolean per row per note; the flags are cached
  in the tag book and counted by the same focus reservation (treat a note as a pseudo focus value in `again.py`). Cost:
  about 15 prompt tokens per note per call and one extra boolean per row (about 6 output tokens in the tool JSON, so about
  240 per 40-row call), which is roughly $0.0001 per call at Haiku rates; complexity medium (schema and prompt version bump
  for the tagging call, `TagBook` storage, selection reservation, trace, tests), about a day. Titles alone will often not
  show a back type, so the flags are only as good as the information given; for shown items the photo can carry it.
- **Vocabulary entry.** Adding `mesh` to the material values (and keywords) is about an hour: one tuple in `vocab.py`, the
  tagging prompts pick it up through `vocab_text`, tests. It fixes this request and desk chairs generally, costs a few
  prompt tokens, and does nothing for the next unlisted word ("fluted", "swivel", "with arms"). Worth doing as a quick win
  next to, not instead of, the general fix.

### 3. Case 06, 8x10 neutral area rugs under $400: 43% attributes, lift -19.6, no round 3

- **Why 43%.** Round 1, 23 items: attributes `yes` 10, `cannot_tell` 12, `no` 1. The one `no` is a wrong size. Seven of the ten
  `yes` titles state 8x10; none of the twelve `cannot_tell` do, and several name no colour either. So the stated attributes
  are "8x10" and "neutral", titles often omit both (size lives in a variant selector), and the scorer counts `cannot_tell`
  as not matching. Nearly all of the shortfall is the judge being unable to tell, not contradicted.
- **How sizes are used.** `must_have` carries "8x10" as text to the ranker (`search.rank_user_text`). The size table in
  `categories.py` (rug 24 to 180 inches) and `resolve_size` only produce the card's size hint from a title; nothing filters or
  reserves by size.
- **Why no round 3.** The footer shows 0 Serper calls, 1.0 s, "0 seen or rated, 0 duplicates, 0 filtered out", no pool. The
  request has one search phrasing and no learned phrasings; round 1 fetched page 1, round 2 made 2 calls (pages 2 and 3 of the
  same phrasing; `BASE_PAGE_MAX` is 2 and page 3 is a last resort); in round 3 the next page is 4, which is never fetched
  (`search.run_query`, `eligible` and `LAST_RESORT_PAGE`), and the model query writer (the $0.0002) added nothing new. That
  fits every counter in the file; the query plan itself was not saved, so it is an inference, not a reading.
- **Chips and learned preferences.** Baseline like rate after round 1 was 92% (11 of 12 rated up), so no seller, tier,
  marketplace or leaning can reach its 1.5 times test: nothing was learnable (exact). Round 2's footer reads 2 of 24
  matching a decided signal, so something was decided; the file does not say what.
- **Why the match fell from 70% to 50%.** Round 1 was the top of the first page and already high; round 2 is deeper pages of
  the same phrasing (attributes 43% to 38%, 3 multicolour patterns). Four of the 12 cases started at 65% or more and moved
  by -0.7 points on average, against +9.6 for the other eight: regression from a high first batch, not a learned error.

### 4. Case 04, top seller 33%

- Lowe's, 4 of 12 items. The pipeline's own cap is `SELLER_DEFAULT_CAP = 4` results per seller, an absolute number; the
  first batch had only 12 items (16 candidates after filters for a price cap of $150 each), so 4 is 33%. Round 2 (24 items)
  had a top seller at 12.5% and round 3 (12 items) at 25%.
- Not a naming problem: `seller_key` of the saved seller names gives the same groups as the scorer; there is no variant of
  Lowe's in the batch. Not a relaxation either: the cap held at 4. It is a mismatch of kinds: the pipeline caps a count, the
  threshold caps a share, and they disagree whenever a batch is smaller than 14 items (4 of 13 is 31%).

### 5. Lift, all 12 cases

Persona match rate = love or like of all shown items. Columns: ratings submitted in round 1 and round 2 as
love/like/dislike; match rates in rounds 1 / 2 / 3; what the evidence layer could have decided from the ratings before round
2 and before round 3 (sellers, tier and marketplace exact; leanings from title keywords, a lower bound; converged values not
determinable); the class of a case that is flat or worse.

| Case | Round 1 ratings | Round 2 ratings | Match r1 / r2 / r3 | Evidence before round 2 | Evidence before round 3 | Class |
|---|---|---|---|---|---|---|
| case-01 | 1/2/8 | 2/6/4 | 27% / 67% / 67% | lean toward upholstered (material) | seller: Wayfair; lean toward white (color) | none needed |
| case-02 | 0/3/9 | 0/4/8 | 21% / 21% / 42% | retailers like Target | seller: IKEA; seller: Target; lean away from rectangular (shape); lean away from industrial (style) | (b)+(e) |
| case-03 | 3/6/3 | 1/10/1 | 67% / 75% / 100% | none | none | (a) |
| case-04 | 2/4/6 | 1/3/8 | 50% / 54% / 75% | lean away from upholstered (material); lean toward wood (material); lean toward warm wood (finish); lean toward brown (color) | lean away from upholstered (material); lean toward wood (material); lean toward warm wood (finish); lean toward brown (color) | (e) |
| case-05 | 1/3/8 | 1/6/5 | 33% / 46% / 47% | exclude marketplaces | exclude marketplaces; lean toward wood (material) | (e) |
| case-06 | 4/7/1 | 3/4/5 | 70% / 50% / n/a | none | none | (a)+(e) |
| case-07 | 2/6/4 | 2/6/4 | 58% / 67% / 67% | none | none | none needed |
| case-08 | 1/7/4 | 2/8/2 | 50% / 62% / 75% | none | none | none needed |
| case-09 | 0/7/5 | 1/5/6 | 58% / 50% / 54% | none | none | (b)+(d) |
| case-10 | 2/6/4 | 1/11/0 | 71% / 88% / 50% | none | none | (a)+(e) |
| case-11 | 1/7/4 | 1/8/3 | 71% / 62% / 75% | none | none | (a)+(d) |
| case-12 | 3/1/8 | 1/5/6 | 46% / 54% / 42% | lean toward modern (style) | none | (b)+(d) |

Classes: (a) ceiling, round 1 match at 65% or more; (b) not enough evidence reached the thresholds, so nothing was learned;
(c) something learned was wrong or too strong; (d) judge noise; (e) other (thin or drifting supply). No case is a clear (c):
none of the recomputed chips contradicts its persona. Two come near it: case 02's seller and tier chips rest on three likes of
twelve (a weak signal that the 1.5 times rule let through), and case 01's round 3 "lean toward white" and seller Wayfair
chips; both are cheap, soft, and did not hurt.

- **Cases with a lift of 0 or less:** 02 (0.0), 06 (-19.6), 09 (-8.3), 11 (-8.3). **Round 3 worse than round 2:** 10 (-37.5
  points), 12 (-12.5), 09 is not (r3 above r2). Classes are in the table.
- **Cases that produced at least one new chip or leaning in round 2:** 5 of 12 by the recomputation (01, 02, 04, 05, 12), as
  a lower bound for the evidence layer; round-2 converged values and the exact alignment signals cannot be read from the file.
  Footers that read "0 of 24" (cases 03 and 07 in rounds 2 and 3, case 11 in round 3) are most likely "no decided signal" (the
  runner printed 0 of 24 for both cases; it now prints "none yet" when there is no signal, like the page).
- **Noise.** The spread of the 12 round-2 lifts has a standard deviation of 14.3 points. Two independent 24-item batches at a
  55% match rate differ by 14.4 points (binomial), so the spread of per-case lifts is what sampling alone predicts: a single
  case moving by 8.3 points is two items. The mean lift is +6.2 with a standard error of 4.1: positive and about 2.2 standard
  errors under the +15 goal. Round 3 minus round 1 averages +12.8 (11 cases). So the per-case "worst" threshold of -10 sits
  inside one standard deviation of noise and cannot be a stable gate by itself, while the mean is informative.
- **Display order carries signal.** In round 2 the first 12 items matched at 64% against 51% for the next 12 (11 cases with
  24 items), so the ranker's ordering tracks the persona; the 12 rated items are the better half.

### 6. Judge noise: the rejudge command

`python -m evals.shop rejudge --results <file> [--rounds 1,2,3] [--cases ...]` (target `make eval-shop-rejudge`, needs
`EVAL_SHOP_CONFIRM=yes`, refuses under CI or pytest) re-runs only the persona judge on the saved batches and reports, per case
and overall, the agreement of the raw three-way answer and of the match answer, and the change in match rate. For this file it
estimates 35 non-empty batches (case 06 round 3 is empty), 781 items, 67 judge calls, about $0.031 (the live run's 134 judge
calls cost $0.055). It was not run. Until it is, the binomial argument above is the only noise estimate; the rejudge measures
the judge's own repeatability, which is a part of that noise.

**Rejudge result (recorded afterwards; the baseline numbers above are unchanged).** The owner ran the rejudge on run
`20261027-041854-b00d9f`: 35 batches, 781 items. The persona judge agreed with itself on 90% of the three-way answers
(love, like, dislike) and on 93% of the match / no-match answers (love or like against dislike). The mean absolute difference
in a batch's match rate between the two judgements was 3.6 points, the mean difference -0.3 points, the spread (standard
deviation) 6.1 points, and the cost $0.0256. The noisiest cases were case-12, case-04, case-08, case-09 and case-11 (85 to 88%
three-way agreement). Reading: judge noise on one case's lift is about 6 points, which is about 1.8 points on the 12-case mean
(6.1 divided by the square root of 12). The mean lift shortfall against +15 (+6.2 measured) is therefore not judge noise. The
sampling argument above (14.3 points between two different batches) still holds: most of the spread of single-case lifts is
the batches, not the judge.

### 7. Ranking of the findings, and what is real

Expected impact on the failed checks and on real use, against effort:

1. **Shop Again can come back empty or short.** Case 06 round 3 returned nothing after two rounds; four first batches held
   fewer than 24 items (11, 12, 17, 23). Real use, visible, and a pipeline defect (the paging policy and a single phrasing leave
   no query to run). Fix: when the planned phrasings are used up, derive new ones (category synonyms, the model query writer
   forced to differ, or page 4 when the pool is empty). About a day. Also unknown: how much of the short first batches is price
   filtering after retrieval; not investigated here.
2. **A typed product colour is not enforced** (case 12). Real use, likely a real defect, size unknown until colour is measured
   from photos. Measurement fix is small (1, below); the pipeline fix is medium (photo colour tags for top candidates plus a
   reserved share).
3. **Free-note refinements do nothing measurable** (case 10). Real defect for any instruction outside the vocabulary. Vocabulary
   entry: an hour. General per-item note flags: about a day.
4. **Category folding** (findings from stage 3): "desk chairs" is the topic key "desk", so a desk search followed by a desk-chair
   search keeps the desk preferences; "dining chair"/"dining chairs" and "bookshelves" are not folded. Real, small (an hour in
   `categories.py`), moderate impact.
5. **Seller cap in absolute numbers** (case 04). Low impact; a design mismatch with the share threshold. An hour if wanted
   (`cap = min(4, ceil(0.30 * n))` for small batches).
6. **Attribute match counts "cannot tell" as a miss** (case 06). Measurement: nothing to fix in the pipeline; the eval could
   report `no` separately from `cannot_tell`.

Which failed checks are probably real, which measurement or threshold shape, which undecidable now:

| Failed check | Reading |
|---|---|
| `attribute_case_min` (case 06, 43.5%) | measurement: 12 of 13 misses are "cannot tell", 1 is a wrong size |
| `product_color_share_min` (case 12, 20.8%) | partly measurement (title keywords), probably also real (colour not enforced); undecidable without a photo-based colour measure |
| `top_seller_share_max` (case 04, 33.3%) | threshold shape: an absolute cap of 4 on a 12-item batch; thin supply is the real cause |
| `refine_focus_share_min` (case 10, 41.7%) | real defect (free notes reserve nothing), measured as a floor from titles |
| `lift_mean_min_points` (+6.2) | probably real but modest, with ceiling effects; the noise part is undecided until the rejudge |
| `lift_case_worst_min_points` (-19.6, case 06) | threshold shape: inside the noise of a 24-item batch, and a high first batch regressing; undecided until the rejudge |

### Ranked fix list (estimated effort)

1. Eval: add a photo-based `color_match` to the category judge and report `cannot_tell` apart from `no` (about 2 hours; changes
   the eval's prompt version, so it needs its own baseline note).
2. Run `make eval-shop-rejudge` on this file ($0.03) before acting on any lift number.
3. Pipeline: new phrasings when a Shop Again has none left (about 1 day).
4. Pipeline: a typed product colour as a reserved share plus photo colour tags for the top candidates (about 1.5 days).
5. Pipeline: free-note match flags in candidate tagging (about 1 day), after adding `mesh` and similar words to the vocabulary
   (about 1 hour).
6. Pipeline: fold "desk chair", "dining chair" and "bookshelves" plurals in `categories.py` (about 1 hour).
7. Pipeline: seller cap as a share on small batches (about 1 hour), only if the share threshold is kept.
8. Run the live eval two or three times and record the spread in `known_instability.md` (the cost is about $0.15 and 204 credits
   per run).

### Harness changes made while diagnosing (eval code only)

- The saved trace now keeps, per batch, the parsed request's colour terms and counts, the profile's chips with their status, the
  alignment signals, the per-dimension decision, the marketplace decision and the pool counters (no titles, no links).
- The runner's footer line says "matches your preferences: none yet" when no signal was decided, as the page does.
- `rejudge` (above), with `plan.judge_call_usd` factored out of the estimate.

### What was deliberately not changed

`evals/shop/thresholds.json` and `THRESHOLDS.md` are untouched, and the file's hash is the one recorded in the run
(`c64fe35a51c389603c507ed0a31e4388d08a1e96ee25f2d0bf9288f1fb75d237`). No pipeline file (`src/app/shop/`) was changed, no case
or persona was changed, and no prompt of the pipeline was versioned; the judge prompts stay at version 1. The failed checks
therefore stay failed in this baseline, and a later run is compared with it on the same cases, thresholds and prompts.
