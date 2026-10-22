from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from app import llm, shopping_retrieval
from app.config import Config
from app.images import synthetic_room_image
from app.llm import Budget, Completion
from app.mock_llm import (
    ScriptedClient,
    mock_perception_payload,
    proposal_payload,
    response,
    style_payload,
    text_block,
)
from app.preferences import (
    PreferenceError,
    check_flag_overlap,
    resolve_preferences,
    split_names,
)
from app.prompts import load_prompt
from app.proposal import proposal_user_text, propose_and_check, slot_inputs, validate_proposal
from app.retrieval import (
    RetrievalResult,
    SlotResult,
    derive_slots,
    item_ceiling_usd,
    plan_slots,
    retrieve,
)
from app.schemas import ProductCandidate, ProposalItem, ProposalOutput, RoomPerception
from app.selfcheck import (
    FLAT_NOTE,
    MISMATCH_NOTE,
    check_fit,
    check_grounded,
    title_mismatch,
)
from app.shopping import (
    FixtureSearch,
    ProductDetails,
    ProviderError,
    SearchCandidate,
    SerpApiProductLookup,
    Store,
)
from app.spike import run_spike, write_outputs

FIXTURES = Path(__file__).parent / "fixtures"


def _perception(**overrides: Any) -> RoomPerception:
    payload = copy.deepcopy(mock_perception_payload())
    payload.update(overrides)
    return RoomPerception.model_validate(payload)


def _live_room() -> RoomPerception:
    data = json.loads((FIXTURES / "perception_live_run_categories.json").read_text())
    return RoomPerception.model_validate(data)


def _furniture(fid: str, category: str, keepable: str = "likely") -> dict[str, Any]:
    return {
        "id": fid, "category": category, "position": {"x": 0.5, "y": 0.5},
        "size": {"width_m": 2.0, "depth_m": 0.9}, "keepable": keepable, "note": "-",
    }  # fmt: skip


# ---- flags and preferences --------------------


def test_split_names() -> None:
    assert split_names("sofa, f2 ,,coffee table") == ["sofa", "f2", "coffee table"]
    assert split_names(None) == [] and split_names("") == []


def test_ids_categories_and_synonyms_resolve() -> None:
    p = _perception()  # f1 sofa (unlikely), f2 armchair (likely)
    by_id = {r.id: r for r in resolve_preferences(p, keep=["f1"], replace=["F2"]).resolved}
    assert (by_id["f1"].decision, by_id["f1"].source) == ("keep", "requested")
    assert (by_id["f2"].decision, by_id["f2"].source) == ("replace", "requested")
    by_cat = {r.id: r for r in resolve_preferences(p, keep=[], replace=[" Couch "]).resolved}
    assert by_cat["f1"].decision == "replace" and by_cat["f1"].source == "requested"  # synonym
    assert by_cat["f2"].decision == "keep" and by_cat["f2"].source == "default"


def test_defaults_come_from_the_keepable_flag() -> None:
    p = _perception(
        furniture=[
            _furniture("f1", "sofa", "unlikely"),
            _furniture("f2", "desk", "likely"),
            _furniture("f3", "bench", "unclear"),
        ]
    )
    got = {r.id: (r.decision, r.source) for r in resolve_preferences(p).resolved}
    assert got == {
        "f1": ("replace", "suggested"), "f2": ("keep", "default"), "f3": ("keep", "default"),
    }  # fmt: skip


def test_a_category_naming_several_items_applies_to_all_of_them() -> None:
    p = _perception(furniture=[_furniture("f1", "office chair"), _furniture("f2", "armchair")])
    with pytest.raises(PreferenceError):  # "chair" is neither an id nor a whole category
        resolve_preferences(p, replace=["chair"])
    # an exact shared category does match both
    p2 = _perception(furniture=[_furniture("f1", "stool"), _furniture("f2", "stool")])
    assert {r.id for r in resolve_preferences(p2, replace=["stool"]).decided("replace")} == {
        "f1",
        "f2",
    }


def test_unknown_name_lists_the_valid_ids_and_categories() -> None:
    p = _perception()
    with pytest.raises(PreferenceError) as info:
        resolve_preferences(p, replace=["piano"])
    msg = str(info.value)
    assert "'piano'" in msg and "--replace" in msg
    assert (
        "f1 (sofa)" in msg
        and "f2 (armchair)" in msg
        and "valid categories: armchair, desk, sofa" in msg
    )
    with pytest.raises(PreferenceError, match="--keep"):
        resolve_preferences(p, keep=["f9"])


def test_the_same_item_in_both_flags_is_an_error() -> None:
    p = _perception()
    with pytest.raises(PreferenceError, match="both --keep and --replace"):
        resolve_preferences(p, keep=["sofa"], replace=["f1"])  # same item by category and by id
    with pytest.raises(PreferenceError, match="both"):
        resolve_preferences(p, keep=["sofa"], replace=["Sofa"])
    with pytest.raises(PreferenceError, match="both"):
        check_flag_overlap(["f1"], ["F1"])
    check_flag_overlap(["f1"], ["f2"])  # distinct names are fine


def test_resolution_serializes_for_result_json() -> None:
    prefs = resolve_preferences(_perception(), keep=["armchair"], replace=["sofa"])
    assert prefs.to_json() == {
        "keep": ["armchair"],
        "replace": ["sofa"],
        "resolved": [
            {"id": "f1", "category": "sofa", "decision": "replace", "source": "requested"},
            {"id": "f2", "category": "armchair", "decision": "keep", "source": "requested"},
            {"id": "f3", "category": "desk", "decision": "keep", "source": "default"},
        ],
    }


# ---- slot policy --------------------


def test_slot_order_is_requested_replacements_gaps_suggested_then_accents() -> None:
    p = _perception(
        furniture=[
            _furniture("f1", "sofa", "unlikely"),
            _furniture("f2", "armchair"),
            _furniture("f3", "desk"),
        ],
        furniture_gaps=[{"category": "coffee table", "reason": "bare floor"}],
    )
    prefs = resolve_preferences(p, replace=["desk"])
    plan = plan_slots(p, 800, 6, prefs)
    assert [(s.category, s.source) for s in plan.slots] == [
        ("desk", "replace_requested"),
        ("coffee table", "gap"),
        ("sofa", "replace_suggested"),
        ("area rug", "accent"),
        ("floor lamp", "accent"),
        ("bookshelf", "accent"),
    ]  # the replaced desk and sofa categories never appear as accents; armchair is kept


def test_default_cap_is_three_and_dropped_requests_are_reported() -> None:
    p = _perception(
        furniture=[_furniture(f"f{i}", c) for i, c in enumerate(["sofa", "desk", "bed"], start=1)],
        furniture_gaps=[
            {"category": "coffee table", "reason": "-"},
            {"category": "bookshelf", "reason": "-"},
        ],
    )
    prefs = resolve_preferences(p, replace=["sofa", "desk", "bed"])
    plan = plan_slots(p, 800, 3, prefs)
    assert [s.category for s in plan.slots] == ["sofa", "desk", "bed"]
    assert plan.dropped_by_cap == ["gap: coffee table", "gap: bookshelf"]
    assert plan.to_json()["dropped_by_cap"] == plan.dropped_by_cap
    # accents past the cap are filler, not reported as dropped
    p2 = _perception(furniture_gaps=[])
    assert plan_slots(p2, 800, 1).dropped_by_cap == []


def test_accents_skip_what_stays_using_the_last_live_room() -> None:
    p = _live_room()  # has a rug and a floor lamp as decor, a nesting side table, a fireplace...
    plan = plan_slots(p, 800, 3)
    categories = [s.category for s in plan.slots]
    # the gap comes first, then accents; everything the room already has is skipped
    assert categories == ["media unit or TV", "bookshelf", "console table"]
    assert [s.source for s in plan.slots] == ["gap", "accent", "accent"]
    covered = {"sofa", "coffee table", "area rug", "floor lamp", "armchair", "side table"}
    assert not covered & set(categories) and "media unit" not in categories
    assert any("side table beside sofa" in s for s in plan.skipped)  # covered by the kept one


def test_a_replaced_items_category_is_not_an_accent_but_a_kept_one_is_covered() -> None:
    p = _perception(furniture=[_furniture("f1", "sofa", "unlikely")], furniture_gaps=[])
    cats = lambda prefs: [s.category for s in plan_slots(p, 800, 6, prefs).slots]  # noqa: E731
    assert cats(resolve_preferences(p, keep=["sofa"])).count("sofa") == 0  # kept: covered
    assert cats(resolve_preferences(p, replace=["sofa"])).count("sofa") == 1  # only as its own slot


def test_replaces_id_and_size_hint_are_set_for_replacement_slots_only() -> None:
    p = _perception()
    slots = {s.category: s for s in derive_slots(p, 800, 6)}
    sofa = slots["sofa"]
    assert sofa.source == "replace_suggested" and sofa.replaces_id == "f1"
    assert sofa.size_hint == "replaces your sofa (f1), about 2.0 m x 0.9 m"
    assert slots["coffee table"].replaces_id is None and slots["coffee table"].size_hint == ""
    assert slots["area rug"].replaces_id is None


def test_requested_replacement_beats_a_keepable_flag() -> None:
    p = _perception()
    plan = plan_slots(p, 800, 3, resolve_preferences(p, replace=["armchair"]))
    assert plan.slots[0].category == "armchair" and plan.slots[0].source == "replace_requested"
    assert plan.slots[0].replaces_id == "f2"


# ---- budget ceiling --------------------


def test_ceiling_formula_and_its_use_for_every_slot() -> None:
    assert item_ceiling_usd(800, Config()) == round(800 * 0.6 / 1.1, 2) == 436.36
    assert item_ceiling_usd(1000, Config(item_cap_fraction=0.5, shipping_buffer=0.25)) == 400.0
    slots = derive_slots(_perception(), 800, 3)
    assert {s.budget_usd for s in slots} == {436.36}  # the same for every slot, no equal split
    assert {s.total_cap_usd for s in slots} == {480.0}  # budget x item cap


class _Search:
    name = "serper"

    def __init__(self, results: list[SearchCandidate]) -> None:
        self.results = results

    def search(self, query: str, gl: str = "us", hl: str = "en") -> list[SearchCandidate]:
        return self.results


class _Lookup:
    name = "serpapi"

    def __init__(self, details: dict[str, ProductDetails | Exception]) -> None:
        self.details = details

    def lookup(self, product_id: str, gl: str = "us", hl: str = "en") -> ProductDetails:
        out = self.details.get(product_id, ProductDetails())
        if isinstance(out, Exception):
            raise out
        return out


def _sc(pid: str, price: float, **kw: Any) -> SearchCandidate:
    return SearchCandidate(
        pid, kw.get("title", f"Product {pid}"), "Shop", price, "USD", 4.5, 10,
        f"https://g.example/{pid}",
    )  # fmt: skip


def _store(url: str, price: float, shipping: float | None, total: float | None) -> Store:
    return Store("Shop", url, price, shipping, total, None)


def _retrieve(
    monkeypatch: pytest.MonkeyPatch,
    results: list[SearchCandidate],
    lookups: dict[str, ProductDetails | Exception],
    ranked: list[str],
    *,
    budget_usd: float = 1000.0,
) -> RetrievalResult:
    client = ScriptedClient(
        [response([text_block({"finalists": [{"id": i, "reason": "r"} for i in ranked]})])]
    )
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    p = _perception(furniture=[], furniture_gaps=[{"category": "sofa", "reason": "-"}])
    return retrieve(
        p, budget_usd=budget_usd, style="x", max_slots=1, budget=Budget(Config()),
        providers=(_Search(results), _Lookup(lookups)),
    )  # fmt: skip


def test_search_keeps_only_results_priced_at_or_under_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ceiling = item_ceiling_usd(1000, Config())  # 545.45
    results = [_sc("ok", ceiling), _sc("over", ceiling + 0.01)]
    out = _retrieve(monkeypatch, results, {}, ["ok"])
    assert out.slots[0].summary["search"]["over_budget"] == 1
    assert [c.product_id for c in out.slots[0].candidates] == ["ok"]


def test_finalists_over_the_total_cap_after_lookup_are_dropped_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cap = 1000 x 0.6 = 600. "cheap" is $540 + $50 shipping = $590 (kept); "pricey" is $540 +
    # $80 = $620, which passed the $545 search ceiling but not the total cap.
    lookups: dict[str, ProductDetails | Exception] = {
        "cheap": ProductDetails([_store("https://s.example/products/a", 540, 50, 590)]),
        "pricey": ProductDetails([_store("https://s.example/products/b", 540, 80, 620)]),
    }
    out = _retrieve(
        monkeypatch, [_sc("cheap", 540), _sc("pricey", 540)], lookups, ["pricey", "cheap"]
    )
    slot = out.slots[0]
    assert [c.product_id for c in slot.candidates] == ["cheap"]
    assert {"name": "Product pricey", "reason": "total_over_item_cap", "total": "620.00",
            "cap": "600.00"} in slot.dropped  # fmt: skip
    assert slot.summary["over_item_cap_after_lookup"] == 1


def test_a_slot_can_end_with_no_finalists_and_carries_through_with_a_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookups: dict[str, ProductDetails | Exception] = {
        "a": ProductDetails([_store("https://s.example/products/a", 540, 100, 640)])
    }
    out = _retrieve(monkeypatch, [_sc("a", 540)], lookups, ["a"])
    slot = out.slots[0]
    assert slot.candidates == [] and "over the $600 per-item cap" in slot.note
    items, _ = validate_proposal(
        ProposalOutput.model_validate(proposal_payload({"sofa": None})), slot_inputs(out)
    )
    assert items[0].pick is None and "per-item cap" in items[0].note


# ---- proposal v2 --------------------------------------------------------------


def _cand(pid: str, price: float, **kw: Any) -> ProductCandidate:
    return ProductCandidate(
        name=kw.get("name", f"Product {pid}"), retailer="Shop", price=price, currency="USD",
        url=kw.get("url", f"https://shop.example/products/{pid}"),
        url_kind=kw.get("url_kind", "retailer_product"), shipping=0.0, product_id=pid,
        price_source="lookup", store_title=kw.get("store_title", ""),
    )  # fmt: skip


def _retrieved_with_replacement() -> RetrievalResult:
    p = _perception()
    plan = plan_slots(p, 800, 2, resolve_preferences(p, replace=["sofa"]))
    out = RetrievalResult(mode="shopping", plan=plan)
    for slot, cands in zip(plan.slots, ([_cand("a1", 100)], [_cand("b1", 50)]), strict=True):
        out.slots.append(SlotResult(slot, cands, [], "", Completion(text="")))
    return out


def test_proposal_v2_items_record_what_they_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    retrieved = _retrieved_with_replacement()
    names = [s.slot.category for s in retrieved.slots]
    picks = {s.slot.category: s.candidates[0].product_id for s in retrieved.slots}
    client = ScriptedClient(
        [
            response([text_block(proposal_payload(picks))]),
            response([text_block(style_payload({n: "good" for n in names}))]),
        ]
    )  # fmt: skip
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    run = propose_and_check(
        _perception(), retrieved, budget_usd=800, style="x", budget=Budget(Config()),
        kept=[("f2", "armchair")],
    )  # fmt: skip
    sofa = next(i for i in run.proposal.items if i.slot == "sofa")
    assert sofa.replaces is not None
    assert (sofa.replaces.id, sofa.replaces.category) == ("f1", "sofa")
    assert (sofa.replaces.width_m, sofa.replaces.depth_m) == (2.0, 0.9)
    assert next(i for i in run.proposal.items if i.slot != "sofa").replaces is None
    request = client.calls[0]["messages"][0]["content"]
    assert "Replaces: sofa (f1), about 2.0 m x 0.9 m" in request
    assert "Keeping (stays in the room): armchair (f2)" in request
    assert run.prompt_versions["proposal"] == "2"


def test_proposal_prompt_versions_v2_current_and_v1_selectable() -> None:
    v2, v1 = load_prompt("proposal"), load_prompt("proposal", "1")
    assert (v2.version, v1.version) == ("2", "1") and v1.text != v2.text
    assert "replace an existing item" in v2.text.lower() or "Replaces:" in v2.text
    assert "whole list, not per slot" in v2.text and "Replaces:" not in v1.text
    assert Config().proposal_prompt_version == "2"
    assert load_prompt("rank").version == "3"
    assert load_prompt("rank", "2").version == "2" and load_prompt("rank", "1").version == "1"


def test_v1_can_be_selected_and_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_spike(
        synthetic_room_image(), budget_usd=800, style="x",
        config=Config(proposal_prompt_version="1"),
    )  # fmt: skip
    assert result.prompt_versions["proposal"] == "1"


def test_proposal_text_marks_the_replaced_item_and_empty_slots() -> None:
    retrieved = _retrieved_with_replacement()
    text = proposal_user_text(_perception(), 800, "x", slot_inputs(retrieved, _perception()))
    assert text.count("Replaces:") == 1 and "Keeping" not in text


# ---- title / page mismatch --------------------

JUDITH_SEARCH = 'Article Oak Wood Media Unit 79"W Low Profile Closed Storage'
JUDITH_URL = "https://www.article.com/pla/26917/judith-63-low-media-unit-walnut"
LEIF_SEARCH = 'Leif 54" Oak Wood Media Unit, Scandinavian Design'
LEIF_URL = "https://www.article.com/pla/26917/leif-54-media-unit-smoked-oak"


def _grounded(c: ProductCandidate) -> Any:
    return check_grounded(
        [ProposalItem(slot="media unit", pick_id=c.product_id, pick=c)],
        {"media unit": {c.product_id}},
    )[0]


def test_the_real_example_fails_and_the_leif_example_passes() -> None:
    assert title_mismatch(JUDITH_SEARCH, "", JUDITH_URL) is True
    assert title_mismatch(LEIF_SEARCH, "", LEIF_URL) is False
    bad = _grounded(_cand("p", 100, name=JUDITH_SEARCH, url=JUDITH_URL))
    assert bad.status == "fail" and MISMATCH_NOTE in bad.message
    assert MISMATCH_NOTE == "store page title disagrees with search title (size or material)"
    assert _grounded(_cand("p", 100, name=LEIF_SEARCH, url=LEIF_URL)).status == "pass"


def test_sizes_compare_across_units_with_rounding_tolerance() -> None:
    assert not title_mismatch("Sofa 120 cm wide", 'Sofa 47" wide', "https://x.example/p/sofa")
    assert title_mismatch("Sofa 100 cm wide", 'Sofa 47" wide', "https://x.example/p/sofa")
    assert not title_mismatch("Table 600 mm", 'Table 23.6"', "https://x.example/p/t")
    assert not title_mismatch('Desk 48"', "Desk 48 in", "https://x.example/p/d")


def test_no_size_or_no_material_on_either_side_is_not_a_mismatch() -> None:
    assert not title_mismatch(
        "Nice grey sofa", "Different grey couch", "https://x.example/p/sofa-1"
    )
    assert not title_mismatch('Oak desk 48"', "Desk", "https://x.example/p/desk")  # one-sided
    assert not title_mismatch("Desk", 'Walnut desk 60"', "https://x.example/p/desk")


def test_materials_match_by_word_and_disjoint_sets_fail() -> None:
    assert not title_mismatch("Oak table", "Smoked oak coffee table", "https://x.example/p/t")
    assert title_mismatch("Oak table", "Walnut table", "https://x.example/p/t")
    assert not title_mismatch("Oak and glass table", "Glass top table", "https://x.example/p/t")


def test_store_title_is_used_alongside_the_slug() -> None:
    c = _cand("p", 100, name="Oak side table 20\"", url="https://x.example/p/table",
              store_title='Walnut side table 20"')  # fmt: skip
    assert _grounded(c).status == "fail"


def test_a_google_panel_link_is_not_checked_for_a_mismatch() -> None:
    c = _cand("p", 100, name=JUDITH_SEARCH, url="https://www.google.com/search?ibp=oshop",
              url_kind="google_panel")  # fmt: skip
    assert _grounded(c).status == "pass"


# ---- flat goods --------------------


@pytest.mark.parametrize("slot", ["rug", "area rug", "runner", "door mat", "carpet", "Rug"])
def test_flat_items_skip_the_footprint_rule(slot: str) -> None:
    huge = ProductCandidate(
        name="x", retailer="s", price=1, currency="USD", url="https://s.example/p/1",
        dimensions_m={"width_m": 4.0, "depth_m": 3.0}, product_id="1", price_source="lookup",
    )  # fmt: skip
    (r,) = check_fit([ProposalItem(slot=slot, pick_id="1", pick=huge)], _perception(), Config())
    assert (
        r.status == "unknown" and r.message == FLAT_NOTE == "flat item; footprint rule not applied"
    )


def test_other_items_are_still_checked_including_words_that_merely_contain_mat() -> None:
    huge = ProductCandidate(
        name="x", retailer="s", price=1, currency="USD", url="https://s.example/p/1",
        dimensions_m={"width_m": 4.0, "depth_m": 3.0}, product_id="1", price_source="lookup",
    )  # fmt: skip
    for slot in ("mattress", "sofa"):
        (r,) = check_fit([ProposalItem(slot=slot, pick_id="1", pick=huge)], _perception(), Config())
        assert r.status == "fail"


# ---- lookup failures --------------------


def _cli() -> Any:
    spec = importlib.util.spec_from_file_location(
        "spike_cli", Path(__file__).resolve().parents[1] / "scripts" / "spike.py"
    )
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def test_lookup_429_warns_after_the_table_and_in_the_final_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    search = FixtureSearch("serper", "serper_search_oak_side_table.json")
    lookup = _Lookup({})
    lookup.lookup = lambda pid, gl="us", hl="en": (_ for _ in ()).throw(  # type: ignore[method-assign]
        ProviderError(
            "serpapi", "HTTP 429 rate limited or out of searches for this plan", status=429
        )
    )
    monkeypatch.setattr(shopping_retrieval, "build_providers", lambda config: (search, lookup))
    result = run_spike(synthetic_room_image(), budget_usd=800, style="x", config=Config())
    assert result.warnings and "product lookup(s) failed" in result.warnings[0]
    assert "HTTP 429" in result.warnings[0] and "Google page link" in result.warnings[0]
    table = (write_outputs(result, tmp_path) / "table.txt").read_text()
    assert table.index("WARNING 6 product lookup") < table.index("FINAL LIST")
    assert "[WARNING: 1 run warning(s) above]" in table
    cands = [c for r in result.retrieval for c in r["candidates"]]
    assert cands and all(c["url_kind"] == "google_panel" for c in cands)


def test_a_401_from_a_lookup_exits_4_without_printing_a_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    key = "SERPAPI-FAKE-KEY-9999zzzz"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad key {key}")

    lookup = SerpApiProductLookup(key, httpx.Client(transport=httpx.MockTransport(handler)))
    search = FixtureSearch("serper", "serper_search_oak_side_table.json")
    monkeypatch.setattr(shopping_retrieval, "build_providers", lambda config: (search, lookup))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_LLM_MODE", "mock")
    monkeypatch.setenv("SERPAPI_API_KEY", key)
    photo = tmp_path / "room.jpg"
    photo.write_bytes(synthetic_room_image())
    code = _cli().main([str(photo), "--budget", "800", "--style", "x"])
    captured = capsys.readouterr()
    assert code == 4
    assert "shopping provider error" in captured.err and "401" in captured.err
    assert key not in captured.out + captured.err
    for path in (tmp_path / "spike-output").rglob("*"):
        if path.is_file():
            assert key not in path.read_text()


def test_429_is_retried_once() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"product_results": {"stores": []}})

    lookup = SerpApiProductLookup("k" * 8, httpx.Client(transport=httpx.MockTransport(handler)))
    assert lookup.lookup("1").stores == [] and calls["n"] == 2


# ---- cli and output shape --------------------


def test_cli_unknown_name_exits_2_with_the_valid_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_LLM_MODE", "mock")
    photo = tmp_path / "room.jpg"
    photo.write_bytes(synthetic_room_image())
    code = _cli().main([str(photo), "--budget", "800", "--style", "x", "--replace", "piano"])
    err = capsys.readouterr().err
    assert code == 2 and "valid ids: f1 (sofa), f2 (armchair)" in err and "valid categories" in err


def test_cli_conflict_exits_2_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_LLM_MODE", "mock")
    photo = tmp_path / "room.jpg"
    photo.write_bytes(synthetic_room_image())
    base = [str(photo), "--budget", "800", "--style", "x"]
    assert _cli().main([*base, "--keep", "sofa", "--replace", "Sofa"]) == 2
    assert "both --keep and --replace" in capsys.readouterr().err
    assert not (tmp_path / "spike-output").exists()  # failed before perception, nothing written
    assert (
        _cli().main([*base, "--keep", "sofa", "--replace", "couch"]) == 2
    )  # caught after perception
    assert "both --keep and --replace" in capsys.readouterr().err


def test_end_to_end_with_flags_prints_replaces_and_keeping_and_writes_the_shape(
    tmp_path: Path,
) -> None:
    result = run_spike(
        synthetic_room_image(), budget_usd=800, style="warm minimalist", config=Config(),
        replace=["armchair"], keep=["sofa"],
    )  # fmt: skip
    out = write_outputs(result, tmp_path)
    data = json.loads((out / "result.json").read_text())
    assert data["preferences"] == {
        "keep": ["sofa"],
        "replace": ["armchair"],
        "resolved": [
            {"id": "f1", "category": "sofa", "decision": "keep", "source": "requested"},
            {"id": "f2", "category": "armchair", "decision": "replace", "source": "requested"},
            {"id": "f3", "category": "desk", "decision": "keep", "source": "default"},
        ],
    }
    assert data["kept_items"] == [
        {"id": "f1", "category": "sofa"}, {"id": "f3", "category": "desk"},
    ]  # fmt: skip
    assert data["slot_plan"]["slots"] == ["armchair", "coffee table", "area rug"]
    sources = [r["slot"]["source"] for r in data["retrieval"]]
    assert sources == ["replace_requested", "gap", "accent"]
    items = data["proposal"]["items"]
    assert items[0]["replaces"] == {
        "id": "f2", "category": "armchair", "width_m": 0.9, "depth_m": 0.9,
    }  # fmt: skip
    assert items[1]["replaces"] is None and len(items) == 3
    table = (out / "table.txt").read_text()
    assert "Replaces: armchair (f2)" in table and "Keeping: sofa (f1), desk (f3)" in table
    assert table.index("Keeping:") < table.index("Total: $")


def test_end_to_end_without_flags_uses_the_keepable_defaults(tmp_path: Path) -> None:
    result = run_spike(synthetic_room_image(), budget_usd=800, style="x", config=Config())
    assert len(result.proposal["items"]) > 1
    sources = [r["slot"]["source"] for r in result.retrieval]
    assert sources == ["gap", "replace_suggested", "accent"]
    assert result.kept_items == [
        {"id": "f2", "category": "armchair"}, {"id": "f3", "category": "desk"},
    ]  # fmt: skip
    assert {r["source"] for r in result.preferences["resolved"]} == {"suggested", "default"}
    table = (write_outputs(result, tmp_path) / "table.txt").read_text()
    assert "Replaces: sofa (f1)" in table and "Keeping: armchair (f2), desk (f3)" in table


def test_a_dropped_request_is_printed_in_the_run_output(tmp_path: Path) -> None:
    result = run_spike(
        synthetic_room_image(), budget_usd=800, style="x", config=Config(), slots=1,
        replace=["sofa", "armchair"],
    )  # fmt: skip
    table = (write_outputs(result, tmp_path) / "table.txt").read_text()
    assert result.slot_plan["dropped_by_cap"] == [
        "replace_requested: armchair",
        "gap: coffee table",
    ]
    assert (
        "NOTE slots over the cap of 1 were dropped: replace_requested: armchair; gap: coffee table"
        in table
    )


def test_fake_keys_appear_in_no_output_with_preferences(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import tracing

    keys = ["SERPER-FAKE-KEY-aaaa0000", "SERPAPI-FAKE-KEY-bbbb1111"]
    monkeypatch.setenv("SERPER_API_KEY", keys[0])
    monkeypatch.setenv("SERPAPI_API_KEY", keys[1])
    monkeypatch.setattr(tracing, "TRACE_PATH", tmp_path / "out" / "trace.jsonl")
    result = run_spike(
        synthetic_room_image(), budget_usd=800, style="x", config=Config(), replace=["sofa"]
    )
    out = write_outputs(result, tmp_path / "out")
    (tmp_path / "out" / "trace.jsonl").replace(out / "trace.jsonl")
    assert not any(k in "\n".join(p.read_text() for p in out.iterdir()) for k in keys)
