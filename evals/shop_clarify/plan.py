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
from evals.shop_clarify.runner import (
    ANTHROPIC_STOP_USD,
    ROUND2_STOP_CREDITS,
    ROUND2_STOP_USD,
    SERPER_STOP_CREDITS,
    SHOWN,
    SMALL_LISTINGS,
)
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


# ---- round 2: verified pictures ---------------------------------------------------------------

# Measured in the round 1 live run (clarify-live-20261105-043006): $0.0717 for the 12 requests, which
# is parse, question call, both pipeline arms and the judges.
ROUND1_MEASURED_USD_PER_CASE = round(0.0717 / 12, 4)
IMAGE_TOKENS = 150  # a small product thumbnail
OPTIONS_PER_REQUEST = 11  # round 1: 130 options over 12 requests
CANDIDATES_TRIED = 1.3  # pictures looked at per option (a second candidate for a no or unclear)
AUDITABLE_SHARE = 0.75  # colour, material and shape options
VERIFY_SYSTEM_TOKENS, AUDIT_SYSTEM_TOKENS = 380, 330
VERIFY_LINE_TOKENS, AUDIT_LINE_TOKENS = 45, 8
ROUND2_WORST_FACTOR = 2.0


def estimate_round2(
    config: Config, cases: list[ClarifyCase], mode: str = "batched"
) -> dict[str, Any]:
    n = len(cases)
    images = round(OPTIONS_PER_REQUEST * CANDIDATES_TRIED)
    audit_images = round(OPTIONS_PER_REQUEST * AUDITABLE_SHARE)

    def verify_usd(m: str) -> tuple[float, int]:
        if m == "single":
            calls = images
            return calls * call_usd(
                config, VERIFY_SYSTEM_TOKENS + VERIFY_LINE_TOKENS + IMAGE_TOKENS, 25
            ), calls
        calls = (
            -(-OPTIONS_PER_REQUEST // 12) + 1 + 1
        )  # round 1 images, then a second and third round
        per_image = VERIFY_LINE_TOKENS + IMAGE_TOKENS
        total = call_usd(
            config, calls * VERIFY_SYSTEM_TOKENS + images * per_image, images * 14 + calls * 20
        )
        return total, calls

    verify_b, calls_b = verify_usd("batched")
    verify_s, calls_s = verify_usd("single")
    verify, calls = (verify_s, calls_s) if mode == "single" else (verify_b, calls_b)
    audit = call_usd(
        config,
        AUDIT_SYSTEM_TOKENS + audit_images * (AUDIT_LINE_TOKENS + IMAGE_TOKENS),
        audit_images * 34 + 30,
    )
    parse = call_usd(config, PARSE_TOKENS_IN, PARSE_TOKENS_OUT)
    clarify25 = call_usd(
        config,
        CLARIFY_PROMPT_TOKENS + SMALL_LISTINGS * CLARIFY_TOKENS_PER_ROW + 150,
        CLARIFY_TOKENS_OUT,
    )
    per_case = ROUND1_MEASURED_USD_PER_CASE + parse + clarify25 + verify + audit
    worst = per_case * ROUND2_WORST_FACTOR
    return {
        "cases": n, "mode": mode, "images_per_request": images, "audit_images_per_request": audit_images,
        "verify_calls_per_request": calls, "verify_usd": round(verify, 5),
        "verify_usd_batched": round(verify_b, 5), "verify_usd_single": round(verify_s, 5),
        "audit_usd": round(audit, 5), "parse_usd": round(parse, 5), "clarify25_usd": round(clarify25, 5),
        "round1_usd": ROUND1_MEASURED_USD_PER_CASE,
        "per_case_usd_expected": round(per_case, 4), "per_case_usd_worst": round(worst, 4),
        "anthropic_usd_expected": round(n * per_case, 4), "anthropic_usd_worst": round(n * worst, 4),
        "extra_usd_expected": round(n * (verify + audit + parse + clarify25), 4),
        "stop_usd": ROUND2_STOP_USD, "stop_credits": ROUND2_STOP_CREDITS,
        "within_usd_stop": n * worst <= ROUND2_STOP_USD,
        "expected_within_usd_stop": n * per_case <= ROUND2_STOP_USD,
    }  # fmt: skip


def plan_lines_round2(config: Config, cases: list[ClarifyCase], mode: str = "batched") -> list[str]:
    est = estimate_round2(config, cases, mode)
    return [
        f"Clarify spike plan, ROUND 2 (verified pictures): {len(cases)} requests, spends nothing.",
        f"evals/shop/thresholds.json SHA-256 (must stay c64fe35a...d237): {thresholds_sha256()}",
        "",
        "What a request does (round 1's steps, then):",
        "  - title matching is strict (a value word after with/and/w/ or next to hardware, legs, a top... does not count)",
        f"  - every chosen picture is looked at by Haiku vision ({mode} verification: yes, no or unclear; a no or unclear tries the next",
        "    title match, up to 3, then the option is text only); options with no title evidence, or whose pictures were all a clear no, are dropped;",
        "    a question left with fewer than 2 options is dropped",
        "  - an independent audit prompt (told nothing about the option) names the main colour, material and shape of every shown picture",
        "  - timing: time to text, time to pictures, and the overlap (parse and fetch at the same time) and 25-listing variants",
        "  - the with/without-answers arms re-run on the saved round 1 questions; Serper is REPLAY ONLY (0 new credits; a call that was",
        "    never saved aborts that step, not the run)",
        "",
        "Estimated live cost (an estimate from round 1's measured cost and assumed token counts, not a measurement):",
        f"  extra per request: verification ${est['verify_usd']:.4f} ({est['verify_calls_per_request']} calls, {est['images_per_request']} pictures; "
        f"batched ${est['verify_usd_batched']:.4f}, one picture a call ${est['verify_usd_single']:.4f}), audit ${est['audit_usd']:.4f} "
        f"({est['audit_images_per_request']} pictures), overlap parse ${est['parse_usd']:.4f}, 25-listing call ${est['clarify25_usd']:.4f}",
        f"  per request: round 1's ${est['round1_usd']:.4f} + the extras = about ${est['per_case_usd_expected']:.4f} (worst case ${est['per_case_usd_worst']:.4f})",
        f"  Anthropic: expected ${est['anthropic_usd_expected']:.3f} (the extras alone: ${est['extra_usd_expected']:.3f}), WORST CASE ${est['anthropic_usd_worst']:.3f} "
        f"for {est['cases']} requests; hard stop ${est['stop_usd']:.2f} (worst case is {'within' if est['within_usd_stop'] else 'OVER'} the stop"
        f"{'' if est['expected_within_usd_stop'] else ', and so is the expected figure: the run would stop early and say so'})",
        f"  Serper: 0 new credits expected and allowed (hard stop {est['stop_credits']}); saved responses only",
        "  to compare the verification modes, run one request both ways: CLARIFY_VERIFY_MODE=single CASES=cl-01 (the report prints each mode's cost)",
        "Assumptions:",
        f"  - a thumbnail is about {IMAGE_TOKENS} tokens; about {OPTIONS_PER_REQUEST} options a request and {CANDIDATES_TRIED} pictures tried per option; "
        f"{AUDITABLE_SHARE:.0%} of options (colour, material, shape) are audited",
        "  - batched verification is up to 12 pictures a call (as the photo tagger), three rounds at most; single is one picture a call",
        "  - round 1 measured $0.0717 for the 12 requests (parse, question call, both arms, judges); round 2 repeats all of it",
        "  - Serper cache entries older than 14 days are ignored (then that step is skipped): run this within two weeks of round 1",
    ]
