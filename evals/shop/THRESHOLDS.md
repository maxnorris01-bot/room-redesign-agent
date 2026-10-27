# Shop eval thresholds

**These are judgment values, set before any live run, by the owner's proposal. None was tuned on a
result.** They change only in their own commit, never to make a failing run pass. The SHA-256 of
`thresholds.json` is recorded in every results file, so a report always says which numbers it was
held to. A threshold is measured on the first batch of a case unless the line says otherwise.

| Key | Value | Reason |
|-----|-------|--------|
| `price_compliance_min` | 1.0 | A result outside the parsed price range is a plain bug, not taste. Every batch of every round, every case. |
| `category_mean_min` | 0.90 | Mean over cases of the share of the first batch the judge calls the requested category (accessories and parts count as wrong). Wrong products are the most visible failure. |
| `category_case_min` | 0.75 | No single case may fall below this: one bad case must not hide behind a good mean. |
| `attribute_mean_min` | 0.70 | Mean share of the first batch that meets the stated style, material and must-haves ("cannot tell" is not a match). Lower than category because titles often omit attributes. |
| `attribute_case_min` | 0.50 | The worst case may miss half the attributes, not more. |
| `avoid_color_share_max` | 0.15 | Context case: the first batch may hold at most this share in a colour the setting says to avoid (the pipeline's own avoid-colour cap, stage 2k). |
| `context_colors_in_search_text_max` | 0 | Setting words ("dark blue room") must never become search words. Zero tolerance. |
| `product_color_share_min` | 0.70 | Contrast case (navy velvet chairs): navy is the product, so most of the batch must be navy. |
| `near_duplicates_mean_max` | 0.5 | Mean number of near-duplicate groups left in a shown batch (`nearby.py` signals). Stage 2n should leave close to none. |
| `near_duplicates_case_max` | 1 | No batch may show more than one remaining group. |
| `distinct_sellers_min` | 6 | A batch from fewer than six sellers is not a comparison. |
| `top_seller_share_max` | 0.30 | No seller above 30% of a batch (the pipeline's default cap is lower; this is the allowance for affinity and refine focus). |
| `repeats_max` | 0 | An item shown in two rounds of one run is a bug (seen memory). |
| `refine_exclusion_min` | 1.0 | An excluded seller must never appear after the refine step (a hard filter). |
| `refine_focus_share_min` | 0.50 | A focus refine takes at least this share of the batch where the supply exists (`REFINE_FOCUS_MIN_SHARE`). No supply: not scored. |
| `lift_mean_min_points` | 15.0 | Mean of (persona match rate in round 2 minus round 1), in points. The point of Shop Again: results move toward what the person likes. Match rate = share of the batch the persona judge calls love or like. |
| `lift_improved_cases_min` | 8 | Round 2 must beat round 1 in at least 8 of the 12 cases, so the mean is not carried by two. |
| `lift_case_worst_min_points` | -10.0 | No case may get more than 10 points worse. Round 3 minus round 1 is reported without a gate. |
| `search_median_s_max` | 30.0 | Median first-search time (the app's own aim, stage 2g). |
| `again_median_s_max` | 20.0 | Median Shop Again time (target 30 s aim, 45 s hard stop in the app; the median should be well inside). |
| `again_max_s_max` | 45.0 | The app's hard stop; no Shop Again may take longer. |
| `cost_per_request_usd_max` | 0.05 | Pipeline cost (Anthropic plus Serper) of one case: a search and two Shop Agains. The judges' own cost is reported separately. |
| `hard_stop_anthropic_usd` | 0.60 | The run stops and writes partial results past this Anthropic spend (pipeline plus judges). |
| `hard_stop_serper_credits` | 500 | The run stops past this many Serper credits. |

Mock mode evaluates the same thresholds for information only: its fixtures and judges are
deterministic stand-ins, so a mock pass or fail says nothing about quality.
