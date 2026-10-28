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
# Measured on the first live run (docs/evals/shop-eval-baseline-2026-10-26.md): the pipeline's
# Anthropic cost of one case (a search and two Shop Agains, before stage 2p), its Serper credits, and
# one judge call. They replace the earlier guesses (the plan had estimated $0.36 for the pipeline).
MEASURED_PIPELINE_USD_PER_CASE = 0.0074
MEASURED_CREDITS_PER_CASE = 17
MEASURED_JUDGE_CALL_USD = 0.0004
# What stage 2p adds, estimated from the calls the mock run makes and the token limits (not measured):
WRITER_TOKENS_IN = 750  # request, used queries, leanings, titles, rules
WRITER_TOKENS_OUT = 90  # two queries as a tool call
WRITER_CALLS_PER_CASE = (
    2 + 1 / 3
)  # every Shop Again, and a thin first search about a third of the time
WRITER_CREDITS_PER_AGAIN = 4  # two more searches of one call (2 credits) each
THIN_VARIANT_CREDITS = 4 * (1 / 3)  # two variant searches, about a third of the first searches
PHOTO_CALLS_PER_ROUND = 3  # 36 candidates, 12 a call
PHOTO_TOKENS_IN = 1200 + 12 * (70 + 150)  # prompt and vocabulary, 12 rows, 12 thumbnails
PHOTO_TOKENS_OUT = 12 * 48  # one tool object per item (cap: tag_max_tokens)


def judge_call_usd(config: Config) -> float:
    """Estimated dollars of one judge call of `BATCH` listings with thumbnails (assumptions above)."""
    rates = rates_for(config.model_retrieval, 0)
    n_in = JUDGE_BASE_TOKENS + BATCH * (JUDGE_TOKENS_PER_ITEM + JUDGE_TOKENS_PER_IMAGE)
    n_out = JUDGE_OUT_BASE + BATCH * JUDGE_OUT_PER_ITEM
    return (n_in * rates.input + n_out * rates.output) / 1_000_000


def _usd(config: Config, tokens_in: float, tokens_out: float) -> float:
    rates = rates_for(config.model_retrieval, 0)
    return (tokens_in * rates.input + tokens_out * rates.output) / 1_000_000


def estimate(config: Config, cases: list[Case], t: Thresholds) -> dict[str, Any]:
    """Estimated Anthropic dollars and Serper credits for the whole run, expected and worst case,
    with the hard stops beside them. The base figures are measured (first live run); the query
    writer, the photo tagging and the variant searches stage 2p added are estimated."""
    n = len(cases)
    n_refine = sum(1 for c in cases if c.refine)
    writer_usd = _usd(config, WRITER_TOKENS_IN, WRITER_TOKENS_OUT)
    photo_usd = _usd(config, PHOTO_TOKENS_IN, PHOTO_TOKENS_OUT)
    photo_per_round = PHOTO_CALLS_PER_ROUND * photo_usd
    per_case_added = WRITER_CALLS_PER_CASE * writer_usd + ROUNDS * photo_per_round
    per_case_pipeline = MEASURED_PIPELINE_USD_PER_CASE + per_case_added
    judge_calls_per_case = math.ceil(SHOWN_PER_BATCH / BATCH) * 2 * ROUNDS
    credits_per_case = (
        MEASURED_CREDITS_PER_CASE + 2 * WRITER_CREDITS_PER_AGAIN + THIN_VARIANT_CREDITS
    )
    again_calls_worst = config.shop_again_max_serper_calls_total
    typed_calls_worst = config.shop_again_max_serper_calls
    credits_worst_case = (
        typed_calls_worst + 2 * again_calls_worst
    ) * config.serper_credits_per_call
    pipeline = n * per_case_pipeline + n_refine * REFINE_USD
    judges = n * judge_calls_per_case * MEASURED_JUDGE_CALL_USD
    credits = round(n * credits_per_case)
    credit_usd = config.serper_usd_per_credit
    anthropic = pipeline + judges
    per_again_usd = (
        MEASURED_PIPELINE_USD_PER_CASE / ROUNDS + writer_usd + photo_per_round
    )  # Anthropic only
    per_again_credits = MEASURED_CREDITS_PER_CASE / ROUNDS + WRITER_CREDITS_PER_AGAIN
    rerun_usd = 5 * (
        per_case_pipeline
        + judge_calls_per_case * MEASURED_JUDGE_CALL_USD
        + credits_per_case * credit_usd
    )
    return {
        "per_case_pipeline_usd": round(per_case_pipeline + credits_per_case * credit_usd, 4),
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
        "rerun_5_cases_credits": round(5 * credits_per_case),
        "hard_stop_anthropic_usd": t.hard_stop_anthropic_usd,
        "hard_stop_serper_credits": t.hard_stop_serper_credits,
        "within_anthropic_stop": anthropic <= t.hard_stop_anthropic_usd,
        "within_credit_stop": n * credits_worst_case <= t.hard_stop_serper_credits,
        "assumptions": [
            f"measured on the first live run: pipeline ${MEASURED_PIPELINE_USD_PER_CASE} per case "
            f"(Anthropic), {MEASURED_CREDITS_PER_CASE} Serper credits per case, judge call "
            f"${MEASURED_JUDGE_CALL_USD}",
            f"estimated, query writer: {WRITER_CALLS_PER_CASE:.2f} calls per case of "
            f"{WRITER_TOKENS_IN} in and {WRITER_TOKENS_OUT} out tokens (${writer_usd:.5f} a call), "
            f"{WRITER_CREDITS_PER_AGAIN} more credits per Shop Again, {THIN_VARIANT_CREDITS:.1f} "
            "for thin first searches",
            f"estimated, photo tagging: {PHOTO_CALLS_PER_ROUND} calls per round of "
            f"{PHOTO_TOKENS_IN} in and {PHOTO_TOKENS_OUT} out tokens (${photo_usd:.5f} a call), "
            f"{config.model_retrieval} rates from app.pricing",
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
        f"  total about ${est['total_usd_expected']:.2f}",
        f"  one Shop Again about ${est['per_shop_again_usd']:.4f} "
        f"(Anthropic ${est['per_shop_again_anthropic_usd']:.4f}, "
        f"{est['per_shop_again_credits']} Serper credits)",
        f"  a 5-case re-run about ${est['rerun_5_cases_usd']:.2f}, "
        f"{est['rerun_5_cases_credits']} Serper credits (make eval-shop-live CASES=...)",
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
