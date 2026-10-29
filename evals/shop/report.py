"""The Markdown report: a per-case table and a pass/fail line per threshold. It carries no
thumbnails or links and cuts titles short, so it can be committed under docs/evals/."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

TITLE_CHARS = 60
LIMITS = (
    "The judges are Haiku, the same model family as the pipeline's tagger and ranker, so blind "
    "spots may be shared. The persona is a text description, not a person, and neither judge has "
    "been calibrated against human labels yet (docs/todo.md). Treat one failing case as a prompt "
    "to re-run it once (evals/shop/known_instability.md), not as a verdict."
)


def cut(text: str, n: int = TITLE_CHARS) -> str:
    flat = " ".join(str(text).split()).replace("|", "/")
    return flat if len(flat) <= n else flat[: n - 1].rstrip() + "…"


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def num(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def mark(passed: bool | None) -> str:
    return {True: "pass", False: "FAIL", None: "no data"}[passed]


def _round(case: dict[str, Any], n: int) -> dict[str, Any]:
    rounds = case.get("rounds", [])
    return rounds[n] if len(rounds) > n else {}


SOURCE_KINDS = ("base", "writer", "seller", "peer", "thin_variant", "supply", "refill")


def source_rows(case: dict[str, Any]) -> list[tuple[int, str, int, dict[str, Any], dict[str, Any]]]:
    """(round, source kind, shown, attribute rate, persona rate) for each kind in each round of a
    case. Items from results files before stage 2q carry no `source` and are skipped."""
    rows: list[tuple[int, str, int, dict[str, Any], dict[str, Any]]] = []
    for r in case.get("rounds", []):
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for item in r.get("items", []):
            src = item.get("source")
            if src and src.get("kind"):
                by_kind.setdefault(str(src["kind"]), []).append(item)
        ordered = [k for k in SOURCE_KINDS if k in by_kind] + sorted(
            set(by_kind) - set(SOURCE_KINDS)
        )
        for kind in ordered:
            items = by_kind[kind]
            rows.append((r.get("round", 0), kind, len(items), _rate(items, "attributes", {"yes"}),
                         _rate(items, "persona", {"love", "like"})))  # fmt: skip
    return rows


def _rate(items: list[dict[str, Any]], key: str, good: set[str]) -> dict[str, Any]:
    scored = [i[key] for i in items if i.get(key) is not None]
    hits = sum(1 for v in scored if v in good)
    return {"scored": len(scored), "good": hits, "rate": hits / len(scored) if scored else None}


def source_section(data: dict[str, Any]) -> list[str]:
    """A secondary table: per case and round, the shown items by the kind of search that found
    them, with their attribute and persona match rates. Informational; no threshold reads it."""
    rows = [(c["id"], *row) for c in data["cases"] for row in source_rows(c)]
    out = ["", "## Where shown items came from (secondary, no threshold)", ""]
    if not rows:
        return out + ["No source data (a results file from before stage 2q, or no items)."]
    out += ["| Case | Round | Source | Shown | Attributes | Persona |", "|---|---|---|---|---|---|"]
    for cid, rnd, kind, shown, att, per in rows:
        out.append(
            f"| {cid} | {rnd} | {kind} | {shown} | {att['good']}/{att['scored']} ({pct(att['rate'])}) "
            f"| {per['good']}/{per['scored']} ({pct(per['rate'])}) |"
        )
    return out + [""]


def decidable(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Attribute match among the items the judge could decide on: good / (good + no), with the
    count of `cannot_tell` beside it. Secondary: the verdict keeps counting cannot_tell as a miss."""
    good = sum(1 for i in items if i.get("attributes") == "yes")
    no = sum(1 for i in items if i.get("attributes") == "no")
    unsure = sum(1 for i in items if i.get("attributes") == "cannot_tell")
    return {"good": good, "no": no, "cannot_tell": unsure,
            "rate": good / (good + no) if good + no else None}  # fmt: skip


def decidable_section(data: dict[str, Any]) -> list[str]:
    """Round 1 per case and overall (the round the attribute threshold reads)."""
    out = ["", "## Attribute match among decidable items (secondary, no threshold)", ""]
    rows: list[tuple[str, dict[str, Any], float | None]] = []
    for c in data["cases"]:
        r1 = _round(c, 0)
        if r1.get("items"):
            rows.append((c["id"], decidable(r1["items"]), r1.get("attributes", {}).get("rate")))
    if not rows:
        return out + ["No item data."]
    for cid, d, verdict_rate in rows:
        out.append(
            f"- {cid}: {d['good']} of {d['good'] + d['no']} ({pct(d['rate'])}) among items the "
            f"judge could decide; {d['cannot_tell']} cannot_tell; the verdict counts it as "
            f"{pct(verdict_rate)}"
        )
    total = decidable([i for c in data["cases"] for i in _round(c, 0).get("items", [])])
    out.append(
        f"- Overall (round 1): {total['good']} of {total['good'] + total['no']} "
        f"({pct(total['rate'])}); {total['cannot_tell']} cannot_tell"
    )
    out.append(
        "- The verdict number, its threshold and its checks are unchanged: cannot_tell still "
        "counts as a miss there. This line only shows how much of a low figure is the judge "
        "declining to decide (listings often state no size)."
    )
    return out + [""]


def render(data: dict[str, Any]) -> str:
    v = data["verdict"]
    out: list[str] = [
        f"# Shop eval report: {data['mode']} run {data['run_id']}",
        "",
        f"- Verdict: **{v['status']}**"
        + (f" (stopped: {data['stopped']})" if data.get("stopped") else ""),
        f"- Started {data['started_at']}, finished {data['finished_at']}",
        f"- thresholds.json SHA-256: `{data['thresholds_sha256']}`",
        "- Prompt versions: "
        + ", ".join(f"{k} v{ver}" for k, ver in sorted(data["prompt_versions"].items())),
        f"- Models: pipeline {data['models']['pipeline']}, rank {data['models']['rank']}, "
        f"judges {data['models']['judge']}",
    ]
    if data["mode"] == "mock":
        out += [
            "",
            "> Mock run: fixtures and deterministic stand-in judges. The numbers below show that the "
            "harness works, not how good Shop Mode is.",
        ]
    out += [
        "",
        "## Thresholds",
        "",
        "| Check | Result | Limit | Cases | |",
        "|---|---|---|---|---|",
    ]
    for c in data["checks"]:
        val = c["value"]
        shown = "n/a" if val is None else (f"{val:.3f}" if isinstance(val, float) else str(val))
        out.append(
            f"| {c['label']} | {shown} | {c['op']} {c['limit']} | {c['n']} | {mark(c['passed'])} |"
        )
    out += ["", "## Cases", "",
            "| Case | Request | Status | Category | Attributes | Persona r1 / r2 / r3 | Lift r2 | "
            "Sellers | Near-dups | Price | Search s | Again s | Pipeline $ |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for c in data["cases"]:
        r1, r2, r3 = _round(c, 0), _round(c, 1), _round(c, 2)
        rates = " / ".join(
            "exhausted" if r.get("exhausted") else pct(r.get("persona", {}).get("rate"))
            for r in (r1, r2, r3)
        )
        out.append(
            "| {id} | {req} | {st} | {cat} | {att} | {rates} | {lift} | {sel} | {nd} | {pr} | {s1} | {s2} | {cost} |".format(
                id=c["id"],
                req=cut(c.get("request", "")),
                st=c["status"],
                cat=pct(r1.get("category", {}).get("rate")),
                att=pct(r1.get("attributes", {}).get("rate")),
                rates=rates,
                lift=num((c.get("lift") or {}).get("r2_minus_r1")),
                sel=f"{r1.get('sellers', {}).get('distinct', 'n/a')} (top {pct(r1.get('sellers', {}).get('top_share'))})",
                nd=r1.get("near_dups", {}).get("groups", "n/a"),
                pr=pct(min((r["price"]["share"] for r in c.get("rounds", [])), default=None)),
                s1=num(r1.get("seconds")),
                s2=num(
                    statistics.median([r["seconds"] for r in c["rounds"][1:]])
                    if len(c.get("rounds", [])) > 1
                    else None
                ),
                cost=num((c.get("cost") or {}).get("pipeline_usd"), 4),
            )  # fmt: skip
        )
    failed = [c for c in data["cases"] if c["status"] not in ("ok",)]
    if failed:
        out += ["", "Cases not completed:", ""]
        out += [
            f"- {c['id']}: {c['status']}" + (f" ({cut(c['error'], 100)})" if c.get("error") else "")
            for c in failed
        ]

    lifts = [
        c["lift"]
        for c in data["cases"]
        if c.get("lift") and c["lift"].get("r2_minus_r1") is not None
    ]
    out += ["", "## Shop Again lift (persona match rate, points)", ""]
    if lifts:
        d2 = [x["r2_minus_r1"] for x in lifts]
        d3 = [x["r3_minus_r1"] for x in lifts if x.get("r3_minus_r1") is not None]
        out += [
            f"- Round 2 minus round 1: mean {statistics.fmean(d2):+.1f}, min {min(d2):+.1f}, "
            f"max {max(d2):+.1f}; improved in {sum(1 for x in d2 if x > 0)} of {len(d2)} cases",
            "- Round 3 minus round 1 (no gate): "
            + (
                f"mean {statistics.fmean(d3):+.1f}, min {min(d3):+.1f}, max {max(d3):+.1f}"
                if d3
                else "n/a"
            ),
            "- Per case (r2, r3): "
            + ", ".join(
                f"{c['id']} {num(c['lift']['r2_minus_r1'])}/{num(c['lift'].get('r3_minus_r1'))}"
                for c in data["cases"]
                if c.get("lift") and c["lift"].get("r2_minus_r1") is not None
            ),
        ]
    else:
        out.append("No lift data.")

    out += ["", "## Category coverage findings", ""]
    findings = data.get("category_findings", [])
    if findings:
        out += ["Where `categories.py` cannot represent a request; kept as written.", "",
                "| Case | Noun | Finding | categories.py says | Expected |", "|---|---|---|---|---|"]  # fmt: skip
        out += [
            f"| {f['case']} | {f['noun']} | {f['kind']} | {f['categories_py_says']} | {f['expected_category']} |"
            for f in findings
        ]
    else:
        out.append("None.")
    refines = [c for c in data["cases"] if c.get("refine_step")]
    if refines:
        out += ["", "## Refine steps", ""]
        for c in refines:
            step = c["refine_step"]
            chips = ", ".join(
                f"{x['label']} ({x['kind']}{', approximate' if x.get('approximate') else ''})"
                for x in step.get("chips", [])
            )
            out.append(
                f'- {c["id"]}: "{step["text"]}" -> '
                + (chips if step.get("accepted") else f"not accepted ({step.get('message', '')})")
            )

    colors = [
        (c["id"], c["rounds"][0]["product_color"])
        for c in data["cases"]
        if c.get("rounds") and c["rounds"][0].get("product_color")
    ]
    if colors:
        out += ["", "## Product colour", ""]
        for cid, row in colors:
            if row.get("measure") == "judge":
                out.append(
                    f"- {cid}: {pct(row['share'])} judged from the thumbnails (category judge v"
                    f"{data['judge_versions']['category']}); {pct(row.get('title_share'))} by title "
                    f"keywords (the figure the baseline report used)"
                )
            else:
                out.append(f"- {cid}: {pct(row['share'])} by title keywords only")
        if any(r.get("measure") == "judge" for _, r in colors):
            out.append(
                "- The measurement changed after the baseline: the first live run scored colour from "
                "title keywords, which miss every title that names no colour. The threshold is unchanged."
            )

    out += decidable_section(data)
    out += source_section(data)
    t = data["totals"]
    searches = [c["rounds"][0]["seconds"] for c in data["cases"] if c.get("rounds")]
    agains = [r["seconds"] for c in data["cases"] for r in c.get("rounds", [])[1:]]
    js = data["judge_stats"]
    out += [
        "", "## Cost and time", "",
        f"- Anthropic ${t['anthropic_usd']:.4f} (pipeline and judges), Serper {t['serper_credits']:.0f} credits",
        f"- Judges: {js['calls']} calls, {js['retries']} retries, {js['failed_items']} items judge-failed, "
        f"${js['cost_usd']:.4f}, thumbnails used {js['images_used']} of {js['images_offered']}",
        f"- First search: median {num(statistics.median(searches) if searches else None)} s; "
        f"Shop Again: median {num(statistics.median(agains) if agains else None)} s, "
        f"max {num(max(agains) if agains else None)} s",
        "", "## Limits of this eval", "", LIMITS, "",
    ]  # fmt: skip
    return "\n".join(out)


def render_file(results_path: Path) -> str:
    data = json.loads(results_path.read_text())
    return render(data)
