from __future__ import annotations

import base64
import io

from PIL import Image

from app.images import prepare_image, synthetic_room_image


def _decode(prepared_b64: str) -> bytes:
    return base64.standard_b64decode(prepared_b64)


def test_synthetic_input_really_has_gps_and_exif() -> None:
    """Guard the guard: the strip test below is meaningless if the input had no metadata."""
    exif = Image.open(io.BytesIO(synthetic_room_image())).getexif()
    assert exif.get_ifd(0x8825), "synthetic image should carry a GPS block"


def test_exif_and_gps_are_removed() -> None:
    prepared = prepare_image(synthetic_room_image())
    data = _decode(prepared.data_b64)
    out = Image.open(io.BytesIO(data))
    assert len(out.getexif()) == 0
    assert not out.getexif().get_ifd(0x8825)
    assert b"Exif" not in data
    assert not {"exif", "xmp", "icc_profile"} & set(out.info)


def test_downscale_respects_max_edge_and_keeps_aspect() -> None:
    prepared = prepare_image(synthetic_room_image(2400, 1600), max_edge=800)
    assert max(prepared.width, prepared.height) == 800
    assert abs(prepared.width / prepared.height - 1.5) < 0.01  # aspect kept (up to rounding)
    assert (prepared.original_width, prepared.original_height) == (2400, 1600)
    decoded = Image.open(io.BytesIO(_decode(prepared.data_b64)))
    assert decoded.size == (prepared.width, prepared.height)
    assert prepared.media_type == "image/jpeg"


def test_small_image_is_not_upscaled() -> None:
    prepared = prepare_image(synthetic_room_image(400, 300), max_edge=1568)
    assert (prepared.width, prepared.height) == (400, 300)


def test_exif_orientation_is_applied_before_stripping() -> None:
    # Stored 200x100 with the left half red; orientation 6 = "rotate 90 degrees clockwise to
    # view", so the upright picture is 100x200 with the red half on TOP.
    img = Image.new("RGB", (200, 100), (0, 0, 255))
    img.paste((255, 0, 0), (0, 0, 100, 100))
    exif = Image.Exif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95, exif=exif)

    prepared = prepare_image(buf.getvalue())
    assert (prepared.width, prepared.height) == (100, 200)
    out = Image.open(io.BytesIO(_decode(prepared.data_b64))).convert("RGB")
    top, bottom = out.getpixel((50, 20)), out.getpixel((50, 180))
    assert top[0] > 200 and top[2] < 60  # red
    assert bottom[2] > 200 and bottom[0] < 60  # blue
    assert 0x0112 not in out.getexif()


def test_transparent_png_is_flattened_to_rgb_jpeg() -> None:
    buf = io.BytesIO()
    Image.new("RGBA", (64, 64), (255, 0, 0, 0)).save(buf, format="PNG")
    prepared = prepare_image(buf.getvalue())
    out = Image.open(io.BytesIO(_decode(prepared.data_b64)))
    assert out.mode == "RGB"
    assert out.getpixel((5, 5))[0] > 240 and out.getpixel((5, 5))[2] > 240  # white, not red


def test_content_block_and_summary_shape() -> None:
    prepared = prepare_image(synthetic_room_image(300, 200))
    block = prepared.content_block()
    assert block["type"] == "image"
    assert block["source"] == {
        "type": "base64",
        "media_type": "image/jpeg",
        "data": prepared.data_b64,
    }
    assert "data" not in str(prepared.summary())  # trace-safe: never pixel data
