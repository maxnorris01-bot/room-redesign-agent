"""The plan: the request list, the expected calls and the expected and worst-case cost. Spends nothing.

Figures are estimates from configuration and from the measurements the Shop eval already made (see
`evals/shop/plan.py`), with the assumptions stated so they can be checked. The live run stops at
$0.30 Anthropic and 150 Serper credits whatever these say.
"""

from __future__ import annotations

from typing import Any

from app.config import Config
from app.pricing import rates_for
from app.shop.clarify import CANDIDATES_SHOWN, MAX_QUESTIONS
from evals.shop.plan import (
    MEASURED_CREDITS_PER_CASE,
    MEASURED_JUDGE_CALL_USD,
    MEASURED_PIPELINE_USD_PER_CASE,
)
from evals.shop.runner import ROUNDS
from evals.shop.spec import thresholds_sha256
from evals.shop_clarify.runner import ANTHROPIC_STOP_USD, SERPER_STOP_CREDITS, SHOWN
from evals.shop_clarify.spec import ClarifyCase

# Assumptions (tokens), not measurements:
PARSE_TOKENS_IN, PARSE_TOKENS_OUT = 900, 160
CLARIFY_PROMPT_TOKENS = 750
CLARIFY_TOKENS_PER_ROW = 38
CLARIFY_TOKENS_OUT = 650  # five questions of three or four options
JUDGE_CALLS_PER_ARM = 2 * -(-SHOWN // 12)  # category and persona judge, 12 items a call
WORST_PIPELINE_FACTOR = 2.0  # a search that tags and fills: twice the measured average
WORST_OTHER_FACTOR = 1.25


def call_usd(config: Config, tokens_in: int, tokens_out: int) -> float:
    rates = rates_for(config.model_retrieval, tokens_in)
    return (tokens_in * rates.input + tokens_out * rates.output) / 1_000_000


def estimate(config: Config, cases: list[ClarifyCase]) -> dict[str, Any]:
    n = len(cases)
    credit = config.serper_credits_per_call
    parse = call_usd(config, PARSE_TOKENS_IN, PARSE_TOKENS_OUT)
    clarify = call_usd(
        config,
        CLARIFY_PROMPT_TOKENS + CANDIDATES_SHOWN * CLARIFY_TOKENS_PER_ROW + 150,
        CLARIFY_TOKENS_OUT,
    )
    # the Shop eval's measured case (a search and two Shop Agains): the Anthropic part, per search
    per_search = (
        max(
            0.0,
            MEASURED_PIPELINE_USD_PER_CASE
            - MEASURED_CREDITS_PER_CASE * config.serper_usd_per_credit,
        )
        / ROUNDS
    )
    judges_arm = JUDGE_CALLS_PER_ARM * MEASURED_JUDGE_CALL_USD
    expected_case = parse + clarify + 2 * per_search + 2 * judges_arm
    worst_case = (
        (parse + clarify) * WORST_OTHER_FACTOR
        + 2 * per_search * WORST_PIPELINE_FACTOR
        + 2 * judges_arm * WORST_OTHER_FACTOR
    )
    # Serper calls that are new (not a cache hit): the fetch, the baseline's other page, the answers arm
    calls_expected = 1 + 1 + 2
    calls_worst = 1 + 2 + 3  # page 2 and a colour query in each arm
    return {
        "cases": n,
        "haiku_calls_per_case": 2 + 2 + 2 * JUDGE_CALLS_PER_ARM + 2 * 2,  # parse, clarify, 2 arms x (rank, tag) + judges
        "per_case_usd_expected": round(expected_case, 4),
        "per_case_usd_worst": round(worst_case, 4),
        "anthropic_usd_expected": round(n * expected_case, 4),
        "anthropic_usd_worst": round(n * worst_case, 4),
        "serper_calls_expected": n * calls_expected,
        "serper_calls_worst": n * calls_worst,
        "serper_credits_expected": n * calls_expected * credit,
        "serper_credits_worst": n * calls_worst * credit,
        "serper_usd_expected": round(n * calls_expected * credit * config.serper_usd_per_credit, 4),
        "parse_usd": round(parse, 5),
        "clarify_usd": round(clarify, 5),
        "per_search_usd": round(per_search, 5),
        "judge_arm_usd": round(judges_arm, 5),
        "stop_usd": ANTHROPIC_STOP_USD,
        "stop_credits": SERPER_STOP_CREDITS,
        "within_usd_stop": n * worst_case <= ANTHROPIC_STOP_USD,
        "within_credit_stop": n * calls_worst * credit <= SERPER_STOP_CREDITS,
    }  # fmt: skip


def plan_lines(config: Config, cases: list[ClarifyCase]) -> list[str]:
    est = estimate(config, cases)
    lines = [
        f"Clarify spike plan: {len(cases)} requests ({sum(c.vagueness == 'vague' for c in cases)} vague, "
        f"{sum(c.vagueness == 'specific' for c in cases)} specific), spends nothing.",
        f"evals/shop/thresholds.json SHA-256 (must stay c64fe35a...d237): {thresholds_sha256()}",
        "",
        "Requests (and the hidden persona's scripted answers; never shown to the pipeline):",
    ]
    for c in cases:
        answers = "; ".join(f"{k}: {v}" for k, v in c.answers.items())
        lines += [f"  {c.id}  {c.request}  ({c.vagueness})", f"      answers: {answers}"]
    lines += [
        "",
        "What each request does:",
        "  1. parse the request (Haiku, as the pipeline does)",
        "  2. ONE Serper call for the base query (the Shop eval's record/replay cache: a saved response is free)",
        f"  3. ONE Haiku call: up to {MAX_QUESTIONS} questions from the first {CANDIDATES_SHOWN} listings (forced tool call, thinking off)",
        "  4. pictures chosen by title matching (no model call); quality flags computed in code",
        "  5. the persona's scripted answers; the normal pipeline twice (baseline, with answers applied);",
        f"     the existing persona and attribute judges on the top {SHOWN} of each ({JUDGE_CALLS_PER_ARM} calls a judge-arm)",
        "",
        "Estimated live cost (an estimate from configuration and the Shop eval's measurements, not a measurement):",
        f"  per request: parse ${est['parse_usd']:.4f}, questions ${est['clarify_usd']:.4f}, a search ${est['per_search_usd']:.4f} x 2, "
        f"judges ${est['judge_arm_usd']:.4f} x 2 = about ${est['per_case_usd_expected']:.4f} (worst case ${est['per_case_usd_worst']:.4f})",
        f"  Anthropic: expected ${est['anthropic_usd_expected']:.2f}, WORST CASE ${est['anthropic_usd_worst']:.2f} for {est['cases']} requests; hard stop ${est['stop_usd']:.2f} "
        f"(worst case is {'within' if est['within_usd_stop'] else 'OVER'} the stop)",
        f"  Serper: {est['serper_calls_expected']} new calls expected = {est['serper_credits_expected']:.0f} credits "
        f"(about ${est['serper_usd_expected']:.2f}); worst case {est['serper_calls_worst']} calls = {est['serper_credits_worst']:.0f} credits; "
        f"hard stop {est['stop_credits']} credits (worst case is {'within' if est['within_credit_stop'] else 'OVER'} the stop)",
        "  with every Serper call a cache hit: the same Anthropic dollars and 0 credits (a re-run costs no credits)",
        "Assumptions:",
        f"  - parse call about {PARSE_TOKENS_IN} tokens in and {PARSE_TOKENS_OUT} out; question call about "
        f"{CLARIFY_PROMPT_TOKENS + CANDIDATES_SHOWN * CLARIFY_TOKENS_PER_ROW + 150} in and {CLARIFY_TOKENS_OUT} out (Haiku rates from app/pricing.py)",
        f"  - a search's Anthropic cost is the Shop eval's measured per-case figure over its {ROUNDS} rounds "
        f"(${MEASURED_PIPELINE_USD_PER_CASE} a case, {MEASURED_CREDITS_PER_CASE} credits, docs/evals/shop-eval-full-run-2026-10-29.md); worst case is {WORST_PIPELINE_FACTOR:.0f} times that",
        f"  - a judge call costs ${MEASURED_JUDGE_CALL_USD} (measured by the same run)",
        "  - new Serper calls: the fetch (1), the baseline's second page (1), the answers arm's two pages (2); the baseline's first page is a cache hit "
        "on the fetch. Worst case adds a colour query to each arm",
        "  - Serper timing for a cached call is the latency recorded when it was live (fetch-latency.json beside the results)",
    ]
    return lines
