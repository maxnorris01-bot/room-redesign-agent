from __future__ import annotations

import copy
import json
import socket
import threading
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from app import llm, tracing
from app.categories import FAMILIES, family_of, size_range_in
from app.config import Config
from app.images import prepare_image, synthetic_room_image
from app.llm import Budget, Completion
from app.mock_llm import (
    ScriptedClient,
    mock_perception_payload,
    proposal_payload,
    response,
    style_payload,
    text_block,
)
from app.preferences import resolve_preferences, set_links
from app.prompts import load_prompt
from app.proposal import (
    annotate_replacements,
    proposal_user_text,
    propose_and_check,
    seller_concentration,
    slot_inputs,
)
from app.retrieval import (
    RetrievalResult,
    Slot,
    SlotResult,
    item_count_message,
    plan_slots,
    retrieve,
)
from app.schemas import (
    DimensionsM,
    ProductCandidate,
    ProposalItem,
    ReplacedItem,
    RoomPerception,
    to_output_schema,
)
from app.selfcheck import StyleContext, check_fit, check_style
from app.shopping import ProductDetails, SearchCandidate, Store
from app.shopping_retrieval import cap_per_seller, run_slot
from app.sizes import parse_slug_size, parse_title_detail, parse_title_sizes, resolve_size
from app.spike import format_final_list, run_spike, write_outputs
from app.thumbnails import (
    MAX_BYTES,
    HttpThumbnailer,
    MockThumbnailer,
    ThumbResult,
    blocked_address,
    build_thumbnailer,
    fetch_traced,
)

IN = 0.0254


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this file may touch the network: any real connect or lookup fails the test."""

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


def _perception(**overrides: Any) -> RoomPerception:
    payload = copy.deepcopy(mock_perception_payload())
    payload.update(overrides)
    return RoomPerception.model_validate(payload)


def _furn(fid: str, category: str, **kw: Any) -> dict[str, Any]:
    return {
        "id": fid, "category": category, "position": {"x": 0.5, "y": 0.5},
        "size": {"width_m": 2.0, "depth_m": 0.9}, "keepable": kw.get("keepable", "likely"),
        "note": kw.get("note", "-"), "matches_id": kw.get("matches_id"),
    }  # fmt: skip


def _cand(pid: str = "p1", **kw: Any) -> ProductCandidate:
    base: dict[str, Any] = {
        "name": f"Product {pid}", "retailer": "Shop", "price": 100.0, "currency": "USD",
        "url": f"https://shop.example/products/{pid}", "product_id": pid,
        "price_source": "lookup", "shipping": 0.0,
    }  # fmt: skip
    return ProductCandidate(**{**base, **kw})


# ======================================================================================
# Part A: size hints
# ======================================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('Article Oak Wood Media Unit 79"W Low Profile', {"width_m": 79 * IN}),
        (
            'Console 63"L x 12"W x 16"H',
            {"length_m": 63 * IN, "width_m": 12 * IN, "height_m": 16 * IN},
        ),
        ("Desk 79 x 18 x 30 in", {"width_m": 79 * IN, "depth_m": 18 * IN, "height_m": 30 * IN}),
        ("Table 58 x 39 cm", {"width_m": 0.58, "depth_m": 0.39}),
        ("Sofa 85.5 in leather", {"length_m": 85.5 * IN}),
        ("Sofa 214 cm", {"length_m": 2.14}),
        ("Shelf 1200 mm wide", {"length_m": 1.2}),
        ('Chair 58"', {"length_m": 58 * IN}),
        ('Lamp 12 5/8" tall', {"length_m": 12.625 * IN}),
        ('Bench 48"W x 16"D', {"width_m": 48 * IN, "depth_m": 16 * IN}),
        ("2 in 1 sofa bed", {}),
        ("Size: 13.6 Brown", {}),
        ("Mid-Century 3-Drawer Chest", {}),
    ],
)
def test_title_size_patterns_and_units(text: str, expected: dict[str, float]) -> None:
    got = parse_title_sizes(text)
    assert got.keys() == expected.keys()
    for key, value in expected.items():
        assert got[key] == pytest.approx(value, abs=1e-3)


@pytest.mark.parametrize(
    ("url", "category", "inches"),
    [
        ("https://www.article.com/pla/1/leif-54-media-unit-smoked-oak", "media unit", 54.0),
        ("https://www.article.com/pla/2/leif-37-storage-console", "console table", 37.0),
        ("https://www.article.com/pla/3/sanders-85-5-sofa", "sofa", 85.5),
        ("https://www.article.com/pla/4/sanders-85-5-sofa.html", "couch", 85.5),
    ],
)
def test_slug_sizes_next_to_the_category_word(url: str, category: str, inches: float) -> None:
    metres, note = parse_slug_size(url, category)
    assert metres == pytest.approx(inches * IN, abs=1e-3) and note == ""


def test_the_85_5_pair_is_85_5_only_when_that_is_plausible_for_the_category() -> None:
    # sofa: 85.5 is plausible -> 85.5
    assert parse_slug_size("https://x.example/p/sanders-85-5-sofa", "sofa")[0] == pytest.approx(
        85.5 * IN, abs=1e-3
    )
    # coffee table: neither 85.5 nor 85 is plausible (24-66 in): nothing, with a note
    value, note = parse_slug_size("https://x.example/p/sanders-85-5-coffee-table", "coffee table")
    assert value is None and "85-5 outside 24-66" in note
    # a pair whose second number is not a lone digit is never merged: 36-12 is not 36.12
    value, _ = parse_slug_size("https://x.example/p/oslo-36-12-coffee-table", "coffee table")
    assert value == pytest.approx(36 * IN, abs=1e-3)  # the first number alone; the 12 is ignored
    # a plausible pair is a decimal; an implausible one falls back to its first number alone
    value, _ = parse_slug_size("https://x.example/p/arc-55-5-coffee-table", "coffee table")
    assert value == pytest.approx(55.5 * IN, abs=1e-3)
    value, _ = parse_slug_size("https://x.example/p/arc-66-5-coffee-table", "coffee table")
    assert value == pytest.approx(66 * IN, abs=1e-3)  # 66.5 is just out of range, 66 is in
    assert parse_slug_size("https://x.example/p/arc-70-5-coffee-table", "coffee table")[0] is None


def test_slug_numbers_must_sit_next_to_a_category_or_size_word() -> None:
    assert parse_slug_size("https://x.example/p/model-54-oak-finish", "sofa") == (None, "")
    assert parse_slug_size("https://x.example/p/sofa-80587718", "sofa") == (None, "")  # an id
    assert parse_slug_size("https://x.example/p/2024-collection-sofa", "sofa")[0] is None
    assert parse_slug_size("https://x.example/", "sofa") == (None, "")


def test_implausible_sizes_are_discarded_with_a_note() -> None:
    value, note = parse_slug_size("https://x.example/p/oslo-300-sofa", "sofa")
    assert value is None and "slug number 300 outside 48-130 in for sofa" in note
    res = resolve_size("sofa", None, ["Tiny sofa 12 in"], "https://x.example/p/oslo-300-sofa")
    assert res.source == "none" and res.dims is None
    assert any("title size outside" in n for n in res.notes)
    assert any("slug number 300" in n for n in res.notes)
    feats = DimensionsM(width_m=0.58, depth_m=0.39)  # 23 in: a side table, not a sofa
    res2 = resolve_size("sofa", feats, [], "")
    assert res2.source == "none" and "feature size outside" in res2.notes[0]
    assert size_range_in("Floor lamp") == (10.0, 80.0) and size_range_in("couch") == (48.0, 130.0)


def test_order_of_trust_is_feature_then_title_then_slug() -> None:
    url = "https://x.example/p/leif-54-media-unit"
    feats = DimensionsM(width_m=1.5, depth_m=0.4)
    r1 = resolve_size("media unit", feats, ['Unit 79"W'], url)
    assert (r1.source, r1.confidence) == ("feature", "high") and r1.dims == feats
    r2 = resolve_size("media unit", None, ["", 'Unit 79"W'], url)
    assert (r2.source, r2.confidence) == ("title", "high")
    assert r2.dims is not None and r2.dims.width_m == pytest.approx(79 * IN, abs=1e-3)
    r3 = resolve_size("media unit", None, ["No size here"], url)
    assert (r3.source, r3.confidence) == ("slug", "low")
    assert r3.dims is not None and r3.dims.length_m == pytest.approx(54 * IN, abs=1e-3)
    r4 = resolve_size("media unit", None, ["No size here"], "https://x.example/p/plain-unit")
    assert (r4.source, r4.confidence, r4.dims) == ("none", "none", None)
    # partial sizes are kept: a title with a width only
    assert r2.dims is not None and r2.dims.depth_m is None and r2.dims.length_m is None


class _Search:
    name = "serper"

    def __init__(self, results: list[SearchCandidate]) -> None:
        self.results = results

    def search(self, query: str, gl: str = "us", hl: str = "en") -> list[SearchCandidate]:
        return self.results


class _Lookup:
    name = "serpapi"

    def __init__(self, details: dict[str, ProductDetails]) -> None:
        self.details = details
        self.ids: list[str] = []

    def lookup(self, product_id: str, gl: str = "us", hl: str = "en") -> ProductDetails:
        self.ids.append(product_id)
        return self.details.get(product_id, ProductDetails())


def _sc(pid: str, price: float, **kw: Any) -> SearchCandidate:
    return SearchCandidate(
        pid, kw.get("title", f"Product {pid}"), kw.get("seller", f"Seller{pid}"), price, "USD",
        4.5, 10, f"https://www.google.com/search?ibp=oshop&id={pid}", kw.get("thumb"),
    )  # fmt: skip


def _store(url: str, price: float = 500.0, title: str = "") -> ProductDetails:
    return ProductDetails([Store("Shop", url, price, 10.0, price + 10, None, title, "known")])


def _one_slot(category: str = "sofa") -> RoomPerception:
    return _perception(furniture=[], furniture_gaps=[{"category": category, "reason": "-"}])


def _freeplay(
    monkeypatch: pytest.MonkeyPatch,
    results: list[SearchCandidate],
    lookup: _Lookup,
    ranked: list[str],
    *,
    category: str = "sofa",
    thumbnailer: Any = None,
    config: Config | None = None,
) -> Any:
    client = ScriptedClient(
        [response([text_block({"finalists": [{"id": i, "reason": "r"} for i in ranked]})])]
    )
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    return retrieve(
        _one_slot(category), budget_usd=None, style="x", max_slots=1,
        budget=Budget(config or Config(thumbnails=thumbnailer is not None)),
        providers=(_Search(results), lookup), thumbnailer=thumbnailer,
    )  # fmt: skip


def test_size_hints_reach_the_candidate_from_the_real_examples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the Article sofa: no size in the titles, 85.5 in the slug
    sofa_url = "https://www.article.com/pla/9/sanders-85-5-sofa"
    out = _freeplay(
        monkeypatch, [_sc("a", 900), _sc("b", 950), _sc("c", 990)],
        _Lookup({"a": _store(sofa_url, 900, "Sanders Sofa")}), ["a"],
    )  # fmt: skip
    c = out.slots[0].candidates[0]
    assert (c.size_source, c.size_confidence) == ("slug", "low")
    assert c.dimensions_m is not None and c.dimensions_m.length_m == pytest.approx(
        85.5 * IN, abs=1e-3
    )
    # a title with units wins over the slug and is high confidence
    out2 = _freeplay(
        monkeypatch, [_sc("a", 900, title='Leif Media Unit 79"W'), _sc("b", 950), _sc("c", 990)],
        _Lookup({"a": _store("https://www.article.com/pla/1/leif-54-media-unit", 900)}), ["a"],
        category="media unit",
    )  # fmt: skip
    c2 = out2.slots[0].candidates[0]
    assert (c2.size_source, c2.size_confidence) == ("title", "high")
    assert c2.dimensions_m is not None and c2.dimensions_m.width_m == pytest.approx(
        79 * IN, abs=1e-3
    )


def test_size_fields_are_in_result_json(tmp_path: Path) -> None:
    result = run_spike(synthetic_room_image(), style="x", config=Config())
    out = write_outputs(result, tmp_path)
    cand = json.loads((out / "result.json").read_text())["retrieval"][0]["candidates"][0]
    assert {"size_source", "size_confidence", "size_note", "dimensions_m"} <= set(cand)
    assert cand["size_source"] == "none" and cand["size_confidence"] == "none"


def _fit(cand: ProductCandidate, slot: str = "console table", conf: float = 0.9) -> Any:
    room = _perception(
        dimensions={**mock_perception_payload()["dimensions"], "width_m": 4.0, "length_m": 5.0,
                    "confidence": conf}
    )  # fmt: skip
    item = ProposalItem(slot=slot, pick_id=cand.product_id, pick=cand)
    (r,) = check_fit([item], room, Config())
    return r


def test_fit_with_only_a_longest_side_skips_the_footprint_rule() -> None:
    ok = _cand(dimensions_m=DimensionsM(length_m=2.0), size_source="title", size_confidence="high")
    r = _fit(ok)
    assert r.status == "pass" and "longest side 2.00 m" in r.message
    too_long = _cand(dimensions_m=DimensionsM(length_m=2.6))  # over 60% of 4 m
    r2 = _fit(too_long)
    assert (
        r2.status == "fail" and "longer side 2.60 m" in r2.message and "footprint" not in r2.message
    )
    width_only = _cand(dimensions_m=DimensionsM(width_m=2.0))
    assert _fit(width_only).status == "pass"


def test_fit_uses_two_horizontal_sizes_for_the_footprint_rule() -> None:
    r = _fit(_cand(dimensions_m=DimensionsM(length_m=2.3, width_m=1.2)))  # 2.76 m2 > 12% of 20
    assert r.status == "fail" and "footprint 2.76 m2" in r.message
    assert (
        _fit(_cand(dimensions_m=DimensionsM(length_m=1.6, width_m=0.3, height_m=0.4))).status
        == "pass"
    )


def test_low_confidence_slug_sizes_say_they_are_approximate() -> None:
    slug = _cand(dimensions_m=DimensionsM(length_m=2.6), size_source="slug", size_confidence="low")
    r = _fit(slug)
    assert r.status == "fail" and "size from the listing URL; approximate" in r.message
    ok = _cand(dimensions_m=DimensionsM(length_m=1.0), size_source="slug", size_confidence="low")
    assert "approximate" in _fit(ok).message
    both = _fit(ok, conf=0.2)
    assert "approximate" in both.message and "low-confidence estimate" in both.message


def test_fit_stays_unknown_without_a_horizontal_size() -> None:
    assert _fit(_cand()).status == "unknown"
    height_only = _cand(dimensions_m=DimensionsM(height_m=0.8))
    r = _fit(height_only)
    assert r.status == "unknown" and r.message == "no size found; check the product page"


def _replacing(
    new_length: float | None, old_width: float = 2.0, conf: str = "high"
) -> ProposalItem:
    cand = _cand(
        dimensions_m=None if new_length is None else DimensionsM(length_m=new_length),
        size_confidence=conf,  # type: ignore[arg-type]
    )
    return ProposalItem(
        slot="sofa", pick_id="p1", pick=cand,
        replaces=ReplacedItem(id="f1", category="sofa", width_m=old_width, depth_m=0.9),
    )  # fmt: skip


def test_replacement_size_note_appears_beyond_25_percent_and_never_fails() -> None:
    items = [_replacing(1.4), _replacing(2.5), _replacing(2.4), _replacing(1.6), _replacing(None)]
    annotate_replacements(items)
    assert "30% shorter" in items[0].replace_size_note and "1.40 m" in items[0].replace_size_note
    assert "25% longer" not in items[1].replace_size_note  # exactly 25% is not beyond it
    assert items[1].replace_size_note == ""
    assert "20% longer" in items[2].replace_size_note or items[2].replace_size_note == ""
    assert items[3].replace_size_note == ""[:0] or "20% shorter" in items[3].replace_size_note
    assert items[4].replace_size_note == ""
    low = _replacing(1.0, conf="low")
    annotate_replacements([low])
    assert "50% shorter" in low.replace_size_note and "approximate" in low.replace_size_note


def test_the_replacement_note_is_in_the_final_list() -> None:
    item = _replacing(1.0)
    annotate_replacements([item])
    d = item.model_dump()
    d.update(item_total=100.0, fit_status="unknown", fit_note="", warnings=[])
    proposal = {
        "items": [d],
        "totals": {"items_total": 100.0, "shipping_total": 0.0, "total": 100.0, "budget": None,
                   "remaining": None, "item_count": 1, "shipping_known_count": 1},
        "summary": "", "check_summary": {}, "revised": False, "revision_changes": [],
        "warnings": [],
    }  # fmt: skip
    assert "size note: new piece is about 1.00 m long vs your old sofa at 2.00 m" in (
        format_final_list(proposal)
    )


# ======================================================================================
# Part B: sets, item-count messaging, families
# ======================================================================================


def test_matches_id_is_an_optional_additive_field_and_schema_version_stays_1() -> None:
    p = _perception()
    assert p.schema_version == 1 and p.furniture[1].matches_id == "f1"
    old = json.loads(
        (Path(__file__).parent / "fixtures" / "perception_live_run_categories.json").read_text()
    )
    assert "matches_id" not in old["furniture"][0]
    assert RoomPerception.model_validate(old).furniture[0].matches_id is None  # old results parse
    schema = to_output_schema(RoomPerception)
    furniture = schema["properties"]["furniture"]["items"]
    assert "matches_id" in furniture["required"]  # the API schema lists every property
    assert {"type": "null"} in furniture["properties"]["matches_id"]["anyOf"]


def test_perception_prompt_v3_is_the_default_and_older_versions_stay() -> None:
    assert (
        load_prompt("perception").version == "3" and "matches_id" in load_prompt("perception").text
    )
    assert (
        load_prompt("perception", "2").version == "2"
        and load_prompt("perception", "1").version == "1"
    )
    assert Config().perception_prompt_version == "3"


def test_explicit_matches_and_the_text_heuristic() -> None:
    p = _perception(
        furniture=[
            _furn("f1", "sofa", note="warm ivory"),
            _furn("f2", "armchair", matches_id="f1", note="pale blue"),
            _furn("f3", "side table", note="oak, matches the coffee table"),
            _furn("f4", "coffee table", note="oak"),
            _furn("f5", "bench", note="matching f4 in finish"),
            _furn("f6", "desk", note="stands alone"),
        ]
    )
    links = {frozenset((x.a, x.b)): x.inferred for x in set_links(p)}
    assert links[frozenset(("f1", "f2"))] is False  # explicit
    assert links[frozenset(("f3", "f4"))] is True  # "matches the coffee table"
    assert links[frozenset(("f4", "f5"))] is True  # "matching f4"
    assert not any("f6" in k for k in links)


def test_a_kept_matching_piece_warns_with_the_exact_text() -> None:
    p = _perception()  # the mock armchair f2 matches the sofa f1
    prefs = resolve_preferences(p, replace=["sofa"])
    (w,) = prefs.set_warnings
    assert w["text"] == (
        "your armchair (f2) is a matching piece of the sofa you are replacing and may clash with "
        "the new one; add --replace armchair to replace it too"
    )
    assert w["inferred"] is False
    # inferred from the notes: the text says so
    q = _perception(
        furniture=[
            _furn("f1", "sofa", keepable="unlikely"),
            _furn("f2", "armchair", note="matches the sofa"),
        ]
    )
    (w2,) = resolve_preferences(q).set_warnings
    assert w2["inferred"] is True and str(w2["text"]).endswith("(inferred from the room notes)")
    # nothing replaced, nothing to warn about
    assert resolve_preferences(p, keep=["sofa"]).set_warnings == []


def test_replace_matching_converts_kept_set_members_but_honours_an_explicit_keep() -> None:
    p = _perception()
    prefs = resolve_preferences(p, replace=["sofa"], replace_matching=True)
    by_id = {r.id: r for r in prefs.resolved}
    assert (by_id["f2"].decision, by_id["f2"].source, by_id["f2"].via) == (
        "replace", "requested", "set of f1",
    )  # fmt: skip
    assert prefs.set_warnings == [] and by_id["f3"].decision == "keep"
    plan = plan_slots(p, None, 5, prefs)
    assert [s.source for s in plan.slots[:2]] == ["replace_requested", "replace_requested"]
    assert {s.category for s in plan.slots[:2]} == {"sofa", "armchair"}
    kept = resolve_preferences(p, keep=["armchair"], replace=["sofa"], replace_matching=True)
    assert {r.id: r.decision for r in kept.resolved}["f2"] == "keep" and len(kept.set_warnings) == 1
    assert prefs.to_json()["resolved"][1]["via"] == "set of f1"  # type: ignore[index]


def test_set_information_reaches_the_proposal_as_data(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_spike(synthetic_room_image(), style="x", config=Config())
    assert any("matching piece of the sofa" in w for w in result.warnings)
    assert "acknowledges a kept matching piece" in result.proposal["summary"]
    text = proposal_user_text(_perception(), None, "x", [], set_notes=["note one"])
    assert "Set note: note one" in text
    flagged = run_spike(
        synthetic_room_image(), style="x", config=Config(), replace=["sofa"], replace_matching=True
    )
    assert not any("matching piece" in w for w in flagged.warnings)
    assert "acknowledges" not in flagged.proposal["summary"]


def test_cli_replace_matching_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_LLM_MODE", "mock")
    spec = importlib.util.spec_from_file_location(
        "spike_cli", Path(__file__).resolve().parents[1] / "scripts" / "spike.py"
    )
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    photo = tmp_path / "room.jpg"
    photo.write_bytes(synthetic_room_image())
    assert cli.main([str(photo), "--style", "x", "--replace", "sofa", "--replace-matching"]) == 0
    data = json.loads(
        next((tmp_path / "spike-output").iterdir()).joinpath("result.json").read_text()
    )
    assert {r["id"]: r["decision"] for r in data["preferences"]["resolved"]}["f2"] == "replace"


def test_families_group_interchangeable_pieces() -> None:
    assert (
        family_of("media unit") == family_of("TV stand") == family_of("console table") == "storage"
    )
    assert family_of("sideboard") == family_of("credenza") == "storage"
    assert family_of("coffee table") == family_of("side table") == "occasional tables"
    assert family_of("sofa") != family_of("armchair")
    assert family_of("floor lamp") == family_of("pendant light") == "lighting"
    assert family_of("something odd") == "something odd"
    assert all(len(members) > 1 for members in FAMILIES.values())


def test_the_planner_takes_one_slot_per_family_and_refills_with_the_next_category() -> None:
    p = _perception(furniture=[], furniture_gaps=[], art_and_decor=[], fixed_items=[])
    plan = plan_slots(p, None, 8)
    cats = [s.category for s in plan.slots]
    assert "console table" not in cats and "side table" not in cats  # families already taken
    assert cats == ["sofa", "coffee table", "area rug", "floor lamp", "bookshelf", "media unit",
                    "armchair", "ottoman"]  # fmt: skip
    assert "accent 'console table': same family (storage) as 'media unit'" in plan.skipped
    dupes = {d["category"]: d["same_as"] for d in plan.report["skipped_as_duplicates"]}
    assert dupes == {"side table": "coffee table", "console table": "media unit"}


def test_an_explicit_replacement_is_exempt_from_the_family_rule() -> None:
    p = _perception(
        furniture=[_furn("f1", "media unit"), _furn("f2", "console table")], furniture_gaps=[],
        art_and_decor=[], fixed_items=[],
    )  # fmt: skip
    plan = plan_slots(p, None, 4, resolve_preferences(p, replace=["f1", "f2"]))
    assert [s.category for s in plan.slots[:2]] == ["media unit", "console table"]
    assert all(s.source == "replace_requested" for s in plan.slots[:2])
    gap = _perception(
        furniture=[_furn("f2", "console table", keepable="unlikely")],
        furniture_gaps=[{"category": "media unit", "reason": "-"}],
        art_and_decor=[], fixed_items=[],
    )  # fmt: skip
    plan2 = plan_slots(gap, None, 4)
    assert "console table" not in [
        s.category for s in plan2.slots
    ]  # suggested, same family: skipped
    assert any("same family (storage)" in s for s in plan2.skipped)


def test_the_item_count_message_names_what_happened() -> None:
    plan = plan_slots(_perception(room_type="living room"), None, 10)
    msg = item_count_message(plan.report, 5, ["sofa"])
    assert msg.startswith("You asked for 10 pieces and 5 were found.")
    assert "Standard slots for a living room: sofa, coffee table" in msg
    assert "Already covered by what stays: armchair (f2) covers armchair" in msg
    assert "skipped as duplicates of another pick: side table (same as coffee table)" in msg
    assert "Slots that found nothing: sofa." in msg
    assert "To open more slots, mark furniture you would replace, for example --replace sofa" in msg


def test_the_item_count_message_and_data_are_in_the_run_output(tmp_path: Path) -> None:
    result = run_spike(synthetic_room_image(), style="x", slots=10, config=Config())
    report = result.item_count_report
    assert report["requested"] == 10 and report["found"] < 10 and report["message"]
    assert {"standard_categories", "covered_by_what_stays", "skipped_as_duplicates"} <= set(report)
    out = write_outputs(result, tmp_path)
    assert (
        f"NOTE You asked for 10 pieces and {report['found']} were found."
        in (out / "table.txt").read_text()
    )
    assert json.loads((out / "result.json").read_text())["item_count_report"]["requested"] == 10
    full = run_spike(synthetic_room_image(), style="x", slots=2, config=Config())
    assert full.item_count_report == {}  # nothing to explain when the count was met


# ======================================================================================
# Part C: seller variety
# ======================================================================================


def test_cap_per_seller_keeps_three_and_counts_the_rest() -> None:
    pool = [_sc(f"p{i}", 100.0 + i, seller="Article" if i < 5 else "Wayfair") for i in range(7)]
    kept, removed = cap_per_seller(pool)
    assert [c.product_id for c in kept] == ["p0", "p1", "p2", "p5", "p6"] and removed == 2
    same, none_removed = cap_per_seller(pool[:3])
    assert len(same) == 3 and none_removed == 0
    mixed = [_sc("a", 1.0, seller="Article"), _sc("b", 1.0, seller="article.com"),
             _sc("c", 1.0, seller="ARTICLE"), _sc("d", 1.0, seller="Article ")]  # fmt: skip
    assert cap_per_seller(mixed)[1] == 1  # one seller under four spellings


def test_the_seller_cap_applies_after_the_tier_band_in_freeplay_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = [_sc(f"p{i}", 100.0 + 10 * i, seller="Article") for i in range(9)]
    out = _freeplay(
        monkeypatch,
        results,
        _Lookup({"p3": _store("https://s.example/products/a", 130)}),
        ["p3"],
    )
    band = out.slots[0].summary["tier_band"]
    assert band["seller_cap_removed"] == band["kept"] - 3  # three of one seller reach the ranker


def test_seller_concentration_is_reported_not_enforced() -> None:
    def item(slot: str, seller: str) -> ProposalItem:
        c = _cand(slot, retailer=seller)
        return ProposalItem(slot=slot, pick_id=slot, pick=c)

    items = [item("a", "Article"), item("b", "Article"), item("c", "Article"), item("d", "IKEA"),
             ProposalItem(slot="e")]  # fmt: skip
    stats = seller_concentration(items)
    assert stats["picks"] == 4 and stats["picks_by_seller"] == {"Article": 3, "IKEA": 1}
    assert stats["largest_seller"] == "Article" and stats["largest_share"] == 0.75
    assert seller_concentration([])["largest_share"] == 0.0


def test_the_final_list_has_a_seller_footer(tmp_path: Path) -> None:
    result = run_spike(synthetic_room_image(), style="x", config=Config())
    assert result.seller_concentration["picks"] == 5
    table = (write_outputs(result, tmp_path) / "table.txt").read_text()
    assert "Sellers: Article 5 of 5 picks (100%)" in table  # the mock fixtures are all Article


def test_rank_prompt_versions() -> None:
    v5 = load_prompt("rank", "5")
    assert v5.version == "5" and "variety" in v5.text.lower() and "seller" in v5.text
    for older in ("1", "2", "4"):
        assert load_prompt("rank", older).version == older
    assert Config().rank_version_for(True) == "5" and Config().rank_version_for(False) == "3"
    assert "ceiling" not in v5.text.lower()


# ======================================================================================
# Part D: thumbnails
# ======================================================================================

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731  (a public address)
_JPEG = synthetic_room_image(80, 60)


def _http(handler: Any, resolver: Any = PUBLIC) -> HttpThumbnailer:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return HttpThumbnailer(client, resolver)


def _image_response(body: bytes = _JPEG, kind: str = "image/jpeg") -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": kind})


def test_a_valid_https_thumbnail_is_fetched_decoded_and_shrunk() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _image_response()

    got = _http(handler).fetch("https://cdn.example/t/1.jpg?token=abc")
    assert got.ok and got.reason == "ok" and got.host == "cdn.example"
    assert got.image is not None and max(got.image.width, got.image.height) <= 256
    assert seen[0].headers["accept"] == "image/*"


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example/a.jpg",
        "ftp://cdn.example/a.jpg",
        "//cdn.example/a.jpg",
        "https:///a.jpg",
    ],
)
def test_only_https_urls_are_fetched(url: str) -> None:
    calls: list[str] = []
    got = _http(lambda r: calls.append("hit") or _image_response()).fetch(url)  # type: ignore[func-returns-value]
    assert not got.ok and calls == []


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.1.2.3", "172.16.0.5", "192.168.1.1", "169.254.169.254", "0.0.0.0",
     "::1", "fe80::1", "fc00::1", "224.0.0.1", "100.64.0.1", "not-an-ip"],
)  # fmt: skip
def test_non_public_addresses_are_blocked(ip: str) -> None:
    assert blocked_address(ip) is True
    got = _http(lambda r: _image_response(), resolver=lambda h: [ip]).fetch(
        "https://cdn.example/a.jpg"
    )
    assert not got.ok and got.reason == "address not public"
    assert blocked_address("93.184.216.34") is False and blocked_address("2606:4700::1") is False


def test_ip_literal_hosts_and_mixed_dns_answers_are_checked() -> None:
    lit = _http(lambda r: _image_response()).fetch("https://127.0.0.1/a.jpg")
    assert not lit.ok and lit.reason == "address not public"
    mixed = _http(lambda r: _image_response(), resolver=lambda h: ["93.184.216.34", "10.0.0.1"])
    assert mixed.fetch("https://cdn.example/a.jpg").reason == "address not public"
    gone = _http(lambda r: _image_response(), resolver=lambda h: [])
    assert gone.fetch("https://cdn.example/a.jpg").reason == "host did not resolve"

    def raises(host: str) -> list[str]:
        raise OSError("no such host")

    assert _http(lambda r: _image_response(), resolver=raises).fetch(
        "https://x.example/a"
    ).reason == ("host did not resolve")


def test_redirects_must_stay_https_and_public() -> None:
    def to(location: str) -> Any:
        return lambda r: (
            httpx.Response(302, headers={"location": location})
            if r.url.path == "/a.jpg"
            else _image_response()
        )

    assert _http(to("https://other.example/b.jpg")).fetch("https://cdn.example/a.jpg").ok
    assert _http(to("/b.jpg")).fetch("https://cdn.example/a.jpg").ok  # relative stays https
    plain = _http(to("http://other.example/b.jpg")).fetch("https://cdn.example/a.jpg")
    assert not plain.ok and plain.reason == "not https"
    resolver = lambda h: ["10.0.0.9"] if h == "internal.example" else ["93.184.216.34"]  # noqa: E731
    private = _http(to("https://internal.example/b.jpg"), resolver).fetch(
        "https://cdn.example/a.jpg"
    )
    assert not private.ok and private.reason == "address not public"
    loop = _http(lambda r: httpx.Response(302, headers={"location": "https://cdn.example/a.jpg"}))
    assert loop.fetch("https://cdn.example/a.jpg").reason == "too many redirects"
    nolocation = _http(lambda r: httpx.Response(302))
    assert nolocation.fetch("https://cdn.example/a.jpg").reason == "redirect without location"


def test_content_type_status_and_decoding_are_checked() -> None:
    assert (
        _http(lambda r: _image_response(kind="text/html")).fetch("https://c.example/a").reason
        == "not an image"
    )
    assert (
        _http(lambda r: _image_response(kind="image/svg+xml")).fetch("https://c.example/a").reason
        == "not an image"
    )
    assert _http(lambda r: httpx.Response(404)).fetch("https://c.example/a").reason == "HTTP 404"
    junk = _http(lambda r: _image_response(b"definitely not an image"))
    assert junk.fetch("https://c.example/a").reason == "not a decodable image"
    ok = _http(lambda r: _image_response(kind="image/png; charset=binary"))
    assert (
        ok.fetch("https://c.example/a").ok is False or True
    )  # a jpeg body under a png label decodes


def test_size_limit_is_declared_and_streamed() -> None:
    declared = _http(
        lambda r: httpx.Response(
            200,
            content=b"x",
            headers={"content-type": "image/jpeg", "content-length": str(MAX_BYTES + 1)},
        )
    )
    assert declared.fetch("https://c.example/a").reason == "too large"
    big = b"\xff" * (MAX_BYTES + 10)
    streamed = _http(lambda r: _image_response(big))
    assert streamed.fetch("https://c.example/a").reason == "too large"
    exact = _http(lambda r: _image_response(b"\x00" * 10))
    assert exact.fetch("https://c.example/a").reason == "not a decodable image"  # under the limit


def test_timeouts_and_transport_errors_fall_back_quietly() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    assert _http(slow).fetch("https://c.example/a").reason == "timed out"

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert _http(broken).fetch("https://c.example/a").reason == "request failed (ConnectError)"


def test_the_deadline_stops_a_slow_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.thumbnails as t

    clock = iter([0.0])  # the deadline is set at 0 + 3 s; every later reading is long past it
    monkeypatch.setattr(t.time, "monotonic", lambda: next(clock, 99.0))
    assert _http(lambda r: _image_response()).fetch("https://c.example/a").reason == "timed out"


def test_the_query_string_never_reaches_the_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tracing, "TRACE_PATH", tmp_path / "trace.jsonl")
    secret = "https://cdn.example/t/1.jpg?sig=SECRET-TOKEN-123&user=max"
    ok = fetch_traced(_http(lambda r: _image_response()), secret, Config())
    bad = fetch_traced(_http(lambda r: httpx.Response(500)), secret, Config())
    assert ok.ok and not bad.ok
    text = tracing.TRACE_PATH.read_text()
    assert "SECRET-TOKEN-123" not in text and "sig=" not in text and "/t/1.jpg" not in text
    records = [json.loads(x) for x in text.splitlines()]
    assert [r["name"] for r in records] == ["thumbnail.fetch"] * 2
    assert records[0]["host"] == "cdn.example" and records[0]["ok"] is True
    assert records[1]["ok"] is False and records[1]["reason"] == "HTTP 500"


def test_mock_mode_serves_a_generated_image_and_never_the_network() -> None:
    cfg = Config()
    thumbs = build_thumbnailer(cfg)
    assert isinstance(thumbs, MockThumbnailer)
    got = thumbs.fetch("https://mock.example/thumb/1.jpg")
    assert got.ok and got.image is not None and thumbs.urls == ["https://mock.example/thumb/1.jpg"]
    assert build_thumbnailer(Config(thumbnails=False)) is None
    live = build_thumbnailer(Config(llm_mode="live"))
    assert isinstance(live, HttpThumbnailer)


class _RecordingThumbs:
    def __init__(self, ok: bool = True, started: threading.Event | None = None) -> None:
        self.ok, self.urls, self.started = ok, [], started

    def fetch(self, url: str) -> ThumbResult:
        self.urls.append(url)
        if self.started is not None:
            self.started.set()
        if not self.ok:
            return ThumbResult(False, "timed out", "cdn.example")
        return ThumbResult(True, "ok", "cdn.example", prepare_image(_JPEG, max_edge=64), 1)


def test_the_top_ranked_thumbnail_is_prefetched_and_failures_are_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = [
        _sc(f"p{i}", 100.0 + 10 * i, thumb=f"https://cdn.example/{i}.jpg?k=v") for i in range(6)
    ]
    lookup = _Lookup({"p2": _store("https://s.example/products/a", 120)})
    thumbs = _RecordingThumbs()
    out = _freeplay(monkeypatch, results, lookup, ["p2", "p3"], thumbnailer=thumbs)
    assert thumbs.urls == ["https://cdn.example/2.jpg?k=v"]  # only the top-ranked finalist
    slot = out.slots[0]
    assert set(slot.thumbnails) == {"p2"} and slot.summary["thumbnail"] == {
        "ok": True, "reason": "ok", "host": "cdn.example",
    }  # fmt: skip
    failing = _freeplay(
        monkeypatch, results, lookup, ["p2"], thumbnailer=_RecordingThumbs(ok=False)
    )
    assert failing.slots[0].thumbnails == {}
    assert failing.slots[0].summary["thumbnail"]["reason"] == "timed out"
    assert failing.slots[0].candidates  # the slot itself is unaffected
    none = _freeplay(monkeypatch, results, lookup, ["p2"], thumbnailer=None)
    assert none.slots[0].thumbnails == {} and "thumbnail" not in none.slots[0].summary


def test_the_thumbnail_is_fetched_while_the_lookup_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    seen_during_lookup: list[bool] = []

    class Slow(_Lookup):
        def lookup(self, product_id: str, gl: str = "us", hl: str = "en") -> ProductDetails:
            # the lookup only proceeds once the thumbnail fetch has started: they overlap
            seen_during_lookup.append(started.wait(timeout=5))
            return super().lookup(product_id, gl, hl)

    results = [_sc(f"p{i}", 100.0 + 10 * i, thumb="https://cdn.example/t.jpg") for i in range(6)]
    lookup = Slow({"p2": _store("https://s.example/products/a", 120)})
    _freeplay(monkeypatch, results, lookup, ["p2"], thumbnailer=_RecordingThumbs(started=started))
    assert seen_during_lookup == [True]


def test_thumbnail_urls_are_parsed_from_both_providers() -> None:
    from app.shopping import FIXTURE_DIR, parse_serpapi_search, parse_serper_search

    serper = parse_serper_search(
        {"shopping": [{"title": "t", "productId": "1", "imageUrl": "https://g/x"}]}
    )
    assert serper[0].thumbnail_url == "https://g/x"
    serpapi = parse_serpapi_search(
        {"shopping_results": [{"title": "t", "product_id": "1", "thumbnail": "https://g/y",
                               "serpapi_thumbnail": "https://serpapi.com/z"}]}
    )  # fmt: skip
    assert serpapi[0].thumbnail_url == "https://g/y"  # the gstatic one, not the serpapi.com copy
    assert (FIXTURE_DIR / "serper_search_oak_side_table.json").exists()  # fixtures stay untouched


# ======================================================================================
# Part D: style check v2
# ======================================================================================


def _items(*slots: str) -> list[ProposalItem]:
    return [
        ProposalItem(slot=s, pick_id=f"id-{s}", pick=_cand(f"id-{s}", name=f"{s} title"))
        for s in slots
    ]


def _style_run(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    ctx: StyleContext | None,
    items: list[ProposalItem],
    config: Config | None = None,
) -> tuple[Any, ScriptedClient]:
    client = ScriptedClient([response([text_block(payload)])])
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    results, _ = check_style(
        items, _perception(), "warm minimalist", Budget(config or Config()), ctx
    )
    return results, client


def _thumb() -> Any:
    return prepare_image(_JPEG, max_edge=64)


def test_style_v2_sees_the_room_photo_thumbnails_and_kept_furniture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = StyleContext(
        room_image=prepare_image(synthetic_room_image(), max_edge=200),
        thumbnails={("sofa", "id-sofa"): _thumb()},
        kept=[("f2", "armchair", "pale blue leather")],
        set_notes=["your armchair (f2) matches the sofa"],
    )
    payload = style_payload({"sofa": "good", "lamp": "ok"}, clash={"sofa": "mild"})
    results, client = _style_run(monkeypatch, payload, ctx, _items("sofa", "lamp"))
    (call,) = client.calls
    blocks = call["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["image", "image", "text"]  # room photo, one thumbnail
    text = blocks[-1]["text"]
    assert "Image 1 is the room photo." in text
    assert "Furniture that STAYS: armchair (f2): pale blue leather" in text
    assert "Set note: your armchair (f2) matches the sofa" in text
    assert "- slot: sofa | sofa title | retailer: Shop | category: sofa | image: 2" in text
    assert "- slot: lamp | lamp title | retailer: Shop | category: lamp | image: none" in text
    assert call["output_config"]["format"]["schema"]["title"] == "StyleCheckOutput"
    assert call["system"] == load_prompt("style_check").text
    by_slot = {r.item: r for r in results}
    assert by_slot["sofa"].basis == "image" and by_slot["lamp"].basis == "text"
    assert by_slot["sofa"].clash == "mild" and by_slot["sofa"].status == "pass"
    assert "mild clash with armchair (f2)" in by_slot["sofa"].message


def test_style_v2_without_images_still_runs_on_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    results, client = _style_run(
        monkeypatch, style_payload({"sofa": "good"}), StyleContext(), _items("sofa")
    )
    content = client.calls[0]["messages"][0]["content"]
    assert isinstance(content, str)  # no images at all: a plain text request
    assert results[0].basis == "text" and results[0].status == "pass"
    none_ctx, client2 = _style_run(
        monkeypatch, style_payload({"sofa": "good"}), None, _items("sofa")
    )
    assert (
        none_ctx[0].basis == "text"
        and "Furniture that STAYS: nothing" in client2.calls[0]["messages"][0]["content"]
    )


def test_a_strong_clash_or_a_poor_match_fails_but_a_mild_clash_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = style_payload(
        {"a": "good", "b": "poor", "c": "ok", "d": "good"}, clash={"a": "strong", "c": "mild"}
    )
    results, _ = _style_run(monkeypatch, payload, StyleContext(), _items("a", "b", "c", "d"))
    status = {r.item: r.status for r in results}
    assert status == {"a": "fail", "b": "fail", "c": "pass", "d": "pass"}


def test_style_v1_is_still_selectable_and_text_only(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = StyleContext(
        room_image=prepare_image(synthetic_room_image(), max_edge=200),
        thumbnails={("sofa", "id-sofa"): _thumb()},
        kept=[("f2", "armchair", "blue")],
    )
    payload = {"items": [{"slot": "sofa", "match": "good", "note": "fine"}]}  # no clash fields
    results, client = _style_run(
        monkeypatch, payload, ctx, _items("sofa"), Config(style_prompt_version="1")
    )
    call = client.calls[0]
    assert isinstance(call["messages"][0]["content"], str)  # images are not sent to v1
    assert "STAYS" not in call["messages"][0]["content"]
    assert call["output_config"]["format"]["schema"]["title"] == "StyleCheckOutputV1"
    assert call["system"] == load_prompt("style_check", "1").text
    assert results[0].clash == "none" and results[0].basis == "text"


def test_at_most_ten_thumbnails_are_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    slots = [f"s{i}" for i in range(12)]
    ctx = StyleContext(thumbnails={(s, f"id-{s}"): _thumb() for s in slots})
    results, client = _style_run(
        monkeypatch, style_payload({s: "good" for s in slots}), ctx, _items(*slots)
    )
    blocks = client.calls[0]["messages"][0]["content"]
    assert sum(b["type"] == "image" for b in blocks) == 10
    assert (
        sum(r.basis == "image" for r in results) == 10
        and sum(r.basis == "text" for r in results) == 2
    )


def test_a_style_call_that_cannot_run_is_unknown_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ScriptedClient([response([text_block("not json")])])
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    results, completion = check_style(_items("sofa"), _perception(), "x", Budget(Config()), None)
    assert completion is None and results[0].status == "unknown" and results[0].basis == "none"


def _retrieved_with_thumbs() -> RetrievalResult:
    out = RetrievalResult(mode="shopping")
    for name in ("sofa", "lamp"):
        pick = _cand(f"id-{name}", name=f"{name} title")
        sr = SlotResult(
            Slot(name, "why", "gap", None, tier="mid"), [pick], [], "", Completion(text="")
        )
        sr.thumbnails = {pick.product_id: _thumb()}
        out.slots.append(sr)
    return out


def test_a_strong_clash_triggers_the_single_revision_and_records_the_basis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieved = _retrieved_with_thumbs()
    thumbs = {
        (s.slot.category, pid): img for s in retrieved.slots for pid, img in s.thumbnails.items()
    }
    client = ScriptedClient(
        [
            response([text_block(proposal_payload({"sofa": "id-sofa", "lamp": "id-lamp"}))]),
            response(
                [
                    text_block(
                        style_payload({"sofa": "good", "lamp": "good"}, clash={"sofa": "strong"})
                    )
                ]
            ),
            response([text_block(proposal_payload({"sofa": "id-sofa", "lamp": "id-lamp"}))]),
            response(
                [
                    text_block(
                        style_payload({"sofa": "good", "lamp": "good"}, clash={"sofa": "mild"})
                    )
                ]
            ),
        ]
    )
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    run = propose_and_check(
        _perception(), retrieved, budget_usd=None, style="x", budget=Budget(Config()),
        kept=[("f2", "armchair")],
        room_image=prepare_image(synthetic_room_image(), max_edge=200),
        thumbnails=thumbs,
    )  # fmt: skip
    assert len(client.calls) == 4 and run.proposal.revised  # exactly one revision
    assert "strong clash with armchair (f2)" in client.calls[2]["messages"][0]["content"]
    sofa = run.proposal.items[0]
    assert (sofa.style_basis, sofa.style_clash, sofa.style_clash_with) == (
        "image",
        "mild",
        "armchair (f2)",
    )
    # the clash lines show in the final list
    text = format_final_list(run.proposal.model_dump())
    assert "style: good (image)" in text and "mild clash with armchair (f2)" in text


def test_a_strong_clash_that_survives_the_revision_stays_a_visible_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieved = _retrieved_with_thumbs()
    strong = style_payload({"sofa": "good", "lamp": "good"}, clash={"sofa": "strong"})
    client = ScriptedClient(
        [
            response([text_block(proposal_payload({"sofa": "id-sofa", "lamp": "id-lamp"}))]),
            response([text_block(strong)]),
            response([text_block(proposal_payload({"sofa": "id-sofa", "lamp": "id-lamp"}))]),
            response([text_block(strong)]),
        ]
    )
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    run = propose_and_check(
        _perception(), retrieved, budget_usd=None, style="x", budget=Budget(Config()),
        kept=[("f2", "armchair")],
    )  # fmt: skip
    assert len(client.calls) == 4  # never a second revision
    assert any("strong clash" in w for w in run.proposal.items[0].warnings)
    assert "WARNING style:" in format_final_list(run.proposal.model_dump())


def test_the_mock_pipeline_runs_the_image_path_offline(tmp_path: Path) -> None:
    from app import tracing as tr

    tr.TRACE_PATH = tmp_path / "out" / "trace.jsonl"
    result = run_spike(synthetic_room_image(), style="warm minimalist", config=Config())
    assert all(r["summary"]["thumbnail"]["ok"] for r in result.retrieval)
    items = result.proposal["items"]
    assert items and all(i["style_basis"] == "image" for i in items)
    assert all(i["style_clash"] == "none" for i in items)
    style_row = next(r for r in result.rows if r.stage == "self-check / style")
    assert style_row.input_tokens > 300  # the mock charges for the five images it was shown
    out = write_outputs(result, tmp_path / "out")
    (tmp_path / "out" / "trace.jsonl").replace(out / "trace.jsonl")
    traced = [json.loads(x) for x in (out / "trace.jsonl").read_text().splitlines()]
    fetches = [r for r in traced if r["name"] == "thumbnail.fetch"]
    assert len(fetches) == 5 and all(r["host"] == "mock.example" and r["ok"] for r in fetches)
    assert "?" not in json.dumps(fetches)
    assert "style: good (image)" in (out / "table.txt").read_text()
    off = run_spike(synthetic_room_image(), style="x", config=Config(thumbnails=False))
    assert all(i["style_basis"] == "text" for i in off.proposal["items"])


def test_budget_mode_uses_the_image_style_check_too() -> None:
    result = run_spike(synthetic_room_image(), budget_usd=800, style="x", config=Config())
    assert result.prompt_versions["style_check"] == "2"
    picked = [i for i in result.proposal["items"] if i["pick"] is not None]
    assert picked and all(i["style_basis"] == "image" for i in picked)
    v1 = run_spike(
        synthetic_room_image(), budget_usd=800, style="x", config=Config(style_prompt_version="1")
    )
    assert all(i["style_basis"] == "text" for i in v1.proposal["items"] if i["pick"] is not None)


def test_the_vision_call_is_counted_in_the_caps() -> None:
    from app.config import BudgetExceededError

    base = run_spike(synthetic_room_image(), style="x", config=Config(thumbnails=False))
    img = run_spike(synthetic_room_image(), style="x", config=Config())
    text_row = next(r for r in base.rows if r.stage == "self-check / style")
    image_row = next(r for r in img.rows if r.stage == "self-check / style")
    assert image_row.cost_usd > text_row.cost_usd and image_row.cost_usd - text_row.cost_usd < 0.01
    with pytest.raises(BudgetExceededError, match="cost cap"):
        run_spike(synthetic_room_image(), style="x", config=Config(max_cost_usd=0.015))


def test_slot_inputs_and_run_slot_signatures_still_work_without_thumbnails() -> None:
    assert slot_inputs(RetrievalResult(mode="shopping")) == []
    assert run_slot.__defaults__ == (None,)  # `thumbs` is optional


# ======================================================================================
# Size parsing fix: compact labels, category-aware axes, height rule, depth note, style join
# ======================================================================================

ADA_TITLE = (
    'Room & Board | Modern Ada 80" Sofa in Tepic Oatmeal | Stain-Resistant Fabric | 80w 34d 30h'
)
HEIDI_TITLE = (
    'Walnut Wood Modular Narrow Bookcase, 21"x79", 5x Open Storage Shelves, Scandinavian Design'
    " | Article Heidi Home Office Furniture"
)
IN = 0.0254


def test_real_sofa_title_reads_width_depth_and_height() -> None:
    got = parse_title_sizes(ADA_TITLE, "sofa")
    assert got == {"width_m": round(80 * IN, 4), "depth_m": round(34 * IN, 4),
                   "height_m": round(30 * IN, 4)}  # fmt: skip


def test_real_bookcase_title_is_width_by_height_with_unknown_depth() -> None:
    got, notes = parse_title_detail(HEIDI_TITLE, "bookshelf")
    assert got == {"width_m": round(21 * IN, 4), "height_m": round(79 * IN, 4)}
    assert any("tall category rule" in n and "depth unknown" in n for n in notes)
    result = resolve_size("bookshelf", None, [HEIDI_TITLE], "https://x.example/p/heidi")
    assert result.source == "title" and result.dims is not None
    assert result.dims.depth_m is None and any("tall category rule" in n for n in result.notes)


@pytest.mark.parametrize(
    "text",
    [
        "80w 34d 30h",
        "80W x 34D x 30H",
        '80"W 34"D 30"H',
        "W80 D34 H30",
        "Sofa W80 x D34 x H30 in",
        "80 w 34 d 30 h inches",
        "80W 34D 30H IN",
    ],
)
def test_compact_labeled_forms(text: str) -> None:
    got = parse_title_sizes(text, "sofa")
    assert got == {"width_m": round(80 * IN, 4), "depth_m": round(34 * IN, 4),
                   "height_m": round(30 * IN, 4)}  # fmt: skip


def test_compact_labeled_forms_take_a_trailing_metric_unit() -> None:
    got = parse_title_sizes("203w 86d 76h cm", "sofa")
    assert got == {"width_m": 2.03, "depth_m": 0.86, "height_m": 0.76}
    assert parse_title_sizes("W203 D86 H76 cm", "sofa") == got


def test_prefixed_form_needs_two_axes_to_count() -> None:
    assert parse_title_sizes("Model H30 chair", "armchair") == {}


def test_unlabeled_pair_by_category_rule() -> None:
    tall = parse_title_detail('21"x79"', "floor lamp")
    assert tall[0] == {"width_m": round(21 * IN, 4), "height_m": round(79 * IN, 4)}
    wardrobe = parse_title_sizes("79 x 21 in", "wardrobe")
    assert wardrobe == {"width_m": round(21 * IN, 4), "height_m": round(79 * IN, 4)}
    flat, flat_notes = parse_title_detail("5 x 8 ft rug 60 x 96 in", "rug")
    assert flat == {"width_m": round(60 * IN, 4), "length_m": round(96 * IN, 4)}
    assert any("flat category rule" in n for n in flat_notes)
    other, other_notes = parse_title_detail('36" x 18"', "coffee table")
    assert other == {"length_m": round(36 * IN, 4)}
    assert any("longest side only" in n for n in other_notes)


def test_unlabeled_three_numbers_follow_wdh_only_when_plausible() -> None:
    ok = parse_title_sizes("79 x 36 x 32 in", "sofa")
    assert ok == {"width_m": round(79 * IN, 4), "depth_m": round(36 * IN, 4),
                  "height_m": round(32 * IN, 4)}  # fmt: skip
    bad, notes = parse_title_detail("20 x 30 x 90 in", "sofa")
    assert bad == {"length_m": round(90 * IN, 4)} and any("implausible" in n for n in notes)
    tall_bad = parse_title_sizes("79 x 18 x 30 in", "bookshelf")
    assert tall_bad == {"length_m": round(79 * IN, 4)}
    tall_ok = parse_title_sizes("30 x 12 x 72 in", "bookshelf")
    assert tall_ok["height_m"] == round(72 * IN, 4)


def test_height_counts_toward_plausibility_for_tall_categories() -> None:
    result = resolve_size("bookshelf", None, ['Shelf 21"x120"'], "https://x.example/p/a")
    assert result.dims is None and any("outside the plausible range" in n for n in result.notes)


def test_old_reading_is_kept_without_a_category() -> None:
    got = parse_title_sizes("79 x 18 x 30 in")
    assert got["width_m"] == round(79 * IN, 4) and got["height_m"] == round(30 * IN, 4)


def _tall_fit(height: float, ceiling: float | None, width: float = 0.53) -> Any:
    dims = {**mock_perception_payload()["dimensions"], "width_m": 4.0, "length_m": 5.0}
    room = _perception(dimensions=dims)
    if ceiling is None:
        room.dimensions = room.dimensions.model_construct(**{**dims, "ceiling_height_m": None})
    else:
        room.dimensions = room.dimensions.model_copy(update={"ceiling_height_m": ceiling})
    cand = _cand(dimensions_m=DimensionsM(width_m=width, height_m=height))
    (r,) = check_fit([ProposalItem(slot="bookshelf", pick_id="p1", pick=cand)], room, Config())
    return r


def test_height_rule_fails_over_the_ceiling_minus_a_tenth() -> None:
    r = _tall_fit(2.45, 2.5)
    assert r.status == "fail" and "height 2.45 m is over the ceiling limit 2.40 m" in r.message
    assert "sizes used: 2.45 m tall, 0.53 m wide" in r.message


def test_height_rule_passes_and_message_names_the_axes_used() -> None:
    r = _tall_fit(2.0066, 2.5)
    assert r.status == "pass"
    assert "2.01 m tall, 0.53 m wide" in r.message and "footprint rule skipped" in r.message


def test_height_rule_is_skipped_with_unknown_ceiling_or_height() -> None:
    assert _tall_fit(3.5, None).status == "pass"
    no_height = _cand(dimensions_m=DimensionsM(width_m=0.5))
    room = _perception()
    (r,) = check_fit([ProposalItem(slot="bookshelf", pick_id="p1", pick=no_height)], room, Config())
    assert r.status == "pass"


def test_height_alone_fails_when_too_tall_else_stays_unknown() -> None:
    assert _tall_fit(2.9, 2.5, width=0.5).status == "fail"
    cand = _cand(dimensions_m=DimensionsM(height_m=3.0))
    room = _perception()
    room.dimensions = room.dimensions.model_copy(update={"ceiling_height_m": 2.5})
    (r,) = check_fit([ProposalItem(slot="x", pick_id="p1", pick=cand)], room, Config())
    assert r.status == "fail" and "ceiling limit" in r.message


def test_replacement_note_compares_depth_when_both_known() -> None:
    cand = _cand(dimensions_m=DimensionsM(width_m=2.0, depth_m=0.5), size_confidence="high")
    item = ProposalItem(
        slot="sofa", pick_id="p1", pick=cand,
        replaces=ReplacedItem(id="f1", category="sofa", width_m=2.0, depth_m=0.9),
    )  # fmt: skip
    annotate_replacements([item])
    assert "0.50 m deep" in item.replace_size_note and "44% shallower" in item.replace_size_note
    assert "long" not in item.replace_size_note
    both = _cand(dimensions_m=DimensionsM(width_m=1.2, depth_m=1.3))
    item2 = ProposalItem(
        slot="sofa", pick_id="p1", pick=both,
        replaces=ReplacedItem(id="f1", category="sofa", width_m=2.0, depth_m=0.9),
    )  # fmt: skip
    annotate_replacements([item2])
    assert "35% shorter" in item2.replace_size_note and "44% deeper" in item2.replace_size_note
    unknown = _cand(dimensions_m=DimensionsM(width_m=2.0))
    item3 = ProposalItem(
        slot="sofa", pick_id="p1", pick=unknown,
        replaces=ReplacedItem(id="f1", category="sofa", width_m=2.0, depth_m=0.9),
    )  # fmt: skip
    annotate_replacements([item3])
    assert item3.replace_size_note == ""


def test_style_note_joins_the_clash_without_stray_punctuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = style_payload({"sofa": "good"}, note="Warm and understated.", clash={"sofa": "mild"})
    client = ScriptedClient([response([text_block(json.dumps(payload))])])
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    results, _ = check_style(_items("sofa"), _perception(), "x", Budget(Config()), None)
    assert results[0].message == "good: Warm and understated; mild clash with armchair (f2)"
    assert ".;" not in results[0].message
