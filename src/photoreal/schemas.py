"""Structured outputs of the two Claude vision helpers (locate and judge)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BoxFractions(_Strict):
    x0: float = Field(description="Left edge, 0 to 1 of the image width")
    y0: float = Field(description="Top edge, 0 to 1 of the image height")
    x1: float = Field(description="Right edge, 0 to 1 of the image width")
    y1: float = Field(description="Bottom edge, 0 to 1 of the image height")


class LocatedItem(_Strict):
    slot: str
    placement: str = Field(description="A short phrase pointing at the place")
    box: BoxFractions


class RenderLocateOutput(_Strict):
    items: list[LocatedItem]


class JudgedItem(_Strict):
    slot: str
    look_alike: int = Field(description="1 to 5")
    notes: str


class RenderJudgeOutput(_Strict):
    items: list[JudgedItem]
    rest_of_room_changed: bool
    room_notes: str
