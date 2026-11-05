"""The report writer: `report.md` (per request, a summary table and a verdict against the targets) and
`contact-sheet.html` (the question cards as a visitor would see them), from a results file.

Targets (the brief's, informational: they are the spike's question, not locked thresholds):
median time to questions <= 4 s and max <= 6 s; at least 70% of options with a picture; at most one
flagged question per request on average; mean lift with answers > 0.

The results folder is gitignored (third-party titles and thumbnail URLs); nothing here is committed.
"""

from __future__ import annotations

import html
import json
import statistics
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from evals.shop_clarify.spec import RESULTS_DIR

TARGETS = {
    "median_s": 4.0, "max_s": 6.0, "picture_coverage": 0.70, "flagged_per_request": 1.0, "mean_lift_points": 0.0,
}  # fmt: skip
PLACEHOLDER_HOSTS = ("mock.example", "example.invalid")


def _ok(case: dict[str, Any]) -> bool:
    return case.get("status") in ("ok", "partial") and bool(case.get("timing"))


def flagged_questions(case: dict[str, Any]) -> int:
    """Distinct questions carrying at least one flag (a flag on the whole set counts once)."""
    return len({f["question_id"] for f in case.get("flags", [])})


def summarise(payload: dict[str, Any]) -> dict[str, Any]:
    """The numbers the verdict is made from."""
    cases = [c for c in payload["cases"] if _ok(c)]
    totals = [c["timing"]["total_s"] for c in cases]
    options = sum(c["images"]["options"] for c in cases)
    with_image = sum(c["images"]["with_image"] for c in cases)
    per_case_cov = [
        c["images"]["coverage"] for c in cases if c["images"].get("coverage") is not None
    ]
    flagged = [flagged_questions(c) for c in cases]
    lifts = [
        c["lift"]["persona_points"]
        for c in cases
        if c.get("lift", {}).get("persona_points") is not None
    ]
    attr_lifts = [
        c["lift"]["attribute_points"]
        for c in cases
        if c.get("lift", {}).get("attribute_points") is not None
    ]
    with_answers = [c for c in cases if c.get("arms", {}).get("answers")]
    answerable = sum(1 for c in cases for p in c.get("persona", []) if p["answer"])
    asked = sum(len(c["questions"]) for c in cases)
    return {
        "cases": len(payload["cases"]), "measured": len(cases),
        "failed": [c["id"] for c in payload["cases"] if c.get("status") == "failed"],
        "median_s": statistics.median(totals) if totals else None,
        "max_s": max(totals) if totals else None,
        "median_fetch_s": statistics.median([c["timing"]["fetch_s"] for c in cases]) if cases else None,
        "median_model_s": statistics.median([c["timing"]["model_s"] for c in cases]) if cases else None,
        "median_parse_s": statistics.median([c["timing"]["parse_s"] for c in cases]) if cases else None,
        "median_overlapped_s": statistics.median([c["timing"]["overlapped_s"] for c in cases]) if cases else None,
        "questions_mean": statistics.mean([len(c["questions"]) for c in cases]) if cases else None,
        "options_mean": options / asked if asked else None,
        "picture_coverage": with_image / options if options else None,
        "picture_coverage_mean": statistics.mean(per_case_cov) if per_case_cov else None,
        "flagged_per_request": statistics.mean(flagged) if flagged else None,
        "flag_counts": _flag_counts(cases),
        "requests_with_answers": len(with_answers),
        "questions_asked": asked, "questions_answerable": answerable,
        "mean_lift_points": statistics.mean(lifts) if lifts else None,
        "mean_lift_points_all": statistics.mean([c.get("lift", {}).get("persona_points") or 0.0 for c in cases]) if cases else None,
        "mean_attribute_lift_points": statistics.mean(attr_lifts) if attr_lifts else None,
        "improved": sum(1 for x in lifts if x > 0), "worsened": sum(1 for x in lifts if x < 0),
        "anthropic_usd": payload["totals"]["anthropic_usd"], "serper_credits": payload["totals"]["serper_credits"],
    }  # fmt: skip


def _flag_counts(cases: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in cases:
        for f in c.get("flags", []):
            counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    return counts


def verdict_lines(s: dict[str, Any], mode: str) -> list[str]:
    """A plain-language verdict against the four targets (and what a mock run can and cannot say)."""

    def mark(ok: bool | None) -> str:
        return "met" if ok else ("NOT met" if ok is not None else "no data")

    t = TARGETS
    med, mx = s["median_s"], s["max_s"]
    time_ok = None if med is None else (med <= t["median_s"] and (mx or 0) <= t["max_s"])
    cov = s["picture_coverage"]
    flg = s["flagged_per_request"]
    lift = s["mean_lift_points"]
    lines = [
        f"- Time to questions: median {_f(med)} s, max {_f(mx)} s (targets {t['median_s']:.0f} s and {t['max_s']:.0f} s): "
        f"**{mark(time_ok)}**. Median parts: parse {_f(s['median_parse_s'])} s, fetch {_f(s['median_fetch_s'])} s, "
        f"questions {_f(s['median_model_s'])} s; with the parse overlapped with the fetch the median would be {_f(s['median_overlapped_s'])} s.",
        f"- Pictures: {_pct(cov)} of options have a title-evidenced thumbnail (target {t['picture_coverage']:.0%}): **{mark(None if cov is None else cov >= t['picture_coverage'])}**.",
        f"- Question quality: {_f(flg)} flagged question(s) per request on average (target at most {t['flagged_per_request']:.0f}): "
        f"**{mark(None if flg is None else flg <= t['flagged_per_request'])}**.",
        f"- Effect of answers: mean persona-match lift {_pts(lift)} over the {s['requests_with_answers']} request(s) that had an answerable question "
        f"({_pts(s['mean_lift_points_all'])} counting the others as 0; attribute-match lift {_pts(s['mean_attribute_lift_points'])}) "
        f"(target above 0): **{mark(None if lift is None else lift > t['mean_lift_points'])}**. "
        f"{s['improved']} improved, {s['worsened']} worsened.",
    ]
    if mode == "mock":
        lines.append(
            "- This is a MOCK run: the catalogue, the question writer, the judges and the timings are canned. It shows that the harness works, not how good the questions are."
        )
    else:
        lines.append(
            "- Read the lift as a direction, not a number: the persona is a text, 12 requests are a small sample, and the judges are the pipeline's own model family (see evals/shop/README.md)."
        )
    return lines


def _f(x: float | None, d: int = 1) -> str:
    return "n/a" if x is None else f"{x:.{d}f}"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _pts(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1f} points"


def render_report(payload: dict[str, Any]) -> str:
    s = summarise(payload)
    out = [
        f"# Clarifying questions spike: run {payload['run_id']} ({payload['mode']})",
        "",
        f"Prompt `shop_clarify` v{payload['prompt_version']}; models {payload['models']['clarify']}; "
        f"spend ${payload['totals']['anthropic_usd']:.4f} Anthropic, {payload['totals']['serper_credits']:.0f} Serper credits "
        f"(stops ${payload['limits']['anthropic_usd']:.2f} and {payload['limits']['serper_credits']:.0f}); "
        f"Serper cache: {payload['serper_cache'].get('mode')}"
        + (
            f", {payload['serper_cache'].get('hits')} hits"
            if payload["serper_cache"].get("hits")
            else ""
        )
        + f"; thresholds.json SHA-256 {payload['thresholds_sha256'][:12]}...",
        "",
    ]
    if payload.get("stopped"):
        out += [f"**Stopped early:** {payload['stopped']}", ""]
    out += ["## Verdict", "", *verdict_lines(s, payload["mode"]), ""]
    out += [
        "## Summary", "",
        "| request | kind | time to questions (s) | questions | options | pictures | flagged | persona lift | attribute lift | cost |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]  # fmt: skip
    for c in payload["cases"]:
        if not _ok(c):
            out.append(
                f"| {c['request']} | {c['vagueness']} | {c['status']}: {c.get('error') or ''} | | | | | | | |"
            )
            continue
        n_opts = sum(len(q["options"]) for q in c["questions"])
        lift = c.get("lift") or {}
        cost = c.get("cost", {})
        out.append(
            f"| {c['request']} | {c['vagueness']} | {c['timing']['total_s']:.1f} | {len(c['questions'])} | {n_opts} | "
            f"{_pct(c['images'].get('coverage'))} | {flagged_questions(c)} | {_pts(lift.get('persona_points'))} | "
            f"{_pts(lift.get('attribute_points'))} | ${cost.get('anthropic_usd', 0):.4f} + {cost.get('serper_credits', 0):.0f} cr |"
        )
    out += [
        "",
        f"Flags by kind: {s['flag_counts'] or 'none'}. Questions the persona could answer: {s['questions_answerable']} of {s['questions_asked']}.",
        "",
        "## Per request",
        "",
    ]
    for c in payload["cases"]:
        out += [f"### {c['request']} ({c['vagueness']})", ""]
        if not _ok(c):
            out += [f"Status: {c['status']} {c.get('error') or ''}", ""]
            continue
        t = c["timing"]
        out += [
            f"- Time to questions {t['total_s']:.1f} s: parse {t['parse_s']:.2f}, fetch {t['fetch_s']:.2f}"
            f"{' (cached; the latency recorded when it was live)' if t['fetch_cached'] and t['fetch_latency_known'] else ''}"
            f"{' (cached; no live latency recorded, wall time shown)' if t['fetch_cached'] and not t['fetch_latency_known'] else ''}"
            f", questions {t['model_s']:.2f}; {t['candidates']} candidates, {t['shown_to_model']} shown to the model; search text `{c.get('search_text', '')}`",
            f"- Pictures: {c['images']['with_image']} of {c['images']['options']} options ({_pct(c['images'].get('coverage'))})",
        ]
        for note in c["notes"]:
            out.append(f"- Validation note: {note}")
        out.append("")
        for q in c["questions"]:
            out.append(f"**{q['text']}** ({q['dimension']}, {q['kind']})")
            for o in q["options"]:
                pic = o["pick"]
                out.append(
                    f"  - {o['label']} (`{o['value']}`): "
                    + (
                        f'picture from "{pic["title"][:60]}" ({pic["how"]})'
                        if pic
                        else "no picture"
                    )
                )
            out.append("")
        if c["flags"]:
            out.append(
                "Flags: "
                + "; ".join(
                    f"{f['question_id'] or 'set'}: {f['kind']} ({f['detail']})" for f in c["flags"]
                )
            )
            out.append("")
        if c["persona"]:
            out.append(
                "Persona: "
                + "; ".join(
                    f"{p['question']}: {p['answer'] or 'unanswerable'} [{p['how']}]"
                    for p in c["persona"]
                )
            )
            out.append("")
        if c.get("applied"):
            out.append(
                "Answers became: "
                + "; ".join(f"{a['answer']} -> {a['became']}" for a in c["applied"])
            )
            out.append("")
        base, ans = c["arms"].get("baseline"), c["arms"].get("answers")
        if base:
            out.append(
                f"- Baseline (top {base['count']}): persona match {_pct(base['persona']['rate'])}, attribute match {_pct(base['attributes']['rate'])}, search text `{base['search_text']}`"
            )
        if ans:
            out.append(
                f"- With answers (top {ans['count']}): persona match {_pct(ans['persona']['rate'])}, attribute match {_pct(ans['attributes']['rate'])}, search text `{ans['search_text']}`"
            )
        elif base:
            out.append("- With answers: not run (the persona could not answer any question)")
        out.append("")
    return "\n".join(out) + "\n"


# ---- the contact sheet ------------------------------------------------------------------------

CSS = """
body{font:16px/1.5 system-ui,sans-serif;margin:0;background:#f6f6f3;color:#0b1b33}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.6rem} h2{font-size:1.25rem;margin:40px 0 4px} .meta{color:#6c7686;font-size:.9rem;margin:0 0 12px}
.card{background:#fff;border:1px solid #deddd6;border-radius:6px;padding:20px;margin:0 0 16px;max-width:760px}
.card.flagged{border-color:#e85d3f}
.q{font-weight:600;margin:0 0 12px}
.opts{display:flex;flex-wrap:wrap;gap:12px}
.opt{width:150px;border:1px solid #deddd6;border-radius:6px;overflow:hidden;background:#fff;text-align:center;font-size:.95rem}
.opt .pic{height:120px;background:#f6f6f3;display:flex;align-items:center;justify-content:center;color:#6c7686;font-size:.8rem}
.opt .pic img{width:100%;height:100%;object-fit:contain;display:block}
.opt .lab{padding:8px}
.opt.none .pic{background:#fff}
details{font-size:.85rem;color:#45505f;margin:8px 0 0} .tag{display:inline-block;background:#fbe3dc;color:#9a2f16;border-radius:99px;padding:0 8px;margin-right:6px;font-size:.8rem}
"""


def _thumb(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname is not None
        and parts.hostname not in PLACEHOLDER_HOSTS
    )


def render_contact_sheet(payload: dict[str, Any]) -> str:
    """One self-contained page: per request, the question cards as a visitor would see them (question
    text, option labels, the chosen thumbnail for each option, and a "no preference" choice). A
    thumbnail is the listing's own https image URL (loaded by your browser); a mock run, or an option
    with no picture, shows a plain placeholder. Flags and the persona's pick sit in a closed
    <summary> under each card, so the card itself is what a visitor would see."""
    e = html.escape
    s = summarise(payload)
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Clarifying questions: contact sheet ({e(payload['run_id'])})</title><style>{CSS}</style></head><body><main>",
        f"<h1>Clarifying questions: contact sheet</h1><p class='meta'>Run {e(payload['run_id'])} ({e(payload['mode'])}). "
        f"Median time to questions {_f(s['median_s'])} s, pictures on {_pct(s['picture_coverage'])} of options. "
        "Each card is what a visitor would see; the closed details under it hold the checks.</p>",
    ]
    for c in payload["cases"]:
        parts.append(f"<h2>{e(c['request'])}</h2>")
        if not _ok(c):
            parts.append(f"<p class='meta'>{e(c['status'])}: {e(str(c.get('error') or ''))}</p>")
            continue
        t = c["timing"]
        parts.append(
            f"<p class='meta'>{e(c['vagueness'])} · questions in {t['total_s']:.1f} s · {len(c['questions'])} question(s) · "
            f"pictures {_pct(c['images'].get('coverage'))}</p>"
        )
        if not c["questions"]:
            parts.append("<p class='meta'>No questions for this request.</p>")
        flagged = {f["question_id"] for f in c["flags"]}
        flags_by_q: dict[str, list[dict[str, Any]]] = {}
        for f in c["flags"]:
            flags_by_q.setdefault(f["question_id"], []).append(f)
        persona = {p["question"]: p for p in c["persona"]}
        for q in c["questions"]:
            cls = "card flagged" if q["id"] in flagged else "card"
            tiles = []
            for o in q["options"]:
                pick = o["pick"]
                if pick and _thumb(pick["thumbnail_url"]):
                    pic = f"<img src='{e(pick['thumbnail_url'])}' alt='' loading='lazy' referrerpolicy='no-referrer'>"
                elif pick:
                    pic = "picture (mock)"
                else:
                    pic = "no picture"
                tiles.append(
                    f"<div class='opt'><div class='pic'>{pic}</div><div class='lab'>{e(o['label'])}</div></div>"
                )
            tiles.append(
                "<div class='opt none'><div class='pic'>&nbsp;</div><div class='lab'>No preference</div></div>"
            )
            detail = []
            for f in flags_by_q.get(q["id"], []):
                detail.append(f"<span class='tag'>{e(f['kind'])}</span>{e(f['detail'])}<br>")
            p = persona.get(q["id"])
            if p:
                detail.append(
                    f"Persona ({e(p['trait'] or 'no trait')}): {e(p['answer'] or 'unanswerable')} [{e(p['how'])}]<br>"
                )
            for o in q["options"]:
                pk = o["pick"]
                detail.append(
                    f"{e(o['label'])}: "
                    + (
                        f"picture from {e(pk['title'][:70])} ({e(pk['how'])})"
                        if pk
                        else "no picture"
                    )
                    + "<br>"
                )
            parts.append(
                f"<div class='{cls}'><p class='q'>{e(q['text'])}</p><div class='opts'>{''.join(tiles)}</div>"
                f"<details><summary>checks</summary>{''.join(detail)}</details></div>"
            )
        set_flags = flags_by_q.get("", [])
        if set_flags:
            parts.append(
                "<p class='meta'>"
                + " ".join(
                    f"<span class='tag'>{e(f['kind'])}</span>{e(f['detail'])}" for f in set_flags
                )
                + "</p>"
            )
    parts.append("</main></body></html>")
    return "".join(parts)


def newest(directory: Path = RESULTS_DIR) -> Path | None:
    files = sorted(directory.glob("clarify-*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def write_report(results: Path, out_dir: Path | None = None) -> tuple[Path, Path]:
    """Write `report.md` and `contact-sheet.html` next to the results (or in `out_dir`)."""
    payload = json.loads(results.read_text())
    folder = out_dir or results.parent
    folder.mkdir(parents=True, exist_ok=True)
    md, sheet = folder / "report.md", folder / "contact-sheet.html"
    md.write_text(render_report(payload))
    sheet.write_text(render_contact_sheet(payload))
    return md, sheet
