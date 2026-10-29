# Shop eval: the first full 12-case run after stages 2p and 2q (2026-10-29)

Run `20261029-034000-3f9fa0` (results file in the gitignored `evals/shop/results/`), 12 cases, three rounds each, live. It sits beside
a three-case run, `20261029-033423-ddf257` (cases 04, 06 and 12), made a little earlier. **This is a single run.** The standard error of
the mean round-2 lift is about 3.5 points (sd of the 12 per-case lifts over the square root of 12), the persona judge disagrees with itself
by about 6 points on one case's lift and about 1.8 on the 12-case mean, and one batch of 24 gives a per-case lift noise of about 14 points.
**Nothing here replaces the baseline** (`docs/evals/shop-eval-baseline-2026-10-26.md`); thresholds and `thresholds.json` (SHA-256
`c64fe35a...d237`) were not changed. Verdict: fail, 3 of 22 checks failed. Anthropic $0.2254 (pipeline and judges), 226 Serper credits;
the judges made 138 calls for $0.0615. Prompts: shop_queries v4, shop_candidate_tags v5, shop_rank v6, category judge v2, persona judge v1.

## Per case
| Case | Request | Category | Attributes r1 (yes/no/cannot_tell) | Persona r1 / r2 / r3 | Lift r2 / r3 | Results r1 / r2 / r3 | Sellers r1 (top) | Search s / Again s (median) | Pipeline $ / credits |
|---|---|---|---|---|---|---|---|---|---|
| case-01 | modern couches under $1000 | 100% | 75% (18/2/4) | 29% / 40% / 50% | +10.8 / +20.8 | 24 / 5 / 4 | 14 (17%) | 17.2 / 8.0 | $0.0246 / 18 |
| case-02 | coffee tables with a mix of wood and metal with storage | 96% | 88% (21/0/3) | 17% / 21% / 38% | +4.2 / +20.8 | 24 / 24 / 24 | 10 (17%) | 11.0 / 21.9 | $0.0530 / 38 |
| case-03 | boho curtains that pair well with a dark blue room | 100% | 100% (24/0/0) | 58% / 50% / 67% | -8.3 / +8.3 | 24 / 24 / 24 | 14 (17%) | 9.7 / 14.3 | $0.0294 / 14 |
| case-04 | mid-century dining chairs under $150 each | 100% | 83% (20/0/4) | 62% / 65% / 82% | +2.7 / +19.9 | 24 / 23 / 17 | 10 (25%) | 12.9 / 25.5 | $0.0399 / 28 |
| case-05 | small bookshelves for a studio apartment under $200 | 100% | 100% (24/0/0) | 38% / 67% / 58% | +29.2 / +20.4 | 24 / 24 / 19 | 7 (21%) | 11.7 / 14.9 | $0.0350 / 20 |
| case-06 | 8x10 neutral area rugs under $400 | 100% | 50% (12/2/10) | 46% / 60% / 79% | +14.2 / +33.3 | 24 / 15 / 24 | 12 (17%) | 15.2 / 14.5 | $0.0330 / 18 |
| case-07 | brass floor lamps | 100% | 92% (22/0/2) | 71% / 75% / 70% | +4.2 / -0.8 | 24 / 24 / 20 | 15 (17%) | 8.1 / 14.2 | $0.0254 / 12 |
| case-08 | queen bed frames with storage in wood | 100% | 83% (20/0/4) | 54% / 83% / 50% | +29.2 / -4.2 | 24 / 24 / 24 | 14 (17%) | 8.7 / 15.2 | $0.0375 / 22 |
| case-09 | round black metal side tables | 96% | 54% (13/5/6) | 54% / 62% / 46% | +8.3 / -8.3 | 24 / 24 / 24 | 13 (17%) | 9.9 / 16.1 | $0.0303 / 16 |
| case-10 | ergonomic desk chairs under $300 | 100% | 67% (16/0/8) | 88% / 78% / 67% | -9.2 / -20.8 | 24 / 23 / 9 | 11 (17%) | 10.6 / 16.2 | $0.0254 / 14 |
| case-11 | sectional sofas for a small living room | 100% | 100% (24/0/0) | 67% / 67% / 71% | +0.0 / +4.2 | 24 / 24 / 24 | 12 (17%) | 10.2 / 16.4 | $0.0271 / 12 |
| case-12 | navy velvet accent chairs | 100% | 88% (21/0/3) | 42% / 54% / 50% | +12.5 / +8.3 | 24 / 24 / 24 | 12 (17%) | 9.3 / 16.2 | $0.0292 / 14 |

Attributes show the first batch's share and, in brackets, the judge's yes / no / cannot_tell counts. Results are the number of items shown
in each round (24 is the target). Pipeline $ is Anthropic plus Serper, as the cost threshold counts it.

## The 22 checks against the baseline
| Check | Baseline | This run | Limit | vs baseline |
|---|---|---|---|---|
| price compliance (worst batch) | 1.000 (pass) | 1.000 (pass) | >= 1.0 | unchanged |
| category match, mean of first batches | 0.996 (pass) | 0.993 (pass) | >= 0.9 | unchanged |
| category match, worst case | 0.957 (pass) | 0.958 (pass) | >= 0.75 | unchanged |
| attributes match, mean of first batches | 0.835 (pass) | 0.816 (pass) | >= 0.7 | unchanged |
| attributes match, worst case | 0.435 (FAIL) | 0.500 (pass) | >= 0.5 | fixed |
| avoid-colour share of the first batch (worst) | 0.000 (pass) | 0.042 (pass) | <= 0.15 | unchanged |
| context colours in the search text | 0 (pass) | 0 (pass) | <= 0 | unchanged |
| product-colour share of the first batch | 0.208 (FAIL) | 1.000 (pass) | >= 0.7 | fixed |
| near-duplicate groups, mean | 0.000 (pass) | 0.000 (pass) | <= 0.5 | unchanged |
| near-duplicate groups, worst case | 0 (pass) | 0 (pass) | <= 1 | unchanged |
| distinct sellers, fewest in a first batch | 6 (pass) | 7 (pass) | >= 6 | unchanged |
| top seller share, highest in a first batch | 0.333 (FAIL) | 0.250 (pass) | <= 0.3 | fixed |
| items repeated across rounds | 0 (pass) | 0 (pass) | <= 0 | unchanged |
| refine exclusions respected (worst batch) | 1.000 (pass) | 1.000 (pass) | >= 1.0 | unchanged |
| refine focus share where supply exists | 0.417 (FAIL) | 0.111 (FAIL) | >= 0.5 | still failing |
| Shop Again lift, round 2 minus round 1 (mean) | 6.186 (FAIL) | 8.138 (FAIL) | >= 15.0 | still failing |
| cases where round 2 beat round 1 | 8 (pass) | 9 (pass) | >= 8 | unchanged |
| Shop Again lift, worst case | -19.570 (FAIL) | -9.240 (pass) | >= -10.0 | fixed |
| first search, median seconds | 6.509 (pass) | 10.436 (pass) | <= 30.0 | unchanged |
| Shop Again, median seconds | 12.861 (pass) | 15.599 (pass) | <= 20.0 | unchanged |
| Shop Again, slowest seconds | 21.574 (pass) | 35.593 (pass) | <= 45.0 | unchanged |
| pipeline cost of one case, highest (USD) | 0.039 (pass) | 0.053 (FAIL) | <= 0.05 | new failure |

- **Fixed since the baseline:** attribute worst case (0.435 to 0.500, case-06 exactly at the limit: 12 yes, 10 cannot_tell, 2 no), product
  colour (0.208 to 1.000), top seller share (0.333 to 0.250), worst-case lift (-19.6 to -9.2).
- **Still failing:** refine focus share (0.111; see finding 3), mean lift (+8.1 against the +15 limit; the baseline was +6.2; 9 of 12 cases
  improved).
- **New failure:** cost of one case (0.053 against 0.05): case-02 made 19 Serper calls (38 credits).

## Findings
1. **Batches still run short in later rounds.** Case-01 rounds 2 and 3 gave 5 and 4 results ("Only 5 new results found: 5 seen or rated, 0
   duplicates, 110 filtered out"; only 3 searches of up to 9 were run); case-10 round 3 gave 9; case-04 round 3 gave 17; case-05 round 3 19;
   case-07 round 3 20; case-06 round 2 15. In the three-case run case-06 round 1 gave 18 after 1 Serper call (pool 28, 23 after
   de-duplication, 5 held back by the seller cap) and no variant searches ran. Stage 2r's fill loop and filtered-out breakdown address this.
2. **Case-02 spent the whole 9-call cap twice** on pools of 218 and 205 candidates (stage 2r, phase 2).
3. **Refine focus: case-10 round 3 (9 results, 1 hit, 0.111, supply True).** The eval counted a title already shown in an earlier round as
   "supply", although the whole unseen pool was the 9 shown (`pool_after` 9, `seller_capped` 0, `guarded` 0). Recomputed with the unseen
   reading, round 3 is not scored (at most 1 of the 5 the focus reserves) and the check reads 0.609 (round 2: 14 of 23), a pass. This is a
   measurement change made in stage 2r; the saved file is unchanged.
4. **Case-09 ("round black metal side tables") had the weakest first batch** (13 yes, 5 no, 6 cannot_tell; 0.54). All 24 were tagged
   black and 21 metal, but shape is the one typed attribute with no quota (stage 2r, phase 4).
5. **Case-04 round 3 of the three-case run: "Candidate tagging failed: the call failed: APITimeoutError after 1.0 s".** A queued call was
   given only the second of budget that was left; calls now get at least 8 s or are not started (stage 2r, phase 5).
6. **Case-06 rounds 2 and 3 in the earlier run were shortened by the shape guard** (pool 100% rectangular, 14 of one shape allowed); the
   guard is now off when the pool offers fewer than 6 candidates of another value (stage 2r, phase 4).
7. **Lift.** The 12-case mean round-2 lift is +8.1; see `docs/evals/shop-lift-diagnosis-2026-10-29.md` for which cases learned something.
8. **Cost.** Mean pipeline cost per case $0.0325 (range $0.0246 to $0.053); credits per case 12 to 38, mean 19.

## Caveats
One run; the judges are the same model family as the pipeline; the persona is a text description; `cannot_tell` counts as a miss in the
attribute verdict (the report prints the figure among decidable items beside it). The first run's check list is the baseline's; the measure
of two checks changed after it (product colour, judged from the thumbnail since stage 2p; refine focus supply, unseen since stage 2r).
