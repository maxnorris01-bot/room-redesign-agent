"""Colours of the shown results (the `attrs.color` of the filters, stage F1).

The order of preference for each shown result: the colour a photo tag already gave it (the tagging
pipeline's photo step), else one vision call over the shown results that still have none (batched,
concurrent, at most `COLOR_BUDGET_S` seconds in all, thumbnails from the shared `ThumbCache`), else
the colour words of its title. A call that fails, times out or goes over the budget never fails the
search: the result keeps its title colours. The call is made with the PRODUCT's colours in mind (the
prompt says so: a styled room scene gives the room's colours no vote).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from app.config import BudgetExceededError, Config
from app.images import PreparedImage
from app.llm import Budget, complete
from app.prompts import load_prompt
from app.shop.facets import MAX_COLORS, MULTICOLOR, title_colors
from app.shop.phototags import ThumbCache

COLOR_BUDGET_S = 6.0  # the whole step, however many calls
COLOR_BATCH = 12  # results per call (one call is several images)
COLOR_WORKERS = 4
COLOR_MAX_TOKENS = 600

# the tagging vocabulary's colour ids -> keys of clarify_vocab.json's `colors` (None: no single key)
PHOTO_TAG_COLORS: dict[str, str | None] = {
    "white": "white", "cream": "cream", "beige": "beige", "tan": "tan", "brown": "brown",
    "black": "black", "gray": "gray", "blue": "blue", "navy_dark_blue": "navy", "green": "green",
    "red": "red", "rust_terracotta": "terracotta", "yellow_mustard": "yellow", "pink": "pink",
    "purple": "purple", "orange": "orange", "multicolor_pattern": MULTICOLOR, "metallic": None,
}  # fmt: skip

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "colors": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["index", "colors"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


@dataclass
class ColorReport:
    from_tags: int = 0  # shown results whose colour a photo tag already gave
    wanted: int = 0  # shown results that needed the vision call
    with_image: int = 0
    calls: int = 0
    failed_calls: int = 0
    from_vision: int = 0
    from_title: int = 0  # left to the title's colour words (possibly none)
    seconds: float = 0.0
    usd: float = 0.0
    reason: str = ""
    colors: dict[str, list[str]] = field(default_factory=dict)  # result id -> colour keys

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_photo_tags": self.from_tags, "vision_wanted": self.wanted,
            "with_image": self.with_image, "calls": self.calls,
            "failed_calls": self.failed_calls, "from_vision": self.from_vision,
            "from_title": self.from_title, "seconds": round(self.seconds, 2),
            "budget_s": COLOR_BUDGET_S, "usd": round(self.usd, 6), "reason": self.reason,
        }  # fmt: skip


def allowed_colors() -> list[str]:
    from app.shop.clarify_visual import vocabulary

    keys = ["gray" if k == "grey" else k for k in vocabulary()["colors"]]
    return [*dict.fromkeys(keys), MULTICOLOR]


def clean_colors(values: Any, allowed: Sequence[str]) -> list[str]:
    """The model's colour list as valid keys: lowercase, grey as gray, unknown names dropped,
    unique, "multicolor" first, at most `MAX_COLORS` (multicolor plus two)."""
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for v in values:
        if not isinstance(v, str):
            continue
        key = v.strip().lower().replace("grey", "gray")
        if key in allowed and key not in out:
            out.append(key)
    if MULTICOLOR in out:
        out = [MULTICOLOR, *[c for c in out if c != MULTICOLOR]]
    return out[:MAX_COLORS]


def photo_tag_color(tag: str | None) -> list[str]:
    key = PHOTO_TAG_COLORS.get(tag or "")
    return [key] if key else []


def _user_text(batch: Sequence[tuple[str, str]], numbers: Mapping[str, int], allowed: str) -> str:
    lines = [f"Allowed colours: {allowed}", "", "Results:"]
    for n, (rid, title) in enumerate(batch, start=1):
        image = f"image {numbers[rid]}" if rid in numbers else "image none"
        lines.append(f"{n}. title: {title[:200]!r}; {image}")
    return "\n".join(lines)


def _one_call(
    batch: Sequence[tuple[str, str]],
    images: Mapping[str, PreparedImage],
    budget: Budget,
    config: Config,
    timeout_s: float,
    allowed: Sequence[str],
) -> dict[str, list[str]]:
    import json

    ordered = [rid for rid, _ in batch if rid in images]
    numbers = {rid: n for n, rid in enumerate(ordered, start=1)}
    done = complete(
        span_name="llm.shop_result_colors",
        prompt=load_prompt("shop_result_colors"),
        model=config.rank_model,
        user_text=_user_text(batch, numbers, ", ".join(allowed)),
        budget=budget,
        max_tokens=COLOR_MAX_TOKENS,
        schema=SCHEMA,
        images=[images[r] for r in ordered] or None,
        stream=True,
        timeout_s=timeout_s,
        disable_thinking=True,
    )
    parsed = json.loads(done.text)
    out: dict[str, list[str]] = {}
    for row in parsed.get("items", []) if isinstance(parsed, dict) else []:
        if not isinstance(row, dict) or not isinstance(row.get("index"), int):
            continue
        n = row["index"]
        if 1 <= n <= len(batch):
            out[batch[n - 1][0]] = clean_colors(row.get("colors"), allowed)
    return out


def shown_colors(
    shown: Sequence[tuple[str, str, str]],
    photo_colors: Mapping[str, str | None],
    cache: ThumbCache | None,
    budget: Budget,
    config: Config,
    time_left_s: float,
) -> ColorReport:
    """Colour keys for each of `shown` (result id, title, https thumbnail url).

    `photo_colors` maps a result id to the tagging vocabulary colour a photo tag gave it (only
    for photo-sourced tags). Results without one go to the vision call when `cache` can supply
    their image; the rest, and any the call did not answer, get their title's colour words.
    Never raises (a budget error included): the search goes on with title colours."""
    report = ColorReport()
    started = time.monotonic()
    allowed = allowed_colors()
    titles = {rid: title for rid, title, _ in shown}
    need: list[tuple[str, str, str]] = []
    for rid, title, url in shown:
        got = photo_tag_color(photo_colors.get(rid))
        if got:
            report.colors[rid] = got
            report.from_tags += 1
        else:
            need.append((rid, title, url))
    if need and cache is not None and time_left_s > 1.0:
        report.wanted = len(need)
        _vision(need, cache, budget, config, report, allowed, min(COLOR_BUDGET_S, time_left_s))
    elif need:
        report.reason = "no photos available" if cache is None else "no time left"
    for rid, _title, _url in need:
        if not report.colors.get(rid):
            report.colors[rid] = title_colors(titles[rid])
            report.from_title += 1
    report.seconds = time.monotonic() - started
    return report


def _vision(
    need: Sequence[tuple[str, str, str]],
    cache: ThumbCache,
    budget: Budget,
    config: Config,
    report: ColorReport,
    allowed: Sequence[str],
    budget_s: float,
) -> None:
    started = time.monotonic()
    deadline = started + budget_s
    lock = threading.Lock()
    closed = threading.Event()
    pool = ThreadPoolExecutor(max_workers=COLOR_WORKERS, thread_name_prefix="colortags")
    try:
        urls = {rid: url for rid, _t, url in need if url.startswith("https://")}
        fetches = {rid: pool.submit(cache.image, url) for rid, url in urls.items()}
        wait(list(fetches.values()), timeout=max(0.0, min(2.0, deadline - time.monotonic())))
        images = {
            rid: img for rid, f in fetches.items() if f.done() and (img := f.result()) is not None
        }
        report.with_image = len(images)
        with_img = [(rid, t) for rid, t, _u in need if rid in images]
        batches = [with_img[i : i + COLOR_BATCH] for i in range(0, len(with_img), COLOR_BATCH)]
        cost_before = budget.cost_usd

        def run(batch: list[tuple[str, str]]) -> None:
            left = deadline - time.monotonic()
            if left < 0.5:
                return
            try:
                got = _one_call(batch, images, budget, config, left, allowed)
            except BudgetExceededError:
                with lock:
                    report.failed_calls += 1
                    report.reason = "cost cap reached"
                return
            except Exception:  # a timeout, a refusal, bad JSON: those results keep title colours
                with lock:
                    report.calls += 1
                    report.failed_calls += 1
                return
            with lock:
                if closed.is_set():  # past the cap: this answer is not used
                    return
                report.calls += 1
                for rid, colors in got.items():
                    if colors:
                        report.colors[rid] = colors
                        report.from_vision += 1

        futures = [pool.submit(run, b) for b in batches]
        _done, pending = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
        with lock:
            closed.set()
        if pending and not report.reason:
            report.reason = f"{len(pending)} calls were still running at the {budget_s:.0f} s cap"
        report.usd = max(0.0, budget.cost_usd - cost_before)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
