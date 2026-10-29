"""Why did a case's persona match rate move after round 1? An offline reading of a saved results file
(stage 2r). It changes nothing in the pipeline and spends nothing.

For each case: the chips the pipeline had learned when it built round 2 (`trace.learned`), how many
round-2 items match them (the pipeline's own "matches your preferences x of y"), the persona like
rate among round-2 items that match the decided chips against those that do not, and the round-1 like
rate grouped by the same dimensions. Matching here reads the title with the keyword vocabulary
(`vocab.title_value`; the saved file has no model tags), so an item whose title says nothing about a
dimension is left out of that comparison.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.shop.retailers import seller_key
from app.shop.vocab import tag_values, title_value

GOOD = {"love", "like"}
DECIDED = {"converged", "leaning", "seller"}
CEILING = 0.65  # a first batch this well liked leaves little to learn
SHORT_BATCH = 20
MIN_GROUP = 3
RIGHT_BY_POINTS = 10.0

UNCHECKABLE = "learned something the titles cannot confirm"
VERDICTS = (
    "learned the right thing",
    "learned something that does not predict likes",
    "nothing learned (ceiling or no signal)",
    "supply limited",
    UNCHECKABLE,
)


def liked(items: list[dict[str, Any]]) -> tuple[int, int]:
    scored = [i for i in items if i.get("persona")]
    return sum(1 for i in scored if i["persona"] in GOOD), len(scored)


def rate(items: list[dict[str, Any]]) -> float | None:
    good, n = liked(items)
    return good / n if n else None


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def decided_chips(round_row: dict[str, Any]) -> list[dict[str, Any]]:
    chips = ((round_row.get("trace") or {}).get("learned") or {}).get("chips") or []
    out = []
    for c in chips:
        if c.get("dimension") == "price" or c.get("kind", "").startswith("price"):
            continue
        status = c.get("status") or ""
        if c.get("kind") == "seller" or status in DECIDED:
            out.append(c)
    return out


def holds(item: dict[str, Any], chip: dict[str, Any]) -> bool | None:
    """Whether the item's title shows the chip's value (None: the title says nothing about it)."""
    if chip.get("kind") == "seller":
        text = str(chip.get("text", "")).split(":", 1)[-1].strip()
        return seller_key(item.get("seller", "")) == seller_key(text)
    dim = chip.get("dimension") or ""
    got = title_value(dim, item.get("title", ""))
    if got is None:
        return None
    return bool(set(chip.get("values") or []) & set(tag_values(dim, got)))


def split(
    items: list[dict[str, Any]], chips: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(items whose titles satisfy every decidable chip, all the other items: contradicting titles
    and titles that say nothing)."""
    yes: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for it in items:
        marks = [holds(it, c) for c in chips]
        (yes if marks and all(m is True for m in marks) else rest).append(it)
    return yes, rest


def diagnose_case(case: dict[str, Any]) -> dict[str, Any]:
    rounds = case.get("rounds") or []
    row: dict[str, Any] = {"id": case["id"], "request": case.get("request", "")}
    if len(rounds) < 2:
        return {**row, "verdict": "nothing learned (ceiling or no signal)", "note": "no round 2"}
    r1, r2 = rounds[0], rounds[1]
    chips = decided_chips(r2)
    items1, items2 = r1.get("items") or [], r2.get("items") or []
    yes, no = split(items2, chips)
    align = ((r2.get("trace") or {}).get("learned") or {}).get("alignment") or {}
    by_chip = []
    for c in chips:
        a = [i for i in items1 if holds(i, c) is True]
        b = [i for i in items1 if holds(i, c) is False]
        by_chip.append({"chip": c.get("text"), "r1_with": rate(a), "n_with": liked(a)[1],
                        "r1_without": rate(b), "n_without": liked(b)[1]})  # fmt: skip
    ry, rn = rate(yes), rate(no)
    r1_rate, r2_rate = rate(items1), rate(items2)
    if len(items2) < SHORT_BATCH:
        verdict = "supply limited"
    elif not chips:
        verdict = "nothing learned (ceiling or no signal)"
    elif (
        liked(yes)[1] >= MIN_GROUP
        and liked(no)[1] >= MIN_GROUP
        and ry is not None
        and rn is not None
    ):
        verdict = (
            "learned the right thing"
            if (ry - rn) * 100 >= RIGHT_BY_POINTS
            else "learned something that does not predict likes"
        )
    else:
        verdict = UNCHECKABLE
    if verdict == UNCHECKABLE and r1_rate is not None and r1_rate >= CEILING:
        verdict = "nothing learned (ceiling or no signal)"  # a first batch this good leaves no lift
    return {
        **row, "chips": [c.get("text") for c in chips], "r1_rate": r1_rate, "r2_rate": r2_rate,
        "r2_count": len(items2), "pipeline_matches": [align.get("matches"), align.get("total")],
        "matching": {"n": liked(yes)[1], "rate": ry}, "contradicting": {"n": liked(no)[1], "rate": rn},
        "by_chip": by_chip, "verdict": verdict,
        "ceiling": bool(r1_rate is not None and r1_rate >= CEILING),
    }  # fmt: skip


def diagnose(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [diagnose_case(c) for c in data.get("cases", []) if c.get("status") == "ok"]


def render(data: dict[str, Any], name: str = "") -> str:
    rows = diagnose(data)
    out = [
        "| Case | Chips learned after round 1 | r1 like | r2 like | r2 shown | Pipeline matches | "
        "Titles match (n, like) | Other items (n, like) | Verdict |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        pm = r.get("pipeline_matches") or [None, None]
        out.append(
            f"| {r['id']} | {'; '.join(r.get('chips') or []) or 'none'} | {pct(r.get('r1_rate'))}"
            f"{' (ceiling)' if r.get('ceiling') else ''} | {pct(r.get('r2_rate'))} | {r.get('r2_count', 0)} | "
            f"{pm[0] if pm[0] is not None else 'n/a'} of {pm[1] if pm[1] is not None else 'n/a'} | "
            f"{(r.get('matching') or {}).get('n', 0)}, {pct((r.get('matching') or {}).get('rate'))} | "
            f"{(r.get('contradicting') or {}).get('n', 0)}, {pct((r.get('contradicting') or {}).get('rate'))} | "
            f"{r['verdict']} |"
        )
    out += [
        "",
        "Round-1 like rate grouped by each learned chip (titles that show the value, titles that contradict it):",
        "",
    ]
    for r in rows:
        for g in r.get("by_chip") or []:
            out.append(
                f"- {r['id']} {g['chip']}: with {g['n_with']} items, like {pct(g['r1_with'])}; "
                f"without {g['n_without']} items, like {pct(g['r1_without'])}"
            )
    counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in VERDICTS}
    out += ["", "Verdicts: " + "; ".join(f"{v} {n}" for v, n in counts.items())]
    return "\n".join(out)


def render_file(path: Path) -> str:
    return render(json.loads(path.read_text()), path.name)
