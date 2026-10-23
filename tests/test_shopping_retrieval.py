from __future__ import annotations

import copy
import importlib.util
import json
import threading
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from app import llm, shopping_retrieval
from app.config import BudgetExceededError, Config
from app.images import synthetic_room_image
from app.llm import Budget
from app.mock_llm import (
    MockAnthropicClient,
    ScriptedClient,
    mock_perception_payload,
    response,
    text_block,
)
from app.retrieval import canonical_category, derive_slots, retrieve
from app.schemas import RoomPerception, SchemaError
from app.shopping import (
    FixtureSearch,
    ProductDetails,
    ProviderError,
    SearchCandidate,
    SerpApiProductLookup,
    Store,
)
from app.shopping_retrieval import build_query, filter_candidates
from app.spike import run_spike, write_outputs

FIXTURES = Path(__file__).parent / "fixtures"


def _cand(pid: str, price: float | None, **kw: Any) -> SearchCandidate:
    return SearchCandidate(
        pid, kw.get("title", f"Product {pid}"), kw.get("seller", "Shop"), price, "USD",
        kw.get("rating", 4.5), kw.get("rating_count", 10), kw.get("panel", f"https://g.example/{pid}"),
    )  # fmt: skip


class FakeSearch:
    name = "serper"

    def __init__(self, results: list[SearchCandidate]) -> None:
        self.results = results
        self.queries: list[str] = []

    def search(self, query: str, gl: str = "us", hl: str = "en") -> list[SearchCandidate]:
        self.queries.append(query)
        return self.results


class FakeLookup:
    name = "serpapi"

    def __init__(self, details: dict[str, ProductDetails | Exception]) -> None:
        self.details = details
        self.ids: list[str] = []

    def lookup(self, product_id: str, gl: str = "us", hl: str = "en") -> ProductDetails:
        self.ids.append(product_id)
        outcome = self.details.get(product_id, ProductDetails())
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _perception(**overrides: Any) -> RoomPerception:
    payload = copy.deepcopy(mock_perception_payload())
    payload["furniture"] = []
    payload["furniture_gaps"] = [{"category": "sofa", "reason": "MOCK: empty wall"}]
    payload.update(overrides)
    return RoomPerception.model_validate(payload)


def _rank_client(*finalists: dict[str, str]) -> ScriptedClient:
    return ScriptedClient([response([text_block({"finalists": list(finalists)})])])


def _run(
    monkeypatch: pytest.MonkeyPatch,
    search: Any,
    lookup: Any,
    client: Any,
    *,
    config: Config | None = None,
    budget_usd: float = 300.0,
) -> tuple[Any, Budget]:
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    budget = Budget(config or Config())
    result = retrieve(
        _perception(), budget_usd=budget_usd, style="warm minimalist", max_slots=1,
        budget=budget, providers=(search, lookup),
    )  # fmt: skip
    return result, budget


def _store(name: str, url: str, **kw: Any) -> Store:
    return Store(name, url, kw.get("price"), kw.get("shipping"), kw.get("total"), None)


# ---- stage 1 -----------------------------------------------------------------------------------


def test_query_is_category_plus_a_few_style_words() -> None:
    assert build_query("Floor Lamp", "Warm Minimalist") == "floor lamp warm minimalist"
    assert build_query("sofa", "a b c d e f g") == "sofa a b c d"  # at most 4 style words
    assert build_query("warm sofa", "warm minimalist") == "warm sofa minimalist"  # no repeats
    assert build_query("sofa", "") == "sofa"


def test_filter_keeps_top_25_then_drops_unpriced_duplicates_and_over_budget() -> None:
    results = [_cand(str(i), 10.0 + i) for i in range(30)]  # 0..29; only 0..24 are considered
    kept, counts = filter_candidates(results, ceiling_usd=1000)
    assert [c.product_id for c in kept] == [str(i) for i in range(25)]
    assert counts["returned"] == 30 and counts["considered"] == 25

    mixed = [
        _cand("a", 50.0),
        _cand("b", None),  # no price
        _cand("a", 40.0),  # duplicate id: the first one wins
        _cand("c", 500.0),  # over budget
        _cand("d", 100.0),  # exactly at budget is allowed
    ]
    kept, counts = filter_candidates(mixed, ceiling_usd=100.0)
    assert [(c.product_id, c.price) for c in kept] == [("a", 50.0), ("d", 100.0)]
    assert (counts["unpriced"], counts["duplicates"], counts["over_budget"], counts["kept"]) == (
        1, 1, 1, 2,
    )  # fmt: skip


def test_empty_slot_when_nothing_fits_skips_rank_and_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ScriptedClient([])
    lookup = FakeLookup({})
    search = FakeSearch([_cand("2", None), _cand("3", None)])  # nothing priced: nothing to show
    result, budget = _run(monkeypatch, search, lookup, client, budget_usd=100)
    (slot,) = result.slots
    assert slot.candidates == [] and "per-item ceiling" in slot.note
    assert "2 unpriced" in slot.note and "0 over budget" in slot.note
    assert slot.over_budget_options == []
    assert client.calls == [] and lookup.ids == []
    assert budget.steps == 0  # a search is not an LLM step


# ---- stage 2 -----------------------------------------------------------------------------------


def test_rank_ids_are_validated_deduped_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    search = FakeSearch([_cand("p1", 50.0), _cand("p2", 60.0), _cand("p3", 70.0)])
    client = _rank_client(
        {"id": "invented-1", "reason": "made up"},
        {"id": "p2", "reason": "good"},
        {"id": "p2", "reason": "again"},
        {"id": "p3", "reason": "also good"},
        {"id": "p1", "reason": "over the cap of 2"},
    )
    result, _ = _run(monkeypatch, search, FakeLookup({}), client)
    (slot,) = result.slots
    assert [c.product_id for c in slot.candidates] == ["p2", "p3"]  # rank order, capped at 2
    assert slot.dropped == [{"id": "invented-1", "reason": "id_not_in_candidates"}]
    assert slot.summary["rank"] == {"returned": 5, "valid": 2, "invented": 1}
    assert [c.reason for c in slot.candidates] == ["good", "also good"]


def test_rank_request_has_no_tools_a_schema_and_compact_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search = FakeSearch([_cand("p1", 50.0, title="X" * 300, seller="Acme", rating_count=7)])
    client = _rank_client({"id": "p1", "reason": "fine"})
    _run(monkeypatch, search, FakeLookup({}), client)
    (call,) = client.calls
    assert "tools" not in call
    assert call["output_config"]["format"]["schema"]["title"] == "RankedFinalists"
    text = call["messages"][0]["content"]
    assert "- id: p1 | " + "X" * 100 + " | seller: Acme | USD 50.00 | rating 4.5 (7)" in text
    assert "X" * 101 not in text  # titles are truncated
    assert call["model"] == Config().rank_model == Config().model_retrieval
    assert "Pick at most 2 finalists" in text


def test_the_model_cannot_supply_a_price_or_url(monkeypatch: pytest.MonkeyPatch) -> None:
    search = FakeSearch([_cand("p1", 50.0)])
    client = _rank_client(
        {"id": "p1", "reason": "x", "price": "1.00", "url": "https://evil.example"}
    )
    with pytest.raises(SchemaError, match="rank"):
        _run(monkeypatch, search, FakeLookup({}), client)


def test_no_valid_finalist_returns_an_empty_slot_and_never_looks_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = FakeLookup({})
    result, _ = _run(
        monkeypatch,
        FakeSearch([_cand("p1", 50.0)]),
        lookup,
        _rank_client({"id": "zzz", "reason": "x"}),
    )
    assert result.slots[0].candidates == [] and "no valid finalist" in result.slots[0].note
    assert lookup.ids == []


def test_rank_model_can_differ_from_retrieval_model(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _rank_client({"id": "p1", "reason": "x"})
    _run(
        monkeypatch, FakeSearch([_cand("p1", 5.0)]), FakeLookup({}), client,
        config=Config(model_rank="claude-sonnet-5-5"),
    )  # fmt: skip
    assert client.calls[0]["model"] == "claude-sonnet-5-5"


# ---- stage 3 -----------------------------------------------------------------------------------


def _one(monkeypatch: pytest.MonkeyPatch, details: ProductDetails | Exception, **cand: Any) -> Any:
    search = FakeSearch([_cand("p1", 50.0, **cand)])
    result, budget = _run(
        monkeypatch, search, FakeLookup({"p1": details}), _rank_client({"id": "p1", "reason": "r"})
    )
    return result.slots[0], budget


def test_lookup_success_uses_provider_price_clean_url_and_store_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    details = ProductDetails(
        stores=[
            _store("Cheap Co", "https://cheap.example/p/9", price=40.0, total=45.0),
            _store(
                "Shop",
                "https://shop.example/products/sofa?variant=3&utm_source=g&srsltid=Z",
                price=55.0,
                shipping=9.0,
                total=64.0,
            ),
        ]  # fmt: skip
    )
    slot, _ = _one(monkeypatch, details, seller="shop.com")
    (c,) = slot.candidates
    assert c.url == "https://shop.example/products/sofa?variant=3"  # matched by seller, cleaned
    assert c.url_kind == "retailer_product" and c.price_source == "lookup"
    assert (c.price, c.shipping, c.total, c.retailer) == (55.0, 9.0, 64.0, "Shop")
    assert c.name == "Product p1" and c.rating == 4.5 and c.product_id == "p1"


def test_no_seller_match_picks_the_lowest_total(monkeypatch: pytest.MonkeyPatch) -> None:
    details = ProductDetails(
        stores=[
            _store("A", "https://a.example/p/1", price=60.0, total=70.0),
            _store("B", "https://b.example/p/1", price=65.0, total=66.0),
        ]
    )
    slot, _ = _one(monkeypatch, details, seller="Unrelated")
    assert slot.candidates[0].retailer == "B" and slot.candidates[0].total == 66.0


@pytest.mark.parametrize(
    "details",
    [ProviderError("serpapi", "HTTP 429 rate limited", status=429), ProductDetails()],
    ids=["lookup-error", "no-stores"],
)
def test_failed_or_empty_lookup_falls_back_to_the_google_panel_url(
    monkeypatch: pytest.MonkeyPatch, details: Any
) -> None:
    slot, _ = _one(monkeypatch, details, panel="https://www.google.com/search?ibp=oshop&x=1")
    (c,) = slot.candidates
    assert c.url_kind == "google_panel" and c.url == "https://www.google.com/search?ibp=oshop&x=1"
    assert c.price == 50.0 and c.price_source == "search" and c.shipping is None and c.total is None
    if isinstance(details, Exception):
        assert slot.summary["lookup_errors"] == ["serpapi: HTTP 429 rate limited"]
        assert [x.ok for x in slot.calls if x.kind == "lookup"] == [False]
    else:
        assert slot.summary["lookup_errors"] == []


def test_store_url_failing_the_product_page_guard_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    details = ProductDetails(
        stores=[_store("Shop", "https://shop.example/collections/sofas", price=49.0, total=60.0)]
    )
    slot, _ = _one(monkeypatch, details, seller="Shop", panel="https://g.example/panel")
    (c,) = slot.candidates
    assert c.url == "https://g.example/panel" and c.url_kind == "google_panel"
    assert c.price == 50.0 and c.price_source == "search"  # the rejected store's price is not used
    assert slot.dropped == [
        {
            "name": "Product p1",
            "url": "https://shop.example/collections/sofas",
            "reason": "store_url_not_a_product_page:listing_segment:collections",
        }
    ]


def test_no_url_at_all_is_reported_as_none_never_invented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search = FakeSearch([_cand("p1", 50.0, panel=None)])
    result, _ = _run(
        monkeypatch, search, FakeLookup({"p1": ProductDetails()}),
        _rank_client({"id": "p1", "reason": "r"}),
    )  # fmt: skip
    slot = result.slots[0]
    assert slot.candidates == []  # a finalist with no link of any kind is dropped, not shown
    assert {"name": "Product p1", "reason": "no_link"} in slot.dropped
    assert slot.note == "lookup returned no store for Product p1"
    assert slot.summary["lookup_issues"] == [{"title": "Product p1", "why": "no stores"}]


def test_dimensions_are_attached_when_the_lookup_has_them(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.shopping import Dimensions

    details = ProductDetails(dimensions=Dimensions(width_m=1.9, depth_m=0.9, height_m=0.8))
    slot, _ = _one(monkeypatch, details)
    cand = slot.candidates[0]
    dims = cand.dimensions_m
    assert dims is not None and (dims.width_m, dims.depth_m, dims.height_m) == (1.9, 0.9, 0.8)
    assert dims.length_m is None and (cand.size_source, cand.size_confidence) == ("feature", "high")


def test_a_search_provider_failure_is_loud_not_an_empty_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broken(FakeSearch):
        def search(self, query: str, gl: str = "us", hl: str = "en") -> list[SearchCandidate]:
            raise ProviderError("serper", "HTTP 402 payment required", status=402)

    with pytest.raises(ProviderError, match="402"):
        _run(monkeypatch, Broken([]), FakeLookup({}), ScriptedClient([]))


# ---- cost, budget and concurrency ---------------------------------------------------------


def test_provider_costs_are_estimated_from_settings_and_counted_in_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = Config(serper_usd_per_query=0.001, serpapi_usd_per_search=0.01)
    search = FakeSearch([_cand("p1", 5.0), _cand("p2", 6.0)])
    client = _rank_client({"id": "p1", "reason": "a"}, {"id": "p2", "reason": "b"})
    result, budget = _run(monkeypatch, search, FakeLookup({}), client, config=cfg)
    (slot,) = result.slots
    by_kind = {k: [c for c in slot.calls if c.kind == k] for k in ("search", "rank", "lookup")}
    assert [c.cost_usd for c in by_kind["search"]] == [0.001]
    assert [c.cost_usd for c in by_kind["lookup"]] == [0.01, 0.01]
    assert all(c.estimated for k in ("search", "lookup") for c in by_kind[k])
    assert not by_kind["rank"][0].estimated
    provider = sum(c.cost_usd for c in slot.calls if c.estimated)
    assert budget.provider_cost_usd == pytest.approx(provider)  # kept apart from Anthropic dollars
    assert budget.cost_usd == pytest.approx(slot.cost_usd - provider)
    assert budget.steps == 1  # only the rank call is an LLM step

    cfg_serpapi = Config(search_provider="serpapi", serpapi_usd_per_search=0.02)
    assert shopping_retrieval._estimated_cost(Budget(cfg_serpapi), "serpapi") == 0.02


def test_provider_spend_trips_only_the_provider_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    search = FakeSearch([_cand("p1", 5.0), _cand("p2", 6.0)])
    rank = ({"id": "p1", "reason": "a"}, {"id": "p2", "reason": "b"})
    # one search + two lookups at the default $0.025 each is about $0.05 of provider dollars
    tight_provider = Config(max_provider_cost_usd=0.03)
    with pytest.raises(BudgetExceededError, match="provider cost cap"):
        _run(monkeypatch, search, FakeLookup({}), _rank_client(*rank), config=tight_provider)

    # a tiny Anthropic cap is not touched by provider dollars: the run reaches the rank call and
    # trips on Anthropic dollars instead, with the other message
    tight_anthropic = Config(max_cost_usd=0.00001)
    with pytest.raises(BudgetExceededError) as info:
        _run(monkeypatch, search, FakeLookup({}), _rank_client(*rank), config=tight_anthropic)
    assert "provider" not in str(info.value) and "cost cap" in str(info.value)

    # generous provider cap, default Anthropic cap: fine even though provider dollars exceed 0.25
    roomy = Config(max_provider_cost_usd=5.0, serpapi_usd_per_search=0.2)
    _, budget = _run(monkeypatch, search, FakeLookup({}), _rank_client(*rank), config=roomy)
    assert budget.provider_cost_usd > roomy.max_cost_usd and budget.cost_usd < roomy.max_cost_usd


def test_lookups_run_concurrently_and_results_keep_rank_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(2, timeout=10)  # both lookups must be in flight at once
    threads: set[int] = set()

    class BarrierLookup(FakeLookup):
        def lookup(self, product_id: str, gl: str = "us", hl: str = "en") -> ProductDetails:
            threads.add(threading.get_ident())
            barrier.wait()
            return super().lookup(product_id, gl, hl)

    search = FakeSearch([_cand("p1", 5.0), _cand("p2", 6.0)])
    client = _rank_client({"id": "p2", "reason": "first"}, {"id": "p1", "reason": "second"})
    result, budget = _run(monkeypatch, search, BarrierLookup({}), client)
    assert len(threads) == 2
    assert [c.product_id for c in result.slots[0].candidates] == ["p2", "p1"]
    total = budget.cost_usd + budget.provider_cost_usd
    assert total == pytest.approx(result.slots[0].cost_usd)


def test_many_slots_run_concurrently_with_exact_budget_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    n = 4
    barrier = threading.Barrier(n, timeout=10)  # all slots' searches overlap
    inner = MockAnthropicClient()

    class BarrierSearch(FakeSearch):
        def search(self, query: str, gl: str = "us", hl: str = "en") -> list[SearchCandidate]:
            barrier.wait()
            return super().search(query)

    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: inner)
    payload = copy.deepcopy(mock_perception_payload())
    payload["furniture"] = []
    payload["furniture_gaps"] = [{"category": f"item{i}", "reason": "MOCK"} for i in range(n)]
    search = BarrierSearch([_cand("p1", 5.0), _cand("p2", 6.0)])
    budget = Budget(Config(max_steps=n, max_cost_usd=10.0))
    result = retrieve(
        RoomPerception.model_validate(payload), budget_usd=400, style="x", max_slots=n,
        budget=budget, providers=(search, FakeLookup({})),
    )  # fmt: skip
    assert [r.slot.category for r in result.slots] == [f"item{i}" for i in range(n)]
    assert budget.steps == n
    total = budget.cost_usd + budget.provider_cost_usd
    assert total == pytest.approx(sum(r.cost_usd for r in result.slots))
    assert len(search.queries) == n


def test_step_cap_holds_across_concurrent_shopping_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    inner = MockAnthropicClient()
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: inner)
    payload = copy.deepcopy(mock_perception_payload())
    payload["furniture"] = []
    payload["furniture_gaps"] = [{"category": f"item{i}", "reason": "MOCK"} for i in range(5)]
    budget = Budget(Config(max_steps=2, max_cost_usd=10.0))
    with pytest.raises(BudgetExceededError, match="step cap"):
        retrieve(
            RoomPerception.model_validate(payload), budget_usd=500, style="x", max_slots=5,
            budget=budget,
            providers=(FakeSearch([_cand("p1", 5.0)]), FakeLookup({})),
        )  # fmt: skip
    assert budget.steps == 2 and len(inner.calls) == 2


# ---- secrets never reach outputs ----------------------------------------------------------------


def _all_outputs(out_dir: Path) -> str:
    return "\n".join(p.read_text() for p in sorted(out_dir.iterdir()))


def test_fake_keys_in_the_environment_appear_in_no_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import tracing

    serper, serpapi = "SERPER-FAKE-KEY-aaaa1111", "SERPAPI-FAKE-KEY-bbbb2222"
    monkeypatch.setenv("SERPER_API_KEY", serper)
    monkeypatch.setenv("SERPAPI_API_KEY", serpapi)
    monkeypatch.setattr(tracing, "TRACE_PATH", tmp_path / "out" / "trace.jsonl")
    result = run_spike(synthetic_room_image(), budget_usd=900, style="x", config=Config())
    out_dir = write_outputs(result, tmp_path / "out")
    (tmp_path / "out" / "trace.jsonl").replace(out_dir / "trace.jsonl")
    text = _all_outputs(out_dir)
    assert serper not in text and serpapi not in text
    assert {p.name for p in out_dir.iterdir()} == {"result.json", "table.txt", "trace.jsonl"}


def test_a_failing_http_lookup_leaks_no_key_into_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real SerpApi lookup class on a transport that returns 429: degrade, name status, no key."""
    from app import tracing

    key = "SERPAPI-FAKE-KEY-cccc3333"

    def handler(request: httpx.Request) -> httpx.Response:
        assert key in str(request.url)  # the key IS sent (in the query); it must not come back out
        return httpx.Response(429, text=f"quota exceeded for {key}")

    lookup = SerpApiProductLookup(key, httpx.Client(transport=httpx.MockTransport(handler)))
    search = FixtureSearch("serper", "serper_search_oak_side_table.json")
    monkeypatch.setattr(shopping_retrieval, "build_providers", lambda config: (search, lookup))
    monkeypatch.setattr(tracing, "TRACE_PATH", tmp_path / "out" / "trace.jsonl")

    result = run_spike(synthetic_room_image(), budget_usd=900, style="x", slots=1, config=Config())
    out_dir = write_outputs(result, tmp_path / "out")
    (tmp_path / "out" / "trace.jsonl").replace(out_dir / "trace.jsonl")
    text = _all_outputs(out_dir)
    assert key not in text and "api_key" not in text
    data = json.loads((out_dir / "result.json").read_text())
    assert (
        data["retrieval"][0]["summary"]["lookup_errors"]
        == ["serpapi: HTTP 429 rate limited or out of searches for this plan"] * 2
    )
    assert all(c["url_kind"] == "google_panel" for c in data["retrieval"][0]["candidates"])


# ---- slot coverage ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "canonical"),
    [
        ("area rug", "rug"), ("Rug", "rug"), ("floor lamp", "lamp"), ("torchiere", "lamp"),
        ("Table Lamp", "lamp"), ("couch", "sofa"), ("nesting side table", "side table"),
        ("TV stand", "media unit"), ("bookcase", "bookshelf"), ("desk", "desk"),
    ],
)  # fmt: skip
def test_canonical_category_folds_synonyms(category: str, canonical: str) -> None:
    assert canonical_category(category) == canonical


def _last_live_perception() -> RoomPerception:
    data = json.loads((FIXTURES / "perception_live_run_categories.json").read_text())
    return RoomPerception.model_validate(data)


def test_live_run_room_gets_no_default_rug_or_floor_lamp_slot() -> None:
    p = _last_live_perception()
    assert "rug" in [d.kind for d in p.art_and_decor] and "floor lamp" in [
        d.kind for d in p.art_and_decor
    ]
    p = p.model_copy(update={"furniture_gaps": []})  # nothing flagged: defaults take over
    assert all(f.keepable != "unlikely" for f in p.furniture)
    slots = derive_slots(p, 600, 5)
    assert [s.source for s in slots] == ["accent"] * len(slots)
    categories = [s.category for s in slots]
    assert "area rug" not in categories and "floor lamp" not in categories
    assert "sofa" not in categories and "coffee table" not in categories  # owned furniture
    assert categories[:2] == [
        "bookshelf",
        "media unit",
    ]  # no media unit in the room, no gap planned
    assert "console table" not in categories  # same family (storage) as the media unit


def test_fixed_items_count_as_covered_for_defaults() -> None:
    payload = copy.deepcopy(mock_perception_payload())
    payload.update(furniture=[], furniture_gaps=[], room_type="living room")
    payload["fixed_items"] = [
        {"category": "built-in bookcase", "position": {"x": 0.1, "y": 0.1},
         "size": {"width_m": 1.0, "depth_m": 0.3}, "note": "-"},
    ]  # fmt: skip
    slots = derive_slots(RoomPerception.model_validate(payload), 500, 5)
    assert "bookshelf" not in [s.category for s in slots]


# ---- config and CLI ------------------------------------------------------------------------------


def test_config_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "APP_RETRIEVAL_MODE", "APP_SEARCH_PROVIDER", "APP_FINALISTS_PER_SLOT", "APP_MODEL_RANK",
        "APP_SERPER_USD_PER_QUERY", "APP_SERPAPI_USD_PER_SEARCH",
    ):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    cfg = Config.from_env()
    assert (cfg.retrieval_mode, cfg.search_provider, cfg.finalists_per_slot) == (
        "shopping", "serper", 2,
    )  # fmt: skip
    assert (cfg.serper_usd_per_query, cfg.serpapi_usd_per_search) == (0.0006, 0.025)
    assert cfg.rank_model == cfg.model_retrieval

    monkeypatch.setenv("APP_RETRIEVAL_MODE", "web_search")
    monkeypatch.setenv("APP_SEARCH_PROVIDER", "serpapi")
    monkeypatch.setenv("APP_FINALISTS_PER_SLOT", "3")
    monkeypatch.setenv("APP_MODEL_RANK", "claude-sonnet-5-5")
    monkeypatch.setenv("APP_SERPER_USD_PER_QUERY", "0")
    cfg = Config.from_env()
    assert (cfg.retrieval_mode, cfg.search_provider, cfg.finalists_per_slot) == (
        "web_search", "serpapi", 3,
    )  # fmt: skip
    assert cfg.rank_model == "claude-sonnet-5-5" and cfg.serper_usd_per_query == 0.0


@pytest.mark.parametrize(
    "bad", [{"retrieval_mode": "google"}, {"search_provider": "bing"}, {"finalists_per_slot": 0}]
)
def test_config_rejects_bad_values(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Config(**bad)


def test_cli_retrieval_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
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

    base = [str(photo), "--budget", "900", "--style", "calm", "--slots", "1"]
    assert cli.main([*base, "--search-provider", "serpapi", "--finalists", "1"]) == 0
    out = capsys.readouterr().out
    assert "retrieval=shopping/serpapi" in out and "lookup 2" not in out and "lookup 1" in out

    assert cli.main([*base, "--retrieval-mode", "web_search"]) == 0
    out = capsys.readouterr().out
    assert "retrieval=web_search" in out and "/ web_search" in out and "/ rank" not in out
    modes = {
        json.loads((run / "result.json").read_text())["retrieval_mode"]
        for run in (tmp_path / "spike-output").iterdir()
    }
    assert modes == {"shopping", "web_search"}
