"""Rejudge: run ONLY the persona judge again on the batches saved in a results file, and measure how
much its answers move. It is the judge-noise measurement behind the Shop Again lift: if two
judgements of the same items disagree a lot, a few points of lift are not a signal.

No pipeline call is made (no search, no rating, no Shop Again): the items come from the file. The
persona text comes from the case files (`evals/shop/cases/`), by case id. Costs judge calls only
(`ceil(items / 12)` calls per batch). The result goes to its own gitignored results file; the source
file is never changed.

Agreement is reported two ways: the raw three-way answer (love, like, dislike) and the match answer
(love or like counted as a match, dislike not), the one the lift uses.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any, Protocol

from app.config import Config
from evals.shop.judges import BATCH, JudgeItem, JudgeStats, PersonaVerdict
from evals.shop.plan import judge_call_usd
from evals.shop.runner import new_run_id
from evals.shop.spec import Case, thresholds_sha256

MATCH = {"love", "like"}


class PersonaJudge(Protocol):
    stats: JudgeStats
    versions: dict[str, str]

    def persona(self, case: Case, items: list[JudgeItem]) -> dict[int, PersonaVerdict | None]: ...


def select_batches(
    data: dict[str, Any], rounds: list[int] | None = None, cases: list[str] | None = None
) -> list[tuple[str, int, list[dict[str, Any]]]]:
    """(case id, round, saved items) for every non-empty batch of the file that matches the filters."""
    out = []
    for case in data["cases"]:
        if cases and case["id"] not in cases:
            continue
        for rd in case.get("rounds", []):
            if rounds and rd["round"] not in rounds:
                continue
            if rd.get("items"):
                out.append((case["id"], rd["round"], rd["items"]))
    return out


def estimate_rejudge(
    config: Config, batches: list[tuple[str, int, list[dict[str, Any]]]]
) -> dict[str, Any]:
    """Judge calls and estimated dollars to rejudge these batches (judge calls only)."""
    calls = sum(math.ceil(len(items) / BATCH) for _, _, items in batches)
    items = sum(len(items) for _, _, items in batches)
    return {
        "batches": len(batches),
        "items": items,
        "judge_calls": calls,
        "usd": round(calls * judge_call_usd(config), 4),
    }


def _match(answer: str | None) -> bool | None:
    return None if answer is None else answer in MATCH


def compare(old: list[str | None], new: list[str | None]) -> dict[str, Any]:
    """Agreement and match-rate difference between two judgements of the same items. An item either
    judgement failed on is left out (and counted)."""
    pairs = [(a, b) for a, b in zip(old, new, strict=True) if a is not None and b is not None]
    n = len(pairs)
    raw = sum(1 for a, b in pairs if a == b)
    match = sum(1 for a, b in pairs if (a in MATCH) == (b in MATCH))
    old_rate = sum(1 for a, _ in pairs if a in MATCH) / n if n else None
    new_rate = sum(1 for _, b in pairs if b in MATCH) / n if n else None
    return {
        "items": len(old), "compared": n, "failed": len(old) - n,
        "raw_agree": raw, "raw_agreement": raw / n if n else None,
        "match_agree": match, "match_agreement": match / n if n else None,
        "old_match_rate": old_rate, "new_match_rate": new_rate,
        "match_rate_diff_points": None if old_rate is None or new_rate is None
        else round((new_rate - old_rate) * 100, 2),
    }  # fmt: skip


def run_rejudge(
    data: dict[str, Any],
    cases: dict[str, Case],
    judge: PersonaJudge,
    rounds: list[int] | None = None,
    only: list[str] | None = None,
    source: str = "",
) -> dict[str, Any]:
    """Rejudge the selected batches with `judge` (persona judge only) and summarize the noise."""
    rows: list[dict[str, Any]] = []
    for case_id, round_no, saved in select_batches(data, rounds, only):
        case = cases[case_id]
        items = [
            JudgeItem(
                n, i["title"], i.get("price"), i.get("seller", ""), i.get("thumbnail_url", "")
            )
            for n, i in enumerate(saved, start=1)
        ]
        got = judge.persona(case, items)
        new: list[str | None] = []
        for n in range(1, len(items) + 1):
            verdict = got.get(n)
            new.append(verdict.answer if verdict is not None else None)
        old = [i.get("persona") for i in saved]
        rows.append({"case": case_id, "round": round_no, **compare(old, new), "new": new})
    per_case: dict[str, dict[str, Any]] = {}
    for case_id in dict.fromkeys(r["case"] for r in rows):
        mine = [r for r in rows if r["case"] == case_id]
        per_case[case_id] = _pool(mine)
    return {
        "kind": "rejudge",
        "run_id": new_run_id(),
        "source_run_id": data["run_id"],
        "source_file": source,
        "source_mode": data["mode"],
        "thresholds_sha256": thresholds_sha256(),
        "judge_versions": judge.versions,
        "judge_stats": {
            "calls": judge.stats.calls,
            "retries": judge.stats.retries,
            "failed_items": judge.stats.failed_items,
            "cost_usd": judge.stats.cost_usd,
        },  # fmt: skip
        "batches": rows,
        "per_case": per_case,
        "overall": _pool(rows),
        "match_rate_diff_sd_points": _sd([r["match_rate_diff_points"] for r in rows]),
    }


def _pool(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Agreement pooled over batches (items-weighted) and the mean match-rate difference."""
    n = sum(r["compared"] for r in rows)
    raw = sum(r["raw_agree"] for r in rows)
    match = sum(r["match_agree"] for r in rows)
    diffs = [r["match_rate_diff_points"] for r in rows if r["match_rate_diff_points"] is not None]
    return {
        "batches": len(rows), "compared": n,
        "raw_agreement": raw / n if n else None,
        "match_agreement": match / n if n else None,
        "mean_abs_match_rate_diff_points": statistics.fmean(abs(x) for x in diffs) if diffs else None,
        "mean_match_rate_diff_points": statistics.fmean(diffs) if diffs else None,
    }  # fmt: skip


def _sd(values: list[float | None]) -> float | None:
    real = [v for v in values if v is not None]
    return round(statistics.pstdev(real), 2) if len(real) > 1 else None


def render(payload: dict[str, Any]) -> str:
    """A short text summary for the terminal (no titles)."""

    def pct(x: float | None) -> str:
        return "n/a" if x is None else f"{100 * x:.0f}%"

    lines = [
        f"Rejudge of run {payload['source_run_id']} ({payload['source_mode']}), persona judge only",
        "case      batches  items  3-way agree  match agree  mean |diff| pts  mean diff pts",
    ]
    for case_id, c in payload["per_case"].items():
        lines.append(
            f"{case_id:<9} {c['batches']:>7} {c['compared']:>6} {pct(c['raw_agreement']):>12} "
            f"{pct(c['match_agreement']):>12} {_n(c['mean_abs_match_rate_diff_points']):>15} "
            f"{_n(c['mean_match_rate_diff_points']):>14}"
        )
    o = payload["overall"]
    lines.append(
        f"{'overall':<9} {o['batches']:>7} {o['compared']:>6} {pct(o['raw_agreement']):>12} "
        f"{pct(o['match_agreement']):>12} {_n(o['mean_abs_match_rate_diff_points']):>15} "
        f"{_n(o['mean_match_rate_diff_points']):>14}"
    )
    lines.append(
        f"spread of the per-batch match-rate difference (sd): {_n(payload['match_rate_diff_sd_points'])} points; "
        f"judge calls {payload['judge_stats']['calls']}, failed items {payload['judge_stats']['failed_items']}, "
        f"${payload['judge_stats']['cost_usd']:.4f}"
    )
    return "\n".join(lines)


def _n(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1f}"


def write_rejudge(payload: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rejudge-{payload['source_run_id']}-{payload['run_id']}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path
