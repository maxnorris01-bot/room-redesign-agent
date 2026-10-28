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
PIPELINE_ANTHROPIC_PER_ROUND = 0.01  # the upper end scripts/shop.py states per search or Shop Again
REFINE_USD = 0.001


def judge_call_usd(config: Config) -> float:
    """Estimated dollars of one judge call of `BATCH` listings with thumbnails (assumptions above)."""
    rates = rates_for(config.model_retrieval, 0)
    n_in = JUDGE_BASE_TOKENS + BATCH * (JUDGE_TOKENS_PER_ITEM + JUDGE_TOKENS_PER_IMAGE)
    n_out = JUDGE_OUT_BASE + BATCH * JUDGE_OUT_PER_ITEM
    return (n_in * rates.input + n_out * rates.output) / 1_000_000


def estimate(config: Config, cases: list[Case], t: Thresholds) -> dict[str, Any]:
    """Estimated Anthropic dollars and Serper credits for the whole run, expected and worst case,
    with the hard stops beside them."""
    rates = rates_for(config.model_retrieval, 0)
    calls_per_batch = math.ceil(SHOWN_PER_BATCH / BATCH) * 2  # category + persona
    n_in = JUDGE_BASE_TOKENS + BATCH * (JUDGE_TOKENS_PER_ITEM + JUDGE_TOKENS_PER_IMAGE)
    n_out = JUDGE_OUT_BASE + BATCH * JUDGE_OUT_PER_ITEM
    per_call = (n_in * rates.input + n_out * rates.output) / 1_000_000
    judge_calls_per_case = calls_per_batch * ROUNDS
    judge_per_case = judge_calls_per_case * per_call
    n_refine = sum(1 for c in cases if c.refine)
    searches_expected = 2  # one search, or two when fewer than 24 survive
    again_calls = config.shop_again_max_serper_calls
    again_calls_worst = config.shop_again_max_serper_calls_total
    credits_expected = (
        len(cases) * (searches_expected + 2 * again_calls) * config.serper_credits_per_call
    )
    credits_worst = (
        len(cases) * (searches_expected + 2 * again_calls_worst) * config.serper_credits_per_call
    )
    pipeline = len(cases) * ROUNDS * PIPELINE_ANTHROPIC_PER_ROUND + n_refine * REFINE_USD
    judges = len(cases) * judge_per_case
    serper_usd = credits_expected * config.serper_usd_per_credit
    anthropic = pipeline + judges
    per_case_pipeline = (
        ROUNDS * PIPELINE_ANTHROPIC_PER_ROUND
        + (searches_expected + 2 * again_calls)
        * config.serper_credits_per_call
        * config.serper_usd_per_credit
    )
    return {
        "per_case_pipeline_usd": round(per_case_pipeline, 4),
        "cases": len(cases),
        "judge_calls": len(cases) * judge_calls_per_case,
        "judge_usd": round(judges, 4),
        "pipeline_anthropic_usd": round(pipeline, 4),
        "anthropic_usd": round(anthropic, 4),
        "serper_credits_expected": credits_expected,
        "serper_credits_worst": credits_worst,
        "serper_usd_expected": round(serper_usd, 4),
        "total_usd_expected": round(anthropic + serper_usd, 4),
        "hard_stop_anthropic_usd": t.hard_stop_anthropic_usd,
        "hard_stop_serper_credits": t.hard_stop_serper_credits,
        "within_anthropic_stop": anthropic <= t.hard_stop_anthropic_usd,
        "within_credit_stop": credits_worst <= t.hard_stop_serper_credits,
        "assumptions": [
            f"judge call: {n_in} tokens in (incl. {BATCH} thumbnails at {JUDGE_TOKENS_PER_IMAGE}), "
            f"{n_out} out, {config.model_retrieval} rates from app.pricing",
            f"{calls_per_batch} judge calls per batch of {SHOWN_PER_BATCH}, {ROUNDS} batches per case",
            f"pipeline Anthropic ${PIPELINE_ANTHROPIC_PER_ROUND} per round (session footers), "
            f"refine ${REFINE_USD}",
            f"Serper: {searches_expected} search calls + 2 Shop Agains of {again_calls} calls "
            f"(worst {again_calls_worst}) at {config.serper_credits_per_call} credits each",
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
        f"  total about ${est['total_usd_expected']:.2f}",
        f"  pipeline cost per case about ${est['per_case_pipeline_usd']:.3f} "
        f"(threshold ${t.cost_per_request_usd_max:.2f}: judges not included)",
        f"  hard stops: Anthropic ${t.hard_stop_anthropic_usd:.2f}, "
        f"Serper {t.hard_stop_serper_credits} credits "
        f"(estimate is {'within' if est['within_anthropic_stop'] else 'OVER'} the Anthropic stop, "
        f"worst case {'within' if est['within_credit_stop'] else 'OVER'} the credit stop)",
        "Assumptions:",
        *[f"  - {a}" for a in est["assumptions"]],
    ]
    return lines
