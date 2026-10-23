"""The two optional Claude (Haiku) vision calls: locate where each item goes, judge a render.

Both go through `app.llm.complete`, so they are traced and their dollars are counted. Their output
is advice for the spike (a placement phrase and rough box; a look-alike score), never a gate.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from app.config import Config
from app.images import PreparedImage, prepare_image
from app.llm import Budget, Completion, complete
from app.prompts import load_prompt
from app.schemas import to_output_schema
from photoreal.inputs import Box, RenderItem
from photoreal.schemas import RenderJudgeOutput, RenderLocateOutput

VISION_EDGE = 1024
MAX_TOKENS = 1200


def _prepared(data: bytes) -> PreparedImage:
    return prepare_image(data, max_edge=VISION_EDGE)


def clamp_box(box: Box) -> Box:
    x0, y0, x1, y1 = (min(1.0, max(0.0, v)) for v in box)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def locate_items(
    room: bytes, items: list[RenderItem], budget: Budget
) -> tuple[dict[str, tuple[str, Box]], Completion]:
    """slot-file-name -> (placement phrase, box). Boxes are fractions of the image."""
    from photoreal.inputs import slot_filename

    lines = [
        f"Item: {i.slot} - "
        + (
            f"replace the existing {i.replaces_category}"
            if i.replaces_category
            else "add a new piece"
        )
        for i in items
    ]
    done = complete(
        span_name="render.locate",
        prompt=load_prompt("render_locate"),
        model=budget.config.model_stylecheck,
        user_text="Items:\n" + "\n".join(lines),
        budget=budget,
        max_tokens=MAX_TOKENS,
        schema=to_output_schema(RenderLocateOutput),
        images=[_prepared(room)],
    )
    parsed = RenderLocateOutput.model_validate_json(done.text)
    out = {
        slot_filename(i.slot): (i.placement, clamp_box((i.box.x0, i.box.y0, i.box.x1, i.box.y1)))
        for i in parsed.items
    }
    return out, done


@dataclass
class Judgement:
    items: dict[str, tuple[int, str]]
    rest_of_room_changed: bool
    room_notes: str


def judge_render(
    render: bytes, room: bytes, items: list[RenderItem], product_images: list[bytes], budget: Budget
) -> tuple[Judgement, Completion]:
    lines = [f"Item: {i.slot} (product image {n})" for n, i in enumerate(items, start=3)]
    done = complete(
        span_name="render.judge",
        prompt=load_prompt("render_judge"),
        model=budget.config.model_stylecheck,
        user_text="Items, in product-image order after images 1 and 2:\n" + "\n".join(lines),
        budget=budget,
        max_tokens=MAX_TOKENS,
        schema=to_output_schema(RenderJudgeOutput),
        images=[_prepared(render), _prepared(room), *(_prepared(p) for p in product_images)],
    )
    parsed = RenderJudgeOutput.model_validate_json(done.text)
    return (
        Judgement(
            {j.slot: (min(5, max(1, j.look_alike)), j.notes) for j in parsed.items},
            parsed.rest_of_room_changed,
            parsed.room_notes,
        ),
        done,
    )


def png_bytes(b64: str) -> bytes:
    return base64.b64decode(b64)


__all__ = ["Config", "Judgement", "judge_render", "locate_items"]
