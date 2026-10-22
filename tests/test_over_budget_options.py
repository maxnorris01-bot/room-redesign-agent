from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from app import llm, shopping_retrieval
from app.config import Config
from app.images import synthetic_room_image
from app.llm import Budget
from app.mock_llm import (
    ScriptedClient,
    mock_perception_payload,
    proposal_payload,
    response,
    style_payload,
    text_block,
)
from app.preferences import resolve_preferences
from app.proposal import propose_and_check, slot_inputs, validate_proposal
from app.retrieval import derive_slots, plan_slots, retrieve
from app.schemas import (
    OverBudgetOption,
    ProductCandidate,
    ProposalOutput,
    RoomPerception,
)
from app.shopping import (
    FixtureLookup,
    FixtureSearch,
    ProductDetails,
    ProviderError,
    SearchCandidate,
    Store,
    parse_product_details,
)
from app.shopping_retrieval import price_stats
from app.spike import _empty_slot_lines, format_final_list, run_spike, write_outputs

FIXTURES = Path(__file__).parent / "fixtures"


def _sc(pid: str, price: float | None, **kw: Any) -> SearchCandidate:
    return SearchCandidate(
        pid, kw.get("title", f"Product {pid}"), kw.get("seller", "Shop"), price, "USD", 4.5, 10,
        f"https://g.example/{pid}",
    )  # fmt: skip


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
        self.ids: list[str] = []

    def lookup(self, product_id: str, gl: str = "us", hl: str = "en") -> ProductDetails:
        self.ids.append(product_id)
        out = self.details.get(product_id, ProductDetails(note="no product_results"))
        if isinstance(out, Exception):
            raise out
        return out


def _store(slug: str, price: float, shipping: float | None, total: float | None) -> ProductDetails:
    return ProductDetails(
        [Store("Shop", f"https://shop.example/products/{slug}", price, shipping, total, None)]
    )


def _perception(**overrides: Any) -> RoomPerception:
    payload = copy.deepcopy(mock_perception_payload())
    payload.update(overrides)
    return RoomPerception.model_validate(payload)


def _one_slot_perception(category: str = "sofa") -> RoomPerception:
    return _perception(furniture=[], furniture_gaps=[{"category": category, "reason": "-"}])


def _ranked(*ids: str) -> ScriptedClient:
    return ScriptedClient(
        [response([text_block({"finalists": [{"id": i, "reason": "r"} for i in ids]})])]
    )


def _retrieve(
    monkeypatch: pytest.MonkeyPatch,
    search: _Search,
    lookup: _Lookup,
    client: ScriptedClient,
    *,
    budget_usd: float = 1000.0,
    perception: RoomPerception | None = None,
    preferences: Any = None,
) -> Any:
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    return retrieve(
        perception or _one_slot_perception(), budget_usd=budget_usd, style="x", max_slots=1,
        budget=Budget(Config()), providers=(search, lookup), preferences=preferences,
    )  # fmt: skip


# ---- price stats --------------------------------------------------------------------------


def test_price_stats_cover_priced_unpriced_and_over_ceiling() -> None:
    results = [_sc("a", 10.0), _sc("b", 30.0), _sc("c", None), _sc("d", 50.0), _sc("e", 90.0)]
    assert price_stats(results, 40.0) == {
        "considered": 5, "priced": 4, "unpriced": 1, "over_ceiling": 2,
        "min_usd": 10.0, "median_usd": 40.0, "max_usd": 90.0, "p33_usd": 30.0, "p67_usd": 50.0,
    }  # fmt: skip


def test_price_stats_dedupe_by_id_and_use_only_the_top_25() -> None:
    results = [_sc("a", 10.0), _sc("a", 99.0), *[_sc(f"p{i}", 20.0 + i) for i in range(30)]]
    stats = price_stats(results, 1000.0)
    assert stats["considered"] == 25 and stats["priced"] == 24  # the top 25 minus one duplicate...
    assert stats["max_usd"] == 20.0 + 22  # ...so the last considered result is p22
    assert stats["min_usd"] == 10.0


def test_price_stats_with_nothing_priced_or_nothing_at_all() -> None:
    none_priced = price_stats([_sc("a", None)], 10.0)
    assert none_priced["priced"] == 0 and none_priced["unpriced"] == 1
    assert none_priced["min_usd"] is None and none_priced["median_usd"] is None
    empty = price_stats([], 10.0)
    assert empty["considered"] == 0 and empty["max_usd"] is None and empty["over_ceiling"] == 0


def test_stats_are_in_the_slot_summary_the_trace_and_the_plan_in_result_json(
    tmp_path: Path,
) -> None:
    from app import tracing

    tracing.TRACE_PATH = tmp_path / "t" / "trace.jsonl"
    result = run_spike(synthetic_room_image(), budget_usd=800, style="x", config=Config())
    sofa = next(r for r in result.retrieval if r["slot"]["category"] == "sofa")
    assert (
        sofa["summary"]["price_stats"]["over_ceiling"] == sofa["summary"]["price_stats"]["priced"]
    )
    assert set(result.slot_plan["price_stats"]) == {"coffee table", "sofa", "area rug"}
    traced = [json.loads(x) for x in tracing.TRACE_PATH.read_text().splitlines()]
    stage = next(r for r in traced if r["name"] == "stage.retrieval")
    assert all("price_stats" in s for s in stage["per_slot"])


# ---- option selection ---------------------------------------------------------------------


def _all_over(n: int = 10) -> list[SearchCandidate]:
    # ceiling for $1000 is 545.45: every result is over it, cheapest first is p0 at $600
    return [_sc(f"p{i}", 600.0 + 10 * i) for i in range(n)]


def test_two_cheapest_ranked_options_are_looked_up_and_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = _Lookup({"p1": _store("a", 610, 40, 650), "p3": _store("b", 630, None, None)})
    client = _ranked("p3", "p1", "p0")  # a third id beyond the top 2 is ignored
    out = _retrieve(monkeypatch, _Search(_all_over()), lookup, client)
    (slot,) = out.slots
    assert slot.candidates == []
    assert [o.candidate.product_id for o in slot.over_budget_options] == ["p3", "p1"]
    a, b = slot.over_budget_options[1], slot.over_budget_options[0]
    assert (a.reason, a.item_total, a.shipping_known, a.also_over_total_budget) == (
        "over_item_limit", 650.0, True, False,
    )  # fmt: skip
    assert (b.item_total, b.shipping_known) == (630.0, False)  # unknown shipping counted as 0 here
    assert lookup.ids == ["p3", "p1"]
    request = client.calls[0]["messages"][0]["content"]
    assert request.count("- id: ") == 8  # only the 8 cheapest are ranked
    assert "- id: p7 |" in request and "- id: p8 |" not in request
    assert "EVERY candidate below is above it" in request and "Pick at most 2" in request
    assert [c.kind for c in slot.calls].count("rank") == 1
    assert {c.phase for c in slot.calls if c.kind in ("rank", "lookup")} == {"over limit"}


def test_an_option_whose_lookup_fails_or_has_no_retailer_url_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = _Lookup(
        {
            "p0": ProviderError("serpapi", "HTTP 429 rate limited", status=429),
            "p1": _store("ok", 610, 40, 650),
        }
    )
    out = _retrieve(monkeypatch, _Search(_all_over()), lookup, _ranked("p0", "p1"))
    slot = out.slots[0]
    assert [o.candidate.product_id for o in slot.over_budget_options] == ["p1"]
    assert {"title": "Product p0", "why": "http error 429"} in slot.summary["lookup_issues"]


def test_a_google_page_fallback_is_not_a_verified_retailer_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _retrieve(  # no stores for p0 and the search result has a panel URL: still dropped
        monkeypatch, _Search(_all_over()), _Lookup({}), _ranked("p0", "p1")
    )
    assert out.slots[0].over_budget_options == []


def test_when_no_option_survives_only_the_stats_line_is_shown() -> None:
    item = {
        "slot": "sofa", "limit_usd": 436.36, "replaces": None, "note": "x", "no_finalists": True,
        "price_stats": {"priced": 8, "over_ceiling": 8, "min_usd": 479.12, "median_usd": 1114.88},
        "over_budget_options": [],
    }  # fmt: skip
    lines = _empty_slot_lines(1, item)
    assert lines[0] == "1. sofa: nothing found within your $436 per-item limit"
    assert lines[1] == (
        "     Cheapest sofa seen: $479.12 (median $1114.88, 8 results). "
        "Raise the budget or loosen the style to fit one."
    )
    assert len(lines) == 2 and not any("Over your limit" in x for x in lines)


def test_an_over_ceiling_pool_that_is_empty_costs_no_rank_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ScriptedClient([])
    out = _retrieve(monkeypatch, _Search([]), _Lookup({}), client)
    assert out.slots[0].over_budget_options == [] and client.calls == []


def test_finalists_dropped_for_the_total_cap_become_options_without_new_lookups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # budget 1000: ceiling 545.45, total cap 600, whole budget 1000
    lookup = _Lookup(
        {
            "a": _store("a", 540, 80, 620),  # over the cap after shipping
            "b": _store("b", 540, 500, 1040),  # also over the whole budget
            "c": ProductDetails([]),  # no store: dropped, never an option
        }
    )
    results = [_sc("a", 540), _sc("b", 541), _sc("c", 542)]
    client = ScriptedClient(
        [response([text_block({"finalists": [{"id": i, "reason": "r"} for i in ("a", "b")]})])]
    )
    out = _retrieve(monkeypatch, _Search(results), lookup, client)
    slot = out.slots[0]
    assert slot.candidates == [] and "over the $600 per-item cap" in slot.note
    opts = {o.candidate.product_id: o for o in slot.over_budget_options}
    assert set(opts) == {"a", "b"}
    assert opts["a"].reason == "over_total_cap" and opts["a"].also_over_total_budget is False
    assert opts["b"].also_over_total_budget is True and opts["b"].item_total == 1040.0
    assert lookup.ids == ["a", "b"]  # no extra lookups and no extra rank call
    assert len(client.calls) == 1


def test_a_filled_slot_gets_no_options_even_with_many_results_over_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = [_sc("ok", 100.0), *_all_over()]
    out = _retrieve(
        monkeypatch, _Search(results), _Lookup({"ok": _store("ok", 100, 10, 110)}), _ranked("ok")
    )
    slot = out.slots[0]
    assert [c.product_id for c in slot.candidates] == ["ok"] and slot.over_budget_options == []
    assert slot.summary["price_stats"]["over_ceiling"] == 10  # kept in the summary only


# ---- never picks --------------------------------------------------------------------------


def _option(pid: str = "o1", total: float = 700.0, **kw: Any) -> OverBudgetOption:
    cand = ProductCandidate(
        name=f"Option {pid}", retailer="Shop", price=total - 50, currency="USD",
        url=f"https://shop.example/products/{pid}", url_kind="retailer_product", shipping=50.0,
        product_id=pid, price_source="lookup",
    )  # fmt: skip
    return OverBudgetOption(
        candidate=cand, reason=kw.get("reason", "over_item_limit"), item_total=total,
        shipping_known=kw.get("shipping_known", True),
        also_over_total_budget=kw.get("over_total", False),
    )  # fmt: skip


def test_options_are_not_proposal_candidates_totals_or_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.llm import Completion
    from app.retrieval import RetrievalResult, Slot, SlotResult

    sofa = Slot("sofa", "why", "gap", 436.36, total_cap_usd=480.0, total_budget_usd=800.0)
    lamp = Slot("lamp", "why", "gap", 436.36, total_cap_usd=480.0, total_budget_usd=800.0)
    pick = ProductCandidate(
        name="Real lamp", retailer="Shop", price=100, currency="USD",
        url="https://shop.example/products/lamp", url_kind="retailer_product", shipping=10.0,
        total=110.0, product_id="l1", price_source="lookup",
    )  # fmt: skip
    retrieved = RetrievalResult(mode="shopping")
    empty = SlotResult(sofa, [], [], "none within limit", Completion(text=""))
    empty.over_budget_options = [_option("o1", 2000.0, over_total=True)]
    empty.summary = {
        "price_stats": {"priced": 3, "over_ceiling": 3, "min_usd": 500.0, "median_usd": 600.0}
    }
    retrieved.slots = [empty, SlotResult(lamp, [pick], [], "", Completion(text=""))]

    inputs = slot_inputs(retrieved, _perception())
    assert inputs[0].finalists == {} and len(inputs[0].over_budget_options) == 1
    client = ScriptedClient(
        [
            response([text_block(proposal_payload({"sofa": None, "lamp": "l1"}))]),
            response([text_block(style_payload({"lamp": "good"}))]),
        ]
    )
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    run = propose_and_check(
        _perception(), retrieved, budget_usd=800, style="x", budget=Budget(Config())
    )
    request = client.calls[0]["messages"][0]["content"]
    assert "Option o1" not in request and "o1" not in request.replace("Real lamp", "")
    assert "empty slot: nothing was found within the per-item limit" in request
    p = run.proposal
    assert p.totals.total == 110.0 and p.totals.item_count == 1  # the $2000 option is not counted
    assert p.check_summary["budget"] == "pass"
    sofa_item = p.items[0]
    assert sofa_item.pick is None and sofa_item.no_finalists is True
    assert [o.candidate.product_id for o in sofa_item.over_budget_options] == ["o1"]
    assert not any(c.item == "sofa" for c in p.checks)  # no check ever looked at the option


def test_the_model_summary_cannot_mention_options_because_it_never_sees_them() -> None:
    from app.llm import Completion
    from app.proposal import proposal_user_text
    from app.retrieval import RetrievalResult, Slot, SlotResult

    r = RetrievalResult(mode="shopping")
    sr = SlotResult(Slot("sofa", "why", "gap", 400.0), [], [], "", Completion(text=""))
    sr.over_budget_options = [_option("secretopt")]
    r.slots = [sr]
    text = proposal_user_text(_perception(), 800, "x", slot_inputs(r, _perception()))
    assert "secretopt" not in text and "Option" not in text


# ---- rendering ----------------------------------------------------------------------------


def test_an_empty_replace_requested_slot_is_rendered_by_code(tmp_path: Path) -> None:
    result = run_spike(
        synthetic_room_image(), budget_usd=800, style="x", config=Config(), replace=["sofa"]
    )
    table = (write_outputs(result, tmp_path) / "table.txt").read_text()
    assert result.retrieval[0]["slot"]["source"] == "replace_requested"
    assert (
        "sofa: nothing found within your $436 per-item limit\n     Replaces: sofa (f1)\n" in table
    )
    assert "Cheapest sofa seen: $479.12 (median $1114.88, 8 results). Raise the budget" in table
    assert "Over your limit, not counted in the total:" in table
    assert "[over per-item limit; also over your total budget]" in table
    assert "$1992.00 + $472.00 shipping = $2464.00" in table
    assert "https://www.article.com/pla/23361/torme-c-side-table-white-oak" in table


def test_an_empty_gap_slot_has_the_same_block_without_a_replaces_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def providers(config: Config) -> Any:
        search = FixtureSearch(
            "serper", "serper_search_oak_side_table.json",
            by_prefix={"coffee table": ("serper_search_oak_side_table.json", 8.0)},
        )  # fmt: skip
        lookup = FixtureLookup(
            {}, scaled={"x3228588211018347245": ("serpapi_product_article.json", 8.0)}
        )
        return search, lookup

    monkeypatch.setattr(shopping_retrieval, "build_providers", providers)
    result = run_spike(synthetic_room_image(), budget_usd=800, style="x", config=Config())
    table = (write_outputs(result, tmp_path) / "table.txt").read_text()
    assert result.retrieval[0]["slot"]["source"] == "gap"
    block = table[table.index("1. coffee table: nothing found") :]
    assert block.splitlines()[1].startswith("     Cheapest coffee table seen: $")
    assert "Replaces" not in block.splitlines()[1]


def test_option_lines_show_shipping_unknown_and_the_reason() -> None:
    item = {
        "slot": "sofa", "limit_usd": 100.0, "replaces": None, "note": "", "no_finalists": True,
        "price_stats": {"priced": 2, "over_ceiling": 2, "min_usd": 150.0, "median_usd": 160.0},
        "over_budget_options": [
            _option("u1", 150.0, shipping_known=False).model_dump(),
            _option("t1", 700.0, reason="over_total_cap").model_dump(),
        ],
    }  # fmt: skip
    text = "\n".join(_empty_slot_lines(1, item))
    assert "$100.00, shipping unknown" in text and "[over per-item limit]" in text
    assert "[over total cap after shipping]" in text and "also over" not in text


def test_a_filled_slot_prints_nothing_extra(tmp_path: Path) -> None:
    result = run_spike(synthetic_room_image(), budget_usd=800, style="x", config=Config())
    table = (write_outputs(result, tmp_path) / "table.txt").read_text()
    area_rug = table[table.index("area rug: [FIXTURE]") :].split("Keeping:")[0]
    assert "nothing found" not in area_rug and "Over your limit" not in area_rug


# ---- the mock scenario, cost, concurrency -------------------------------------------------


def test_default_mock_run_has_an_over_ceiling_slot_with_one_surviving_option() -> None:
    result = run_spike(synthetic_room_image(), budget_usd=800, style="x", config=Config())
    sofa = next(r for r in result.retrieval if r["slot"]["category"] == "sofa")
    assert sofa["candidates"] == [] and len(sofa["over_budget_options"]) == 1
    opt = sofa["over_budget_options"][0]
    assert opt["reason"] == "over_item_limit" and opt["also_over_total_budget"] is True
    assert opt["candidate"]["url_kind"] == "retailer_product" and opt["shipping_known"] is True
    assert any("lookup returned no store" in w for w in result.warnings)  # the failed 2nd option
    item = next(i for i in result.proposal["items"] if i["slot"] == "sofa")
    assert item["pick"] is None and item["no_finalists"] and item["over_budget_options"]
    assert result.proposal["totals"]["item_count"] == 2  # the option is not counted
    assert result.proposal["totals"]["total"] == 616.0


def test_over_limit_calls_are_rows_cost_and_stay_concurrent_with_other_slots() -> None:
    result = run_spike(synthetic_room_image(), budget_usd=800, style="x", config=Config())
    names = [r.stage for r in result.rows]
    assert "retrieval: sofa / rank (over limit)" in names
    assert "retrieval: sofa / lookup 1 (over limit)" in names
    assert "retrieval: sofa / lookup 2 (over limit)" in names
    over_rows = [r for r in result.rows if r.stage.endswith("(over limit)")]
    assert sum(r.cost_estimated for r in over_rows) == 2  # two estimated provider lookups
    assert result.provider_cost_usd == pytest.approx(
        sum(r.cost_usd for r in result.rows if r.cost_estimated)
    )
    # one Haiku rank call plus two lookups at most per such slot; the slot chain is still one chain
    assert [r.kind for r in over_rows].count("rank") == 1 and result.steps <= Config().max_steps
    # critical path counts the slowest slot chain once, not the sum of the slots
    by_slot: dict[str, float] = {}
    for r in result.rows:
        if r.slot and r.kind != "lookup":
            by_slot[r.slot] = by_slot.get(r.slot, 0.0) + r.seconds
    assert result._slowest_slot_chain() >= max(by_slot.values())


def test_rank_prompt_versions_and_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.prompts import load_prompt

    v3, v2 = load_prompt("rank"), load_prompt("rank", "2")
    assert (v3.version, v2.version) == ("3", "2")
    assert "above the per-item ceiling" in v3.text and "above the per-item ceiling" not in v2.text
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
    args = [str(photo), "--budget", "800", "--style", "x", "--slots", "1", "--rank-prompt-version"]
    assert cli.main([*args, "2"]) == 0
    data = json.loads(
        next((tmp_path / "spike-output").iterdir()).joinpath("result.json").read_text()
    )
    assert data["prompt_versions"]["rank"] == "2"


# ---- link hygiene -------------------------------------------------------------------------


def test_only_finalists_without_a_store_end_the_slot_empty_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def providers(config: Config) -> Any:
        search = FixtureSearch("serper", "serper_search_oak_side_table.json")
        return search, _Lookup({})  # every lookup: no product_results

    monkeypatch.setattr(shopping_retrieval, "build_providers", providers)
    result = run_spike(synthetic_room_image(), budget_usd=800, style="x", config=Config())
    assert all(
        r["candidates"] == [] or all(c["url_kind"] != "none" for c in r["candidates"])
        for r in result.retrieval
    )
    warnings = [w for w in result.warnings if w.startswith("lookup returned no store for ")]
    assert warnings and "(no product_results)" in warnings[0]
    table = (write_outputs(result, tmp_path) / "table.txt").read_text()
    assert "WARNING lookup returned no store for [FIXTURE]" in table


def test_alternates_without_a_url_or_with_a_mismatch_are_dropped() -> None:
    def cand(pid: str, **kw: Any) -> ProductCandidate:
        base: dict[str, Any] = {
            "name": f"Product {pid}", "retailer": "Shop", "price": 10.0, "currency": "USD",
            "url": f"https://shop.example/products/{pid}", "url_kind": "retailer_product",
            "product_id": pid, "price_source": "lookup",
        }  # fmt: skip
        return ProductCandidate(**{**base, **kw})

    from app.llm import Completion
    from app.retrieval import RetrievalResult, Slot, SlotResult

    finalists = [
        cand("pick"),
        cand("nolink", url="", url_kind="none"),
        cand("model", price_source="model"),
        cand("listing", url="https://shop.example/collections/sofas"),
        cand("mismatch", name='Oak table 20"', url="https://shop.example/products/walnut-table-30"),
        cand("good"),
        cand("panel", url="https://www.google.com/search?ibp=oshop", url_kind="google_panel"),
    ]
    r = RetrievalResult(mode="shopping")
    r.slots = [
        SlotResult(Slot("table", "why", "gap", 100.0), finalists, [], "", Completion(text=""))
    ]
    out = ProposalOutput.model_validate(
        proposal_payload(
            {"table": "pick"},
            alternates={"table": ["nolink", "model", "listing", "mismatch", "good", "panel"]},
        )
    )
    (item,), dropped = validate_proposal(out, slot_inputs(r))
    assert item.alternate_ids == ["good", "panel"]  # a Google page link only warns, as for a pick
    reasons = {d["id"]: d["reason"] for d in dropped}
    assert reasons["nolink"].startswith("alternate_failed_grounded: no link")
    assert "provider data" in reasons["model"] and "product page" in reasons["listing"]
    assert "disagrees" in reasons["mismatch"]


@pytest.mark.parametrize(
    ("payload", "note"),
    [
        ({}, "no product_results"),
        ({"product_results": {}}, "no product_results"),
        ({"product_results": {"stores": []}}, "no stores"),
        (
            {"product_results": {"stores": [{"name": "S", "link": "https://s.example/p/1"}]}},
            "",
        ),
    ],
)
def test_lookup_parse_records_why_there_is_nothing(payload: dict[str, Any], note: str) -> None:
    assert parse_product_details(payload).note == note


def test_lookup_failure_reasons_reach_the_trace_without_any_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import tracing

    monkeypatch.setenv("SERPAPI_API_KEY", "SERPAPI-FAKE-KEY-qqqq")
    monkeypatch.setattr(tracing, "TRACE_PATH", tmp_path / "trace.jsonl")
    lookup = _Lookup(
        {
            "a": ProviderError("serpapi", "HTTP 429 rate limited", status=429),
            "b": ProductDetails(note="no stores"),
        }
    )
    out = _retrieve(
        monkeypatch, _Search([_sc("a", 100), _sc("b", 110)]), lookup, _ranked("a", "b"),
        budget_usd=1000,
    )  # fmt: skip
    whys = {i["title"]: i["why"] for i in out.slots[0].summary["lookup_issues"]}
    assert whys == {"Product a": "http error 429", "Product b": "no stores"}
    text = tracing.TRACE_PATH.read_text()
    assert "http error 429" in text and "no stores" in text and "FAKE-KEY" not in text


# ---- media unit accent --------------------------------------------------------------------


def test_media_unit_follows_bookshelf_in_the_living_room_accents() -> None:
    p = _perception(furniture=[], furniture_gaps=[], art_and_decor=[], fixed_items=[])
    cats = [s.category for s in derive_slots(p, 800, 10)]
    assert cats[:6] == ["sofa", "coffee table", "area rug", "floor lamp", "bookshelf", "media unit"]
    assert len(cats) == 10


@pytest.mark.parametrize(
    "covering",
    [
        {"furniture": [("f1", "TV stand")]},
        {"furniture": [("f1", "entertainment center")]},
        {"fixed": ["built-in media console"]},
    ],
)
def test_media_unit_is_skipped_when_something_already_covers_it(covering: dict[str, Any]) -> None:
    furniture = [
        {
            "id": i, "category": c, "position": {"x": 0.5, "y": 0.5},
            "size": {"width_m": 1.0, "depth_m": 0.4}, "keepable": "likely", "note": "-",
        }
        for i, c in covering.get("furniture", [])
    ]  # fmt: skip
    fixed = [
        {"category": c, "position": {"x": 0.5, "y": 0.1}, "size": {"width_m": 1.0, "depth_m": 0.4},
         "note": "-"}
        for c in covering.get("fixed", [])
    ]  # fmt: skip
    p = _perception(furniture=furniture, furniture_gaps=[], art_and_decor=[], fixed_items=fixed)
    assert "media unit" not in [s.category for s in derive_slots(p, 800, 10)]


def test_media_unit_accent_is_skipped_when_a_media_gap_is_planned_or_replaced() -> None:
    gap = _perception(
        furniture=[], furniture_gaps=[{"category": "TV console", "reason": "-"}], art_and_decor=[]
    )
    cats = [s.category for s in derive_slots(gap, 800, 10)]
    assert cats.count("TV console") == 1 and "media unit" not in cats
    replaced = _perception(
        furniture=[
            {"id": "f1", "category": "tv stand", "position": {"x": 0.5, "y": 0.1},
             "size": {"width_m": 1.2, "depth_m": 0.4}, "keepable": "likely", "note": "-"},
        ],
        furniture_gaps=[], art_and_decor=[],
    )  # fmt: skip
    plan = plan_slots(replaced, 800, 10, resolve_preferences(replaced, replace=["f1"]))
    assert [s.category for s in plan.slots].count("tv stand") == 1
    assert "media unit" not in [s.category for s in plan.slots]


def test_the_cap_of_three_holds_and_requested_replacements_stay_first() -> None:
    p = _perception(
        furniture=[
            {"id": "f1", "category": "desk", "position": {"x": 0.5, "y": 0.1},
             "size": {"width_m": 1.4, "depth_m": 0.7}, "keepable": "likely", "note": "-"},
        ],
        furniture_gaps=[], art_and_decor=[],
    )  # fmt: skip
    plan = plan_slots(p, 800, 3, resolve_preferences(p, replace=["desk"]))
    assert [(s.category, s.source) for s in plan.slots] == [
        ("desk", "replace_requested"), ("sofa", "accent"), ("coffee table", "accent"),
    ]  # fmt: skip
    assert len(plan_slots(p, 800, 6).slots) == 6


# ---- shipping wording ---------------------------------------------------------------------


def test_unknown_shipping_is_never_shown_as_zero_in_the_total_line() -> None:
    pick = {
        "name": "Lamp", "retailer": "Shop", "price": 169.99, "currency": "USD", "shipping": None,
        "total": None, "url": "https://shop.example/products/l", "url_kind": "retailer_product",
    }  # fmt: skip
    item = {
        "slot": "lamp", "pick": pick, "item_total": 169.99, "rationale": "r",
        "fit_status": "unknown", "fit_note": "", "style_match": "good", "style_note": "",
        "warnings": [], "replaces": None,
    }  # fmt: skip
    proposal = {
        "items": [item],
        "totals": {
            "items_total": 169.99, "shipping_total": 0.0, "total": 169.99, "budget": 800.0,
            "remaining": 630.01, "item_count": 1, "shipping_known_count": 0,
            "shipping_note": "shipping known for 0 of 1 items; ...",
        },
        "summary": "", "check_summary": {"budget": "pass"}, "revised": False,
        "revision_changes": [], "warnings": [],
    }  # fmt: skip
    text = format_final_list(proposal)
    assert (
        "Total: $169.99 of $800.00 budget (items $169.99, shipping known for 0 of 1 items, "
        "unknown excluded; $630.01 remaining)"
    ) in text
    assert "known shipping $0.00" not in text and "shipping $0.00" not in text
    assert "shipping unknown" in text


def test_shipping_note_in_result_json_and_known_shipping_keeps_the_old_wording(
    tmp_path: Path,
) -> None:
    result = run_spike(synthetic_room_image(), budget_usd=800, style="x", config=Config())
    totals = result.proposal["totals"]
    assert (
        totals["shipping_known_count"] == totals["item_count"] == 2
        and totals["shipping_note"] == ""
    )
    assert "shipping $118.00)" in (write_outputs(result, tmp_path) / "table.txt").read_text()


def test_the_budget_check_still_counts_unknown_shipping_as_zero() -> None:
    from app.schemas import ProposalItem
    from app.selfcheck import check_budget

    cand = ProductCandidate(
        name="x", retailer="s", price=100, currency="USD", url="https://s.example/p/1",
        product_id="1", price_source="lookup",
    )  # fmt: skip
    results = check_budget([ProposalItem(slot="a", pick_id="1", pick=cand)], 800, Config())
    assert results[0].message.startswith("total $100.00") and results[1].warning
