"""A static HTML report for one spike run (and an index of runs). A developer tool, not the product UI.

`write_report(run_dir)` reads `result.json` (and notes `trace.jsonl` and `room.jpg` when present) and
writes `report.html` beside them. `write_index(out_root)` lists every run under `spike-output/`.

Everything in a run's JSON is untrusted provider text: every string is escaped, links are only
emitted for http and https URLs (with `rel="noopener noreferrer"`), and the page has no script, no
event handler and no external stylesheet or font. Its Content-Security-Policy allows inline styles
and https or data images only. Remote product thumbnails are shown in this local report only.

Old runs lack newer fields (mode, preferences, thumbnails, sizes ...), so every read is defensive.
The output is deterministic for the same input.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

REPORT_NAME = "report.html"
INDEX_NAME = "index.html"
ROOM_IMAGE_NAME = "room.jpg"
PHOTO_DIR = Path("spike-photos")
PLAN_SCALE = 100.0  # SVG units per metre
PLAN_PAD = 40.0
BAR_W = 300.0
BAR_PAD = 12.0
CSP = (
    "default-src 'none'; img-src https: data:; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'"
)

# ---- defensive readers ------------------------------------------------------------------------


def _d(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _l(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _f(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def esc(v: Any) -> str:
    return escape(_s(v), quote=True)


def money(v: Any) -> str:
    n = _f(v)
    return "-" if n is None else f"${n:,.2f}"


def secs(v: Any) -> str:
    n = _f(v)
    return "-" if n is None else (f"{n:.2f} s" if n < 10 else f"{n:.1f} s")


def safe_url(url: Any) -> str | None:
    """The URL if it is plain http(s) with a host and no control characters, else None."""
    text = _s(url).strip()
    if not text or re.search(r"[\x00-\x20\x7f<>\"'`\\]", text):
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return text


def link(url: Any, label: str) -> str:
    """An anchor for a safe URL (label is already-escaped HTML); plain text label otherwise."""
    safe = safe_url(url)
    if safe is None:
        return f'<span class="nolink">{label} (no usable link)</span>'
    return f'<a href="{esc(safe)}" target="_blank" rel="noopener noreferrer">{label}</a>'


def chip(text: Any, tone: str = "neutral") -> str:
    return f'<span class="chip {esc(tone)}">{esc(text)}</span>'


# ---- run facts --------------------------------------------------------------------------------


def run_mode(r: dict[str, Any]) -> str:
    mode = _s(r.get("mode"))
    if mode in ("freeplay", "budget"):
        return mode
    return "freeplay" if r.get("budget_usd") is None else "budget"


def proposal_items(r: dict[str, Any]) -> list[dict[str, Any]]:
    return [_d(i) for i in _l(_d(r.get("proposal")).get("items"))]


def found_count(r: dict[str, Any]) -> int:
    items = proposal_items(r)
    if items:
        return sum(1 for i in items if _d(i.get("pick")))
    return sum(1 for s in _l(r.get("retrieval")) if _l(_d(s).get("candidates")))


def requested_count(r: dict[str, Any]) -> int:
    n = r.get("items")
    if isinstance(n, int) and n > 0:
        return n
    return len(_l(r.get("retrieval"))) or len(proposal_items(r))


def critical_rows(rows: list[dict[str, Any]]) -> set[int]:
    """Indexes of the rows on the critical path (mirrors `SpikeResult.critical_path_seconds`)."""
    on: set[int] = set()
    groups: dict[str, int] = {}
    chains: dict[str, list[int]] = {}
    lookups: dict[str, int] = {}
    for i, row in enumerate(rows):
        slot, group, sec = row.get("slot"), row.get("group"), _f(row.get("seconds")) or 0.0
        if slot is None and group is None:
            on.add(i)
        elif group is not None:
            j = groups.get(str(group))
            if j is None or sec > (_f(rows[j].get("seconds")) or 0.0):
                groups[str(group)] = i
        elif row.get("kind") == "lookup":
            j = lookups.get(str(slot))
            if j is None or sec > (_f(rows[j].get("seconds")) or 0.0):
                lookups[str(slot)] = i
        else:
            chains.setdefault(str(slot), []).append(i)
    on |= set(groups.values())

    def chain_secs(slot: str) -> float:
        total = sum(_f(rows[i].get("seconds")) or 0.0 for i in chains.get(slot, []))
        j = lookups.get(slot)
        return total + (_f(rows[j].get("seconds")) or 0.0 if j is not None else 0.0)

    slots = sorted({*chains, *lookups})
    if slots:
        best = max(slots, key=chain_secs)
        on |= set(chains.get(best, []))
        if best in lookups:
            on.add(lookups[best])
    return on


def run_totals(r: dict[str, Any]) -> dict[str, float | None]:
    rows = [_d(x) for x in _l(r.get("rows"))]
    t = _d(r.get("totals"))
    anthropic = _f(t.get("anthropic_cost_usd"))
    provider = _f(t.get("provider_cost_usd_estimated"))
    if anthropic is None:
        anthropic = sum(_f(x.get("cost_usd")) or 0.0 for x in rows if not x.get("cost_estimated"))
    if provider is None:
        provider = sum(_f(x.get("cost_usd")) or 0.0 for x in rows if x.get("cost_estimated"))
    critical = _f(t.get("critical_path_seconds"))
    if critical is None and rows:
        crit = critical_rows(rows)
        critical = sum(_f(rows[i].get("seconds")) or 0.0 for i in crit)
    wall = _f(t.get("wall_clock_seconds")) or _f(r.get("wall_clock_seconds"))
    return {"anthropic": anthropic, "provider": provider, "critical": critical, "wall": wall}


def one_line_summary(r: dict[str, Any]) -> str:
    if r.get("perception_only"):
        return "perception only"
    if not _l(r.get("retrieval")):
        return "no retrieval"
    found, asked = found_count(r), requested_count(r)
    text = f"{found} of {asked} items"
    totals = _d(_d(r.get("proposal")).get("totals"))
    if _f(totals.get("total")) is not None:
        text += f", {money(totals.get('total'))} total"
    elif not proposal_items(r):
        text += ", no proposal"
    names = [_s(_d(i).get("slot")) for i in proposal_items(r) if _d(i.get("pick"))] or [
        _s(_d(_d(s).get("slot")).get("category")) for s in _l(r.get("retrieval"))
    ]
    names = [n for n in names if n]
    return text + (
        ": " + ", ".join(names[:4]) + (" ..." if len(names) > 4 else "") if names else ""
    )


# ---- floor plan -------------------------------------------------------------------------------


@dataclass
class PlanRect:
    kind: str  # kept | replaced | fixed | new
    x: float  # top-left, metres from the west wall
    y: float  # top-left, metres from the north wall
    w: float
    h: float
    label: str


@dataclass
class PlanOpening:
    kind: str
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass
class PlanShapes:
    width_m: float
    length_m: float
    rects: list[PlanRect] = field(default_factory=list)
    openings: list[PlanOpening] = field(default_factory=list)
    unplaced: list[str] = field(default_factory=list)  # new picks with no old position


def decisions(r: dict[str, Any]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for x in _l(_d(r.get("preferences")).get("resolved")):
        x = _d(x)
        out[_s(x.get("id"))] = (_s(x.get("decision")), _s(x.get("source")))
    return out


def plan_shapes(r: dict[str, Any]) -> PlanShapes | None:
    """Plan geometry from perception: x = fraction of room width (west to east), y = fraction of
    room length (north to south); sizes are width along x and depth along y. None without a room."""
    p = _d(r.get("perception"))
    dims = _d(p.get("dimensions"))
    w, ln = _f(dims.get("width_m")), _f(dims.get("length_m"))
    if not w or not ln or w <= 0 or ln <= 0:
        return None
    shapes = PlanShapes(w, ln)

    def box(item: dict[str, Any]) -> tuple[float, float, float, float] | None:
        pos, size = _d(item.get("position")), _d(item.get("size"))
        px, py = _f(pos.get("x")), _f(pos.get("y"))
        sw, sd = _f(size.get("width_m")), _f(size.get("depth_m"))
        if px is None or py is None or not sw or not sd:
            return None
        return (px * w - sw / 2, py * ln - sd / 2, sw, sd)

    for fx in _l(p.get("fixed_items")):
        fx = _d(fx)
        b = box(fx)
        if b:
            shapes.rects.append(PlanRect("fixed", *b, _s(fx.get("category"))))
    decided = decisions(r)
    furniture = {_s(_d(f).get("id")): _d(f) for f in _l(p.get("furniture"))}
    for fid, f in furniture.items():
        b = box(f)
        if b is None:
            continue
        replaced = decided.get(fid, ("", ""))[0] == "replace"
        shapes.rects.append(
            PlanRect("replaced" if replaced else "kept", *b, f"{fid} {_s(f.get('category'))}")
        )
    for item in proposal_items(r):
        if not _d(item.get("pick")):
            continue
        rep = _d(item.get("replaces"))
        old = furniture.get(_s(rep.get("id")))
        b = box(old) if old else None
        if b:
            shapes.rects.append(PlanRect("new", *b, _s(item.get("slot"))))
        else:
            shapes.unplaced.append(_s(item.get("slot")))
    for o in _l(p.get("openings")):
        o = _d(o)
        along, width = _f(o.get("position_along_wall")), _f(o.get("width_m")) or 0.8
        wall = _s(o.get("wall"))
        if along is None or wall not in ("north", "south", "east", "west"):
            continue
        if wall in ("north", "south"):
            y = 0.0 if wall == "north" else ln
            cx = along * w
            shapes.openings.append(
                PlanOpening(_s(o.get("kind")), cx - width / 2, y, cx + width / 2, y)
            )
        else:
            x = w if wall == "east" else 0.0
            cy = along * ln
            shapes.openings.append(
                PlanOpening(_s(o.get("kind")), x, cy - width / 2, x, cy + width / 2)
            )
    return shapes


def render_plan(s: PlanShapes) -> str:
    k, pad = PLAN_SCALE, PLAN_PAD
    vw, vh = s.width_m * k + 2 * pad, s.length_m * k + 2 * pad

    def n(v: float) -> str:
        return f"{v:.1f}"

    out = [
        f'<svg class="plan" viewBox="0 0 {n(vw)} {n(vh)}" role="img" '
        f'aria-label="Approximate floor plan, {s.width_m:.1f} by {s.length_m:.1f} metres">',
        f'<rect class="room" x="{n(pad)}" y="{n(pad)}" width="{n(s.width_m * k)}" '
        f'height="{n(s.length_m * k)}"/>',
    ]
    for rect in s.rects:
        cls = f"p-{rect.kind}"
        x, y, w, h = pad + rect.x * k, pad + rect.y * k, rect.w * k, rect.h * k
        out.append(
            f'<rect class="{cls}" x="{n(x)}" y="{n(y)}" width="{n(w)}" height="{n(h)}" rx="4"/>'
        )
        out.append(
            f'<text class="{cls}-t" x="{n(x + w / 2)}" y="{n(y + h / 2)}" text-anchor="middle" '
            f'dominant-baseline="middle">{esc(rect.label)}</text>'
        )
    for o in s.openings:
        cls = "o-window" if o.kind == "window" else "o-door" if o.kind == "door" else "o-other"
        out.append(
            f'<line class="{cls}" x1="{n(pad + o.x1 * k)}" y1="{n(pad + o.y1 * k)}" '
            f'x2="{n(pad + o.x2 * k)}" y2="{n(pad + o.y2 * k)}"><title>{esc(o.kind)}</title></line>'
        )
    out.append(
        f'<text class="compass" x="{n(vw / 2)}" y="{n(pad - 14)}" text-anchor="middle">N</text>'
    )
    out.append("</svg>")
    return "".join(out)


# ---- price bar --------------------------------------------------------------------------------


def price_bar_positions(stats: dict[str, Any], price: Any) -> dict[str, float] | None:
    """x positions (0..BAR_W) of min, p33, median, p67, max and the pick on a price bar."""
    lo, hi = _f(stats.get("min_usd")), _f(stats.get("max_usd"))
    if lo is None or hi is None or hi < lo:
        return None
    span = BAR_W - 2 * BAR_PAD

    def pos(v: float | None) -> float | None:
        if v is None:
            return None
        if hi == lo:
            return BAR_W / 2
        return round(BAR_PAD + min(max((v - lo) / (hi - lo), 0.0), 1.0) * span, 2)

    out: dict[str, float] = {"min": pos(lo) or 0.0, "max": pos(hi) or 0.0}
    for key, value in (
        ("p33", _f(stats.get("p33_usd"))),
        ("median", _f(stats.get("median_usd"))),
        ("p67", _f(stats.get("p67_usd"))),
        ("pick", _f(price)),
    ):
        p = pos(value)
        if p is not None:
            out[key] = p
    return out


def render_price_bar(stats: dict[str, Any], price: Any) -> str:
    pos = price_bar_positions(stats, price)
    if pos is None:
        return ""
    band = ""
    if "p33" in pos and "p67" in pos:
        band = (
            f'<rect class="pb-band" x="{pos["p33"]}" y="14" width="{round(pos["p67"] - pos["p33"], 2)}" '
            'height="12" rx="3"/>'
        )
    med = (
        f'<line class="pb-med" x1="{pos["median"]}" x2="{pos["median"]}" y1="10" y2="30"/>'
        if "median" in pos
        else ""
    )
    pick = (
        f'<path class="pb-pick" d="M{pos["pick"]} 13 l-6 -11 h12 z"/>'
        f'<line class="pb-pickline" x1="{pos["pick"]}" x2="{pos["pick"]}" y1="13" y2="26"/>'
        if "pick" in pos
        else ""
    )
    return (
        f'<svg class="pricebar" viewBox="0 0 {BAR_W:.0f} 54" role="img" aria-label="Price position">'
        f'<line class="pb-track" x1="{pos["min"]}" x2="{pos["max"]}" y1="20" y2="20"/>'
        f"{band}{med}{pick}"
        f'<text class="pb-t" x="{pos["min"]}" y="46" text-anchor="start">{esc(money(stats.get("min_usd")))}</text>'
        f'<text class="pb-t" x="{pos["max"]}" y="46" text-anchor="end">{esc(money(stats.get("max_usd")))}</text>'
        "</svg>"
    )


# ---- html pieces ------------------------------------------------------------------------------

CSS = """
:root{--bg:#faf7f2;--surface:#fffdf9;--surface2:#f3eee6;--ink:#2b2623;--muted:#675e56;--line:#e4dccf;
--accent:#b4532a;--accent-soft:#f7e4d9;--ok:#2d6a4d;--ok-bg:#e3f1e8;--warn:#8a5a0a;--warn-bg:#fbefd2;
--bad:#a3392d;--bad-bg:#f8e0dc;--info:#3c5a7a;--info-bg:#e1ebf4;--grey:#c9c1b6;--grey-ink:#4b443d}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#1b1815;--surface:#252120;
--surface2:#2e2926;--ink:#efe8df;--muted:#b0a69b;--line:#3b3530;--accent:#e58f63;--accent-soft:#3d2a20;
--ok:#7fcf9f;--ok-bg:#1f3328;--warn:#e6b85c;--warn-bg:#382c13;--bad:#f09a8f;--bad-bg:#40221e;
--info:#9cc0e6;--info-bg:#1f2d3b;--grey:#4a433c;--grey-ink:#d8d0c6}}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
overflow-wrap:anywhere}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px}
h1,h2,h3{font-family:ui-serif,Georgia,"Times New Roman",serif;font-weight:600;line-height:1.2;margin:0}
h1{font-size:1.9rem}h2{font-size:1.35rem;margin:40px 0 12px;padding-top:12px;border-top:1px solid var(--line)}
h3{font-size:1.1rem}
p{margin:6px 0}
a{color:var(--accent);text-underline-offset:2px}
a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.muted{color:var(--muted)}.small{font-size:.875rem}
.num{font-variant-numeric:tabular-nums}
.sub{color:var(--muted);margin-top:4px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:20px}
.stat{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.stat b{display:block;font-size:1.2rem;font-variant-numeric:tabular-nums}
.stat span{color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.04em}
.kv{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
.chip{display:inline-block;border-radius:999px;padding:1px 10px;font-size:.78rem;line-height:1.6;
border:1px solid var(--line);background:var(--surface2);color:var(--ink);white-space:normal}
.chip.ok{background:var(--ok-bg);color:var(--ok);border-color:transparent}
.chip.warn{background:var(--warn-bg);color:var(--warn);border-color:transparent}
.chip.bad{background:var(--bad-bg);color:var(--bad);border-color:transparent}
.chip.info{background:var(--info-bg);color:var(--info);border-color:transparent}
.chip.accent{background:var(--accent-soft);color:var(--accent);border-color:transparent}
.cols{display:grid;gap:20px;grid-template-columns:1fr}
@media (min-width:820px){.cols{grid-template-columns:minmax(0,5fr) minmax(0,6fr)}}
.photo{background:var(--surface2);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.photo img{display:block;width:100%;height:auto}
.placeholder{display:grid;place-items:center;min-height:200px;color:var(--muted);padding:16px;text-align:center}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px;margin:14px 0}
.card header{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between}
.card .row{display:grid;gap:14px;grid-template-columns:96px minmax(0,1fr);margin-top:12px}
@media (max-width:420px){.card .row{grid-template-columns:72px minmax(0,1fr)}}
.thumb{width:100%;aspect-ratio:1;border-radius:8px;background:var(--surface2);border:1px solid var(--line);
overflow:hidden;display:grid;place-items:center;color:var(--muted);font-size:.7rem}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:6px 14px;margin:10px 0}
.facts div span{display:block;color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.04em}
.facts div b{font-weight:600;font-variant-numeric:tabular-nums}
.checks{display:flex;flex-direction:column;gap:6px;margin-top:10px}
.checks .line{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.btn{display:inline-block;border:1px solid var(--accent);border-radius:8px;padding:4px 12px;
text-decoration:none;font-weight:600;font-size:.9rem}
.sub-block{margin-top:14px;padding-top:12px;border-top:1px dashed var(--line)}
.sub-block h4{margin:0 0 6px;font-size:.95rem}
.mini{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 12px;padding:6px 0;border-bottom:1px solid var(--line)}
.mini:last-child{border-bottom:0}
.empty{background:var(--warn-bg);color:var(--ink);border-radius:10px;padding:10px 12px;margin-top:12px}
details{margin-top:10px}summary{cursor:pointer;color:var(--muted)}
.scroll{overflow-x:auto;max-width:100%}
.scroll table{min-width:680px}
.scroll td:first-child,.scroll td:nth-child(3){white-space:nowrap}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:8px;border-radius:4px;background:var(--accent);min-width:2px}
.bar.cost{background:var(--info)}
.bartrack{min-width:90px}
tr.crit td:first-child{box-shadow:inset 3px 0 0 var(--accent)}
ul.notes{margin:6px 0;padding-left:20px}ul.notes li{margin:4px 0}
.plan{width:100%;height:auto;max-height:560px;background:var(--surface);border:1px solid var(--line);border-radius:12px}
.plan .room{fill:var(--surface2);stroke:var(--ink);stroke-width:3}
.plan .p-kept{fill:var(--grey);stroke:var(--grey-ink);stroke-width:1.5}
.plan .p-replaced{fill:none;stroke:var(--grey-ink);stroke-width:2;stroke-dasharray:7 5}
.plan .p-fixed{fill:none;stroke:var(--muted);stroke-width:1.5}
.plan .p-new{fill:var(--accent-soft);stroke:var(--accent);stroke-width:3}
.plan text{font:13px system-ui,sans-serif;fill:var(--ink)}
.plan .p-new-t{fill:var(--accent);font-weight:700}
.plan .p-fixed-t{fill:var(--muted);font-size:11px}
.plan .compass{fill:var(--muted);font-size:13px}
.plan .o-window{stroke:var(--info);stroke-width:7}.plan .o-door{stroke:var(--warn);stroke-width:7}
.plan .o-other{stroke:var(--muted);stroke-width:7}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin:8px 0;font-size:.82rem;color:var(--muted)}
.pricebar{width:100%;max-width:340px;height:auto;display:block;margin-top:6px}
.pricebar .pb-track{stroke:var(--grey);stroke-width:3;stroke-linecap:round}
.pricebar .pb-band{fill:var(--accent-soft);stroke:var(--accent);stroke-width:1}
.pricebar .pb-med{stroke:var(--muted);stroke-width:2}
.pricebar .pb-pick{fill:var(--accent)}.pricebar .pb-pickline{stroke:var(--accent);stroke-width:2}
.pricebar .pb-t{font:11px system-ui,sans-serif;fill:var(--muted)}
.foot{margin-top:40px;color:var(--muted);font-size:.875rem}
"""


def page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{esc(CSP)}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>\n"
    )


def stat(label: str, value: str) -> str:
    return f'<div class="stat"><span>{esc(label)}</span><b>{value}</b></div>'


def table(head: list[tuple[str, bool]], rows: list[list[str]]) -> str:
    th = "".join(
        f'<th class="r">{esc(h)}</th>' if right else f"<th>{esc(h)}</th>" for h, right in head
    )
    trs = "".join(
        "<tr>"
        + "".join(
            f'<td class="r">{c}</td>' if head[i][1] else f"<td>{c}</td>" for i, c in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return (
        f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'
    )


def shipping_text(c: dict[str, Any]) -> str:
    status = _s(c.get("shipping_status")) or (
        "known" if _f(c.get("shipping")) is not None else "unknown"
    )
    amount = _f(c.get("shipping"))
    if status == "free":
        return "free"
    if status == "known" and amount is not None:
        return money(amount)
    return "unknown"


def size_text(c: dict[str, Any]) -> str:
    d = _d(c.get("dimensions_m"))
    parts = []
    for key, label in (("width_m", "W"), ("depth_m", "D"), ("height_m", "H"), ("length_m", "L")):
        v = _f(d.get(key))
        if v is not None:
            parts.append(f"{label} {v:.2f} m")
    if not parts:
        return "not found"
    src, conf = _s(c.get("size_source")), _s(c.get("size_confidence"))
    tail = f" ({src}, {conf} confidence)" if src and src != "none" else ""
    return " x ".join(parts) + tail


def thumb(c: dict[str, Any]) -> str:
    url = safe_url(c.get("thumbnail_url"))
    inner = (
        f'<img src="{esc(url)}" alt="" loading="lazy" referrerpolicy="no-referrer" '
        'width="96" height="96">'
        if url and url.lower().startswith("https://")
        else "no image"
    )
    return f'<div class="thumb">{inner}</div>'


def status_chip(status: Any) -> str:
    tone = {"pass": "ok", "fail": "bad", "unknown": "warn"}.get(_s(status), "neutral")
    return chip(_s(status) or "unknown", tone)


def mini_candidate(c: dict[str, Any], link_label: str) -> str:
    total = _f(c.get("total"))
    return (
        '<div class="mini">'
        f'<div><b>{esc(c.get("name"))}</b><br><span class="muted small">{esc(c.get("retailer"))}'
        f"{' - ' + esc(c.get('reason')) if c.get('reason') else ''}</span></div>"
        f'<div class="num">{esc(money(c.get("price")))}'
        f"{' (total ' + esc(money(total)) + ')' if total is not None else ''}</div>"
        f'<div class="small">{link(c.get("url"), link_label)}</div><div></div></div>'
    )


GOOGLE_LABEL = "opens Google Shopping, retailer link on request"


def alt_candidate(c: dict[str, Any]) -> str:
    label = esc(GOOGLE_LABEL) if _s(c.get("url_kind")) == "google_panel" else "view at retailer"
    return mini_candidate(c, label)


def slot_views(r: dict[str, Any]) -> list[dict[str, Any]]:
    """One normalized view per slot: from the proposal when it exists, else from retrieval."""
    retrieval = {_s(_d(_d(s).get("slot")).get("category")): _d(s) for s in _l(r.get("retrieval"))}
    items = proposal_items(r)
    views: list[dict[str, Any]] = []
    if items:
        for it in items:
            sr = retrieval.get(_s(it.get("slot")), {})
            views.append({"item": it, "slot": _d(sr.get("slot")), "sr": sr})
    else:
        for sr in retrieval.values():
            views.append({"item": None, "slot": _d(sr.get("slot")), "sr": sr})
    return views


def render_slot(view: dict[str, Any], r: dict[str, Any]) -> str:
    item: dict[str, Any] | None = view["item"]
    sr, slot = _d(view["sr"]), _d(view["slot"])
    name = _s(_d(item).get("slot") if item else slot.get("category"))
    head = [f"<h3>{esc(name)}</h3>"]
    chips = []
    if slot.get("source"):
        chips.append(chip(slot.get("source"), "info"))
    tier = _s(slot.get("tier"))
    if tier:
        chips.append(chip(f"{tier} tier", "neutral"))
    rep = _d(_d(item).get("replaces")) if item else {}
    if rep:
        chips.append(chip(f"replaces {rep.get('id')} {rep.get('category')}", "accent"))
    out = [
        f'<article class="card"><header>{"".join(head)}<div class="kv">{"".join(chips)}</div></header>'
    ]
    if slot.get("reason"):
        out.append(f'<p class="muted">{esc(slot.get("reason"))}</p>')
    pick = _d(_d(item).get("pick")) if item else {}
    stats = _d(_d(item).get("price_stats")) if item else {}
    stats = stats or _d(_d(sr.get("summary")).get("price_stats"))
    if pick and item:
        out.append(render_pick(pick, item, stats))
    elif item:
        note = _s(item.get("note")) or _s(sr.get("note")) or "No pick for this slot."
        out.append(f'<div class="empty"><b>No pick for this slot.</b> {esc(note)}</div>')
        if stats:
            out.append(render_stats_line(stats))
    else:
        cands = [_d(c) for c in _l(sr.get("candidates"))]
        out.append('<div class="sub-block"><h4>Finalists (no proposal was made)</h4>')
        out.append(
            "".join(mini_candidate(c, "view at retailer") for c in cands)
            or '<p class="muted">none</p>'
        )
        out.append("</div>")
        if stats:
            out.append(render_stats_line(stats))
        if sr.get("note"):
            out.append(f'<p class="muted small">{esc(sr.get("note"))}</p>')
    if item:
        extra = [_d(c) for c in _l(item.get("alternates"))]
        if extra:
            out.append('<div class="sub-block"><h4>Other verified options</h4>')
            out.append("".join(mini_candidate(c, "view at retailer") for c in extra) + "</div>")
        unverified = [_d(c) for c in _l(item.get("unverified_alternates"))]
        if unverified:
            out.append(
                f"<details><summary>Unverified alternates ({len(unverified)})</summary>"
                + "".join(alt_candidate(c) for c in unverified)
                + "</details>"
            )
        over = [_d(c) for c in _l(item.get("over_budget_options"))]
        if over:
            out.append(
                '<div class="sub-block"><h4>Over-budget options</h4>'
                '<p class="muted small">These are above the per-item limit; shown so you can see '
                "what exists.</p>" + "".join(alt_candidate(c) for c in over) + "</div>"
            )
    out.append("</article>")
    return "".join(out)


def render_stats_line(stats: dict[str, Any]) -> str:
    bits = [
        f"{k.replace('_usd', '')} {money(stats.get(k))}"
        for k in ("min_usd", "p33_usd", "median_usd", "p67_usd", "max_usd")
        if _f(stats.get(k)) is not None
    ]
    counts = f"{stats.get('priced', '?')} priced of {stats.get('considered', '?')} considered"
    return f'<p class="muted small num">Search prices: {esc(", ".join(bits) or "none")} ({esc(counts)})</p>'


def render_pick(pick: dict[str, Any], item: dict[str, Any], stats: dict[str, Any]) -> str:
    ship = shipping_text(pick)
    ship_status = "free" if ship == "free" else "unknown" if ship == "unknown" else "known"
    ship_tone = "ok" if ship == "free" else "warn" if ship == "unknown" else "neutral"
    ship_chip = "" if ship_status == "known" else " " + chip(ship_status, ship_tone)
    rating = _f(pick.get("rating"))
    rating_text = (
        f"{rating:.1f}" + (f" ({pick.get('rating_count')})" if pick.get("rating_count") else "")
        if rating is not None
        else "-"
    )
    facts = (
        f"<div><span>Price</span><b>{esc(money(pick.get('price')))}</b></div>"
        f"<div><span>Shipping</span><b>{esc(ship)}</b>{ship_chip}</div>"
        f"<div><span>Total</span><b>{esc(money(pick.get('total')))}</b></div>"
        f"<div><span>Seller</span><b>{esc(pick.get('retailer') or '-')}</b></div>"
        f"<div><span>Size</span><b>{esc(size_text(pick))}</b></div>"
        f"<div><span>Rating</span><b>{esc(rating_text)}</b></div>"
    )
    checks: list[str] = []
    if item.get("fit_status"):
        checks.append(
            f'<div class="line">{chip("fit", "info")} {status_chip(item.get("fit_status"))}'
            f'<span class="small">{esc(item.get("fit_note"))}</span></div>'
        )
    if item.get("style_match"):
        basis = _s(item.get("style_basis"))
        basis_chip = chip(f"{basis} based", "info") if basis in ("image", "text") else ""
        tone = {"good": "ok", "ok": "ok", "poor": "bad"}.get(_s(item.get("style_match")), "neutral")
        checks.append(
            f'<div class="line">{chip("style", "info")} {chip(item.get("style_match"), tone)} '
            f'{basis_chip}<span class="small">{esc(item.get("style_note"))}</span></div>'
        )
    clash = _s(item.get("style_clash"))
    if clash and clash != "none":
        with_ = _s(item.get("style_clash_with")) or "a kept item"
        checks.append(
            f'<div class="line">{chip(clash + " clash", "bad" if clash == "strong" else "warn")}'
            f'<span class="small">with {esc(with_)}</span></div>'
        )
    if item.get("replace_size_note"):
        checks.append(
            f'<div class="line">{chip("size change", "accent")}<span class="small">{esc(item.get("replace_size_note"))}</span></div>'
        )
    for w in _l(item.get("warnings")):
        checks.append(
            f'<div class="line">{chip("warning", "warn")}<span class="small">{esc(w)}</span></div>'
        )
    if pick.get("size_note"):
        checks.append(
            f'<div class="line"><span class="muted small">Size note: {esc(pick.get("size_note"))}</span></div>'
        )
    bar = render_price_bar(stats, pick.get("price")) if stats else ""
    bar_block = (
        '<div class="sub-block"><h4>Price position</h4>'
        f'{bar}<p class="muted small">Band = p33 to p67 of the search prices, line = median, '
        "triangle = this pick.</p></div>"
        if bar
        else ""
    )
    button = link(pick.get("url"), "view at retailer").replace("<a ", '<a class="btn" ', 1)
    return (
        f'<div class="row">{thumb(pick)}<div>'
        f"<p><b>{esc(pick.get('name'))}</b></p>"
        f"<p>{button}</p></div></div>"
        f'<div class="facts">{facts}</div>'
        f"<p>{esc(item.get('rationale'))}</p>"
        f'<div class="checks">{"".join(checks)}</div>{bar_block}'
    )


def status_for(r: dict[str, Any], fid: str) -> str:
    decision, source = decisions(r).get(fid, ("", ""))
    if decision == "replace":
        return (
            chip("replace requested", "accent")
            if source == "requested"
            else chip("suggested replacement", "warn")
            if source == "suggested"
            else chip(f"replace ({source})" if source else "replace", "accent")
        )
    if decision == "keep":
        return chip("keep by default", "neutral") if source == "default" else chip("keep", "ok")
    return chip("-", "neutral") if not decision else chip(f"{decision} ({source})", "neutral")


def render_room(r: dict[str, Any], image_html: str) -> str:
    p = _d(r.get("perception"))
    if not p:
        return f'<h2>Room</h2><div class="cols"><div class="photo">{image_html}</div><p class="muted">No perception data in this run.</p></div>'
    dims = _d(p.get("dimensions"))
    conf = _f(dims.get("confidence"))
    left = f'<div class="photo">{image_html}</div>'
    facts = [
        f"<p><b>{esc(p.get('room_type'))}</b>"
        + (
            f" - {_f(dims.get('width_m')) or 0:.1f} x {_f(dims.get('length_m')) or 0:.1f} m, ceiling "
            f"{_f(dims.get('ceiling_height_m')) or 0:.1f} m"
            if dims
            else ""
        )
        + (
            f" {chip(f'confidence {conf:.2f}', 'ok' if conf >= 0.7 else 'warn')}"
            if conf is not None
            else ""
        )
        + "</p>"
    ]
    if dims.get("derivation"):
        facts.append(
            f'<p class="muted small">How the size was worked out: {esc(dims.get("derivation"))}</p>'
        )
    openings = [_d(o) for o in _l(p.get("openings"))]
    if openings:
        items_html = []
        for o in openings:
            ow = _f(o.get("width_m"))
            items_html.append(
                f"<li>{esc(o.get('kind'))} on the {esc(o.get('wall'))} wall"
                + (f", {ow:.1f} m wide" if ow else "")
                + (f" - {esc(o.get('note'))}" if o.get("note") else "")
                + "</li>"
            )
        facts.append('<p><b>Openings</b></p><ul class="notes">' + "".join(items_html) + "</ul>")
    fixed = [_d(o) for o in _l(p.get("fixed_items"))]
    if fixed:
        facts.append(
            '<p><b>Fixed items</b></p><ul class="notes">'
            + "".join(
                f"<li>{esc(o.get('category'))}{' - ' + esc(o.get('note')) if o.get('note') else ''}</li>"
                for o in fixed
            )
            + "</ul>"
        )
    desc = [_s(x) for x in _l(p.get("style_descriptors"))]
    if desc:
        facts.append(f'<p><b>Style</b></p><div class="kv">{"".join(chip(x) for x in desc)}</div>')
    unc = [_s(x) for x in _l(p.get("uncertainties"))]
    if unc:
        facts.append(
            "<details><summary>Uncertainties ("
            + str(len(unc))
            + ')</summary><ul class="notes">'
            + "".join(f"<li>{esc(x)}</li>" for x in unc)
            + "</ul></details>"
        )
    furniture = [_d(f) for f in _l(p.get("furniture"))]
    ids = {_s(f.get("id")) for f in furniture}
    rows = []
    for f in furniture:
        size = _d(f.get("size"))
        match = _s(f.get("matches_id"))
        match_html = (
            esc(f"matches {match}") if match and match in ids else (esc(match) if match else "-")
        )
        rows.append(
            [
                esc(f.get("id")),
                esc(f.get("category")),
                f"{_f(size.get('width_m')) or 0:.1f} x {_f(size.get('depth_m')) or 0:.1f} m",
                status_for(r, _s(f.get("id"))),
                esc(f.get("keepable")),
                match_html,
                esc(f.get("note")),
            ]
        )
    furn = (
        '<h3 style="margin-top:20px">Detected furniture</h3>'
        + table(
            [
                ("Id", False),
                ("Category", False),
                ("Size", False),
                ("Status", False),
                ("Keepable", False),
                ("Set", False),
                ("Note", False),
            ],
            rows,
        )
        if rows
        else ""
    )
    return f'<h2>Room</h2><div class="cols">{left}<div>{"".join(facts)}</div></div>{furn}'


def render_plan_section(r: dict[str, Any]) -> str:
    shapes = plan_shapes(r)
    if shapes is None:
        return ""
    legend = (
        '<div class="legend"><span>grey = stays</span><span>dashed = being replaced</span>'
        "<span>orange = new pick at the old item's position</span><span>outline = fixed item</span>"
        "<span>blue/amber bar = window/door</span></div>"
    )
    side = ""
    if shapes.unplaced:
        side = (
            '<p class="small"><b>New picks with no position</b> (not drawn): '
            + esc(", ".join(shapes.unplaced))
            + "</p>"
        )
    return (
        "<h2>Rough floor plan</h2>"
        f"{render_plan(shapes)}{legend}{side}"
        '<p class="muted small">Approximate, drawn from what the model saw in the photo: x runs west to '
        "east across the room width, y north to south along its length. Positions and sizes are "
        "estimates, not measurements.</p>"
    )


def render_notes(r: dict[str, Any]) -> str:
    items: list[str] = []
    pref = _d(r.get("preferences"))
    for w in _l(pref.get("set_warnings")):
        items.append(f"{chip('set', 'warn')} {esc(_d(w).get('text'))}")
    msg = _s(_d(r.get("item_count_report")).get("message"))
    if msg:
        items.append(f"{chip('item count', 'info')} {esc(msg)}")
    plan = _d(r.get("slot_plan"))
    for s in _l(plan.get("skipped")):
        items.append(f"{chip('skipped', 'neutral')} {esc(s)}")
    for s in _l(plan.get("dropped_by_cap")):
        items.append(f"{chip('dropped by cap', 'neutral')} {esc(s)}")
    for s in _l(plan.get("notes")):
        items.append(f"{chip('plan', 'neutral')} {esc(s)}")
    for sr in _l(r.get("retrieval")):
        summ = _d(_d(sr).get("summary"))
        for key, label in (("lookup_issues", "lookup issue"), ("lookup_errors", "lookup error")):
            for x in _l(summ.get(key)):
                items.append(f"{chip(label, 'warn')} {esc(_d(summ).get('slot'))}: {esc(x)}")
    totals = _d(_d(r.get("proposal")).get("totals"))
    if totals.get("shipping_note"):
        items.append(f"{chip('shipping', 'warn')} {esc(totals.get('shipping_note'))}")
    conc = _d(r.get("seller_concentration"))
    if conc.get("picks"):
        share = _f(conc.get("largest_share")) or 0.0
        items.append(
            f"{chip('sellers', 'info')} {esc(conc.get('largest_seller'))}: "
            f"{esc(_d(conc.get('picks_by_seller')).get(_s(conc.get('largest_seller')), '?'))} of "
            f"{esc(conc.get('picks'))} picks ({share:.0%})"
        )
    for w in _l(r.get("warnings")):
        items.append(f"{chip('warning', 'warn')} {esc(w)}")
    for d in _l(r.get("proposal_dropped")):
        d = _d(d)
        items.append(f"{chip('dropped', 'neutral')} {esc(d.get('slot'))}: {esc(d.get('reason'))}")
    if _l(r.get("unpriced_models")):
        items.append(
            f"{chip('pricing', 'warn')} no price table for: {esc(', '.join(map(_s, _l(r.get('unpriced_models')))))}"
        )
    if not items:
        return '<h2>Notes</h2><p class="muted">Nothing to flag.</p>'
    return '<h2>Notes</h2><ul class="notes">' + "".join(f"<li>{i}</li>" for i in items) + "</ul>"


def render_totals(r: dict[str, Any]) -> str:
    t = _d(_d(r.get("proposal")).get("totals"))
    if not t:
        return ""
    count, known = t.get("item_count"), t.get("shipping_known_count")
    cells = [
        stat("Items", money(t.get("items_total"))),
        stat("Shipping", f"{money(t.get('shipping_total'))}"),
        stat("Total", money(t.get("total"))),
    ]
    if known is not None and count is not None:
        cells.insert(2, stat("Shipping known", f"{esc(known)} of {esc(count)} items"))
    if _f(t.get("budget")) is not None:
        cells.append(stat("Budget", money(t.get("budget"))))
        cells.append(stat("Remaining", money(t.get("remaining"))))
    return f'<h2>Totals</h2><div class="stats">{"".join(cells)}</div>'


def render_cost(r: dict[str, Any]) -> str:
    rows = [_d(x) for x in _l(r.get("rows"))]
    if not rows:
        return ""
    crit = critical_rows(rows)
    max_s = max((_f(x.get("seconds")) or 0.0 for x in rows), default=0.0) or 1.0
    max_c = max((_f(x.get("cost_usd")) or 0.0 for x in rows), default=0.0) or 1.0
    body = []
    for i, x in enumerate(rows):
        sec, cost = _f(x.get("seconds")) or 0.0, _f(x.get("cost_usd")) or 0.0
        est = chip("estimated", "warn") if x.get("cost_estimated") else ""
        mark = chip("critical path", "accent") if i in crit else ""
        body.append(
            f'<tr class="{"crit" if i in crit else ""}"><td>{esc(x.get("stage"))} {mark}</td>'
            f'<td class="muted">{esc(x.get("model"))}</td>'
            f'<td class="r">{esc(secs(sec))}</td>'
            f'<td class="bartrack"><div class="bar" style="width:{max(sec / max_s * 100, 1):.1f}%"></div></td>'
            f'<td class="r">{esc(money(cost) if cost >= 0.01 else f"${cost:.4f}")} {est}</td>'
            f'<td class="bartrack"><div class="bar cost" style="width:{max(cost / max_c * 100, 1):.1f}%"></div></td></tr>'
        )
    head = "".join(
        f'<th class="r">{h}</th>' if h in ("Seconds", "Cost") else f"<th>{h}</th>"
        for h in ("Stage", "Model", "Seconds", "", "Cost", "")
    )
    tt = run_totals(r)
    foot = (
        f'<p class="muted small num">Anthropic {esc(money(tt["anthropic"]))}; provider estimate '
        f"{esc(money(tt['provider']))} (estimated from per-call settings, not billed amounts); "
        f"critical path {esc(secs(tt['critical']))}; wall clock {esc(secs(tt['wall']))}. Stage seconds "
        "overlap (slots run in parallel), so their sum is longer than the critical path.</p>"
    )
    return (
        "<h2>Cost and time</h2>"
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>{foot}'
    )


def render_header(r: dict[str, Any], has_trace: bool) -> str:
    mode = run_mode(r)
    tt = run_totals(r)
    budget = _f(r.get("budget_usd"))
    cells = [
        stat("Mode", esc(mode + (f" ({r.get('llm_mode')})" if r.get("llm_mode") else ""))),
        stat(
            "Budget" if mode == "budget" else "Price tier",
            esc(money(budget) if mode == "budget" else (r.get("price_tier") or "mid")),
        ),
        stat("Items", f"{found_count(r)} of {requested_count(r)}"),
        stat("Anthropic cost", esc(money(tt["anthropic"]))),
        stat("Provider (estimated)", esc(money(tt["provider"]))),
        stat("Critical path", esc(secs(tt["critical"]))),
        stat("Wall clock", esc(secs(tt["wall"]))),
    ]
    pv = _d(r.get("prompt_versions"))
    models = _d(r.get("models"))
    chips = [chip(f"{k} prompt v{v}") for k, v in sorted(pv.items())] + [
        chip(f"{k}: {v}", "info") for k, v in sorted(models.items())
    ]
    extra = [chip(f"retrieval: {r.get('retrieval_mode')}")] if r.get("retrieval_mode") else []
    if r.get("search_provider"):
        extra.append(chip(f"search: {r.get('search_provider')}"))
    links = '<a href="result.json">result.json</a>' + (
        ' - <a href="trace.jsonl">trace.jsonl</a>' if has_trace else ""
    )
    return (
        f"<h1>Run {esc(r.get('run_id'))}</h1>"
        f'<p class="sub">{esc(r.get("started_at"))} - style: <b>{esc(r.get("style") or "(none)")}</b> - '
        f"photo: {esc(r.get('photo_name'))}</p>"
        f'<div class="stats">{"".join(cells)}</div><div class="kv">{"".join(extra + chips)}</div>'
        f'<p class="small muted" style="margin-top:12px">Developer report, not the product UI. {links}</p>'
    )


def room_image_html(run_dir: Path | None, result: dict[str, Any], photo_dir: Path) -> str:
    data = None
    if run_dir is not None:
        saved = run_dir / ROOM_IMAGE_NAME
        if saved.is_file():
            data = saved.read_bytes()
        else:
            name = Path(_s(result.get("photo_name"))).name
            fallback = photo_dir / name
            if name and fallback.is_file():
                try:
                    from app.images import prepare_image  # strips EXIF, downscales

                    data = base64.b64decode(prepare_image(fallback, max_edge=900).data_b64)
                except Exception:
                    data = None
    if data is None:
        return '<div class="placeholder">Room photo not saved for this run</div>'
    encoded = base64.b64encode(data).decode("ascii")
    return f'<img alt="The room photo used for this run" src="data:image/jpeg;base64,{encoded}">'


def build_report(
    result: dict[str, Any], *, image_html: str | None = None, has_trace: bool = False
) -> str:
    r = _d(result)
    image = image_html or '<div class="placeholder">Room photo not saved for this run</div>'
    body = [render_header(r, has_trace), render_room(r, image), render_plan_section(r)]
    views = slot_views(r)
    if r.get("perception_only"):
        body.append(
            '<h2>Proposal</h2><p class="muted">This was a perception-only run: no retrieval or proposal.</p>'
        )
    elif views:
        title = "Proposal" if proposal_items(r) else "Retrieval (no proposal)"
        body.append(f"<h2>{title}</h2>" + "".join(render_slot(v, r) for v in views))
    else:
        body.append(
            '<h2>Proposal</h2><p class="muted">No retrieval or proposal data in this run.</p>'
        )
    body += [render_notes(r), render_totals(r), render_cost(r)]
    body.append(
        '<p class="foot">Product thumbnails load from their hosts when you open this file.</p>'
    )
    return page(f"Run {_s(r.get('run_id')) or 'report'}", "".join(body))


# ---- files ------------------------------------------------------------------------------------


def load_result(run_dir: Path) -> dict[str, Any]:
    return _d(json.loads((run_dir / "result.json").read_text()))


def write_report(run_dir: Path, photo_dir: Path = PHOTO_DIR) -> Path:
    result = load_result(run_dir)
    html = build_report(
        result,
        image_html=room_image_html(run_dir, result, photo_dir),
        has_trace=(run_dir / "trace.jsonl").is_file(),
    )
    path = run_dir / REPORT_NAME
    path.write_text(html)
    return path


def run_dirs(out_root: Path) -> list[Path]:
    return sorted(
        (p for p in out_root.iterdir() if p.is_dir() and (p / "result.json").is_file()),
        key=lambda p: p.name,
    )


def build_index(rows: list[dict[str, Any]]) -> str:
    head = [
        ("Run", False),
        ("Date", False),
        ("Mode", False),
        ("Style", False),
        ("Tier / budget", False),
        ("Items", False),
        ("Anthropic", True),
        ("Provider (est.)", True),
        ("Critical path", True),
        ("Summary", False),
    ]
    body = []
    for x in rows:
        r = _d(x.get("result"))
        run_id = _s(x.get("run_id"))
        if not r:
            body.append([esc(run_id)] + ["-"] * 8 + [chip("unreadable result.json", "bad")])
            continue
        tt = run_totals(r)
        mode = run_mode(r)
        budget = _f(r.get("budget_usd"))
        label = (
            f'<a href="{esc(quote(run_id))}/{REPORT_NAME}">{esc(run_id)}</a>'
            if x.get("has_report")
            else esc(run_id)
        )
        body.append(
            [
                label,
                esc(_s(r.get("started_at"))[:19].replace("T", " ") or "-"),
                esc(f"{mode} ({r.get('llm_mode')})" if r.get("llm_mode") else mode),
                esc(r.get("style") or "-"),
                esc(money(budget) if mode == "budget" else (r.get("price_tier") or "mid")),
                esc(f"{found_count(r)} of {requested_count(r)}")
                if not r.get("perception_only")
                else "-",
                esc(money(tt["anthropic"])),
                esc(money(tt["provider"])),
                esc(secs(tt["critical"])),
                esc(one_line_summary(r)),
            ]
        )
    intro = (
        f'<h1>Spike runs</h1><p class="sub">{len(rows)} runs, newest first. Developer index, not the '
        "product UI.</p>"
    )
    return page(
        "Spike runs", intro + (table(head, body) if body else '<p class="muted">No runs yet.</p>')
    )


def write_index(out_root: Path) -> Path:
    rows = []
    for d in reversed(run_dirs(out_root)):
        try:
            result: dict[str, Any] = load_result(d)
        except (OSError, ValueError):
            result = {}
        rows.append({"run_id": d.name, "result": result, "has_report": (d / REPORT_NAME).is_file()})
    path = out_root / INDEX_NAME
    path.write_text(build_index(rows))
    return path
