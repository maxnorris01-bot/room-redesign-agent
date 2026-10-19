from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from app.mock_llm import mock_perception_payload
from app.schemas import (
    PERCEPTION_SCHEMA_VERSION,
    RoomPerception,
    SchemaError,
    SlotCandidates,
    parse_model,
    to_output_schema,
)


def _parse(payload: dict[str, Any]) -> RoomPerception:
    return parse_model(RoomPerception, json.dumps(payload), what="perception")


def test_valid_perception_parses() -> None:
    p = _parse(mock_perception_payload())
    assert p.schema_version == PERCEPTION_SCHEMA_VERSION == 1
    assert p.living_things[0].kind == "pet"
    assert {f.keepable for f in p.furniture} == {"likely", "unlikely"}


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda d: d.update(schema_version=2), "schema_version"),
        (lambda d: d["dimensions"].update(confidence=1.5), "confidence"),
        (lambda d: d["furniture"][0]["position"].update(x=-0.1), "position"),
        (lambda d: d["furniture"][0]["size"].update(width_m=0), "size"),
        (lambda d: d["furniture"][0].update(keepable="maybe"), "keepable"),
        (lambda d: d["living_things"][0].update(kind="sofa"), "kind"),
        (lambda d: d["openings"][0].update(wall="up"), "wall"),
        (lambda d: d.update(surprise="field"), "surprise"),
        (lambda d: d.pop("uncertainties"), "uncertainties"),
    ],
)
def test_invalid_perception_is_rejected(mutate: Any, fragment: str) -> None:
    payload = copy.deepcopy(mock_perception_payload())
    mutate(payload)
    with pytest.raises(SchemaError, match=fragment):
        _parse(payload)


def test_non_json_is_a_schema_error() -> None:
    with pytest.raises(SchemaError, match="not valid JSON"):
        parse_model(RoomPerception, "Sure! Here is the room:", what="perception")


def _walk(node: Any) -> Any:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


@pytest.mark.parametrize("model", [RoomPerception, SlotCandidates])
def test_output_schema_has_no_unreliable_keywords_or_refs(model: type) -> None:
    schema = to_output_schema(model)
    text = json.dumps(schema)
    for banned in ("maxItems", "maxLength", "$ref", "$defs", "minimum", "maximum"):
        assert banned not in text
    assert schema["title"] == model.__name__
    for node in _walk(schema):
        if node.get("type") == "object" and "properties" in node:
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])


def test_living_things_are_not_in_furniture_schema() -> None:
    schema = to_output_schema(RoomPerception)
    furniture_kinds = schema["properties"]["furniture"]["items"]["properties"]
    assert "kind" not in furniture_kinds  # pets/people only exist under living_things
    assert "living_things" in schema["properties"]
