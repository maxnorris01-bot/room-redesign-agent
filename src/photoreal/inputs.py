"""Inputs for a render run: the picks (from a spike run), validated product images, placement text
and boxes, the box mask, and the cheap outside-the-edit-regions signal."""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from app.images import prepare_image, synthetic_room_image
from app.schemas import DimensionsM
from app.sizes import resolve_size
from photoreal.requests import ImagePart

MAX_PRODUCT_BYTES = 20 * 1024 * 1024
MAX_EDGE = 1536
INPUT_DIR = Path("render-inputs")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
IN_M = 0.0254
BOX_PAD = 0.02  # fraction of the image added around a box when scoring "outside" changes
SIGNAL_EDGE = 128  # long edge, px, of the images compared for the outside-region signal

Box = tuple[float, float, float, float]  # x0, y0, x1, y1 as fractions of the image


class InputError(ValueError):
    """A render input is missing or unusable (the message names the file, never a key)."""


@dataclass
class Placement:
    text: str
    box: Box | None = None
    source: str = "default"  # "place" | "locate" | "default"


@dataclass
class RenderItem:
    slot: str
    name: str
    seller: str
    price: float | None
    dims: DimensionsM | None
    size_source: str
    replaces_id: str | None
    replaces_category: str | None
    placement: Placement = field(default_factory=lambda: Placement(""))
    image_path: Path | None = None


@dataclass
class RunPicks:
    run_id: str
    style: str
    room: dict[str, Any]  # perception dimensions
    room_photo: bytes | None
    items: list[RenderItem]


def slot_filename(slot: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", slot.lower()).strip("-")


def latest_run(out_root: Path = Path("spike-output")) -> Path:
    runs = (
        sorted(p for p in out_root.iterdir() if (p / "result.json").is_file() and p.name[:1] == "2")
        if out_root.is_dir()
        else []
    )
    for run in reversed(runs):
        try:
            data = json.loads((run / "result.json").read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and (data.get("proposal") or {}).get("items"):
            return run
    raise InputError(f"no run with a proposal found in {out_root}")


def load_picks(
    run_dir: Path, only: list[str] | None = None, photo_dir: Path = Path("spike-photos")
) -> RunPicks:
    """The picks of a finished spike run: name, seller, price, real size and what each replaces.

    Sizes are re-read from the stored titles with the current parser unless the listing itself
    stated them, so an older run's mis-read size (for example a tall bookcase read as deep) is
    corrected here.
    """
    try:
        data = json.loads((run_dir / "result.json").read_text())
    except (OSError, ValueError):
        raise InputError(f"cannot read {run_dir}/result.json") from None
    proposal = data.get("proposal") or {}
    items: list[RenderItem] = []
    for raw in proposal.get("items") or []:
        pick = raw.get("pick")
        if not pick:
            continue
        slot = str(raw.get("slot", ""))
        stored = pick.get("dimensions_m")
        dims = DimensionsM(**stored) if isinstance(stored, dict) else None
        source = str(pick.get("size_source") or "none")
        if source != "feature":
            found = resolve_size(
                slot, None, [str(pick.get("store_title") or ""), str(pick.get("name") or "")],
                str(pick.get("url") or ""),
            )  # fmt: skip
            if found.dims is not None:
                dims, source = found.dims, found.source
        rep = raw.get("replaces") or {}
        items.append(
            RenderItem(
                slot=slot,
                name=str(pick.get("name", "")),
                seller=str(pick.get("retailer", "")),
                price=pick.get("price"),
                dims=dims,
                size_source=source,
                replaces_id=rep.get("id"),
                replaces_category=rep.get("category"),
            )  # fmt: skip
        )
    if only:
        wanted = {slot_filename(o) for o in only}
        items = [i for i in items if slot_filename(i.slot) in wanted]
        missing = wanted - {slot_filename(i.slot) for i in items}
        if missing:
            raise InputError(f"no pick for: {', '.join(sorted(missing))}")
    if not items:
        raise InputError("the run has no picks to render")
    photo = None
    saved = run_dir / "room.jpg"
    if saved.is_file():
        photo = saved.read_bytes()
    else:
        fallback = photo_dir / Path(str(data.get("photo_name", ""))).name
        if fallback.is_file():
            photo = fallback.read_bytes()
    return RunPicks(
        run_id=str(data.get("run_id", run_dir.name)),
        style=str(data.get("style", "")),
        room=(data.get("perception") or {}).get("dimensions") or {},
        room_photo=photo,
        items=items,
    )


# ---- images ---------------------------------------------------------------------------------


def validate_product_image(path: Path) -> ImagePart:
    """Type, size, decodes; EXIF stripped and the long edge capped at 1536 px (JPEG out)."""
    if not path.is_file():
        raise InputError(f"missing product image: {path}")
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise InputError(f"{path.name}: not a jpg, png or webp file")
    raw = path.read_bytes()
    if len(raw) > MAX_PRODUCT_BYTES:
        raise InputError(f"{path.name}: larger than {MAX_PRODUCT_BYTES // (1024 * 1024)} MB")
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()
    except Exception:
        raise InputError(f"{path.name}: not a readable image") from None
    prepared = prepare_image(raw, max_edge=MAX_EDGE)
    import base64

    return ImagePart(path.stem + ".jpg", base64.b64decode(prepared.data_b64), prepared.media_type)


def find_product_image(slot: str, input_dir: Path = INPUT_DIR) -> Path | None:
    stem = slot_filename(slot)
    for suffix in IMAGE_SUFFIXES:
        candidate = input_dir / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def placeholder_product_image(slot: str) -> ImagePart:
    """A generated stand-in for a missing product image (mock mode only)."""
    prepared = prepare_image(synthetic_room_image(480, 360), max_edge=480)
    import base64

    return ImagePart(
        f"{slot_filename(slot)}.placeholder.jpg", base64.b64decode(prepared.data_b64), "image/jpeg"
    )


def prepare_room(room_bytes: bytes, *, as_png: bool) -> tuple[ImagePart, tuple[int, int]]:
    """The room photo for sending: EXIF stripped, long edge <= 1536, sides rounded to multiples of
    16 (an OpenAI custom size rule); PNG when a mask will be sent (image and mask share a format)."""
    import base64

    prepared = prepare_image(room_bytes, max_edge=MAX_EDGE)
    with Image.open(io.BytesIO(base64.b64decode(prepared.data_b64))) as opened:
        img = opened.convert("RGB")
    w, h = max(16, round(img.width / 16) * 16), max(16, round(img.height / 16) * 16)
    if (w, h) != img.size:
        img = img.resize((w, h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    if as_png:
        img.save(buf, format="PNG")
        return ImagePart("room.png", buf.getvalue(), "image/png"), (w, h)
    img.save(buf, format="JPEG", quality=90)
    return ImagePart("room.jpg", buf.getvalue(), "image/jpeg"), (w, h)


# ---- placement and masks ------------------------------------------------------------------------


def parse_place(values: list[str] | None) -> dict[str, str]:
    """`--place "slot=free text"` overrides, keyed by the slot's file-style name."""
    out: dict[str, str] = {}
    for v in values or []:
        slot, sep, text = v.partition("=")
        if not sep or not slot.strip() or not text.strip():
            raise InputError(f'--place must look like "slot=free text", got {v!r}')
        out[slot_filename(slot)] = text.strip()
    return out


def default_placement(item: RenderItem) -> Placement:
    if item.replaces_category:
        return Placement(f"in the place of the existing {item.replaces_category}", None, "default")
    return Placement(f"in a natural spot for a {item.slot} in this room", None, "default")


def apply_placements(
    items: list[RenderItem], place: dict[str, str], located: dict[str, tuple[str, Box]] | None
) -> None:
    """Manual --place text wins over --locate; the located box is kept when only the text is set."""
    located = located or {}
    for item in items:
        key = slot_filename(item.slot)
        loc = located.get(key)
        if key in place:
            item.placement = Placement(place[key], loc[1] if loc else None, "place")
        elif loc is not None:
            item.placement = Placement(loc[0], loc[1], "locate")
        else:
            item.placement = default_placement(item)


def build_box_mask(size: tuple[int, int], boxes: list[Box]) -> bytes:
    """A PNG mask (RGBA) of `size`: transparent inside the boxes (editing allowed), opaque elsewhere."""
    w, h = size
    mask = Image.new("RGBA", size, (0, 0, 0, 255))
    clear = Image.new("RGBA", size, (0, 0, 0, 0))
    for x0, y0, x1, y1 in boxes:
        box = (
            round(max(0.0, min(x0, x1)) * w), round(max(0.0, min(y0, y1)) * h),
            round(min(1.0, max(x0, x1)) * w), round(min(1.0, max(y0, y1)) * h),
        )  # fmt: skip
        mask.paste(clear.crop(box), box[:2])
    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    return buf.getvalue()


def outside_change(before: bytes, after: bytes, boxes: list[Box]) -> float | None:
    """Mean absolute pixel change (0..1) outside the padded boxes, at a reduced size. A SIGNAL for a
    person to look at, not a quality gate. None without boxes."""
    if not boxes:
        return None
    with Image.open(io.BytesIO(before)) as a, Image.open(io.BytesIO(after)) as b:
        small_a = a.convert("RGB")
        small_a.thumbnail((SIGNAL_EDGE, SIGNAL_EDGE), Image.Resampling.LANCZOS)
        small_b = b.convert("RGB").resize(small_a.size, Image.Resampling.LANCZOS)
    w, h = small_a.size
    inside = [[False] * w for _ in range(h)]
    for x0, y0, x1, y1 in boxes:
        lo_x, hi_x = round((min(x0, x1) - BOX_PAD) * w), round((max(x0, x1) + BOX_PAD) * w)
        lo_y, hi_y = round((min(y0, y1) - BOX_PAD) * h), round((max(y0, y1) + BOX_PAD) * h)
        for y in range(max(0, lo_y), min(h, hi_y)):
            for x in range(max(0, lo_x), min(w, hi_x)):
                inside[y][x] = True
    pa, pb = small_a.load(), small_b.load()
    assert pa is not None and pb is not None
    total, count = 0.0, 0
    for y in range(h):
        for x in range(w):
            if inside[y][x]:
                continue
            ra, rb = pa[x, y], pb[x, y]
            total += sum(abs(int(ra[i]) - int(rb[i])) for i in range(3)) / 3.0
            count += 1
    return round(total / count / 255.0, 4) if count else None


def size_text(item: RenderItem) -> str:
    """Real size in metres and inches, as written into the instruction."""
    d = item.dims
    if d is None:
        return "size unknown (judge the scale from the product image and the room)"
    parts = []
    for value, word in (
        (d.width_m, "wide"), (d.depth_m, "deep"), (d.height_m, "high"), (d.length_m, "long")
    ):  # fmt: skip
        if value is not None:
            parts.append(f"{value:.2f} m ({value / IN_M:.0f} in) {word}")
    if not parts:
        return "size unknown (judge the scale from the product image and the room)"
    only_longest = d.length_m is not None and not any((d.width_m, d.depth_m, d.height_m))
    text = ", ".join(parts)
    return f"about {text} (longest side only)" if only_longest else text
