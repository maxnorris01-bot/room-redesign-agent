"""The plan: what a run would cost, computed from configuration. Spends nothing."""

from __future__ import annotations

import math
from typing import Any

from app.config import Config
from app.pricing import rates_for
from evals.shop.coverage import category_findings
from evals.shop.judges import BATCH
from evals.shop.runner import ROUNDS
from evals.shop.spec import Case, Thresholds, thresholds_sha256

SHOWN_PER_BATCH = 24
# Assumptions, stated so the estimate can be checked (not measured): tokens of one judge call.
JUDGE_BASE_TOKENS = 900  # prompt, request or persona, framing
JUDGE_TOKENS_PER_ITEM = 70  # one numbered row
JUDGE_TOKENS_PER_IMAGE = 150  # a small product thumbnail
JUDGE_OUT_BASE = 40
JUDGE_OUT_PER_ITEM = 14
REFINE_USD = 0.001
# Measured on the first full 12-case run after stages 2p and 2q (docs/evals/shop-eval-full-run-2026-10-29.md,
# run 20261029-034000-3f9fa0): the pipeline's cost of one case (Anthropic plus Serper, as the threshold
# counts it: a search and two Shop Agains, with the query writer and the photo tagging), its Serper
# credits, and one judge call (138 calls, $0.0615). They replace the four-case re-run's figures.
MEASURED_CASE_PIPELINE_USD = {
    "case-01": 0.0246, "case-02": 0.053, "case-03": 0.0294, "case-04": 0.0399, "case-05": 0.035,
    "case-06": 0.033, "case-07": 0.0254, "case-08": 0.0375, "case-09": 0.0303, "case-10": 0.0254,
    "case-11": 0.0271, "case-12": 0.0292,
}  # fmt: skip
MEASURED_PIPELINE_USD_PER_CASE = round(
    sum(MEASURED_CASE_PIPELINE_USD.values()) / len(MEASURED_CASE_PIPELINE_USD), 4
)  # 0.03, writer and photo calls included
MEASURED_WORST_CASE_PIPELINE_USD = max(MEASURED_CASE_PIPELINE_USD.values())  # case-02, over 0.05
MEASURED_CREDITS_PER_CASE = 19  # 226 credits over 12 cases; 12 (case-07, 11) to 38 (case-02)
MEASURED_JUDGE_CALL_USD = 0.00045  # $0.0615 over 138 calls


def judge_call_usd(config: Config) -> float:
    """Estimated dollars of one judge call of `BATCH` listings with thumbnails (assumptions above)."""
    rates = rates_for(config.model_retrieval, 0)
    n_in = JUDGE_BASE_TOKENS + BATCH * (JUDGE_TOKENS_PER_ITEM + JUDGE_TOKENS_PER_IMAGE)
    n_out = JUDGE_OUT_BASE + BATCH * JUDGE_OUT_PER_ITEM
    return (n_in * rates.input + n_out * rates.output) / 1_000_000


def estimate(config: Config, cases: list[Case], t: Thresholds) -> dict[str, Any]:
    """Estimated Anthropic dollars and Serper credits for the whole run, expected and worst case,
    with the hard stops beside them. The figures are measured on the post-2p live re-run (four
    cases, which already include the query writer, the photo tagging and the variant searches)."""
    n = len(cases)
    n_refine = sum(1 for c in cases if c.refine)
    credit_usd = config.serper_usd_per_credit
    # the measured case cost is the threshold's figure (Anthropic plus Serper); the Anthropic part
    # is what is left after the credits, so the credits are not counted twice below
    per_case_pipeline = max(
        0.0, MEASURED_PIPELINE_USD_PER_CASE - MEASURED_CREDITS_PER_CASE * credit_usd
    )
    judge_calls_per_case = math.ceil(SHOWN_PER_BATCH / BATCH) * 2 * ROUNDS
    credits_per_case = MEASURED_CREDITS_PER_CASE
    again_calls_worst = config.shop_again_max_serper_calls_total
    typed_calls_worst = config.shop_again_max_serper_calls
    credits_worst_case = (
        typed_calls_worst + 2 * again_calls_worst
    ) * config.serper_credits_per_call
    pipeline = n * per_case_pipeline + n_refine * REFINE_USD
    judges = n * judge_calls_per_case * MEASURED_JUDGE_CALL_USD
    credits = round(n * credits_per_case)
    anthropic = pipeline + judges
    per_again_usd = per_case_pipeline / ROUNDS  # Anthropic only
    per_again_credits = MEASURED_CREDITS_PER_CASE / ROUNDS
    rerun_usd = 5 * (
        per_case_pipeline
        + judge_calls_per_case * MEASURED_JUDGE_CALL_USD
        + credits_per_case * credit_usd
    )
    rerun_cached_usd = 5 * (per_case_pipeline + judge_calls_per_case * MEASURED_JUDGE_CALL_USD)
    return {
        "per_case_pipeline_usd": round(per_case_pipeline + credits_per_case * credit_usd, 4),
        "per_case_anthropic_usd": round(per_case_pipeline, 4),
        "cases": n,
        "judge_calls": n * judge_calls_per_case,
        "judge_usd": round(judges, 4),
        "pipeline_anthropic_usd": round(pipeline, 4),
        "anthropic_usd": round(anthropic, 4),
        "serper_credits_expected": credits,
        "serper_credits_worst": n * credits_worst_case,
        "serper_usd_expected": round(credits * credit_usd, 4),
        "total_usd_expected": round(anthropic + credits * credit_usd, 4),
        "per_shop_again_usd": round(per_again_usd + per_again_credits * credit_usd, 4),
        "per_shop_again_anthropic_usd": round(per_again_usd, 4),
        "per_shop_again_credits": round(per_again_credits, 1),
        "rerun_5_cases_usd": round(rerun_usd, 4),
        "rerun_5_cases_cached_usd": round(rerun_cached_usd, 4),  # every Serper call a cache hit
        "total_usd_cached": round(anthropic, 4),  # a whole run with every Serper call a cache hit
        "rerun_5_cases_credits": round(5 * credits_per_case),
        "hard_stop_anthropic_usd": t.hard_stop_anthropic_usd,
        "hard_stop_serper_credits": t.hard_stop_serper_credits,
        "within_anthropic_stop": anthropic <= t.hard_stop_anthropic_usd,
        "within_credit_stop": n * credits_worst_case <= t.hard_stop_serper_credits,
        "worst_measured_case_usd": MEASURED_WORST_CASE_PIPELINE_USD,
        "assumptions": [
            f"measured on the first full 12-case run after stages 2p and 2q: pipeline ${MEASURED_PIPELINE_USD_PER_CASE} "
            f"per case (Anthropic plus Serper, as the threshold counts it, with the query writer and photo tagging; case-02 alone "
            f"${MEASURED_WORST_CASE_PIPELINE_USD}), {MEASURED_CREDITS_PER_CASE} Serper credits per "
            f"case, judge call ${MEASURED_JUDGE_CALL_USD}",
            f"{judge_calls_per_case} judge calls per case ({ROUNDS} batches, category and persona "
            "judge, 12 items a call)",
            f"worst case Serper: a typed search of {typed_calls_worst} calls and two Shop Agains of "
            f"{again_calls_worst} at {config.serper_credits_per_call} credits each",
        ],
    }


def plan_lines(config: Config, cases: list[Case], t: Thresholds) -> list[str]:
    est = estimate(config, cases, t)
    lines = [
        f"Shop eval plan: {len(cases)} cases, {ROUNDS} rounds each "
        "(search, Shop Again, Shop Again), spends nothing.",
        f"thresholds.json SHA-256: {thresholds_sha256()}",
        "",
        "Cases:",
    ]
    for c in cases:
        refine = f"  [refine before round 2: {c.refine.text!r}]" if c.refine else ""
        lines += [f"  {c.id}  {c.request}  ({c.kind}, fixture: {c.fixture}){refine}",
                  f"      persona: {c.persona.text}"]  # fmt: skip
    lines += ["", "Category coverage findings (kept as written, reported as results):"]
    for f in category_findings(cases):
        lines.append(
            f"  {f['case']}  '{f['noun']}': {f['kind']} (categories.py says "
            f"'{f['categories_py_says']}', expected '{f['expected_category']}')"
        )
    lines += [
        "",
        "Estimated live cost (an estimate from configuration, not a measurement):",
        f"  Anthropic ${est['anthropic_usd']:.2f} (pipeline ${est['pipeline_anthropic_usd']:.2f}, "
        f"judges ${est['judge_usd']:.2f} over {est['judge_calls']} calls)",
        f"  Serper {est['serper_credits_expected']} credits expected, "
        f"{est['serper_credits_worst']} worst case (about ${est['serper_usd_expected']:.2f})",
        f"  total about ${est['total_usd_expected']:.2f} (with every Serper call a cache hit: "
        f"about ${est['total_usd_cached']:.2f}, 0 credits)",
        f"  one Shop Again about ${est['per_shop_again_usd']:.4f} "
        f"(Anthropic ${est['per_shop_again_anthropic_usd']:.4f}, "
        f"{est['per_shop_again_credits']} Serper credits)",
        f"  a 5-case re-run about ${est['rerun_5_cases_usd']:.2f}, "
        f"{est['rerun_5_cases_credits']} Serper credits; about ${est['rerun_5_cases_cached_usd']:.2f} "
        "with every Serper call a cache hit (make eval-shop-live CASES=...)",
        f"  pipeline cost per case about ${est['per_case_pipeline_usd']:.3f} "
        f"(threshold ${t.cost_per_request_usd_max:.2f}: judges not included)",
        f"  the costliest measured case (case-02) cost ${est['worst_measured_case_usd']:.4f}, "
        f"{abs(100 * (1 - est['worst_measured_case_usd'] / t.cost_per_request_usd_max)):.1f}% "
        f"{'under' if est['worst_measured_case_usd'] <= t.cost_per_request_usd_max else 'OVER'} "
        "that threshold",
        f"  hard stops: Anthropic ${t.hard_stop_anthropic_usd:.2f}, "
        f"Serper {t.hard_stop_serper_credits} credits "
        f"(estimate is {'within' if est['within_anthropic_stop'] else 'OVER'} the Anthropic stop, "
        f"worst case {'within' if est['within_credit_stop'] else 'OVER'} the credit stop)",
        "Assumptions:",
        *[f"  - {a}" for a in est["assumptions"]],
    ]
    return lines
