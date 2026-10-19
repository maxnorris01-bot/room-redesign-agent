"""Tiered eval runner.

    python -m evals.run --tier fast|standard|nightly

Exits non-zero if thresholds in evals/thresholds.yaml are missed, so CI can gate on it.

Scoring depends on `Config.llm_mode` (`APP_LLM_MODE`, default "mock") - see `app.config.Config`.
With the placeholder pipeline (`app.pipeline.run` echoing its input), `expected_contains` matching
works the same in both modes; once a project has a real pipeline with domain-specific output, mock
mode's check typically needs to become structural-only (does it route/shape correctly) while live
mode checks real quality - see docs/lessons-learned.md for how the first project built from this
template split that.

Cases are scored concurrently (ThreadPoolExecutor, see MAX_WORKERS): each case is dominated by
network wait (a live model call, or a fast but still-a-call mock round trip), so threads overlap
that wait instead of running cases one at a time. This only helps wall-clock time, not cost - each
case still pays for its own calls. Every case gets its own `Budget` (no shared mutable state
across threads); `app.tracing.span`'s file write is lock-protected so concurrent cases' trace
lines can't interleave - don't remove that lock if you ever go back to sequential scoring, and
check for the same kind of shared-file-write hazard before adding concurrency anywhere else.

`max_total_cost_usd` in `thresholds.yaml` (optional - omit the key to skip the check) bounds this
*run's* total spend, on top of `Config.max_cost_usd` bounding each individual case - the per-case
cap alone doesn't stop a run's total from adding up across many cases. This is a CI-time backstop;
it doesn't replace a real spend limit set on the API account itself.

Add LLM-as-judge scorers in evals/judges.py and validate them against a human-labeled gold set
before trusting them.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

from app.config import Config, load_env
from app.llm import Budget
from app.pipeline import run as system_under_test

EVALS_DIR = Path(__file__).resolve().parent
TIER_SIZES: dict[str, int | None] = {"fast": 15, "standard": 50, "nightly": None}
# Each case is one network-bound run() call; this only shortens wall-clock time (cases still run
# one request at a time internally), not cost - every case pays for its own calls regardless.
MAX_WORKERS = 5


def load_cases(tier: str) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted((EVALS_DIR / "cases").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                cases.append(json.loads(line))
    limit = TIER_SIZES[tier]
    return cases if limit is None else cases[:limit]


def score_case(case: dict[str, Any], config: Config) -> dict[str, Any]:
    budget = Budget(config)
    start = time.perf_counter()
    output = ""
    error: str | None = None
    try:
        output = system_under_test(case["input"], budget=budget)
    except Exception as exc:  # a crash is a failed case, not a crashed eval run
        error = repr(exc)
    latency = time.perf_counter() - start
    expected = case.get("expected_contains", [])
    passed = error is None and all(e.lower() in output.lower() for e in expected)
    return {
        "id": case["id"],
        "category": case.get("category", "default"),
        "passed": passed,
        "latency_s": latency,
        "cost_usd": budget.cost_usd,
        "steps": budget.steps,
        "error": error,
        "output": output,
    }


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", choices=list(TIER_SIZES), default="fast")
    args = parser.parse_args()
    load_env()
    config = Config.from_env()
    mode_note = (
        "real API cost, quality-checked"
        if config.llm_mode == "live"
        else "free, structural routing check only - not a quality signal"
    )
    print(f"llm_mode={config.llm_mode} ({mode_note})", file=sys.stderr)

    thresholds = yaml.safe_load((EVALS_DIR / "thresholds.yaml").read_text())
    cases = load_cases(args.tier)
    if not cases:
        print("No eval cases found.", file=sys.stderr)
        return 1

    def _score(case: dict[str, Any]) -> dict[str, Any]:
        return score_case(case, config)

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(cases))) as executor:
        # .map preserves input order in its results, regardless of completion order, so the
        # report's case order stays stable and reproducible run to run.
        results = list(executor.map(_score, cases))
    pass_rate = sum(r["passed"] for r in results) / len(results)
    latency_p95 = p95([r["latency_s"] for r in results])
    total_cost = sum(r["cost_usd"] for r in results)
    mean_cost = total_cost / len(results)

    summary = {
        "tier": args.tier,
        "llm_mode": config.llm_mode,
        "n_cases": len(results),
        "pass_rate": round(pass_rate, 4),
        "latency_p95_s": round(latency_p95, 4),
        "mean_cost_usd": round(mean_cost, 4),
        "total_cost_usd": round(total_cost, 4),
    }

    failures = []
    if pass_rate < thresholds["min_pass_rate"]:
        failures.append(f"pass_rate {pass_rate:.2%} < {thresholds['min_pass_rate']:.2%}")
    if latency_p95 > thresholds["max_latency_p95_s"]:
        failures.append(f"latency_p95 {latency_p95:.2f}s > {thresholds['max_latency_p95_s']}s")
    if mean_cost > thresholds["max_mean_cost_usd"]:
        failures.append(f"mean_cost ${mean_cost:.3f} > ${thresholds['max_mean_cost_usd']}")
    max_total = thresholds.get("max_total_cost_usd")
    if max_total is not None and total_cost > max_total:
        failures.append(f"total_cost ${total_cost:.3f} > ${max_total} (this run's aggregate spend)")

    report_dir = EVALS_DIR / "reports"
    report_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (report_dir / f"{args.tier}-{config.llm_mode}-{stamp}.json").write_text(
        json.dumps({"summary": summary, "failures": failures, "results": results}, indent=2)
    )

    print(json.dumps(summary, indent=2))
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
