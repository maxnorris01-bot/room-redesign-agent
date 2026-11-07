"""Filters B1: every shown result carries `attrs` and the batch carries `facets` (fakes only).

Title matching, word boundaries, colour (photo tag, vision call, title fallback), the facet rules,
`facets_unknown`, a new batch recomputing, an old saved result without attrs, and the order of the
results being exactly what it was before the change."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.llm import Budget, Completion
from app.shop import colortags as ct
from app.shop import search as shop_search
from app.shop.facets import ATTR_DIMENSIONS, compute_facets, title_attrs, title_colors
from app.shop.phototags import ThumbCache
from test_shop import (
    FakeSearch,
    _loopback_only,  # noqa: F401  (the autouse guard must apply here)
    cand,
    make_service,
)
from test_shop_stage2p_typed import FakeThumbs

# ---- title matching ----------------------------------------------------------------------------


def test_title_reads_material_style_and_colour_and_nothing_from_the_word_rug() -> None:
    a = title_attrs("Persian Vintage Brown Cotton Area Rug 5x8")
    assert a["style"] == ["vintage"]
    assert a["color"] == ["brown"]
    assert a["material"] == ["cotton"]
    assert title_attrs("Rug") == {d: [] for d in ATTR_DIMENSIONS}


def test_word_boundaries() -> None:
    assert title_attrs("Cottonwood Side Table")["material"] == []
    assert title_attrs("Wooden Bench")["material"] == ["wood"]
    assert title_attrs("Woodland Print Cushion")["material"] == []
    assert title_attrs("Squares Pattern Throw")["shape"] == []  # "squares" is not "square"


def test_longest_synonym_wins_and_is_not_matched_twice() -> None:
    assert title_attrs("Faux Leather Sofa")["material"] == ["faux-leather"]
    assert title_attrs("Genuine Leather Sofa")["material"] == ["leather"]
    assert title_attrs("Machine Washable Rug")["feature"] == ["washable"]
    assert title_attrs("Stainless Steel Lamp")["material"] == ["metal"]


def test_several_values_in_one_dimension_in_title_order() -> None:
    a = title_attrs("Gray Blue Wool Cotton Blend Rug Washable Non-Slip")
    assert a["color"] == ["gray", "blue"]
    assert a["material"] == ["wool", "cotton"]
    assert a["feature"] == ["washable", "non-slip"]


def test_grey_is_gray_and_a_pattern_word_is_multicolor() -> None:
    assert title_colors("Grey Shag Rug") == ["gray"]
    assert title_colors("Multicolor Patterned Rug")[0] == "multicolor"


def test_no_price_words_in_the_vocabulary_values() -> None:
    a = title_attrs("Cheap Luxe Gold Modern Rug Under $100")
    for values in a.values():
        assert not any("$" in v or "price" in v or "cheap" in v for v in values)


# ---- colour: photo tag, vision call, title -------------------------------------------------


def _cache() -> ThumbCache:
    return ThumbCache(FakeThumbs(), Config(llm_mode="mock", tracing_disabled=True))


def _shown(n: int, title: str = "Plain Rug {i}") -> list[tuple[str, str, str]]:
    return [(f"r{i}", title.format(i=i), f"https://img.example/{i}.jpg") for i in range(n)]


def test_a_photo_tag_colour_is_used_and_needs_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(ct, "complete", lambda **kw: calls.append(1))
    cfg = Config(llm_mode="mock", tracing_disabled=True)
    rep = ct.shown_colors(
        _shown(2, "Persian Vintage Brown Rug {i}"), {"r0": "navy_dark_blue", "r1": "gray"},
        _cache(), Budget(cfg), cfg, 10.0,
    )  # fmt: skip
    assert rep.colors == {"r0": ["navy"], "r1": ["gray"]} and rep.from_tags == 2 and not calls


def test_untagged_results_get_the_vision_call_and_a_room_scene_answer_is_cleaned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake(**kw: Any) -> Completion:
        seen["prompt"] = kw["prompt"].text
        seen["images"] = len(kw["images"])
        text = (
            '{"items":[{"index":1,"colors":["Grey","blue","bogus","red"]},'
            '{"index":2,"colors":["multicolor","green","pink","red"]},'
            '{"index":3,"colors":[]}]}'
        )
        return Completion(text=text, cost_usd=0.001)

    monkeypatch.setattr(ct, "complete", fake)
    cfg = Config(llm_mode="mock", tracing_disabled=True)
    rep = ct.shown_colors(_shown(3, "Rug {i}"), {}, _cache(), Budget(cfg), cfg, 10.0)
    assert rep.colors["r0"] == ["gray", "blue", "red"]
    assert rep.colors["r1"] == ["multicolor", "green", "pink"]  # multicolor plus two
    assert rep.colors["r2"] == [] and rep.from_vision == 2 and seen["images"] == 3
    assert "PRODUCT" in seen["prompt"] and "room" in seen["prompt"]  # the product, not the room


def test_a_failed_call_falls_back_to_title_words(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kw: Any) -> Completion:
        raise RuntimeError("down")

    monkeypatch.setattr(ct, "complete", boom)
    cfg = Config(llm_mode="mock", tracing_disabled=True)
    rep = ct.shown_colors(_shown(2, "Navy Wool Rug {i}"), {}, _cache(), Budget(cfg), cfg, 10.0)
    assert rep.colors == {"r0": ["navy"], "r1": ["navy"]}
    assert rep.failed_calls == 1 and rep.from_title == 2


def test_a_slow_call_is_cut_at_the_cap_and_title_words_stand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = threading.Event()

    def slow(**kw: Any) -> Completion:
        gate.wait(5)
        return Completion(text='{"items":[{"index":1,"colors":["red"]}]}', cost_usd=0.0)

    monkeypatch.setattr(ct, "complete", slow)
    monkeypatch.setattr(ct, "COLOR_BUDGET_S", 1.2)
    cfg = Config(llm_mode="mock", tracing_disabled=True)
    t0 = time.monotonic()
    rep = ct.shown_colors(_shown(1, "Teal Rug"), {}, _cache(), Budget(cfg), cfg, 10.0)
    gate.set()
    assert time.monotonic() - t0 < 3.5
    assert rep.colors["r0"] == ["teal"] and "still running" in rep.reason


def test_no_photos_means_title_colours_without_a_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ct, "complete", lambda **kw: pytest.fail("no call expected"))
    cfg = Config(llm_mode="mock", tracing_disabled=True)
    rep = ct.shown_colors(_shown(1, "Ivory Rug"), {}, None, Budget(cfg), cfg, 10.0)
    assert rep.colors == {"r0": ["ivory"]}


# ---- facets ----------------------------------------------------------------------------------


def _a(**kw: list[str]) -> dict[str, list[str]]:
    return {**{d: [] for d in ATTR_DIMENSIONS}, **kw}


def test_facet_rules_counts_min_options_and_uniform_dimensions() -> None:
    items = [
        _a(color=["gray"], material=["wool"], style=["vintage"]),
        _a(color=["gray", "blue"], material=["wool"], style=["vintage"]),
        _a(color=["blue"], material=["wool"], style=["modern"]),
        _a(color=[], material=["wool"], shape=["round"]),
    ]
    facets, unknown = compute_facets(items)
    by = {f["dimension"]: f for f in facets}
    assert [(o["value"], o["count"]) for o in by["color"]["options"]] == [("gray", 2), ("blue", 2)]
    assert "material" not in by  # one option only (and every result has it)
    assert "shape" not in by  # a single option is not a filter
    assert [o["value"] for o in by["style"]["options"]] == ["vintage", "modern"]
    assert by["color"]["title"] == "Colour" and by["color"]["options"][0]["label"] == "Gray"
    assert unknown == {"color": 1, "material": 0, "shape": 3, "style": 1, "size": 4, "feature": 4}


def test_a_dimension_every_result_carries_in_full_is_hidden() -> None:
    items = [_a(color=["gray", "blue"]) for _ in range(3)]
    facets, unknown = compute_facets(items)
    assert facets == [] and unknown["color"] == 0


def test_options_get_icon_and_swatches_from_option_visual() -> None:
    items = [_a(color=["gray"], style=["mid-century"]), _a(color=["multicolor"], style=["modern"])]
    facets, _ = compute_facets(items)
    colour = {o["value"]: o for o in facets[0]["options"]}
    assert colour["gray"]["swatches"] == ["#8C8C8C"] and colour["gray"]["icon"] is None
    assert colour["multicolor"]["icon"] == "multicolor"
    style = next(f for f in facets if f["dimension"] == "style")
    labels = {o["label"]: o["icon"] for o in style["options"]}
    assert labels["Mid-century"] == "mid-century"


def test_at_most_ten_options_by_count_then_vocabulary_order() -> None:
    names = ["black", "white", "gray", "silver", "beige", "cream", "ivory", "tan", "brown"]
    names += ["walnut", "oak", "natural"]
    items = [_a(color=[n]) for n in names] + [_a(color=["oak"]), _a(color=["tan"])]
    (facet,), _ = compute_facets(items)
    values = [o["value"] for o in facet["options"]]
    assert len(values) == 10
    assert values[:2] == ["tan", "oak"]  # the two with count 2, vocabulary order
    assert values[2:5] == ["black", "white", "gray"]  # then the rest in vocabulary order


def test_dimensions_that_cover_more_results_come_first() -> None:
    items = [
        _a(color=["red"], style=["modern"], material=["wool"]),
        _a(color=["blue"], style=["boho"], material=["cotton"]),
        _a(style=["modern"], material=["wool"]),
        _a(style=["boho"], material=["cotton"]),
    ]
    facets, _ = compute_facets(items)
    assert [f["dimension"] for f in facets] == ["material", "style", "color"]


# ---- through the service ---------------------------------------------------------------------


TITLES = [
    "Gray Wool Rug Vintage 5x8",
    "Blue Cotton Washable Rug Modern 5x8",
    "Navy Jute Rug Round 6ft",
    "Cream Wool Shag Rug Scandinavian",
    "Black Cotton Runner Rug Modern",
    "Green Polyester Rug Boho Round",
]


def _pool() -> list[list[Any]]:
    return [[cand(f"p{i}", 60.0 + i, title=t) for i, t in enumerate(TITLES)]]


def _service(tmp_path: Path, pool: list[list[Any]] | None = None) -> Any:
    cfg = Config(llm_mode="mock", tracing_disabled=True, shop_photo_tag_top_n=0)
    search = FakeSearch(pool or _pool())  # type: ignore[arg-type]
    return make_service(tmp_path, search, cfg=cfg)


def test_the_job_result_carries_attrs_facets_and_unknown_counts(tmp_path: Path) -> None:
    view = _service(tmp_path).search(None, "rug")
    search = view["search"]
    assert search["results"], "the fake search should have produced results"
    for r in search["results"]:
        assert list(r["attrs"]) == list(ATTR_DIMENSIONS)
        assert all(v == v.lower() for vals in r["attrs"].values() for v in vals)
    facets = {f["dimension"]: f for f in search["facets"]}
    assert {"color", "material", "style"} <= set(facets)
    shown = {r["title"]: r["attrs"] for r in search["results"]}
    n_wool = sum(1 for a in shown.values() if "wool" in a["material"])
    assert {o["value"]: o["count"] for o in facets["material"]["options"]}["wool"] == n_wool
    assert set(search["facets_unknown"]) == set(ATTR_DIMENSIONS)


def test_a_new_batch_recomputes_its_own_facets(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    first = svc.search(None, "rug")
    sid = first["session_id"]
    other = [
        [
            cand(f"q{i}", 70.0 + i, title=t)
            for i, t in enumerate(["Red Silk Rug Boho", "Pink Silk Rug Glam", "Red Nylon Rug Glam"])
        ]
    ]
    svc.search_provider.pages = other
    second = svc.search(sid, "silk rug")["search"]
    assert {
        o["value"] for f in second["facets"] if f["dimension"] == "color" for o in f["options"]
    } == {"red", "pink"}
    assert second["facets"] != first["search"]["facets"]


def test_a_result_saved_before_attrs_existed_still_serialises(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    view = svc.search(None, "rug")
    sid = view["session_id"]
    with svc.store.transaction(sid) as session:
        for rid, shown in list(session.results.items()):
            session.results[rid] = shown.model_copy(update={"attrs": None})
    again = svc.session_view(sid)["search"]
    assert all(r["attrs"] for r in again["results"])
    assert again["facets"] == view["search"]["facets"]


def test_the_result_order_is_what_it_was_without_the_attrs_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_attrs = [
        r["id"] for r in _service(tmp_path / "a").search(None, "rug")["search"]["results"]
    ]
    monkeypatch.setattr(shop_search, "_attach_attrs", lambda results, *a, **k: {})
    without = [r["id"] for r in _service(tmp_path / "b").search(None, "rug")["search"]["results"]]
    assert with_attrs == without and len(with_attrs) >= 5
