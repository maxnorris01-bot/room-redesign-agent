"""The edit instruction: prompts/render_edit.md filled in with the room, the sizes and the places."""

from __future__ import annotations

import re

from app.prompts import Prompt, load_prompt
from photoreal.inputs import RenderItem, size_text


def variants(prompt: Prompt | None = None) -> dict[str, str]:
    prompt = prompt or load_prompt("render_edit")
    found = re.split(r"<!--\s*variant:\s*(\w+)\s*-->", prompt.text)
    out = {found[i]: found[i + 1].strip() for i in range(1, len(found) - 1, 2)}
    if not {"all", "single"} <= set(out):
        raise ValueError("render_edit prompt needs 'all' and 'single' variants")
    return out


def room_block(room: dict[str, object]) -> str:
    try:
        w, ln, h = float(room["width_m"]), float(room["length_m"]), float(room["ceiling_height_m"])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        return "The room's real size is unknown."
    text = f"The room is roughly {w:.1f} by {ln:.1f} m ({w / 0.0254:.0f} by {ln / 0.0254:.0f} in), ceiling {h:.1f} m."
    conf = room.get("confidence")
    if isinstance(conf, int | float):
        text += (
            f" These dimensions were estimated from the photo with {'low ' if conf < 0.5 else ''}"
            f"confidence ({conf:.2f}); trust the proportions in the photo over them."
        )
    return text


def item_line(index: int, item: RenderItem) -> str:
    action = (
        f"Replace the existing {item.replaces_category}" if item.replaces_category else "Add it"
    )
    return (
        f"- Image {index}: {item.name} ({item.slot}). Real size: {size_text(item)}. "
        f"{action}, {item.placement.text}."
    )


def build_instruction(
    items: list[RenderItem], room: dict[str, object], *, single: bool, prompt: Prompt | None = None
) -> str:
    """One instruction. `single` is the one-item variant (the product is image 2)."""
    if single and len(items) != 1:
        raise ValueError("the single-item variant takes exactly one item")
    text = variants(prompt)["single" if single else "all"]
    lines = "\n".join(item_line(i, it) for i, it in enumerate(items, start=2))
    return text.replace("{room_block}", room_block(room)).replace("{items_block}", lines)
