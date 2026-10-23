"""Orchestration of one render run: plan, cap, calls (all at once or one by one), signals, outputs."""

from __future__ import annotations

import io
import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image

from app.config import Config
from app.images import synthetic_room_image
from app.llm import Budget
from app.prompts import load_prompt
from photoreal import inputs, instruction, requests, specs, vision
from photoreal.cost import CapError, Plan, SpendGuard, make_plan, plan_text
from photoreal.inputs import InputError, Placement, RenderItem
from photoreal.requests import ImagePart, ProviderRequest, RenderError, Transport

OUT_ROOT = Path("render-output")


@dataclass
class RunArgs:
    from_run: Path
    approach: str = "gpt"
    items: list[str] | None = None
    mode: str = "all-at-once"
    quality: str = "medium"
    mask: str = "none"
    place: list[str] = field(default_factory=list)
    locate: bool = False
    judge: bool = False
    max_usd: float = 3.00
    live: bool = False
    model: str | None = None
    input_dir: Path = inputs.INPUT_DIR
    out_root: Path = OUT_ROOT
    photo_dir: Path = Path("spike-photos")


@dataclass
class RenderRun:
    run_id: str
    out_dir: Path
    result: dict[str, Any]
    ok: bool


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if len(s) >= 8:
            text = text.replace(s, "[redacted]")
    return text


def strip_query(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme and parts.netloc else ""


def secrets_in(env: Mapping[str, str]) -> list[str]:
    names = (*specs.KEY_ENV.values(), "ANTHROPIC_API_KEY", "SERPER_API_KEY", "SERPAPI_API_KEY")
    return [env[n] for n in names if env.get(n)]


def resolve_mode(approach: str, mode: str, n_items: int) -> tuple[str, str]:
    """(mode, note). Gemini defaults to one by one; all at once only fits its 6-object limit."""
    if approach == "gemini" and mode == "all-at-once" and n_items > specs.GEMINI_MAX_OBJECT_IMAGES:
        return (
            "one-by-one",
            f"gemini takes at most {specs.GEMINI_MAX_OBJECT_IMAGES} product images; one by one",
        )
    if approach == "decor8":
        return "one-by-one", "decor8 handles one product per call"
    return mode, ""


def live_refusals(
    spec: specs.ProviderSpec, env: Mapping[str, str], *, locate_or_judge: bool
) -> list[str]:
    """Why a live run cannot start (empty list = ready)."""
    reasons = [f"unverified: {u}" for u in spec.unverified]
    if not env.get(specs.KEY_ENV[spec.approach]):
        reasons.append(f"{specs.KEY_ENV[spec.approach]} is not set in the environment")
    if locate_or_judge and not env.get("ANTHROPIC_API_KEY"):
        reasons.append("ANTHROPIC_API_KEY is not set (needed for --locate / --judge)")
    return reasons


def _png(data: bytes, size: tuple[int, int]) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        img = im.convert("RGB")
    if img.size != size:
        img = img.resize(size, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def run_render(
    args: RunArgs,
    transport: Transport,
    config: Config,
    *,
    env: Mapping[str, str] | None = None,
    run_id: str | None = None,
    on_plan: Callable[[Plan], None] | None = None,
) -> RenderRun:
    env = os.environ if env is None else env
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    mock = not args.live
    spec = specs.spec_for(args.approach, env, args.model)
    if args.live:
        reasons = live_refusals(spec, env, locate_or_judge=args.locate or args.judge)
        if reasons:
            raise InputError("live run refused:\n  - " + "\n  - ".join(reasons))
    picks = inputs.load_picks(args.from_run, args.items, args.photo_dir)
    items = picks.items
    room_bytes = picks.room_photo
    if room_bytes is None:
        if not mock:
            raise InputError(
                "no room photo: run folder has no room.jpg and spike-photos/ has no match"
            )
        room_bytes = synthetic_room_image()
    use_png = args.mask == "boxed" and args.approach == "gpt"
    room_part, work_size = inputs.prepare_room(room_bytes, as_png=use_png)
    products: dict[str, ImagePart] = {}
    placeholders: list[str] = []
    for item in items:
        path = inputs.find_product_image(item.slot, args.input_dir)
        if path is not None:
            products[item.slot] = inputs.validate_product_image(path)
            item.image_path = path
        elif mock:
            products[item.slot] = inputs.placeholder_product_image(item.slot)
            placeholders.append(item.slot)
        else:
            raise InputError(
                f"missing product image for '{item.slot}': put {inputs.slot_filename(item.slot)}.jpg "
                f"in {args.input_dir}/"
            )

    mode, mode_note = resolve_mode(args.approach, args.mode, len(items))
    size = f"{work_size[0]}x{work_size[1]}"
    plan: Plan = make_plan(
        spec, [i.slot for i in items], mode=mode, quality=args.quality, size=size,
        locate=args.locate, judge=args.judge, anthropic_model=config.model_stylecheck, env=env,
    )  # fmt: skip
    if plan.total > args.max_usd + 1e-9:
        raise CapError(
            f"the plan's estimated ${plan.total:.2f} is over --max-usd ${args.max_usd:.2f}; not starting"
        )
    if on_plan is not None:
        on_plan(plan)  # printed before any call is made
    guard = SpendGuard(args.max_usd)
    budget = Budget(config)
    notes: list[str] = [n for n in (mode_note,) if n]
    notes += [f"mock placeholder used for the product image of '{s}'" for s in placeholders]
    if args.mask == "boxed" and args.approach != "gpt":
        notes.append(f"--mask boxed is ignored for {args.approach} (no mask input documented)")
    calls: list[dict[str, Any]] = []

    # ---- placement ----------------------------------------------------------------------
    located: dict[str, tuple[str, inputs.Box]] | None = None
    if args.locate:
        guard.check(plan.calls[0].est_usd)
        located, done = vision.locate_items(room_part.data, items, budget)
        guard.add(done.cost_usd)
        calls.append(
            {"kind": "locate", "provider": "anthropic", "model": done.model, "seconds": None,
             "est_usd": plan.calls[0].est_usd, "cost_usd": done.cost_usd,
             "input_tokens": done.input_tokens, "output_tokens": done.output_tokens}
        )  # fmt: skip
    inputs.apply_placements(items, inputs.parse_place(args.place), located)
    if args.mask == "boxed" and args.approach == "gpt":
        lacking = [i.slot for i in items if i.placement.box is None]
        if lacking:
            raise InputError(f"--mask boxed needs a box for: {', '.join(lacking)} (use --locate)")

    # ---- render calls --------------------------------------------------------------------
    steps: list[dict[str, Any]] = []
    out_dir = args.out_root / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "original.jpg").write_bytes(
        room_part.data if room_part.mime == "image/jpeg" else _jpeg(room_part.data)
    )
    current = room_part
    groups = [items] if mode == "all-at-once" else [[i] for i in items]
    render_plan = [c for c in plan.calls if c.kind == "render"]
    error: str | None = None
    final_bytes: bytes | None = None
    for n, group in enumerate(groups, start=1):
        planned = render_plan[n - 1]
        try:
            guard.check(planned.est_usd)
        except CapError as exc:
            error = str(exc)
            break
        text = instruction.build_instruction(
            group, picks.room, single=len(group) == 1 and mode == "one-by-one"
        )
        images = [current, *(products[i.slot] for i in group)]
        boxes = [i.placement.box for i in group if i.placement.box is not None]
        mask = inputs.build_box_mask(work_size, boxes) if use_png and boxes else None
        if args.approach == "gpt":
            req: ProviderRequest = requests.build_gpt_request(
                text, images, model=spec.model, size=size, quality=args.quality, mask=mask,
                timeout_s=spec.timeout_s,
            )  # fmt: skip
        else:
            req = requests.build_gemini_request(
                text, images, model=spec.model, timeout_s=spec.timeout_s
            )
        record: dict[str, Any] = {
            "kind": "render", "provider": args.approach, "model": spec.model, "step": n,
            "items": [i.slot for i in group], "request": req.log_view(), "instruction": text,
            "image_names": [im.name for im in images], "est_usd": planned.est_usd,
            "est_basis": planned.basis,
        }  # fmt: skip
        try:
            res = transport.send(req)
        except RenderError as exc:
            record.update(status="error", error=redact(str(exc), secrets_in(env)))
            calls.append(record)
            error = record["error"]
            break
        guard.add(max(planned.est_usd, res.reported_cost_usd or 0.0))
        out_png = _png(res.image, work_size)
        step_name = f"step-{n}.png"
        (out_dir / step_name).write_bytes(out_png)
        signal = inputs.outside_change(current.data, out_png, boxes) if boxes else None
        record.update(
            status="ok", seconds=round(res.seconds, 3), request_id=res.request_id,
            reported_usage=res.usage, reported_cost_usd=res.reported_cost_usd,
            outside_change=signal, output=step_name,
        )  # fmt: skip
        calls.append(record)
        steps.append({"step": n, "items": [i.slot for i in group], "image": step_name})
        final_bytes = out_png
        current = ImagePart("previous-step.png", out_png, "image/png")
    if final_bytes is not None:
        (out_dir / "render.png").write_bytes(final_bytes)

    # ---- judge ---------------------------------------------------------------------------
    judgement: dict[str, Any] | None = None
    if args.judge and final_bytes is not None and error is None:
        try:
            guard.check(plan.calls[-1].est_usd)
            jr, done = vision.judge_render(
                final_bytes, room_part.data, items, [products[i.slot].data for i in items], budget
            )
            guard.add(done.cost_usd)
            judgement = {
                "items": {k: {"look_alike": v[0], "notes": v[1]} for k, v in jr.items.items()},
                "rest_of_room_changed": jr.rest_of_room_changed, "room_notes": jr.room_notes,
                "label": "signal only, not a quality gate",
            }  # fmt: skip
            calls.append({"kind": "judge", "provider": "anthropic", "model": done.model,
                          "est_usd": plan.calls[-1].est_usd, "cost_usd": done.cost_usd})  # fmt: skip
        except CapError as exc:
            notes.append(str(exc))
    for item in items:
        (out_dir / f"ref-{inputs.slot_filename(item.slot)}.jpg").write_bytes(
            products[item.slot].data
        )
    anthropic_cost = round(
        sum(c.get("cost_usd") or 0.0 for c in calls if c["provider"] == "anthropic"), 6
    )
    render_est = round(
        sum(c["est_usd"] for c in calls if c["kind"] == "render" and c.get("status") == "ok"), 4
    )
    result: dict[str, Any] = {
        "run_id": run_id, "source_run": picks.run_id, "style": picks.style,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "live": args.live, "approach": args.approach, "model": spec.model, "mode": mode,
        "quality": args.quality, "mask": args.mask, "size": size, "max_usd": args.max_usd,
        "prompt_version": load_prompt("render_edit").version,
        "plan": plan.to_json(), "calls": calls, "steps": steps,
        "items": [_item_json(i, products[i.slot]) for i in items],
        "judge": judgement, "notes": notes, "error": error,
        "totals": {"render_estimated_usd": render_est, "anthropic_usd": anthropic_cost,
                   "running_total_usd": round(guard.spent, 4)},
        "signals_note": "the outside-region change and the judge are signals for a person, not a quality gate",
        "unverified": list(spec.unverified), "spec_notes": list(spec.notes),
    }  # fmt: skip
    text = redact(json.dumps(result, indent=2, default=str), secrets_in(env))
    (out_dir / "result.json").write_text(text + "\n")
    from photoreal.report import write_render_report

    write_render_report(out_dir, json.loads(text))
    return RenderRun(run_id, out_dir, json.loads(text), error is None)


def _jpeg(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as im:
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _item_json(item: RenderItem, image: ImagePart) -> dict[str, Any]:
    d = item.dims.model_dump() if item.dims else None
    return {
        "slot": item.slot, "name": item.name, "seller": item.seller, "price": item.price,
        "size_m": d, "size_source": item.size_source, "size_text": inputs.size_text(item),
        "replaces": item.replaces_category, "reference_image": f"ref-{inputs.slot_filename(item.slot)}.jpg",
        "reference_file": image.name,
        "placement": {"text": item.placement.text, "box": item.placement.box, "source": item.placement.source},
    }  # fmt: skip


__all__ = ["Placement", "RenderRun", "RunArgs", "plan_text", "run_render", "strip_query"]
