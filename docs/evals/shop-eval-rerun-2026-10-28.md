# Shop eval re-run after stage 2p (2026-10-28)

A four-case live re-run after the stage 2p fixes (PR #31), recorded from two saved results files in the gitignored
`evals/shop/results/`. **This is not the 12-case run.** Four cases cannot satisfy the improved-cases check ("at least 8 cases improved"), so
its verdict is `fail` by construction, and **nothing here replaces the baseline**
(`docs/evals/shop-eval-baseline-2026-10-26.md`); this run does not replace the baseline, and the thresholds were not touched
(`thresholds.json` SHA-256 `c64fe35a...d237`).

## Runs
- `20261028-135526-320963` (13:55 to 13:59 UTC): cases 04, 06, 10 and 12. Case-10 died on `APIConnectionError: Connection error`
  before it produced a batch; its judge calls cost $0.0011.
- `20261028-140756-e4c419` (14:07 to 14:09 UTC): case-10 alone, to replace the one that died.
- Prompts: shop_queries v3, shop_candidate_tags v5, shop_rank v6, category judge v2, persona judge v1. Models: claude-haiku-5-5 for
  pipeline, rank and judges.
- Combined cost: Anthropic $0.0562 + $0.0189 = $0.0751 (pipeline and judges); Serper 72 + 12 = 84 credits.

## Per case
| Case | Request | Category | Attributes | Persona r1 / r2 / r3 | Lift r2 / r3 | Sellers (r1) | Search s / Again s (median) | Pipeline $ | Serper credits |
|---|---|---|---|---|---|---|---|---|---|
| case-04 | mid-century dining chairs under $150 each | 100% | 83% | 54% / 64% / 33% | +9.5 / -20.8 | 14 (top 17%) | 18.1 / 23.0 | $0.0489 | 38 |
| case-06 | 8x10 neutral area rugs under $400 | 96% | 42% | 58% / 93% / 36% | +34.5 / -22.6 | 12 (top 17%) | 11.6 / 15.8 | $0.0278 | 14 |
| case-10 | ergonomic desk chairs under $300 | 100% | 82% | 82% / 88% / 83% | +5.7 / +1.5 | 11 (top 18%) | 11.5 / 14.6 | $0.0254 | 12 |
| case-12 | navy velvet accent chairs | 100% | 83% | 50% / 46% / 42% | -4.2 / -8.3 | 14 (top 12%) | 12.0 / 15.8 | $0.0289 | 16 |

Lift is the persona match rate in points against round 1. The first file's mean lift over 3 cases was +13.3; case-10 added +5.7.
Not comparable with the baseline's 12-case mean (+6.2) in either direction: four cases, one run each.

## What was fixed
Top seller share 0.167 (baseline 0.333); case-06 round 3 no longer empty; case-10's "focus on mesh backs" share 0.79 in round 2
and 0.75 in round 3 (was 0.417); navy share 0.833 by the photo judge (title keywords: 0.08); price compliance 100%; category pass;
speed pass.

## What it showed
1. **Round 3 is worse than round 1 in cases 04, 06 and 12** (persona 54 to 33, 58 to 36, 50 to 42). In case-04 the attribute match
   went 83% to 21%: the later queries drifted to upholstered espresso side chairs, not mid-century. The writer's variants
   ("affordable midcentury upholstered dining seat chairs") added a material the request did not contain. Case-10 did not drop.
2. **Case-04 round 1: photo tagging did nothing** (`wanted 36, tagged 0, calls 0, skipped 36`, 6.01 s of a 6.0 s budget, all 36
   thumbnails cached) after a thin first search ran two variant searches; the first search took 18.1 s. The calls had started and
   were still running at the deadline; the step abandoned them and counted them as skipped (fixed in stage 2q).
3. **Case-06 rounds 2 and 3 gave 14 results instead of 24.** The pool was 44 with Lowe's 14, Walmart 7, Home Depot 4 and ten more
   sellers. The trace says `guarded 20, seller_capped 10`, and the pool was 100% rectangular: the shape diversity guard allows
   int(0.6 x 24) = 14 of one shape. The seller cap alone would have filled 24 (a mock reproduction gives 14 with the guard, 24
   without). The stage 2q cap relaxation does not lift this batch; the guard does (docs/todo.md).
4. **Case-10 round 1 showed a $2.23 "Evo Mesh Backrest Ergonomic Office Chair"** (a part, not a chair). Round 3 had one failed
   photo-tagging call that fell back to title tags.
5. **Case-06 attributes:** 10 yes, 2 no, 12 cannot_tell of 24 (listings often state no size), which scores 42%; cannot_tell counts
   as a miss in the verdict. Among the 12 items the judge could decide, 10 matched (83%).

## Cost against the threshold
Case-04's pipeline cost, $0.0489, is 2.2% under the `cost_per_request_usd_max` threshold of $0.05 (Anthropic plus Serper). The
other three cost $0.0254 to $0.0289. `evals/shop/plan.py` now uses these figures (stage 2q).

## Caveats
Four cases and one run each; the judges are the same model family as the pipeline; the persona judge disagrees with itself by
about 6 points on a batch. Treat the round 3 drop as a strong lead, not a measurement, until a re-run of the 04, 06 and 12 cases
after stage 2q.
