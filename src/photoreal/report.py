"""index.html for a render run: the room photo beside each render, with cost, time, settings, the
product references used and the instruction. Static HTML and CSS in the run report's style: no
script, every string escaped, images as data: URIs, nothing loaded from outside."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

from PIL import Image

from app.report import chip, esc, money, page, stat

EXTRA_CSS = """
.pair{display:grid;gap:14px;grid-template-columns:1fr}
@media (min-width:820px){.pair{grid-template-columns:1fr 1fr}}
.pair figure{margin:0;background:var(--surface);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.pair img{display:block;width:100%;height:auto}
.pair figcaption{padding:8px 12px;color:var(--muted);font-size:.85rem}
.refs{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.refs figure{margin:0;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:8px}
.refs img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:6px;background:var(--surface2)}
.refs figcaption{font-size:.8rem;margin-top:4px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--surface2);border:1px solid var(--line);
border-radius:8px;padding:10px;font:.8rem/1.45 ui-monospace,Menlo,monospace}
"""
PREVIEW_EDGE = 900


def _data_uri(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        with Image.open(path) as im:
            img = im.convert("RGB")
        img.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
    except Exception:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _img(out_dir: Path, name: str, alt: str) -> str:
    uri = _data_uri(out_dir / name)
    if uri is None:
        return '<div class="placeholder">image not available</div>'
    return f'<img src="{uri}" alt="{esc(alt)}">'


def _fig(out_dir: Path, name: str, caption: str, alt: str) -> str:
    return f"<figure>{_img(out_dir, name, alt)}<figcaption>{esc(caption)}</figcaption></figure>"


def build_render_page(r: dict[str, Any], out_dir: Path) -> str:
    totals = r.get("totals") or {}
    cells = [
        stat("Approach", esc(f"{r.get('approach')} ({'live' if r.get('live') else 'mock'})")),
        stat("Model", esc(r.get("model"))),
        stat("Mode", esc(r.get("mode"))),
        stat("Quality", esc(r.get("quality"))),
        stat("Render cost (estimated)", esc(money(totals.get("render_estimated_usd")))),
        stat("Anthropic (locate, judge)", esc(money(totals.get("anthropic_usd")))),
        stat("Running total", esc(money(totals.get("running_total_usd")))),
    ]
    body = [
        f"<h1>Photo-real spike {esc(r.get('run_id'))}</h1>",
        f'<p class="sub">from run {esc(r.get("source_run"))} - style: <b>{esc(r.get("style"))}</b> - '
        f"size {esc(r.get('size'))} - mask {esc(r.get('mask'))} - edit prompt v{esc(r.get('prompt_version'))}</p>",
        f'<div class="stats">{"".join(cells)}</div>',
        '<p class="muted small" style="margin-top:12px">An experiment to compare approaches by eye. '
        "Renders are approximate: the real product photos are shown beside them. Cost figures are "
        "estimates from assumed prices, not billed amounts. Scores and signals below are not a quality gate.</p>",
    ]
    if r.get("error"):
        body.append(f'<div class="empty"><b>The run stopped early.</b> {esc(r.get("error"))}</div>')
    for n in r.get("notes") or []:
        body.append(f"<p>{chip('note', 'info')} {esc(n)}</p>")
    for u in r.get("unverified") or []:
        body.append(f'<p>{chip("unverified", "warn")} <span class="small">{esc(u)}</span></p>')

    body.append("<h2>Before and after</h2>")
    final = "render.png" if (out_dir / "render.png").is_file() else ""
    body.append(
        '<div class="pair">'
        + _fig(out_dir, "original.jpg", "Original room photo (as sent)", "Original room")
        + (
            _fig(out_dir, final, "Final render (approximate)", "Final render")
            if final
            else '<div class="placeholder">no render produced</div>'
        )
        + "</div>"
    )
    steps = r.get("steps") or []
    if len(steps) > 1:
        body.append('<h2>Steps (each edits the previous result)</h2><div class="pair">')
        for s in steps:
            cap = f"Step {s.get('step')}: {', '.join(map(str, s.get('items') or []))}"
            body.append(_fig(out_dir, str(s.get("image")), cap, cap))
        body.append("</div>")

    items = [i for i in (r.get("items") or []) if isinstance(i, dict)]
    body.append('<h2>Products used</h2><div class="refs">')
    for it in items:
        ref = str(it.get("reference_image") or "")
        place = it.get("placement") or {}
        cap = (
            f"{it.get('slot')}: {it.get('name')} - {it.get('seller')} {money(it.get('price'))}"
            f" - {it.get('size_text')} - placed: {place.get('text')} ({place.get('source')})"
        )
        body.append(
            f"<figure>{_img(out_dir, ref, str(it.get('slot')))}<figcaption>{esc(cap)}</figcaption></figure>"
        )
    body.append("</div>")

    judge = r.get("judge")
    if isinstance(judge, dict):
        rows = "".join(
            f"<li><b>{esc(k)}</b>: {esc(v.get('look_alike'))}/5 - {esc(v.get('notes'))}</li>"
            for k, v in (judge.get("items") or {}).items()
        )
        body.append(
            f'<h2>Judge (signal only)</h2><ul class="notes">{rows}</ul>'
            f"<p>Rest of the room changed: <b>{'yes' if judge.get('rest_of_room_changed') else 'no'}</b>"
            f" - {esc(judge.get('room_notes'))}</p>"
        )
    body.append("<h2>Calls</h2>")
    for c in r.get("calls") or []:
        if not isinstance(c, dict):
            continue
        head = (
            f"{c.get('kind')} {c.get('step', '')} - {c.get('provider')}/{c.get('model')} - "
            f"{c.get('status', 'ok')} - est {money(c.get('est_usd'))}"
            + (f" - {c['seconds']:.1f} s" if isinstance(c.get("seconds"), int | float) else "")
        )
        extra = []
        if c.get("outside_change") is not None:
            extra.append(
                f'<p class="small">{chip("signal", "info")} mean change outside the edit regions: '
                f"{esc(c.get('outside_change'))} (0 = none, 1 = everything)</p>"
            )
        if c.get("reported_usage"):
            extra.append(
                f'<p class="small">Reported usage: {esc(json.dumps(c.get("reported_usage")))}</p>'
            )
        if c.get("request_id"):
            extra.append(f'<p class="small">Request id: {esc(c.get("request_id"))}</p>')
        if c.get("error"):
            extra.append(f'<p class="small">{chip("error", "bad")} {esc(c.get("error"))}</p>')
        if c.get("instruction"):
            extra.append(
                f"<details><summary>Instruction</summary><pre>{esc(c.get('instruction'))}</pre></details>"
            )
        if c.get("request"):
            extra.append(
                f"<details><summary>Request (no pixels, no key)</summary><pre>{esc(json.dumps(c.get('request'), indent=2))}</pre></details>"
            )
        body.append(f'<article class="card"><h3>{esc(head)}</h3>{"".join(extra)}</article>')
    body.append('<p class="foot"><a href="result.json">result.json</a></p>')
    return page(f"Photo-real spike {r.get('run_id')}", "".join(body), EXTRA_CSS)


def write_render_report(out_dir: Path, result: dict[str, Any]) -> Path:
    path = out_dir / "index.html"
    path.write_text(build_render_page(result, out_dir))
    return path
