"""Image preparation: every photo goes through `prepare_image` before it is sent anywhere.

Room photos are personal. This strips all metadata (EXIF, including GPS; XMP; ICC) by rebuilding
the image from raw pixels, applies the EXIF orientation first so the picture still looks upright,
downscales so the long edge is at most `max_edge` (image tokens scale with pixel area, so this is
also the cost lever), and re-encodes as JPEG.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

JPEG_QUALITY = 85
MEDIA_TYPE = "image/jpeg"


@dataclass(frozen=True)
class PreparedImage:
    data_b64: str
    media_type: str
    width: int
    height: int
    original_width: int
    original_height: int
    n_bytes: int

    def content_block(self) -> dict[str, object]:
        """The Messages API image content block for this image."""
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": self.media_type, "data": self.data_b64},
        }

    def summary(self) -> dict[str, object]:
        """Trace-safe description (never includes pixel data)."""
        return {
            "width": self.width,
            "height": self.height,
            "original_width": self.original_width,
            "original_height": self.original_height,
            "bytes": self.n_bytes,
            "media_type": self.media_type,
        }


def prepare_image(source: Path | str | bytes, *, max_edge: int = 1568) -> PreparedImage:
    """Strip metadata, fix orientation, downscale to `max_edge` on the long side, re-encode."""
    if max_edge < 16:
        raise ValueError(f"max_edge must be >= 16, got {max_edge}")
    raw = source if isinstance(source, bytes) else Path(source).read_bytes()
    with Image.open(io.BytesIO(raw)) as opened:
        original_width, original_height = opened.size
        upright = ImageOps.exif_transpose(opened)  # bakes in orientation, drops the tag
        rgb = _flatten_to_rgb(upright)
    # Rebuild from raw pixels so no metadata (EXIF/GPS/XMP/ICC) can ride along in .info.
    clean = Image.frombytes("RGB", rgb.size, rgb.tobytes())
    clean.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)  # never upscales
    buf = io.BytesIO()
    clean.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    encoded = buf.getvalue()
    return PreparedImage(
        data_b64=base64.standard_b64encode(encoded).decode("ascii"),
        media_type=MEDIA_TYPE,
        width=clean.width,
        height=clean.height,
        original_width=original_width,
        original_height=original_height,
        n_bytes=len(encoded),
    )


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def synthetic_room_image(width: int = 2400, height: int = 1600) -> bytes:
    """A generated placeholder 'room' (floor, wall, window, sofa block) as JPEG bytes.

    Used by tests and the mock spike so no real photo is ever needed. Carries a fake EXIF
    orientation and GPS block on purpose, so the strip path is exercised end to end.
    """
    from PIL import ImageDraw

    img = Image.new("RGB", (width, height), (225, 220, 210))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, int(height * 0.62), width, height), fill=(150, 110, 70))  # floor
    draw.rectangle(
        (int(width * 0.60), int(height * 0.12), int(width * 0.88), int(height * 0.45)),
        fill=(170, 205, 235),
    )  # window
    draw.rectangle(
        (int(width * 0.10), int(height * 0.50), int(width * 0.45), int(height * 0.78)),
        fill=(90, 100, 130),
    )  # sofa
    exif = Image.Exif()
    exif[0x0112] = 1  # orientation
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (40.0, 0.0, 0.0), "W", (73.0, 0.0, 0.0)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, exif=exif)
    return buf.getvalue()
