"""Anomalies in a saved live results file, listed automatically (stage 2r). Spends nothing.

`python -m evals.shop lint --results FILE` (or `make eval-shop-lint RESULTS=FILE`) reads a results
file and prints what is worth a look, most severe first, on one screen. It reads only what the file
holds; a result from before a field existed simply has nothing to say about it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SEVERITY = {"high": 0, "medium": 1, "low": 2}
BATCH_TARGET = 24
CALLS_SOFT_CAP = 7  # of a Shop Again's 9
COST_MARGIN = 0.10  # within this share of the cost threshold
CANNOT_TELL_SHARE = 0.40
FIRST_BATCH_NOS = 3
SLOW_AGAIN_S = 30.0
MAX_LINES = 38  # one screen
COST_THRESHOLD_USD = 0.05  # evals/shop/THRESHOLDS.md; the file's own `thresholds` wins when present


def _finding(sev: str, where: str, text: str) -> dict[str, str]:
    return {"severity": sev, "where": where, "text": text}


def _calls_of(round_row: dict[str, Any]) -> int | None:
    m = re.search(r"(\d+) Serper calls", round_row.get("footer", ""))
    if m:
        return int(m.group(1))
    pool = (round_row.get("trace") or {}).get("pool") or {}
    return pool.get("serper_calls")


def _short_reason(round_row: dict[str, Any]) -> str:
    trace = round_row.get("trace") or {}
    for w in trace.get("warnings", []):
        if w.startswith(("Only ", "I ran out")):
            return str(w)[:150]
    fill = trace.get("fill") or {}
    if fill.get("stop"):
        return f"fill stopped: {fill['stop']}"
    thin = trace.get("thin") or {}
    return "no reason recorded" if not thin else "thin first search"


def lint(data: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    limit = float(
        ((data.get("thresholds") or {}).get("cost_per_request_usd_max")) or COST_THRESHOLD_USD
    )
    for case in data.get("cases", []):
        cid = case["id"]
        if case.get("status") != "ok":
            out.append(
                _finding("high", cid, f"case {case.get('status')}: {str(case.get('error'))[:100]}")
            )
        cost = (case.get("cost") or {}).get("pipeline_usd")
        if cost is not None and cost >= limit * (1 - COST_MARGIN):
            over = "OVER" if cost > limit else "within 10% of"
            out.append(_finding("high" if cost > limit else "medium", cid,
                                f"pipeline cost ${cost:.4f} is {over} the ${limit:.2f} threshold"))  # fmt: skip
        for r in case.get("rounds", []):
            where = f"{cid} r{r['round']}"
            trace = r.get("trace") or {}
            n = r.get("count", 0)
            if n < BATCH_TARGET:
                out.append(
                    _finding("medium", where, f"{n} of {BATCH_TARGET} results: {_short_reason(r)}")
                )
            photo = (trace.get("typed") or {}).get("photo") or {}
            if photo:
                bad = (photo.get("failed_calls") or 0) + (photo.get("abandoned_calls") or 0)
                if bad:
                    out.append(_finding("high", where, f"photo tagging: {photo.get('failed_calls', 0)} failed, "
                                        f"{photo.get('abandoned_calls', 0)} abandoned call(s). {photo.get('reason', '')}".strip()))  # fmt: skip
                elif (photo.get("tagged") or 0) < (photo.get("wanted") or 0):
                    out.append(_finding("medium", where, f"photo tagging tagged {photo.get('tagged')} of "
                                        f"{photo.get('wanted')}. {photo.get('reason', '')}".strip()))  # fmt: skip
            for w in trace.get("warnings", []):
                if "tagging failed" in w or "APITimeout" in w or "timed out" in w:
                    out.append(_finding("high", where, w[:140]))
                elif w.startswith("Could not keep"):
                    out.append(_finding("low", where, w[:110]))
            calls = _calls_of(r)
            if r.get("kind") == "shop_again" and calls is not None and calls > CALLS_SOFT_CAP:
                out.append(
                    _finding("medium", where, f"{calls} of 9 Serper calls ({calls * 2} credits)")
                )
            if r.get("kind") == "shop_again" and (r.get("seconds") or 0) > SLOW_AGAIN_S:
                out.append(_finding("medium", where, f"Shop Again took {r['seconds']:.1f} s"))
            items = r.get("items") or []
            tells = [i.get("attributes") for i in items if i.get("attributes")]
            if tells and tells.count("cannot_tell") / len(tells) > CANNOT_TELL_SHARE:
                out.append(
                    _finding(
                        "medium",
                        where,
                        f"cannot_tell on {tells.count('cannot_tell')} of {len(tells)} attribute answers",
                    )
                )
            if r["round"] == 1 and tells.count("no") >= FIRST_BATCH_NOS:
                out.append(
                    _finding(
                        "medium",
                        where,
                        f"{tells.count('no')} outright attribute 'no' answers in the first batch",
                    )
                )
    out.sort(key=lambda f: (SEVERITY[f["severity"]], f["where"], f["text"]))
    return out


def render(data: dict[str, Any], name: str = "") -> str:
    found = lint(data)
    counts = {s: sum(1 for f in found if f["severity"] == s) for s in SEVERITY}
    head = (
        f"Shop eval lint{' of ' + name if name else ''}: run {data.get('run_id', '?')} "
        f"({data.get('mode', '?')}): {counts['high']} high, {counts['medium']} medium, {counts['low']} low"
    )
    lines = [head, ""]
    shown = found[:MAX_LINES]
    lines += [f"{f['severity'].upper():6} {f['where']:11} {f['text']}" for f in shown]
    if len(found) > len(shown):
        lines.append(
            f"... and {len(found) - len(shown)} more (lower severity); the file has them all"
        )
    if not found:
        lines.append("nothing to report")
    return "\n".join(lines)


def lint_file(path: Path) -> str:
    return render(json.loads(path.read_text()), path.name)
