# Shop lift diagnosis: why round 2 moved, run 20261029-034000-3f9fa0 (2026-10-29)

An offline reading of the saved full-run file with `python -m evals.shop lift --results ...`. It spends nothing and changes no pipeline
behaviour. The mean round-2 lift was +8.1 points (9 of 12 cases improved). For each case: the chips the pipeline had learned when it built
round 2 (`trace.learned`), the pipeline's own "matches your preferences x of y", the persona like rate of round-2 items whose titles show
every decided chip against all other round-2 items, and the round-1 like rate grouped by each chip.

**Method limits.** The file keeps no model tags, so "titles match" reads the title with the keyword vocabulary (`vocab.title_value`); a
title that says nothing about a dimension counts as "other". Most chips (colour, finish, frame colour, style) are rarely named in titles, so
most cases cannot be confirmed this way and say so ("the titles cannot confirm"; a fifth verdict beside the four asked for). The persona
judge's noise (about 6 points per case) is larger than most differences below. One run.

| Case | Chips learned after round 1 | r1 like | r2 like | r2 shown | Pipeline matches | Titles match (n, like) | Other items (n, like) | Verdict |
|---|---|---|---|---|---|---|---|---|
| case-01 | cream | 29% | 40% | 5 | 0 of 5 | 0, n/a | 5, 40% | supply limited |
| case-02 | none | 17% | 21% | 24 | 0 of 24 | 0, n/a | 24, 21% | nothing learned (ceiling or no signal) |
| case-03 | beige | 58% | 50% | 24 | 2 of 24 | 0, n/a | 24, 50% | learned something the titles cannot confirm |
| case-04 | wood | 62% | 65% | 23 | 19 of 23 | 16, 62% | 7, 71% | learned something that does not predict likes |
| case-05 | warm wood; black; metal; leaning toward black (frame color); leaning toward industrial (style); leaning away from painted (finish) | 38% | 67% | 24 | 21 of 24 | 0, n/a | 24, 67% | learned something the titles cannot confirm |
| case-06 | cream | 46% | 60% | 15 | 7 of 15 | 3, 67% | 12, 58% | supply limited |
| case-07 | glass | 71% (ceiling) | 75% | 24 | 0 of 24 | 0, n/a | 24, 75% | nothing learned (ceiling or no signal) |
| case-08 | retailers like IKEA; beige; light wood; leaning toward natural wood (frame color); traditional_leaning | 54% | 83% | 24 | 16 of 24 | 0, n/a | 24, 83% | learned something the titles cannot confirm |
| case-09 | painted | 54% | 62% | 24 | 10 of 24 | 0, n/a | 24, 62% | learned something the titles cannot confirm |
| case-10 | mesh back | 88% (ceiling) | 78% | 23 | 8 of 23 | 0, n/a | 23, 78% | nothing learned (ceiling or no signal) |
| case-11 | gray | 67% (ceiling) | 67% | 24 | 4 of 24 | 0, n/a | 24, 67% | nothing learned (ceiling or no signal) |
| case-12 | channel tufting | 42% | 54% | 24 | 0 of 24 | 0, n/a | 24, 54% | learned something the titles cannot confirm |

Round-1 like rate grouped by each learned chip (titles that show the value, titles that contradict it):

- case-01 cream: with 0 items, like n/a; without 5 items, like 40%
- case-03 beige: with 8 items, like 100%; without 7 items, like 14%
- case-04 wood: with 12 items, like 83%; without 9 items, like 33%
- case-05 warm wood: with 2 items, like 50%; without 0 items, like n/a
- case-05 black: with 0 items, like n/a; without 1 items, like 100%
- case-05 metal: with 2 items, like 100%; without 4 items, like 50%
- case-05 leaning toward black (frame color): with 0 items, like n/a; without 1 items, like 100%
- case-05 leaning toward industrial (style): with 0 items, like n/a; without 4 items, like 75%
- case-05 leaning away from painted (finish): with 0 items, like n/a; without 2 items, like 50%
- case-06 cream: with 8 items, like 75%; without 10 items, like 30%
- case-07 glass: with 0 items, like n/a; without 16 items, like 81%
- case-08 retailers like IKEA: with 0 items, like n/a; without 0 items, like n/a
- case-08 beige: with 0 items, like n/a; without 3 items, like 100%
- case-08 light wood: with 0 items, like n/a; without 3 items, like 67%
- case-08 leaning toward natural wood (frame color): with 3 items, like 33%; without 0 items, like n/a
- case-08 traditional_leaning: with 6 items, like 0%; without 2 items, like 100%
- case-09 painted: with 1 items, like 100%; without 1 items, like 100%
- case-10 mesh back: with 0 items, like n/a; without 0 items, like n/a
- case-11 gray: with 0 items, like n/a; without 2 items, like 0%
- case-12 channel tufting: with 0 items, like n/a; without 0 items, like n/a

Verdicts: learned the right thing 0; learned something that does not predict likes 1; nothing learned (ceiling or no signal) 4; supply limited 2; learned something the titles cannot confirm 5


## What the table suggests
- **Where the pipeline's own match count was high, lift was high.** Case-05 matched 21 of 24 and gained 29 points; case-08 matched 16 of 24
  and gained 29; case-06 matched 7 of 15 and gained 14; case-04 matched 19 of 23 and gained 3. Where it matched 0 to 2 of 24 (case-03 two,
  case-07 none, case-12 none, case-02 none), the learned chip did not shape the batch and lift was -8, +4, +12 and +4.
- **A chip that covers the whole pool teaches nothing.** Case-04's "wood" matched 19 of 23 but wood is 83% of the pool and of the first
  batch; the persona liked wood and non-wood alike (62% against 71%).
- **Round-1 evidence behind a chip can be strong while round 2 barely carries it.** Case-03: round-1 items showing "beige" were liked 100%
  of the time (8 items) against 14% for the rest (7 items), yet only 2 of the 24 round-2 items matched the beige chip.
- **Supply, not learning, limited cases 01 and 06** (5 and 15 results in round 2); the fill loop of stage 2r targets those.
- **Ceilings:** cases 07, 10 and 11 started at 67 to 88%, so a flat or negative lift is not a defect.

## What to try next (not done; nothing in the pipeline was changed from this)
1. Make a decided chip shape more of the batch when the pool can supply it (cases 03, 07, 12 and 02 learned a chip and showed 0 to 2 items
   that carry it), for example a reserved share like the focus chips have.
2. Do not turn a value that covers most of the pool (case-04 wood) into a chip, or give it no weight.
3. Re-check the above on the next full run with model tags saved in the results file, so the match can be measured per item instead of from
   titles. (A small change: save each shown item's tags in `trace_summary`.)
