"""`make eval-shop-replay`: a zero-spend structural check against saved Serper responses (stage 2r).

The run uses the saved responses (`evals/shop/cache/serper/`, miss is an error), the mock model and
the mock persona and judges. Nothing here says anything about quality: the mock model tags and ranks
arbitrarily. What it can show is structure: batch sizes against 24, sellers, price compliance,
repeats, near-duplicate groups, the calls and credits a live run would spend, the fill loop's stop
reasons, the guards switched off, and what removed candidates.
"""

from __future__ import annotations

from typing import Any

LABEL = "structural only; not a quality result"
EMPTY_MESSAGE = (
    "The saved Serper responses are missing or empty: run a live eval once with recording "
    "(make eval-shop-live, EVAL_SHOP_SERPER_CACHE=record, the default for that target)."
)


def _round_row(case_id: str, r: dict[str, Any]) -> dict[str, Any]:
    trace = r.get("trace") or {}
    short = ((trace.get("pool") or {}).get("shortfall")) or {}
    fill = trace.get("fill") or {}
    return {
        "case": case_id, "round": r["round"], "count": r.get("count", 0),
        "distinct": (r.get("sellers") or {}).get("distinct"),
        "top_share": (r.get("sellers") or {}).get("top_share"),
        "price": (r.get("price") or {}).get("share"),
        "near_dups": (r.get("near_dups") or {}).get("groups"),
        "credits": (r.get("cost") or {}).get("serper_credits"),
        "fill_stop": fill.get("stop") or "",
        "fill_steps": len(fill.get("steps") or []),
        "guards_off": [g["dimension"] for g in (trace.get("guards_off") or [])],
        "breakdown": short.get("breakdown") or {},
    }  # fmt: skip


def summarize(data: dict[str, Any]) -> dict[str, Any]:
    rows = [_round_row(c["id"], r) for c in data["cases"] for r in c.get("rounds", [])]
    repeats = {c["id"]: (c.get("repeats") or {}).get("count") for c in data["cases"]}
    credits = sum(float(r["credits"] or 0) for r in rows)
    return {
        "rows": rows,
        "repeats": repeats,
        "credits": credits,
        "short": [r for r in rows if r["count"] < 24],
        "cache": data.get("serper_cache") or {},
        "failed": [c["id"] for c in data["cases"] if c.get("status") != "ok"],
    }


def render(data: dict[str, Any]) -> str:
    s = summarize(data)
    cache = s["cache"]
    lines = [
        f"Shop eval replay: {LABEL}",
        f"Serper cache: {cache.get('hits', 0)} hits, {cache.get('misses', 0)} misses; a live run "
        f"would spend about {s['credits']:.0f} credits ({s['credits'] / 1000:.2f} dollars at "
        "$0.001 a credit).",
        "",
        "case     rnd shown sellers top   price near-dup credits fill stop / guards off",
    ]
    for r in s["rows"]:
        top = "n/a" if r["top_share"] is None else f"{100 * r['top_share']:.0f}%"
        price = "n/a" if r["price"] is None else f"{100 * r['price']:.0f}%"
        extra = r["fill_stop"] or "-"
        if r["guards_off"]:
            extra += f"; guards off: {', '.join(r['guards_off'])}"
        lines.append(
            f"{r['case']:8} {r['round']:3} {r['count']:5} {r['distinct'] if r['distinct'] is not None else 'n/a':>7} "
            f"{top:5} {price:5} {r['near_dups'] if r['near_dups'] is not None else 'n/a':>8} "
            f"{r['credits'] if r['credits'] is not None else 'n/a':>7} {extra}"
        )
    short = s["short"]
    lines += ["", f"{len(short)} of {len(s['rows'])} batches under 24"]
    for r in short:
        bd = r["breakdown"]
        if bd:
            causes = ", ".join(
                f"{k} {v}" for k, v in bd.items() if k not in ("returned", "shown") and v
            )
            lines.append(
                f"  {r['case']} r{r['round']}: {r['count']} shown of {bd.get('returned')} returned: {causes}"
            )
    bad = {k: v for k, v in s["repeats"].items() if v}
    lines.append(
        "repeats across rounds: "
        + ("none" if not bad else ", ".join(f"{k} {v}" for k, v in bad.items()))
    )
    if s["failed"]:
        lines.append("cases that did not finish: " + ", ".join(s["failed"]))
    lines += ["", LABEL]
    return "\n".join(lines)
