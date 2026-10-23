from __future__ import annotations

import base64
import importlib.util
import io
import json
import socket
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from PIL import Image

from app.config import Config
from app.images import synthetic_room_image
from photoreal import inputs, instruction, specs
from photoreal.cost import CapError, SpendGuard, make_plan, plan_text
from photoreal.inputs import InputError, Placement, RenderItem
from photoreal.report import build_render_page
from photoreal.requests import (
    HttpTransport,
    ImagePart,
    MockTransport,
    ProviderResult,
    RenderError,
    build_gemini_request,
    build_gpt_request,
    find_gemini_image,
    gemini_body,
)
from photoreal.runner import RunArgs, live_refusals, redact, run_render, strip_query

FAKE_KEY = "sk-test-0123456789abcdef"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("tests must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def _png(size: tuple[int, int] = (160, 96), color: tuple[int, int, int] = (200, 190, 180)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def make_run(tmp_path: Path, name: str = "20261001T000000Z-aaaaaa") -> Path:
    run = tmp_path / "spike-output" / name
    run.mkdir(parents=True)
    pick = {
        "name": "Walnut Bookcase, 21\"x79\", Open Shelves", "retailer": "Article", "price": 399.0,
        "dimensions_m": {"width_m": 0.5334, "depth_m": 2.0066}, "size_source": "title",
        "store_title": "Walnut Wood Modular Narrow Bookcase, 21\"x79\", 5x Open Storage Shelves",
        "url": "https://www.article.com/p/heidi?utm=secret&x=1",
    }  # fmt: skip
    items = [
        {"slot": "sofa", "replaces": {"id": "f1", "category": "sofa", "width_m": 2.2, "depth_m": 0.9},
         "pick": {"name": "Ada Sofa", "retailer": "Room & Board", "price": 1699.0,
                  "dimensions_m": {"width_m": 2.032, "depth_m": 0.8636, "height_m": 0.762},
                  "size_source": "feature", "store_title": "", "url": "https://rb.example/sofa"}},
        {"slot": "bookshelf", "replaces": None, "pick": pick},
        {"slot": "media unit", "replaces": None, "pick": None},
    ]  # fmt: skip
    data = {
        "run_id": name, "style": "warm minimalist", "photo_name": "<bytes>",
        "perception": {"dimensions": {"width_m": 6.0, "length_m": 5.0, "ceiling_height_m": 2.6,
                                       "confidence": 0.35}},
        "proposal": {"items": items},
    }  # fmt: skip
    (run / "result.json").write_text(json.dumps(data))
    (run / "room.jpg").write_bytes(synthetic_room_image(1200, 800))
    return run


def _args(tmp_path: Path, **kw: Any) -> RunArgs:
    base: dict[str, Any] = {
        "from_run": make_run(tmp_path) if "from_run" not in kw else kw.pop("from_run"),
        "input_dir": tmp_path / "render-inputs", "out_root": tmp_path / "render-output",
        "photo_dir": tmp_path / "photos",
    }  # fmt: skip
    return RunArgs(**{**base, **kw})


def _cfg() -> Config:
    return Config(llm_mode="mock")


# ---- estimates and the cap ----------------------------------------------------------------------


def test_gemini_estimate_uses_the_verified_price() -> None:
    usd, basis = specs.estimate_call_usd("gemini", n_input_images=3, quality="medium", env={})
    assert usd == pytest.approx(0.134 + 3 * 0.0011) and "Google" in basis


def test_gpt_estimate_is_an_assumption_unless_the_owner_supplies_a_price() -> None:
    usd, basis = specs.estimate_call_usd("gpt", n_input_images=4, quality="high", env={})
    assert usd == 0.25 and "unverified" in basis
    usd, basis = specs.estimate_call_usd(
        "gpt", n_input_images=4, quality="high", env={"RENDER_OPENAI_USD_PER_IMAGE": "0.5"}
    )
    assert usd == 0.5 and "owner-supplied" in basis


def test_plan_lists_calls_models_and_total() -> None:
    spec = specs.spec_for("gemini", {})
    plan = make_plan(spec, ["sofa", "bookshelf"], mode="one-by-one", quality="medium",
                     size="1536x1024", locate=True, judge=True, anthropic_model="m", env={})  # fmt: skip
    assert [c.kind for c in plan.calls] == ["locate", "render", "render", "judge"]
    assert plan.total == pytest.approx(sum(c.est_usd for c in plan.calls), abs=1e-3)
    text = plan_text(plan, 3.0, live=False)
    assert "gemini-3-pro-image" in text and "1536x1024" in text and "--max-usd $3.00" in text


def test_a_plan_over_the_cap_refuses_before_any_call(tmp_path: Path) -> None:
    transport = MockTransport()
    with pytest.raises(CapError, match="over --max-usd"):
        run_render(_args(tmp_path, approach="gpt", quality="high", max_usd=0.10),
                   transport, _cfg(), env={}, run_id="r")  # fmt: skip
    assert transport.requests == []


def test_spend_guard_stops_before_the_call_that_would_pass_the_cap() -> None:
    guard = SpendGuard(0.30)
    guard.check(0.13)
    guard.add(0.13)
    guard.check(0.13)
    guard.add(0.13)
    with pytest.raises(CapError, match="would pass the cap"):
        guard.check(0.13)


class _Pricey:
    """A transport that reports a bigger cost than estimated, so the second call must not start."""

    def __init__(self) -> None:
        self.calls = 0

    def send(self, req: Any) -> ProviderResult:
        self.calls += 1
        return ProviderResult(_png(), 0.1, f"id{self.calls}", {}, 0.9)


def test_the_running_total_stops_a_chain_midway(tmp_path: Path) -> None:
    transport = _Pricey()
    run = run_render(
        _args(tmp_path, approach="gemini", mode="one-by-one", max_usd=1.0),
        transport, _cfg(), env={}, run_id="chain",
    )  # fmt: skip
    assert transport.calls == 1 and not run.ok
    assert "would pass the cap" in run.result["error"]
    assert run.result["totals"]["running_total_usd"] == pytest.approx(0.9)
    assert (run.out_dir / "step-1.png").is_file() and (run.out_dir / "index.html").is_file()


# ---- request building ---------------------------------------------------------------------------


def _parts(n: int = 2) -> list[ImagePart]:
    return [ImagePart(f"img{i}.png", _png(color=(i * 40, 0, 0)), "image/png") for i in range(n)]


def test_gpt_request_has_room_first_then_products_and_the_documented_params() -> None:
    req = build_gpt_request("do it", _parts(3), model="m1", size="1536x1024", quality="medium",
                            mask=b"MASK")  # fmt: skip
    assert [i.name for i in req.images] == ["img0.png", "img1.png", "img2.png"]
    assert req.params == {"size": "1536x1024", "quality": "medium", "n": 1, "output_format": "png"}
    assert (
        req.timeout_s >= 600
        or build_gpt_request("x", _parts(), model="m", size="s", quality="low").timeout_s == 600.0
    )


def test_gpt_http_request_is_multipart_with_an_image_array_a_mask_and_bearer_auth() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["ctype"] = request.headers["content-type"]
        seen["body"] = request.read()
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_png()).decode()}],
                                         "usage": {"input_tokens": 5}},
                              headers={"x-request-id": "req_1"})  # fmt: skip

    transport = HttpTransport(
        {"gpt": FAKE_KEY}, httpx.Client(transport=httpx.MockTransport(handler))
    )
    mask = inputs.build_box_mask((160, 96), [(0.1, 0.1, 0.5, 0.5)])
    req = build_gpt_request("go", _parts(3), model="m1", size="160x96", quality="low", mask=mask)
    res = transport.send(req)
    body = seen["body"]
    assert seen["ctype"].startswith("multipart/form-data") and seen["auth"] == f"Bearer {FAKE_KEY}"
    assert body.count(b'name="image[]"') == 3 and body.count(b'name="mask"') == 1
    assert body.index(b"img0.png") < body.index(b"img1.png") < body.index(b"img2.png")
    for field_name in (b'name="model"', b'name="prompt"', b'name="size"', b'name="quality"'):
        assert field_name in body
    assert (
        res.request_id == "req_1"
        and res.usage == {"input_tokens": 5}
        and res.image[:4] == b"\x89PNG"
    )
    assert FAKE_KEY not in json.dumps(req.log_view()) and "?" not in req.log_view()["endpoint"]


def test_gemini_http_request_is_json_with_text_then_images_and_the_key_header() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers["x-goog-api-key"]
        seen["json"] = json.loads(request.read())
        out = {"interaction": {"output_image": {"data": base64.b64encode(_png()).decode()}}}
        return httpx.Response(200, json=out)

    transport = HttpTransport(
        {"gemini": FAKE_KEY}, httpx.Client(transport=httpx.MockTransport(handler))
    )
    req = build_gemini_request("edit it", _parts(2), model="gemini-3-pro-image")
    res = transport.send(req)
    body = seen["json"]
    assert seen["key"] == FAKE_KEY and body["model"] == "gemini-3-pro-image"
    assert [p["type"] for p in body["input"]] == ["text", "image", "image"]
    assert body["input"][0]["text"] == "edit it" and body["input"][1]["mime_type"] == "image/png"
    assert body == gemini_body(req) and res.image[:4] == b"\x89PNG"


def test_gemini_image_is_found_at_the_documented_path_or_under_interaction() -> None:
    assert find_gemini_image({"output_image": {"data": "AAA"}}) == "AAA"
    assert find_gemini_image({"interaction": {"output_image": {"data": "BBB"}}}) == "BBB"
    assert (
        find_gemini_image({"outputs": [{"type": "text"}, {"type": "image", "data": "CCC"}]})
        == "CCC"
    )
    assert find_gemini_image({"nothing": 1}) is None


def test_http_errors_name_the_provider_and_status_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad key {FAKE_KEY} at {request.url}")

    transport = HttpTransport(
        {"gemini": FAKE_KEY}, httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(RenderError) as err:
        transport.send(build_gemini_request("x", _parts(2), model="m"))
    assert str(err.value) == "gemini: HTTP 401" and FAKE_KEY not in str(err.value)
    assert FAKE_KEY not in repr(transport)


def test_a_missing_key_is_reported_without_a_call() -> None:
    with pytest.raises(RenderError, match="API key is not set"):
        HttpTransport({}).send(build_gemini_request("x", _parts(2), model="m"))


# ---- redaction ----------------------------------------------------------------------------------


def test_redact_and_strip_query() -> None:
    assert redact(f"a {FAKE_KEY} b", [FAKE_KEY]) == "a [redacted] b"
    assert strip_query("https://x.example/p/1?token=abc&y=2#frag") == "https://x.example/p/1"


def test_result_json_never_holds_a_key(tmp_path: Path) -> None:
    class Leaky:
        def send(self, req: Any) -> ProviderResult:
            raise RenderError(f"gemini: failed with {FAKE_KEY}")

    env = {"GEMINI_API_KEY": FAKE_KEY}
    run = run_render(_args(tmp_path, approach="gemini"), Leaky(), _cfg(), env=env, run_id="leak")
    text = (run.out_dir / "result.json").read_text() + (run.out_dir / "index.html").read_text()
    assert FAKE_KEY not in text and "[redacted]" in text and not run.ok


# ---- inputs -------------------------------------------------------------------------------------


def test_product_image_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = tmp_path / "sofa.png"
    good.write_bytes(_png((3000, 2000)))
    part = inputs.validate_product_image(good)
    with Image.open(io.BytesIO(part.data)) as im:
        assert max(im.size) == 1536 and not im.getexif()
    with pytest.raises(InputError, match="missing"):
        inputs.validate_product_image(tmp_path / "nope.jpg")
    bad = tmp_path / "x.jpg"
    bad.write_bytes(b"not an image")
    with pytest.raises(InputError, match="not a readable image"):
        inputs.validate_product_image(bad)
    gif = tmp_path / "x.gif"
    gif.write_bytes(_png())
    with pytest.raises(InputError, match="jpg, png or webp"):
        inputs.validate_product_image(gif)
    monkeypatch.setattr(inputs, "MAX_PRODUCT_BYTES", 100)
    with pytest.raises(InputError, match="larger than"):
        inputs.validate_product_image(good)


def test_exif_is_stripped_from_product_images(tmp_path: Path) -> None:
    img = Image.new("RGB", (64, 64), (10, 20, 30))
    exif = Image.Exif()
    exif[0x010F] = "SecretMaker"
    path = tmp_path / "lamp.jpg"
    img.save(path, format="JPEG", exif=exif)
    assert b"SecretMaker" in path.read_bytes()
    assert b"SecretMaker" not in inputs.validate_product_image(path).data


def test_slot_file_names_and_lookup(tmp_path: Path) -> None:
    assert inputs.slot_filename("Media unit") == "media-unit"
    (tmp_path / "media-unit.png").write_bytes(_png())
    assert inputs.find_product_image("media unit", tmp_path) == tmp_path / "media-unit.png"
    assert inputs.find_product_image("sofa", tmp_path) is None


def test_load_picks_corrects_an_old_misread_size(tmp_path: Path) -> None:
    picks = inputs.load_picks(make_run(tmp_path))
    by = {i.slot: i for i in picks.items}
    assert set(by) == {"sofa", "bookshelf"}  # the empty slot is skipped
    assert by["sofa"].dims is not None and by["sofa"].dims.height_m == 0.762  # listing size kept
    shelf = by["bookshelf"].dims
    assert shelf is not None and shelf.depth_m is None and shelf.height_m == pytest.approx(2.0066)
    assert (
        inputs.load_picks(make_run(tmp_path, "20261002T000000Z-bbbbbb"), ["sofa"]).items[0].slot
        == "sofa"
    )
    with pytest.raises(InputError, match="no pick for"):
        inputs.load_picks(make_run(tmp_path, "20261003T000000Z-cccccc"), ["armchair"])


def test_latest_run_skips_runs_without_a_proposal(tmp_path: Path) -> None:
    make_run(tmp_path, "20261001T000000Z-aaaaaa")
    empty = tmp_path / "spike-output" / "20261009T000000Z-zzzzzz"
    empty.mkdir()
    (empty / "result.json").write_text(json.dumps({"proposal": {}}))
    assert inputs.latest_run(tmp_path / "spike-output").name == "20261001T000000Z-aaaaaa"


# ---- placement, mask, signal --------------------------------------------------------------------


def _item(slot: str, replaces: str | None = None) -> RenderItem:
    return RenderItem(slot, f"{slot} product", "Shop", 1.0, None, "none", None, replaces)


def test_manual_place_wins_over_locate_and_keeps_the_located_box() -> None:
    items = [_item("sofa", "sofa"), _item("media unit"), _item("rug")]
    located = {"sofa": ("located sofa spot", (0.1, 0.2, 0.5, 0.6)),
               "media-unit": ("located wall", (0.6, 0.2, 0.9, 0.7))}  # fmt: skip
    inputs.apply_placements(
        items, inputs.parse_place(["Media Unit=right of the fireplace"]), located
    )
    sofa, media, rug = items
    assert (sofa.placement.source, sofa.placement.text) == ("locate", "located sofa spot")
    assert media.placement == Placement("right of the fireplace", (0.6, 0.2, 0.9, 0.7), "place")
    assert rug.placement.source == "default" and rug.placement.box is None
    assert "in the place of the existing sofa" in inputs.default_placement(items[0]).text


def test_parse_place_rejects_malformed_values() -> None:
    assert inputs.parse_place(["a b=c d"]) == {"a-b": "c d"}
    for bad in ("nothing", "=x", "x="):
        with pytest.raises(InputError):
            inputs.parse_place([bad])


def test_box_mask_is_a_same_size_rgba_png_transparent_inside_and_opaque_outside() -> None:
    mask = inputs.build_box_mask((200, 100), [(0.25, 0.25, 0.75, 0.75)])
    with Image.open(io.BytesIO(mask)) as im:
        assert im.format == "PNG" and im.mode == "RGBA" and im.size == (200, 100)
        assert im.getpixel((100, 50))[3] == 0  # inside: editable
        assert im.getpixel((5, 5))[3] == 255 and im.getpixel((195, 95))[3] == 255  # outside


def test_outside_change_signal() -> None:
    before = _png((200, 100), (100, 100, 100))
    box = [(0.0, 0.0, 0.5, 1.0)]
    same = inputs.outside_change(before, before, box)
    assert same == 0.0
    inside_only = Image.new("RGB", (200, 100), (100, 100, 100))
    inside_only.paste(Image.new("RGB", (100, 100), (250, 0, 0)), (0, 0))
    buf = io.BytesIO()
    inside_only.save(buf, format="PNG")
    assert inputs.outside_change(before, buf.getvalue(), box) == 0.0
    changed_all = _png((200, 100), (200, 100, 100))
    signal = inputs.outside_change(before, changed_all, box)
    assert signal is not None and 0.05 < signal < 0.2
    assert inputs.outside_change(before, before, []) is None


def test_boxed_mask_needs_a_box_for_every_item(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="needs a box"):
        run_render(
            _args(tmp_path, approach="gpt", mask="boxed"),
            MockTransport(),
            _cfg(),
            env={},
            run_id="m",
        )


def test_boxed_mask_is_sent_to_gpt_as_a_png_with_the_room_as_png(tmp_path: Path) -> None:
    transport = MockTransport()
    run = run_render(_args(tmp_path, approach="gpt", mask="boxed", locate=True),
                     transport, _cfg(), env={}, run_id="boxed")  # fmt: skip
    (req,) = transport.requests
    assert req.mask is not None and req.images[0].mime == "image/png"
    with Image.open(io.BytesIO(req.mask)) as m, Image.open(io.BytesIO(req.images[0].data)) as r:
        assert m.size == r.size and m.mode == "RGBA"
    assert run.result["calls"][1]["outside_change"] is not None
    assert [c["kind"] for c in run.result["calls"]] == ["locate", "render"]
    assert run.result["totals"]["anthropic_usd"] > 0


# ---- instruction --------------------------------------------------------------------------------


def test_instruction_variants_sizes_and_room_confidence() -> None:
    sofa = _item("sofa", "sofa")
    from app.schemas import DimensionsM

    sofa.dims = DimensionsM(width_m=2.032, depth_m=0.8636, height_m=0.762)
    sofa.placement = Placement("on the right wall", None, "place")
    room = {"width_m": 6.0, "length_m": 5.0, "ceiling_height_m": 2.6, "confidence": 0.35}
    single = instruction.build_instruction([sofa], room, single=True)
    assert "Image 2 is one specific" in single
    assert "2.03 m (80 in) wide, 0.86 m (34 in) deep, 0.76 m (30 in) high" in single
    assert "low confidence" in single and "Replace the existing sofa, on the right wall" in single
    both = instruction.build_instruction([sofa, _item("lamp")], room, single=False)
    assert "Each following image is one specific" in both and "Image 3: lamp product" in both
    assert "Keep everything else identical" in both and "{items_block}" not in both
    with pytest.raises(ValueError):
        instruction.build_instruction([sofa, sofa], room, single=True)


# ---- runs ---------------------------------------------------------------------------------------


def test_mock_run_writes_outputs_and_a_safe_report(tmp_path: Path) -> None:
    run = run_render(_args(tmp_path, approach="gemini", mode="one-by-one", locate=True, judge=True),
                     MockTransport(), _cfg(), env={}, run_id="ok")  # fmt: skip
    assert run.ok and [s["step"] for s in run.result["steps"]] == [1, 2]
    for name in (
        "result.json",
        "index.html",
        "original.jpg",
        "render.png",
        "step-1.png",
        "step-2.png",
        "ref-sofa.jpg",
        "ref-bookshelf.jpg",
    ):
        assert (run.out_dir / name).is_file(), name
    html = (run.out_dir / "index.html").read_text()
    assert "<script" not in html and "Judge (signal only)" in html and "Steps (each edits" in html
    assert run.result["judge"]["label"].startswith("signal only")
    assert [c["kind"] for c in run.result["calls"]] == ["locate", "render", "render", "judge"]
    assert "?" not in json.dumps(run.result["calls"][1]["request"]["endpoint"])


def test_gemini_defaults_to_chained_calls_each_editing_the_previous_result(tmp_path: Path) -> None:
    transport = MockTransport()
    run_render(
        _args(tmp_path, approach="gemini", mode="one-by-one"), transport, _cfg(), env={}, run_id="c"
    )
    first, second = transport.requests
    assert [i.name for i in first.images][0] == "room.jpg"
    assert second.images[0].name == "previous-step.png" and len(second.images) == 2


def test_live_needs_a_verified_provider_and_a_key() -> None:
    gpt = specs.spec_for("gpt", {})
    reasons = live_refusals(gpt, {}, locate_or_judge=False)
    assert any("model id" in r for r in reasons) and any("price per image" in r for r in reasons)
    assert any("OPENAI_API_KEY" in r for r in reasons)
    ready = {"OPENAI_API_KEY": FAKE_KEY, "RENDER_OPENAI_MODEL": "some-id",
             "RENDER_OPENAI_USD_PER_IMAGE": "0.1"}  # fmt: skip
    assert live_refusals(specs.spec_for("gpt", ready), ready, locate_or_judge=False) == []
    decor = live_refusals(
        specs.spec_for("decor8", {}), {"DECOR8_API_KEY": FAKE_KEY}, locate_or_judge=False
    )
    assert len(decor) == 4 and all(r.startswith("unverified") for r in decor)
    gem = {"GEMINI_API_KEY": FAKE_KEY}
    assert live_refusals(specs.spec_for("gemini", gem), gem, locate_or_judge=False) == []
    assert "ANTHROPIC_API_KEY" in " ".join(
        live_refusals(specs.spec_for("gemini", gem), gem, locate_or_judge=True)
    )


def test_a_live_refusal_happens_before_any_input_or_call(tmp_path: Path) -> None:
    transport = MockTransport()
    with pytest.raises(InputError, match="live run refused"):
        run_render(
            _args(tmp_path, approach="decor8", live=True), transport, _cfg(), env={}, run_id="x"
        )
    assert transport.requests == []


def test_a_live_run_without_product_images_is_refused(tmp_path: Path) -> None:
    env = {"GEMINI_API_KEY": FAKE_KEY}
    with pytest.raises(InputError, match="missing product image for 'sofa'"):
        run_render(_args(tmp_path, approach="gemini", live=True, max_usd=3.0), MockTransport(), _cfg(),
                   env=env, run_id="x")  # fmt: skip


def test_a_live_run_with_images_prints_the_plan_first_then_calls(tmp_path: Path) -> None:
    (tmp_path / "render-inputs").mkdir()
    for slot in ("sofa", "bookshelf"):
        (tmp_path / "render-inputs" / f"{slot}.png").write_bytes(_png())
    order: list[str] = []

    class Spy(MockTransport):
        def send(self, req: Any) -> ProviderResult:
            order.append("call")
            return super().send(req)

    run_render(_args(tmp_path, approach="gemini", live=True, mode="one-by-one"), Spy(), _cfg(),
               env={"GEMINI_API_KEY": FAKE_KEY}, run_id="live-ish", on_plan=lambda p: order.append("plan"))  # fmt: skip
    assert order == ["plan", "call", "call"]


def test_render_report_escapes_hostile_strings(tmp_path: Path) -> None:
    evil = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    result = {
        "run_id": evil, "source_run": evil, "style": evil, "approach": evil, "model": evil, "mode": evil,
        "quality": evil, "mask": evil, "size": evil, "prompt_version": evil, "notes": [evil],
        "unverified": [evil], "error": evil, "totals": {},
        "items": [{"slot": evil, "name": evil, "seller": evil, "size_text": evil,
                   "reference_image": "../../etc/passwd", "placement": {"text": evil, "source": evil}}],
        "calls": [{"kind": evil, "provider": evil, "instruction": evil, "error": evil,
                   "request": {"x": evil}, "request_id": evil}],
        "judge": {"items": {evil: {"look_alike": 1, "notes": evil}}, "room_notes": evil},
        "steps": [{"step": 1, "items": [evil], "image": "../../x.png"}, {"step": 2, "items": [], "image": "y.png"}],
    }  # fmt: skip
    html = build_render_page(result, tmp_path)
    assert "<script" not in html and "<img src=x" not in html and "&lt;script&gt;" in html
    assert "../../" not in html.replace("&#x2F;", "/") or "alt=" in html
    assert "Content-Security-Policy" in html and "default-src &#x27;none&#x27;" in html
    assert "https://" not in html and "http://" not in html


def test_the_script_runs_offline_and_refuses_live_without_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    for key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "DECOR8_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    run = make_run(tmp_path)
    spec = importlib.util.spec_from_file_location(
        "render_cli", Path(__file__).resolve().parents[1] / "scripts" / "render_spike.py"
    )
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main(["--from-run", str(run), "--approach", "gemini", "--judge"]) == 0
    out = capsys.readouterr().out
    assert "MOCK (offline) render plan" in out and "estimated total" in out and "open " in out
    (out_dir,) = list((tmp_path / "render-output").iterdir())
    assert (out_dir / "index.html").is_file()
    assert cli.main(["--from-run", str(run), "--approach", "gemini", "--live"]) == 2
    assert "GEMINI_API_KEY is not set" in capsys.readouterr().err
    assert cli.main(["--from-run", str(run), "--approach", "gpt", "--max-usd", "0.001"]) == 2
