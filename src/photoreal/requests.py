"""Provider request building and the HTTP transport. Keys are added only at send time.

A `ProviderRequest` holds everything a call needs except the key: it is what gets logged (with the
images reduced to names and sizes). The HTTP layer turns it into a multipart (OpenAI) or JSON
(Gemini) request, and errors name the provider and the HTTP status only: never a URL or a key.
"""

from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx2 as httpx
from PIL import Image, ImageDraw

from photoreal.specs import GEMINI_ENDPOINT, GPT_ENDPOINT


class RenderError(RuntimeError):
    """A render call failed. The message never contains a URL, a key or a response body."""


@dataclass(frozen=True)
class ImagePart:
    name: str
    data: bytes
    mime: str


@dataclass
class ProviderRequest:
    provider: str  # "gpt" | "gemini" | "decor8"
    model: str
    text: str
    images: list[ImagePart]  # images[0] is the room (or the previous step's result)
    mask: bytes | None = None
    params: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 300.0

    @property
    def endpoint(self) -> str:
        return {"gpt": GPT_ENDPOINT, "gemini": GEMINI_ENDPOINT}.get(self.provider, "")

    def log_view(self) -> dict[str, Any]:
        """The request as it may be logged: no pixels, no key, no URL query."""
        parts = urlsplit(self.endpoint)
        return {
            "provider": self.provider,
            "model": self.model,
            "endpoint": f"{parts.netloc}{parts.path}",
            "params": self.params,
            "images": [{"name": i.name, "bytes": len(i.data), "mime": i.mime} for i in self.images],
            "mask_bytes": len(self.mask) if self.mask else 0,
            "timeout_s": self.timeout_s,
        }


@dataclass
class ProviderResult:
    image: bytes
    seconds: float
    request_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    reported_cost_usd: float | None = None


class Transport(Protocol):
    def send(self, req: ProviderRequest) -> ProviderResult: ...


# ---- builders ---------------------------------------------------------------------------------


def build_gpt_request(
    text: str,
    images: list[ImagePart],
    *,
    model: str,
    size: str,
    quality: str,
    mask: bytes | None = None,
    timeout_s: float = 600.0,
) -> ProviderRequest:
    """OpenAI image edit: the room first, then one image per product, in prompt order; an optional
    PNG mask with an alpha channel (transparent = may be edited) the same size as image 1."""
    return ProviderRequest(
        "gpt", model, text, images, mask,
        {"size": size, "quality": quality, "n": 1, "output_format": "png"},
        timeout_s,
    )  # fmt: skip


def build_gemini_request(
    text: str, images: list[ImagePart], *, model: str, timeout_s: float = 300.0
) -> ProviderRequest:
    """Gemini Interactions request: the instruction, then the room (or the previous result), then
    the product reference images."""
    return ProviderRequest("gemini", model, text, images, None, {}, timeout_s)


def gemini_body(req: ProviderRequest) -> dict[str, Any]:
    return {
        "model": req.model,
        "input": [
            {"type": "text", "text": req.text},
            *(
                {
                    "type": "image",
                    "mime_type": img.mime,
                    "data": base64.b64encode(img.data).decode("ascii"),
                }
                for img in req.images
            ),
        ],
    }


def gpt_form(
    req: ProviderRequest,
) -> tuple[dict[str, str], list[tuple[str, tuple[str, bytes, str]]]]:
    data = {"model": req.model, "prompt": req.text, **{k: str(v) for k, v in req.params.items()}}
    files = [("image[]", (img.name, img.data, img.mime)) for img in req.images]
    if req.mask is not None:
        files.append(("mask", ("mask.png", req.mask, "image/png")))
    return data, files


# ---- live transport ---------------------------------------------------------------------------


def find_gemini_image(body: Any) -> str | None:
    """The base64 image in a Gemini response: output_image.data at the top level or under
    'interaction'; failing that, the first image entry in an 'outputs' list."""
    if not isinstance(body, dict):
        return None
    for root in (body, body.get("interaction")):
        if isinstance(root, dict):
            out = root.get("output_image")
            if isinstance(out, dict) and isinstance(out.get("data"), str):
                return str(out["data"])
            outputs = root.get("outputs")
            if isinstance(outputs, list):
                for o in outputs:
                    if (
                        isinstance(o, dict)
                        and o.get("type") == "image"
                        and isinstance(o.get("data"), str)
                    ):
                        return str(o["data"])
    return None


class HttpTransport:
    """Sends a request with the provider's key. The key never leaves this object."""

    def __init__(self, keys: dict[str, str], client: httpx.Client | None = None) -> None:
        self._keys = dict(keys)
        self._client = client or httpx.Client(timeout=600.0)

    def __repr__(self) -> str:
        return "HttpTransport()"

    def send(self, req: ProviderRequest) -> ProviderResult:
        key = self._keys.get(req.provider, "")
        if not key:
            raise RenderError(f"{req.provider}: API key is not set")
        started = time.perf_counter()
        try:
            if req.provider == "gpt":
                data, files = gpt_form(req)
                resp = self._client.post(
                    req.endpoint, headers={"Authorization": f"Bearer {key}"}, data=data,
                    files=files, timeout=req.timeout_s,
                )  # fmt: skip
            elif req.provider == "gemini":
                resp = self._client.post(
                    req.endpoint, headers={"x-goog-api-key": key}, json=gemini_body(req),
                    timeout=req.timeout_s,
                )  # fmt: skip
            else:
                raise RenderError(f"{req.provider}: live calls are not implemented (unverified)")
        except httpx.TimeoutException:
            raise RenderError(f"{req.provider}: request timed out") from None
        except httpx.HTTPError as exc:
            raise RenderError(f"{req.provider}: request failed ({type(exc).__name__})") from None
        seconds = time.perf_counter() - started
        if resp.status_code >= 400:
            raise RenderError(f"{req.provider}: HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise RenderError(f"{req.provider}: response was not valid JSON") from None
        request_id = resp.headers.get("x-request-id") or resp.headers.get("x-goog-request-id")
        usage: dict[str, Any] = {}
        if req.provider == "gpt":
            items = body.get("data") if isinstance(body, dict) else None
            b64 = items[0].get("b64_json") if isinstance(items, list) and items else None
            if isinstance(body, dict) and isinstance(body.get("usage"), dict):
                usage = body["usage"]
        else:
            b64 = find_gemini_image(body)
            if isinstance(body, dict) and isinstance(body.get("usage"), dict):
                usage = body["usage"]
        if not isinstance(b64, str):
            raise RenderError(f"{req.provider}: no image in the response")
        try:
            image = base64.b64decode(b64, validate=False)
        except ValueError:
            raise RenderError(f"{req.provider}: image data could not be decoded") from None
        return ProviderResult(image, seconds, request_id, usage, None)


# ---- mock transport ---------------------------------------------------------------------------


class MockTransport:
    """Offline stand-in: returns the input room image with a banner, deterministically."""

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    def send(self, req: ProviderRequest) -> ProviderResult:
        self.requests.append(req)
        with Image.open(io.BytesIO(req.images[0].data)) as room:
            img = room.convert("RGB")
        draw = ImageDraw.Draw(img, "RGBA")
        h = max(24, img.height // 12)
        draw.rectangle((0, img.height - h, img.width, img.height), fill=(180, 83, 42, 200))
        draw.text(
            (8, img.height - h + h // 3),
            f"MOCK RENDER {req.provider} | {len(req.images) - 1} product image(s)",
            fill=(255, 255, 255, 255),
        )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return ProviderResult(buf.getvalue(), 0.0, f"mock-{len(self.requests)}", {}, None)
