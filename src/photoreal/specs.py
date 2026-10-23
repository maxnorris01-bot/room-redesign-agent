"""What each render provider needs, what was verified against its docs, and what it costs (assumed).

Facts were checked on 2026-10-23 against each vendor's own documentation pages (see
docs/sessions/2026-10-23-photo-real-spike.md). Anything not verified is listed in `unverified` and
blocks live calls until the owner supplies the missing value explicitly (env var or flag).
Prices are ASSUMPTIONS to check on each vendor dashboard before a live run.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

APPROACHES = ("gpt", "gemini", "decor8")
KEY_ENV = {"gpt": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY", "decor8": "DECOR8_API_KEY"}

# ---- Gemini (verified: model id, endpoint, header, body shape, 6 object images, $0.134 per image) --
GEMINI_MODEL = "gemini-3-pro-image"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_MAX_OBJECT_IMAGES = 6  # "up to 6 images of objects" for Gemini 3 Pro Image
GEMINI_USD_PER_IMAGE = 0.134  # standard tier, 1K/2K output
GEMINI_USD_PER_INPUT_IMAGE = 0.0011

# ---- OpenAI (UNVERIFIED: the docs list gpt-image-2.5-sunburst and gpt-image-2.5-flare, not "gpt-image-2") --
GPT_MODEL_DEFAULT = "gpt-image-2"  # from the brief; NOT listed in OpenAI's docs on 2026-10-23
GPT_ENDPOINT = "https://api.openai.com/v1/images/edits"  # not confirmed from the reference page
GPT_MAX_INPUT_IMAGES = 15  # from the brief; the docs do not state a maximum
GPT_TIMEOUT_S = 600.0
# Assumed per-image prices (the docs list none): gpt-image-1 era list prices, may be far off.
GPT_USD_PER_IMAGE_ASSUMED = {"low": 0.016, "medium": 0.063, "high": 0.25}
GPT_QUALITIES = ("low", "medium", "high")

# ---- Decor8 (UNVERIFIED: stub) --------------------------------------------------------------------
DECOR8_ENDPOINT_HOST = "api.decor8.ai"
DECOR8_USD_PER_CALL_ASSUMED = 0.20  # no price documented

# ---- Anthropic helper calls (locate, judge) ---------------------------------------------------------
ANTHROPIC_VISION_CALL_USD_ASSUMED = 0.01  # one Haiku vision call with a few images

GEMINI_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class ProviderSpec:
    approach: str
    model: str
    max_input_images: int
    timeout_s: float
    unverified: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)


def spec_for(
    approach: str, env: Mapping[str, str] | None = None, model: str | None = None
) -> ProviderSpec:
    env = os.environ if env is None else env
    if approach == "gemini":
        return ProviderSpec(
            "gemini",
            model or env.get("RENDER_GEMINI_MODEL") or GEMINI_MODEL,
            GEMINI_MAX_OBJECT_IMAGES + 1,  # the room photo plus up to 6 object images
            GEMINI_TIMEOUT_S,
            notes=(
                "REST response nesting is only partly documented: the image is read from "
                "output_image.data (top level or under 'interaction'); no usage field is documented",
                "no image size option is documented for this endpoint: the model's default is used",
                "no mask input is documented: --mask is ignored for this approach",
            ),
        )
    if approach == "gpt":
        chosen = model or env.get("RENDER_OPENAI_MODEL")
        has_price = bool(env.get("RENDER_OPENAI_USD_PER_IMAGE"))
        blockers: list[str] = []
        if not chosen:
            blockers.append(
                "the model id: OpenAI's docs list gpt-image-2.5-sunburst and gpt-image-2.5-flare, "
                "not gpt-image-2; set RENDER_OPENAI_MODEL (or --model) to the id you want to test"
            )
        if not has_price:
            blockers.append(
                "the price per image: no price is published on the pages I could read; set "
                "RENDER_OPENAI_USD_PER_IMAGE so the cap uses your number"
            )
        return ProviderSpec(
            "gpt",
            chosen or GPT_MODEL_DEFAULT,
            GPT_MAX_INPUT_IMAGES,
            GPT_TIMEOUT_S,
            unverified=tuple(blockers),
            notes=(
                "not verified, but not blocking: the maximum number of input images (the brief says "
                "15; the docs do not state one) and the edits endpoint's multipart field names (the "
                "reference page could not be read; they follow the earlier gpt-image documentation)",
                "verified: custom sizes (multiples of 16, aspect 1:3 to 3:1, edges up to 3840), "
                "quality values, alpha-channel mask, image and mask the same format and size",
            ),
        )
    if approach == "decor8":
        return ProviderSpec(
            "decor8",
            "decor8-staging",
            1,
            GEMINI_TIMEOUT_S,
            unverified=(
                "the official SDK documentation lists /generate_designs_for_room (with decor_items "
                "and mask_info) but no remove-objects or furniture-replace endpoint, so the "
                "remove-then-place test cannot be built honestly",
                "decor_items and mask_info are described only as strings; their format is not documented",
                "input_image_url takes a URL only: a local room photo cannot be supplied without "
                "uploading it to a public host, which this spike does not do",
                "no price is documented",
            ),
        )
    raise ValueError(f"unknown approach {approach!r}")


def gpt_price_per_image(quality: str, env: Mapping[str, str] | None = None) -> tuple[float, str]:
    """(usd, basis): the owner's override if set, else the assumed list price."""
    env = os.environ if env is None else env
    override = env.get("RENDER_OPENAI_USD_PER_IMAGE")
    if override:
        try:
            return float(override), "RENDER_OPENAI_USD_PER_IMAGE (owner-supplied)"
        except ValueError:
            pass
    return GPT_USD_PER_IMAGE_ASSUMED[quality], "assumed (gpt-image-1 era list price, unverified)"


def estimate_call_usd(
    approach: str, *, n_input_images: int, quality: str, env: Mapping[str, str] | None = None
) -> tuple[float, str]:
    """Estimated dollars for one render call and the basis of the number."""
    if approach == "gemini":
        usd = GEMINI_USD_PER_IMAGE + GEMINI_USD_PER_INPUT_IMAGE * n_input_images
        return round(usd, 4), "Google pricing page, standard tier (checked 2026-10-23)"
    if approach == "gpt":
        return gpt_price_per_image(quality, env)
    if approach == "decor8":
        return DECOR8_USD_PER_CALL_ASSUMED, "assumed (no price documented)"
    raise ValueError(f"unknown approach {approach!r}")
