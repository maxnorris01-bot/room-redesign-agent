"""Threshold evaluation: per-case results in, a list of pass/fail checks out.

A case result is a plain dict (see `evals/shop/runner.py`, `CaseResult`):

    {"id", "status", "error", "kind", "rounds": [round, ...], "context": {...}, "repeats": {...},
     "lift": {"r2_minus_r1": points | None, "r3_minus_r1": points | None},
     "cost": {"pipeline_usd", "judge_usd", "serper_credits"}}

and a round is {"round", "kind", "seconds", "price", "category", "attributes", "persona",
"sellers", "near_dups", "avoid", "product_color", "refine"}. A check with no data (no case has the
thing it measures) has `passed: None` and does not fail the run.
"""

from __future__ import annotations

import statistics
from typing import Any

from evals.shop.spec import Thresholds


def _check(
    key: str, label: str, value: Any, op: str, limit: Any, passed: bool | None, n: int
) -> dict[str, Any]:
    return {"key": key, "label": label, "value": value, "op": op, "limit": limit,
            "passed": passed, "n": n}  # fmt: skip


def _ok(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in results if r.get("status") == "ok"]


def _first(r: dict[str, Any]) -> dict[str, Any]:
    return r["rounds"][0] if r.get("rounds") else {}


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def evaluate(results: list[dict[str, Any]], t: Thresholds) -> list[dict[str, Any]]:
    ok = _ok(results)
    checks: list[dict[str, Any]] = []

    # price: every batch of every round of every case
    shares = [rd["price"]["share"] for r in ok for rd in r["rounds"] if rd.get("price")]
    low = min(shares) if shares else None
    checks.append(_check("price_compliance_min", "price compliance (worst batch)", low, ">=",
                         t.price_compliance_min, None if low is None else low >= t.price_compliance_min,
                         len(shares)))  # fmt: skip

    # category and attributes: the first batch
    for name, mean_key, case_key, mean_min, case_min in (
        ("category", "category_mean_min", "category_case_min", t.category_mean_min, t.category_case_min),
        ("attributes", "attribute_mean_min", "attribute_case_min", t.attribute_mean_min, t.attribute_case_min),
    ):  # fmt: skip
        rates = [
            _first(r)[name]["rate"] for r in ok if _first(r).get(name, {}).get("rate") is not None
        ]
        mean = _mean(rates)
        worst = min(rates) if rates else None
        checks.append(_check(mean_key, f"{name} match, mean of first batches", mean, ">=", mean_min,
                             None if mean is None else mean >= mean_min, len(rates)))  # fmt: skip
        checks.append(_check(case_key, f"{name} match, worst case", worst, ">=", case_min,
                             None if worst is None else worst >= case_min, len(rates)))  # fmt: skip

    # context and colour
    avoid = [_first(r)["avoid"]["share"] for r in ok if _first(r).get("avoid")]
    worst_avoid = max(avoid) if avoid else None
    checks.append(_check("avoid_color_share_max", "avoid-colour share of the first batch (worst)",
                         worst_avoid, "<=", t.avoid_color_share_max,
                         None if worst_avoid is None else worst_avoid <= t.avoid_color_share_max,
                         len(avoid)))  # fmt: skip
    leaks = [r["context"]["leakage"]["count"] for r in ok if r.get("context", {}).get("leakage")]
    total_leaks = sum(leaks) if leaks else None
    checks.append(_check("context_colors_in_search_text_max", "context colours in the search text",
                         total_leaks, "<=", t.context_colors_in_search_text_max,
                         None if total_leaks is None
                         else total_leaks <= t.context_colors_in_search_text_max, len(leaks)))  # fmt: skip
    pc = [_first(r)["product_color"]["share"] for r in ok if _first(r).get("product_color")]
    worst_pc = min(pc) if pc else None
    checks.append(_check("product_color_share_min", "product-colour share of the first batch",
                         worst_pc, ">=", t.product_color_share_min,
                         None if worst_pc is None else worst_pc >= t.product_color_share_min,
                         len(pc)))  # fmt: skip

    # near duplicates and sellers: the first batch
    nd = [_first(r)["near_dups"]["groups"] for r in ok if _first(r).get("near_dups")]
    nd_mean, nd_max = _mean([float(x) for x in nd]), (max(nd) if nd else None)
    checks.append(_check("near_duplicates_mean_max", "near-duplicate groups, mean", nd_mean, "<=",
                         t.near_duplicates_mean_max,
                         None if nd_mean is None else nd_mean <= t.near_duplicates_mean_max,
                         len(nd)))  # fmt: skip
    checks.append(_check("near_duplicates_case_max", "near-duplicate groups, worst case", nd_max,
                         "<=", t.near_duplicates_case_max,
                         None if nd_max is None else nd_max <= t.near_duplicates_case_max,
                         len(nd)))  # fmt: skip
    sd = [_first(r)["sellers"] for r in ok if _first(r).get("sellers")]
    fewest = min((s["distinct"] for s in sd), default=None)
    topmost = max((s["top_share"] for s in sd), default=None)
    checks.append(_check("distinct_sellers_min", "distinct sellers, fewest in a first batch", fewest,
                         ">=", t.distinct_sellers_min,
                         None if fewest is None else fewest >= t.distinct_sellers_min, len(sd)))  # fmt: skip
    checks.append(_check("top_seller_share_max", "top seller share, highest in a first batch",
                         topmost, "<=", t.top_seller_share_max,
                         None if topmost is None else topmost <= t.top_seller_share_max, len(sd)))  # fmt: skip

    # repeats
    reps = [r["repeats"]["count"] for r in ok if r.get("repeats") is not None]
    total_reps = sum(reps) if reps else None
    checks.append(_check("repeats_max", "items repeated across rounds", total_reps, "<=",
                         t.repeats_max, None if total_reps is None else total_reps <= t.repeats_max,
                         len(reps)))  # fmt: skip

    # refine
    ex = [rd["refine"]["exclusion"]["share"] for r in ok for rd in r["rounds"]
          if rd.get("refine") and rd["refine"].get("exclusion")]  # fmt: skip
    worst_ex = min(ex) if ex else None
    checks.append(_check("refine_exclusion_min", "refine exclusions respected (worst batch)",
                         worst_ex, ">=", t.refine_exclusion_min,
                         None if worst_ex is None else worst_ex >= t.refine_exclusion_min,
                         len(ex)))  # fmt: skip
    focus = [rd["refine"]["focus"] for r in ok for rd in r["rounds"]
             if rd.get("refine") and rd["refine"].get("focus") and rd["refine"]["focus"]["supply"]]  # fmt: skip
    worst_focus = min((f["share"] for f in focus), default=None)
    checks.append(_check("refine_focus_share_min", "refine focus share where supply exists",
                         worst_focus, ">=", t.refine_focus_share_min,
                         None if worst_focus is None else worst_focus >= t.refine_focus_share_min,
                         len(focus)))  # fmt: skip

    # lift
    lifts = [r["lift"]["r2_minus_r1"] for r in ok
             if r.get("lift") and r["lift"].get("r2_minus_r1") is not None]  # fmt: skip
    lift_mean = _mean(lifts)
    improved = sum(1 for x in lifts if x > 0)
    lift_worst = min(lifts) if lifts else None
    checks.append(_check("lift_mean_min_points", "Shop Again lift, round 2 minus round 1 (mean)",
                         lift_mean, ">=", t.lift_mean_min_points,
                         None if lift_mean is None else lift_mean >= t.lift_mean_min_points,
                         len(lifts)))  # fmt: skip
    checks.append(_check("lift_improved_cases_min", "cases where round 2 beat round 1",
                         improved if lifts else None, ">=", t.lift_improved_cases_min,
                         None if not lifts else improved >= t.lift_improved_cases_min,
                         len(lifts)))  # fmt: skip
    checks.append(_check("lift_case_worst_min_points", "Shop Again lift, worst case", lift_worst,
                         ">=", t.lift_case_worst_min_points,
                         None if lift_worst is None else lift_worst >= t.lift_case_worst_min_points,
                         len(lifts)))  # fmt: skip

    # latency
    searches = [_first(r)["seconds"] for r in ok if _first(r)]
    agains = [rd["seconds"] for r in ok for rd in r["rounds"][1:]]
    s_med = statistics.median(searches) if searches else None
    a_med = statistics.median(agains) if agains else None
    a_max = max(agains) if agains else None
    checks.append(_check("search_median_s_max", "first search, median seconds", s_med, "<=",
                         t.search_median_s_max, None if s_med is None else s_med <= t.search_median_s_max,
                         len(searches)))  # fmt: skip
    checks.append(_check("again_median_s_max", "Shop Again, median seconds", a_med, "<=",
                         t.again_median_s_max, None if a_med is None else a_med <= t.again_median_s_max,
                         len(agains)))  # fmt: skip
    checks.append(_check("again_max_s_max", "Shop Again, slowest seconds", a_max, "<=",
                         t.again_max_s_max, None if a_max is None else a_max <= t.again_max_s_max,
                         len(agains)))  # fmt: skip

    # cost
    costs = [r["cost"]["pipeline_usd"] for r in ok if r.get("cost")]
    worst_cost = max(costs) if costs else None
    checks.append(_check("cost_per_request_usd_max", "pipeline cost of one case, highest (USD)",
                         worst_cost, "<=", t.cost_per_request_usd_max,
                         None if worst_cost is None else worst_cost <= t.cost_per_request_usd_max,
                         len(costs)))  # fmt: skip
    return checks


def verdict(
    results: list[dict[str, Any]], checks: list[dict[str, Any]], stopped: str = ""
) -> dict[str, Any]:
    """The run's verdict: `pass` (every check with data passed and every case completed), `fail`,
    or `incomplete` (the run stopped at a guard before every case finished)."""
    failed = [c["key"] for c in checks if c["passed"] is False]
    broken = [r["id"] for r in results if r.get("status") not in ("ok",)]
    status = "incomplete" if stopped else ("fail" if failed or broken else "pass")
    return {"status": status, "failed_checks": failed, "cases_not_ok": broken, "stopped": stopped}
