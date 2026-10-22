from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from app import llm
from app.config import Config
from app.llm import Budget
from app.mock_llm import (
    MOCK_URL_FOUND,
    ScriptedClient,
    mock_perception_payload,
    response,
    search_blocks,
    text_block,
)
from app.retrieval import derive_slots, retrieve, verify_candidates
from app.schemas import Candidate, RoomPerception


def _web_config() -> Config:
    return Config(retrieval_mode="web_search")


def _perception(**overrides: Any) -> RoomPerception:
    payload = copy.deepcopy(mock_perception_payload())
    payload.update(overrides)
    return RoomPerception.model_validate(payload)


def _cand(url: str) -> Candidate:
    return Candidate(name="x", retailer="r", price=1.0, currency="USD", url=url)


def test_unverified_url_is_dropped() -> None:
    kept, dropped = verify_candidates(
        [_cand("https://shop.example/a"), _cand("https://invented.example/b")],
        {"https://shop.example/a"},
    )
    assert [c.url for c in kept] == ["https://shop.example/a"]
    assert dropped == [
        {
            "name": "x",
            "url": "https://invented.example/b",
            "reason": "url_not_in_search_results",
        }
    ]


def test_url_match_ignores_only_fragment_and_trailing_slash() -> None:
    kept, dropped = verify_candidates(
        [_cand("https://s.example/p/1/#reviews"), _cand("https://s.example/p/1?color=red")],
        {"https://s.example/p/1"},
    )
    assert len(kept) == 1 and len(dropped) == 1  # a different query string is a different URL


def test_no_search_results_means_everything_is_dropped() -> None:
    kept, dropped = verify_candidates([_cand("https://a.example/")], set())
    assert kept == [] and len(dropped) == 1


def test_slots_replaceable_first_then_gaps_and_never_keepable() -> None:
    slots = derive_slots(_perception(), 900, 5)
    assert [s.category for s in slots] == ["sofa", "coffee table", "floor lamp"]
    assert [s.source for s in slots] == ["replaceable", "gap", "gap"]
    assert "armchair" not in [s.category for s in slots]  # marked keepable
    assert sum(s.budget_usd for s in slots) == pytest.approx(900)


def test_slots_are_capped_at_n() -> None:
    assert len(derive_slots(_perception(), 900, 2)) == 2
    assert derive_slots(_perception(), 900, 0) == []


def test_slots_fall_back_to_room_type_defaults_when_nothing_flagged() -> None:
    p = _perception(furniture_gaps=[], furniture=[], room_type="Cosy bedroom")
    slots = derive_slots(p, 600, 3)
    assert [s.category for s in slots] == ["bed frame", "nightstand", "dresser"]
    assert {s.source for s in slots} == {"default"}


def test_default_slots_skip_what_the_room_already_has() -> None:
    p = _perception(furniture_gaps=[], room_type="living room")
    keep_sofa = p.furniture[0].model_copy(update={"keepable": "likely"})
    p = p.model_copy(update={"furniture": [keep_sofa]})
    assert "sofa" not in [s.category for s in derive_slots(p, 600, 3)]


def _candidates_payload(*urls: str) -> dict[str, Any]:
    return {
        "slot": "sofa",
        "candidates": [
            {"name": f"s{i}", "retailer": "R", "price": 10.0 + i, "currency": "USD", "url": u}
            for i, u in enumerate(urls)
        ],
        "note": "",
    }


def test_retrieve_drops_hallucinated_url_and_logs_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ScriptedClient(
        [
            response(
                [
                    *search_blocks("grey sofa under 300", ["https://shop.example/real"]),
                    text_block(
                        _candidates_payload(
                            "https://shop.example/real", "https://from-memory.example/sofa"
                        )
                    ),
                ],
                searches=1,
            )
        ]
    )
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    perception = _perception(furniture_gaps=[], furniture=[], room_type="sofa room")
    result = retrieve(
        perception, budget_usd=300, style="modern", max_slots=1, budget=Budget(_web_config())
    )
    (slot,) = result.slots
    assert [c.url for c in slot.candidates] == ["https://shop.example/real"]
    assert len(slot.dropped) == 1
    assert slot.completion.searches[0] == {"query": "grey sofa under 300"}
    # the request carried the search tool, capped by config, and the configured tool type
    (call,) = client.calls
    (tool,) = call["tools"]
    assert tool["max_uses"] == _web_config().max_searches_per_slot
    assert tool["type"] == _web_config().web_search_tool


def test_first_text_block_is_the_json_not_a_join(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ScriptedClient(
        [
            response(
                [
                    text_block(_candidates_payload(MOCK_URL_FOUND)),
                    text_block("Here are some thoughts after the JSON."),
                ]
            )
        ]
    )
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    result = retrieve(
        _perception(), budget_usd=100, style="x", max_slots=1, budget=Budget(_web_config())
    )
    assert json.loads(result.slots[0].completion.text)["slot"] == "sofa"
