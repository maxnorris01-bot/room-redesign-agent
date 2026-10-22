"""Typed data model for the pipeline. The perception output is the product's core data model.

`RoomPerception` will drive the 2D top-down canvas (ADR 0003) and the retrieval slots, so it holds
only what those need. It carries `schema_version` and any breaking change must bump it.

Coordinate convention (stated in prompts/perception.md too): a top-down plan of the room. Point
(0, 0) is the north-west corner, x grows east (right), y grows south (down); both are fractions of
the room's width / length in [0, 1]. The wall farthest from the camera is "north", the camera is
near the south wall, and the left of the photo is west. If the photo makes this ambiguous the model
says so under `uncertainties`.

Bounds (0..1 positions, positive sizes, confidence 0..1) are enforced here by pydantic, AFTER
parsing. They are deliberately not relied on in the JSON Schema sent to the API: `maxItems` is
rejected and `maxLength` is not enforced by structured output, so `to_output_schema` strips every
bounding keyword instead of pretending they work.
"""

from __future__ import annotations

import json
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PERCEPTION_SCHEMA_VERSION = 1

Wall = Literal["north", "east", "south", "west", "unknown"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Point(_Strict):
    x: float = Field(ge=0.0, le=1.0, description="Fraction of room width, 0 = west wall")
    y: float = Field(ge=0.0, le=1.0, description="Fraction of room length, 0 = north wall")


class Size(_Strict):
    width_m: float = Field(gt=0.0, description="Rough extent along x, metres")
    depth_m: float = Field(gt=0.0, description="Rough extent along y, metres")


class Dimensions(_Strict):
    width_m: float = Field(gt=0.0, description="East-west extent in metres")
    length_m: float = Field(gt=0.0, description="North-south extent in metres")
    ceiling_height_m: float = Field(gt=0.0)
    confidence: float = Field(ge=0.0, le=1.0, description="0 = guess, 1 = certain")
    derivation: str = Field(description="How the numbers were derived, e.g. 'door ~2m tall'")


class Opening(_Strict):
    kind: Literal["window", "door", "archway"]
    wall: Wall
    position_along_wall: float = Field(
        ge=0.0,
        le=1.0,
        description="Centre of the opening along its wall, west->east / north->south",
    )
    width_m: float = Field(gt=0.0)
    note: str


class FixedItem(_Strict):
    category: str = Field(description="e.g. radiator, fireplace, built-in shelving, counter")
    position: Point
    size: Size
    note: str


class FurnitureItem(_Strict):
    id: str = Field(description="Stable short id, f1, f2, ...")
    category: str = Field(description="e.g. sofa, armchair, coffee table, bed, desk")
    position: Point = Field(description="Centre of the item in the plan")
    size: Size
    keepable: Literal["likely", "unlikely", "unclear"] = Field(
        description="Whether it looks worth keeping: good condition and fits the room's style"
    )
    note: str


class FurnitureGap(_Strict):
    category: str = Field(description="Furniture a room like this normally has but this one lacks")
    reason: str


class LivingThing(_Strict):
    kind: Literal["pet", "person", "other"]
    description: str
    position: Point
    # Living things are never furniture, never replaced, never planned around as obstacles to
    # buy for. Kept as a separate list so downstream code cannot confuse the two.


class DecorItem(_Strict):
    kind: str = Field(description="e.g. wall art, rug, plant, curtains, lamp, mirror")
    location: str = Field(description="Where it is, e.g. 'north wall above sofa'")
    note: str


class RoomPerception(_Strict):
    schema_version: Literal[1]
    room_type: str = Field(description="e.g. living room, bedroom, home office")
    dimensions: Dimensions
    openings: list[Opening]
    fixed_items: list[FixedItem]
    furniture: list[FurnitureItem]
    furniture_gaps: list[FurnitureGap]
    living_things: list[LivingThing]
    art_and_decor: list[DecorItem]
    style_descriptors: list[str]
    uncertainties: list[str]


class Candidate(_Strict):
    name: str
    retailer: str
    price: float = Field(ge=0.0, description="Price shown on the product page")
    currency: str = Field(description="ISO 4217 code, e.g. USD")
    url: str = Field(description="Product page URL exactly as returned by the search tool")


class SlotCandidates(_Strict):
    slot: str
    candidates: list[Candidate]
    note: str = Field(description="Anything the reader should know, e.g. why few results")


class DimensionsM(_Strict):
    """Stated product dimensions in metres; any of them may be missing from the listing."""

    width_m: float | None = None
    depth_m: float | None = None
    height_m: float | None = None
    length_m: float | None = None


class ProductCandidate(Candidate):
    """A candidate as the pipeline reports it. Not sent to the API as a schema.

    `Candidate` stays the model-facing shape (web_search mode). In shopping mode the model never
    supplies price, seller or URL; they come from the provider. `url_kind` says what `url` is:
    a retailer's product page, the Google product panel (when no retailer URL could be obtained),
    or "none" (empty `url`; we never invent one).
    """

    url_kind: Literal["retailer_product", "google_panel", "none"] = "retailer_product"
    shipping: float | None = None
    total: float | None = None
    dimensions_m: DimensionsM | None = None
    rating: float | None = None
    rating_count: int | None = None
    reason: str = ""
    product_id: str = ""
    price_source: Literal["search", "lookup", "model"] = "model"


class Finalist(_Strict):
    id: str = Field(description="A product id copied from the candidate list")
    reason: str = Field(description="Why it fits, at most 12 words")


class RankedFinalists(_Strict):
    finalists: list[Finalist]


# ---- JSON Schema for structured output ---------------------------------------------------

# Keywords the API rejects (maxItems) or does not enforce (maxLength), plus numeric/size bounds
# that are validated by pydantic after parsing instead. Stripped so nothing pretends to rely
# on them.
_UNSUPPORTED = {
    "maxItems", "minItems", "maxLength", "minLength", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "default", "title",
}  # fmt: skip


def to_output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic -> a plain, $ref-free JSON Schema safe to send as `output_config.format`."""
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(defs[node["$ref"].split("/")[-1]])
        out = {k: walk(v) for k, v in node.items() if k not in _UNSUPPORTED and k != "$defs"}
        if out.get("type") == "object":
            out["additionalProperties"] = False
        return out

    schema: dict[str, Any] = walk(raw)
    schema["title"] = model.__name__  # root title only: the mock client routes on it
    return schema


class SchemaError(ValueError):
    """The model's JSON did not validate against the typed schema."""


M = TypeVar("M", bound=BaseModel)


def parse_model(model: type[M], text: str, *, what: str) -> M:
    """Parse the first text block's JSON into `model`; raise `SchemaError` with a short reason."""
    try:
        return model.model_validate(json.loads(text))
    except json.JSONDecodeError as exc:
        raise SchemaError(f"{what}: not valid JSON: {text[:200]!r}") from exc
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise SchemaError(
            f"{what}: {len(exc.errors())} validation error(s); first: {loc}: {first['msg']}"
        ) from exc
