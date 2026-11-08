# Shop eval report: live run 20261029-140403-6d1a50

- Verdict: **fail**
- Started 2026-10-29T14:04:03+00:00, finished 2026-10-29T14:17:04+00:00
- thresholds.json SHA-256: `c64fe35a51c389603c507ed0a31e4388d08a1e96ee25f2d0bf9288f1fb75d237`
- Prompt versions: eval_shop_category_judge v2, eval_shop_persona_judge v1, shop_candidate_tags v5, shop_parse v2, shop_prefs v7, shop_queries v4, shop_rank v6, shop_refine v2
- Models: pipeline claude-haiku-5-5, rank claude-haiku-5-5, judges claude-haiku-5-5

## Thresholds

| Check | Result | Limit | Cases | |
|---|---|---|---|---|
| price compliance (worst batch) | 1.000 | >= 1.0 | 33 | pass |
| category match, mean of first batches | 0.992 | >= 0.9 | 11 | pass |
| category match, worst case | 0.958 | >= 0.75 | 11 | pass |
| attributes match, mean of first batches | 0.845 | >= 0.7 | 11 | pass |
| attributes match, worst case | 0.542 | >= 0.5 | 11 | pass |
| avoid-colour share of the first batch (worst) | 0.000 | <= 0.15 | 1 | pass |
| context colours in the search text | 0 | <= 0 | 1 | pass |
| product-colour share of the first batch | 0.958 | >= 0.7 | 1 | pass |
| near-duplicate groups, mean | 0.000 | <= 0.5 | 11 | pass |
| near-duplicate groups, worst case | 0 | <= 1 | 11 | pass |
| distinct sellers, fewest in a first batch | 8 | >= 6 | 11 | pass |
| top seller share, highest in a first batch | 0.250 | <= 0.3 | 11 | pass |
| items repeated across rounds | 0 | <= 0 | 11 | pass |
| refine exclusions respected (worst batch) | 1.000 | >= 1.0 | 2 | pass |
| refine focus share where supply exists | 0.542 | >= 0.5 | 2 | pass |
| Shop Again lift, round 2 minus round 1 (mean) | 8.144 | >= 15.0 | 11 | FAIL |
| cases where round 2 beat round 1 | 9 | >= 8 | 11 | pass |
| Shop Again lift, worst case | -4.170 | >= -10.0 | 11 | pass |
| first search, median seconds | 11.094 | <= 30.0 | 11 | pass |
| Shop Again, median seconds | 13.544 | <= 20.0 | 22 | pass |
| Shop Again, slowest seconds | 26.267 | <= 45.0 | 22 | pass |
| pipeline cost of one case, highest (USD) | 0.049 | <= 0.05 | 11 | pass |

## Cases

| Case | Request | Status | Category | Attributes | Persona r1 / r2 / r3 | Lift r2 | Sellers | Near-dups | Price | Search s | Again s | Pipeline $ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| case-01 | modern couches under $1000 | ok | 100% | 83% | 29% / 38% / 67% | 8.3 | 15 (top 17%) | 0 | 100% | 13.9 | 15.6 | 0.0419 |
| case-02 | coffee tables with a mix of wood and metal with storage | ok | 100% | 83% | 12% / 25% / 17% | 12.5 | 12 (top 17%) | 0 | 100% | 11.1 | 13.3 | 0.0287 |
| case-03 | boho curtains that pair well with a dark blue room | ok | 100% | 96% | 75% / 71% / 92% | -4.2 | 13 (top 17%) | 0 | 100% | 10.6 | 12.9 | 0.0285 |
| case-04 | mid-century dining chairs under $150 each | ok | 100% | 100% | 56% / 67% / 87% | 10.4 | 11 (top 25%) | 0 | 100% | 14.8 | 18.4 | 0.0492 |
| case-05 | small bookshelves for a studio apartment under $200 | ok | 100% | 88% | 33% / 42% / 38% | 8.3 | 11 (top 17%) | 0 | 100% | 13.3 | 13.5 | 0.0292 |
| case-06 | 8x10 neutral area rugs under $400 | ok | 96% | 54% | 54% / 71% / 46% | 16.7 | 8 (top 21%) | 0 | 100% | 10.5 | 19.8 | 0.0291 |
| case-07 | brass floor lamps | failed | n/a | n/a | n/a / n/a / n/a | n/a | n/a (top n/a) | n/a | n/a | n/a | n/a | 0.0000 |
| case-08 | queen bed frames with storage in wood | ok | 100% | 75% | 46% / 54% / 58% | 8.3 | 12 (top 17%) | 0 | 100% | 10.7 | 12.6 | 0.0305 |
| case-09 | round black metal side tables | ok | 100% | 79% | 58% / 62% / 54% | 4.2 | 12 (top 17%) | 0 | 100% | 9.3 | 11.6 | 0.0274 |
| case-10 | ergonomic desk chairs under $300 | ok | 100% | 96% | 79% / 83% / 71% | 4.2 | 9 (top 17%) | 0 | 100% | 11.6 | 16.5 | 0.0327 |
| case-11 | sectional sofas for a small living room | ok | 96% | 96% | 54% / 75% / 58% | 20.8 | 14 (top 17%) | 0 | 100% | 12.1 | 12.6 | 0.0257 |
| case-12 | navy velvet accent chairs | ok | 100% | 79% | 50% / 50% / 42% | 0.0 | 13 (top 21%) | 0 | 100% | 9.8 | 12.0 | 0.0263 |

Cases not completed:

- case-07: failed (APIConnectionError: Connection error.)

## Shop Again lift (persona match rate, points)

- Round 2 minus round 1: mean +8.1, min -4.2, max +20.8; improved in 9 of 11 cases
- Round 3 minus round 1 (no gate): mean +7.3, min -8.3, max +37.5
- Per case (r2, r3): case-01 8.3/37.5, case-02 12.5/4.2, case-03 -4.2/16.7, case-04 10.4/30.7, case-05 8.3/4.2, case-06 16.7/-8.3, case-08 8.3/12.5, case-09 4.2/-4.2, case-10 4.2/-8.3, case-11 20.8/4.2, case-12 0.0/-8.3

## Category coverage findings

None.

## Refine steps

- case-02: "no Walmart" -> no Walmart (exclude_seller)
- case-10: "focus on mesh backs" -> focus on mesh backs (free_note, approximate)

## Product colour

- case-12: 96% judged from the thumbnails (category judge v2); 8% by title keywords (the figure the baseline report used)
- The measurement changed after the baseline: the first live run scored colour from title keywords, which miss every title that names no colour. The threshold is unchanged.

## Attribute match among decidable items (secondary, no threshold)

- case-01: 20 of 21 (95%) among items the judge could decide; 3 cannot_tell; the verdict counts it as 83%
- case-02: 20 of 20 (100%) among items the judge could decide; 4 cannot_tell; the verdict counts it as 83%
- case-03: 23 of 23 (100%) among items the judge could decide; 1 cannot_tell; the verdict counts it as 96%
- case-04: 16 of 16 (100%) among items the judge could decide; 0 cannot_tell; the verdict counts it as 100%
- case-05: 21 of 21 (100%) among items the judge could decide; 3 cannot_tell; the verdict counts it as 88%
- case-06: 13 of 16 (81%) among items the judge could decide; 8 cannot_tell; the verdict counts it as 54%
- case-08: 18 of 19 (95%) among items the judge could decide; 5 cannot_tell; the verdict counts it as 75%
- case-09: 19 of 19 (100%) among items the judge could decide; 5 cannot_tell; the verdict counts it as 79%
- case-10: 23 of 23 (100%) among items the judge could decide; 1 cannot_tell; the verdict counts it as 96%
- case-11: 23 of 23 (100%) among items the judge could decide; 1 cannot_tell; the verdict counts it as 96%
- case-12: 19 of 19 (100%) among items the judge could decide; 5 cannot_tell; the verdict counts it as 79%
- Overall (round 1): 215 of 220 (98%); 36 cannot_tell
- The verdict number, its threshold and its checks are unchanged: cannot_tell still counts as a miss there. This line only shows how much of a low figure is the judge declining to decide (listings often state no size).


## Where shown items came from (secondary, no threshold)

| Case | Round | Source | Shown | Attributes | Persona |
|---|---|---|---|---|---|
| case-01 | 1 | base | 14 | 14/14 (100%) | 5/14 (36%) |
| case-01 | 1 | thin_variant | 10 | 6/10 (60%) | 2/10 (20%) |
| case-01 | 2 | writer | 3 | 3/3 (100%) | 1/3 (33%) |
| case-01 | 2 | seller | 12 | 9/12 (75%) | 4/12 (33%) |
| case-01 | 2 | peer | 6 | 5/6 (83%) | 3/6 (50%) |
| case-01 | 2 | filler | 3 | 3/3 (100%) | 1/3 (33%) |
| case-01 | 3 | writer | 22 | 17/22 (77%) | 14/22 (64%) |
| case-01 | 3 | seller | 2 | 2/2 (100%) | 2/2 (100%) |
| case-02 | 1 | base | 24 | 20/24 (83%) | 3/24 (12%) |
| case-02 | 2 | base | 12 | 10/12 (83%) | 4/12 (33%) |
| case-02 | 2 | writer | 12 | 9/12 (75%) | 2/12 (17%) |
| case-02 | 3 | writer | 12 | 7/12 (58%) | 3/12 (25%) |
| case-02 | 3 | seller | 12 | 7/12 (58%) | 1/12 (8%) |
| case-03 | 1 | base | 24 | 23/24 (96%) | 18/24 (75%) |
| case-03 | 2 | base | 14 | 13/14 (93%) | 11/14 (79%) |
| case-03 | 2 | writer | 10 | 7/10 (70%) | 6/10 (60%) |
| case-03 | 3 | writer | 24 | 24/24 (100%) | 22/24 (92%) |
| case-04 | 1 | base | 14 | 14/14 (100%) | 8/14 (57%) |
| case-04 | 1 | thin_variant | 2 | 2/2 (100%) | 1/2 (50%) |
| case-04 | 2 | base | 22 | 12/22 (55%) | 15/22 (68%) |
| case-04 | 2 | writer | 2 | 2/2 (100%) | 1/2 (50%) |
| case-04 | 3 | base | 5 | 4/5 (80%) | 4/5 (80%) |
| case-04 | 3 | writer | 7 | 7/7 (100%) | 5/7 (71%) |
| case-04 | 3 | filler | 11 | 11/11 (100%) | 11/11 (100%) |
| case-05 | 1 | base | 22 | 19/22 (86%) | 7/22 (32%) |
| case-05 | 1 | thin_variant | 2 | 2/2 (100%) | 1/2 (50%) |
| case-05 | 2 | base | 15 | 12/15 (80%) | 6/15 (40%) |
| case-05 | 2 | writer | 9 | 8/9 (89%) | 4/9 (44%) |
| case-05 | 3 | base | 9 | 2/9 (22%) | 2/9 (22%) |
| case-05 | 3 | writer | 15 | 14/15 (93%) | 7/15 (47%) |
| case-06 | 1 | base | 24 | 13/24 (54%) | 13/24 (54%) |
| case-06 | 2 | base | 10 | 10/10 (100%) | 7/10 (70%) |
| case-06 | 2 | writer | 14 | 11/14 (79%) | 10/14 (71%) |
| case-06 | 3 | base | 8 | 4/8 (50%) | 5/8 (62%) |
| case-06 | 3 | writer | 16 | 8/16 (50%) | 6/16 (38%) |
| case-08 | 1 | base | 24 | 18/24 (75%) | 11/24 (46%) |
| case-08 | 2 | base | 16 | 8/16 (50%) | 7/16 (44%) |
| case-08 | 2 | writer | 8 | 6/8 (75%) | 6/8 (75%) |
| case-08 | 3 | base | 14 | 1/14 (7%) | 7/14 (50%) |
| case-08 | 3 | writer | 10 | 9/10 (90%) | 7/10 (70%) |
| case-09 | 1 | base | 24 | 19/24 (79%) | 14/24 (58%) |
| case-09 | 2 | base | 21 | 17/21 (81%) | 12/21 (57%) |
| case-09 | 2 | writer | 3 | 3/3 (100%) | 3/3 (100%) |
| case-09 | 3 | base | 4 | 0/4 (0%) | 0/4 (0%) |
| case-09 | 3 | writer | 20 | 14/20 (70%) | 13/20 (65%) |
| case-10 | 1 | base | 24 | 23/24 (96%) | 19/24 (79%) |
| case-10 | 2 | writer | 23 | 19/23 (83%) | 19/23 (83%) |
| case-10 | 2 | filler | 1 | 0/1 (0%) | 1/1 (100%) |
| case-10 | 3 | base | 10 | 6/10 (60%) | 5/10 (50%) |
| case-10 | 3 | writer | 9 | 7/9 (78%) | 8/9 (89%) |
| case-10 | 3 | filler | 5 | 3/5 (60%) | 4/5 (80%) |
| case-11 | 1 | base | 24 | 23/24 (96%) | 13/24 (54%) |
| case-11 | 2 | base | 8 | 6/8 (75%) | 7/8 (88%) |
| case-11 | 2 | writer | 16 | 15/16 (94%) | 11/16 (69%) |
| case-11 | 3 | writer | 24 | 24/24 (100%) | 14/24 (58%) |
| case-12 | 1 | base | 24 | 19/24 (79%) | 12/24 (50%) |
| case-12 | 2 | base | 18 | 16/18 (89%) | 9/18 (50%) |
| case-12 | 2 | writer | 6 | 6/6 (100%) | 3/6 (50%) |
| case-12 | 3 | writer | 24 | 15/24 (62%) | 10/24 (42%) |


## Cost and time

- Anthropic $0.2124 (pipeline and judges), Serper 202 credits
- Judges: 134 calls, 0 retries, 0 items judge-failed, $0.0608, thumbnails used 1588 of 1588
- First search: median 11.1 s; Shop Again: median 13.5 s, max 26.3 s

## Serper cache

- Mode record: 0 hits, 101 misses, 101 responses saved, 0 credits saved (spent 202)

## Measurement notes

- Refine focus: supply is the pipeline's count of unseen candidates that matched the focus and passed every filter, and a round is scored only when that count covers the quota the focus reserves (ceil(0.5 x the batch)). The baseline counted any title already shown, which includes items that cannot be shown again; the threshold (0.5) is unchanged. Stage 2r.

## Limits of this eval

The judges are Haiku, the same model family as the pipeline's tagger and ranker, so blind spots may be shared. The persona is a text description, not a person, and neither judge has been calibrated against human labels yet (docs/todo.md). Treat one failing case as a prompt to re-run it once (evals/shop/known_instability.md), not as a verdict.
