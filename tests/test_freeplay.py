from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from app import llm
from app.config import BudgetExceededError, Config
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
from app.prompts import load_prompt
from app.proposal import proposal_user_text, propose_and_check, slot_inputs
from app.retrieval import RetrievalResult, Slot, SlotResult, plan_slots, retrieve
from app.schemas import ProductCandidate, ProposalItem, RoomPerception
from app.selfcheck import check_budget, item_shipping, item_total, summarize
from app.shopping import (
    ProductDetails,
    ProviderError,
    SearchCandidate,
    Store,
    parse_product_details,
    parse_shipping,
)
from app.shopping_retrieval import (
    MIN_BAND_RESULTS,
    OUTLIER_FACTOR,
    _percentile,
    lookup_alternate,
    price_stats,
    tier_pool,
)
from app.spike import format_final_list, run_spike, write_outputs


def _sc(pid: str, price: float | None, **kw: Any) -> SearchCandidate:
    return SearchCandidate(
        pid, kw.get("title", f"Product {pid}"), kw.get("seller", "Shop"), price, "USD", 4.5, 10,
        kw.get("panel", f"https://www.google.com/search?ibp=oshop&id={pid}"),
    )  # fmt: skip


def _prices(*values: float) -> list[SearchCandidate]:
    return [_sc(f"p{i}", v) for i, v in enumerate(values)]


def _perception(**overrides: Any) -> RoomPerception:
    payload = copy.deepcopy(mock_perception_payload())
    payload.update(overrides)
    return RoomPerception.model_validate(payload)


def _empty_room(room_type: str, **kw: Any) -> RoomPerception:
    return _perception(
        room_type=room_type, furniture=[], furniture_gaps=[], art_and_decor=[], fixed_items=[], **kw
    )


# ---- percentiles and price stats --------------------------------------------------------


def test_percentiles_interpolate_and_handle_tiny_pools() -> None:
    assert _percentile([], 0.5) is None
    assert _percentile([7.0], 1 / 3) == 7.0 and _percentile([7.0], 2 / 3) == 7.0
    assert _percentile([10.0, 20.0], 0.5) == 15.0
    ten = [float(v) for v in range(10, 110, 10)]  # 10..100
    assert _percentile(ten, 1 / 3) == 40.0 and _percentile(ten, 2 / 3) == 70.0


def test_price_stats_gain_p33_and_p67_and_keep_the_old_fields() -> None:
    stats = price_stats(_prices(*[float(v) for v in range(10, 110, 10)]), 50.0)
    assert (stats["p33_usd"], stats["p67_usd"]) == (40.0, 70.0)
    assert stats["over_ceiling"] == 5 and stats["median_usd"] == 55.0
    assert {"considered", "priced", "unpriced", "min_usd", "max_usd"} <= set(stats)
    one = price_stats(_prices(25.0), None)
    assert one["p33_usd"] == one["p67_usd"] == 25.0 and one["over_ceiling"] == 0
    empty = price_stats([], None)
    assert empty["p33_usd"] is None and empty["p67_usd"] is None and empty["priced"] == 0
    assert price_stats([_sc("a", None)], None)["p33_usd"] is None


# ---- tier bands -------------------------------------------------------------------------

TWELVE = _prices(*[float(v) for v in range(100, 1300, 100)])  # 100..1200, p33=470?, p67=830?


def test_tier_bands_split_the_distribution_in_thirds() -> None:
    stats = price_stats(TWELVE, None)
    p33, p67 = stats["p33_usd"], stats["p67_usd"]
    assert p33 is not None and p67 is not None
    value, vb = tier_pool(TWELVE, "value")
    mid, mb = tier_pool(TWELVE, "mid")
    premium, pb = tier_pool(TWELVE, "premium")
    assert all(c.price is not None and c.price <= p33 for c in value)
    assert all(c.price is not None and p33 < c.price <= p67 for c in mid)
    assert all(c.price is not None and c.price > p67 for c in premium)
    assert (
        len(value) + len(mid) + len(premium) == 12 and min(len(value), len(mid), len(premium)) >= 3
    )
    assert (vb["tier"], mb["tier"], pb["tier"]) == ("value", "mid", "premium")
    assert vb["widened_to"] is None and vb["kept"] == len(value)
    assert vb["low_usd"] == 100.0 and pb["high_usd"] == 1200.0 and mb["low_usd"] > vb["high_usd"]
    assert {c.product_id for c in value}.isdisjoint({c.product_id for c in premium})


def test_a_band_with_fewer_than_three_results_is_widened_and_says_so() -> None:
    assert MIN_BAND_RESULTS == 3
    pool = _prices(10, 11, 12, 13, 14, 15, 16, 17, 18, 100)  # a tiny premium band
    premium, band = tier_pool(pool, "premium")
    assert band["widened_to"] == "premium+mid" and len(premium) >= 3
    assert band["kept"] == len(premium) and band["low_usd"] is not None
    value, vband = tier_pool(_prices(10, 20, 30, 40, 50, 60), "value")  # only 2 at or below p33
    assert vband["widened_to"] == "value+mid" and len(value) == 4
    mid, mband = tier_pool(_prices(5, 5, 5, 5, 5, 5), "mid")  # everything ties at the boundary
    assert len(mid) == 6 and mband["tier"] == "mid"


def test_mid_widens_to_everything_when_it_is_too_small() -> None:
    kept, band = tier_pool(_prices(10, 20, 30, 40, 50, 60), "mid")  # 2 in the middle third
    assert band["widened_to"] == "value+mid+premium" and len(kept) == 6


def test_outliers_above_three_medians_never_enter_the_premium_band() -> None:
    assert OUTLIER_FACTOR == 3.0
    pool = _prices(100, 110, 120, 130, 140, 150, 160, 170, 180, 5000)  # median 145 -> limit 435
    premium, band = tier_pool(pool, "premium")
    assert all(c.price is not None and c.price <= 435 for c in premium)
    assert band["high_usd"] is not None and band["high_usd"] <= 435
    everything, _ = tier_pool(pool, "mid")  # even a full widening ignores the outlier
    assert all(c.product_id != "p9" for c in everything)


def test_tiny_and_empty_pools() -> None:
    assert tier_pool([], "mid") == ([], {
        "tier": "mid", "low_usd": None, "high_usd": None, "kept": 0, "widened_to": None,
    })  # fmt: skip
    kept, band = tier_pool(_prices(42.0), "premium")  # widening finds nothing: use everything
    assert [c.price for c in kept] == [42.0] and band["widened_to"] == "all"
    kept2, _ = tier_pool([_sc("a", None)], "mid")
    assert kept2 == []


def test_the_rank_pool_is_filtered_in_code_before_the_rank_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ScriptedClient([response([text_block({"finalists": [{"id": "p6", "reason": "r"}]})])])
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    lookup = _Lookup({"p6": _store("a", 700)})
    out = retrieve(
        _empty_room("living room"), budget_usd=None, style="x", max_slots=1,
        budget=Budget(Config(price_tier="premium")), providers=(_Search(TWELVE), lookup),
    )  # fmt: skip
    request = client.calls[0]["messages"][0]["content"]
    assert "Price tier: premium" in request and "price ceiling" not in request.lower()
    assert "budget" not in request.lower()
    ids = [
        line.split("|")[0].strip()[6:] for line in request.splitlines() if line.startswith("- id:")
    ]
    assert ids and all(int(i[1:]) >= 8 for i in ids)  # only the top third (p8..p11) is ranked
    summary = out.slots[0].summary
    assert summary["tier_band"]["tier"] == "premium" and summary["tier_band"]["kept"] == len(ids)
    assert out.plan is not None and out.slots[0].slot.budget_usd is None
    assert out.slots[0].slot.tier == "premium"


# ---- items, tiers and modes on the command line -----------------------------------------


def _cli() -> Any:
    spec = importlib.util.spec_from_file_location(
        "spike_cli", Path(__file__).resolve().parents[1] / "scripts" / "spike.py"
    )
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_LLM_MODE", "mock")
    monkeypatch.delenv("APP_MAX_ITEMS", raising=False)
    photo = tmp_path / "room.jpg"
    photo.write_bytes(synthetic_room_image())
    return _cli(), photo


def test_items_defaults_per_mode_and_bounds() -> None:
    cfg = Config()
    assert cfg.items_for(True) == 5 and cfg.items_for(False) == 3 == cfg.default_slots
    assert Config(items=7).items_for(True) == 7 == Config(items=7).items_for(False)
    assert cfg.max_items == 10
    for bad in (1, 11):
        with pytest.raises(ValueError, match="between 2 and 10"):
            Config(items=bad)


@pytest.mark.parametrize("bad", ["1", "0", "11", "-3"])
def test_items_outside_the_range_exit_2(cli_env: Any, capsys: Any, bad: str) -> None:
    cli, photo = cli_env
    assert cli.main([str(photo), "--style", "x", "--items", bad]) == 2
    assert "--items must be between 2 and 10" in capsys.readouterr().err


def test_items_honours_a_lower_hard_max(
    cli_env: Any, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, photo = cli_env
    monkeypatch.setenv("APP_MAX_ITEMS", "6")
    assert cli.main([str(photo), "--style", "x", "--items", "7"]) == 2
    assert "between 2 and 6" in capsys.readouterr().err
    assert cli.main([str(photo), "--style", "x", "--items", "6"]) == 0


def test_price_tier_with_budget_is_an_error(cli_env: Any, capsys: Any) -> None:
    cli, photo = cli_env
    with pytest.raises(SystemExit) as exc:
        cli.main([str(photo), "--style", "x", "--budget", "800", "--price-tier", "premium"])
    assert exc.value.code == 2
    assert "--price-tier only applies to freeplay" in capsys.readouterr().err


def test_no_budget_is_freeplay_and_records_the_mode(cli_env: Any, capsys: Any) -> None:
    cli, photo = cli_env
    assert cli.main([str(photo), "--style", "x", "--price-tier", "premium", "--items", "4"]) == 0
    out = capsys.readouterr().out
    assert "freeplay tier=premium items=4" in out
    run = next((Path("spike-output")).iterdir())
    data = json.loads((run / "result.json").read_text())
    assert (data["mode"], data["price_tier"], data["items"], data["budget_usd"]) == (
        "freeplay", "premium", 4, None,
    )  # fmt: skip
    assert data["prompt_versions"]["rank"] == "4" and data["prompt_versions"]["proposal"] == "3"
    assert set(data["slot_plan"]["tier_bands"]) == set(data["slot_plan"]["slots"])


def test_budget_mode_is_unchanged_and_keeps_its_defaults(cli_env: Any, capsys: Any) -> None:
    cli, photo = cli_env
    assert cli.main([str(photo), "--style", "x", "--budget", "800"]) == 0
    out = capsys.readouterr().out
    assert "budget=$800 items=3" in out and "freeplay tier" not in out
    data = json.loads((next(Path("spike-output").iterdir()) / "result.json").read_text())
    assert (data["mode"], data["price_tier"], data["items"]) == ("budget", None, 3)
    assert data["prompt_versions"]["rank"] == "3" and data["prompt_versions"]["proposal"] == "2"


def test_a_version_flag_overrides_both_modes(cli_env: Any) -> None:
    cli, photo = cli_env
    assert cli.main([str(photo), "--style", "x", "--rank-prompt-version", "3", "--items", "2"]) == 0
    data = json.loads((next(Path("spike-output").iterdir()) / "result.json").read_text())
    assert data["prompt_versions"]["rank"] == "3"


def test_prompt_files() -> None:
    v4, v3 = load_prompt("rank", "4"), load_prompt("rank")
    assert (v4.version, v3.version) == ("4", "3")
    assert "ceiling" not in v4.text.lower() and "ceiling" in v3.text.lower()
    p3, p2 = load_prompt("proposal", "3"), load_prompt("proposal")
    assert (p3.version, p2.version) == ("3", "2") and load_prompt("proposal", "1").version == "1"
    assert "no budget" in p3.text.lower() and "remaining" not in p3.text.lower()


# ---- slot lists scale -------------------------------------------------------------------


@pytest.mark.parametrize(
    "room", ["living room", "bedroom", "home office", "dining room", "studio flat"]
)
def test_every_room_type_can_reach_ten_distinct_slots(room: str) -> None:
    plan = plan_slots(_empty_room(room), None, 10)
    cats = [s.category for s in plan.slots]
    assert len(cats) == 10 and len(set(cats)) == 10
    assert plan.notes == [] and all(s.source == "accent" for s in plan.slots)
    assert all(s.budget_usd is None and s.total_cap_usd is None for s in plan.slots)


def test_existing_lists_keep_their_order_and_entries() -> None:
    cats = [s.category for s in plan_slots(_empty_room("living room"), None, 6).slots]
    assert cats == ["sofa", "coffee table", "area rug", "floor lamp", "bookshelf", "media unit"]
    bed = [s.category for s in plan_slots(_empty_room("bedroom"), None, 5).slots]
    assert bed == ["bed frame", "nightstand", "dresser", "area rug", "reading lamp"]


def test_covered_categories_are_skipped_and_a_shortfall_is_noted_not_padded() -> None:
    p = _perception(room_type="living room")  # furniture, a gap and decor exist
    plan = plan_slots(p, None, 10)
    cats = [s.category for s in plan.slots]
    assert len(cats) == len(set(cats)) < 10  # never padded with duplicates
    assert "armchair" not in cats and "desk" not in cats  # kept furniture
    assert any("this room type has 10 standard slots" in n for n in plan.notes)
    assert f"only {len(cats)} slots are available" in plan.notes[0]
    assert plan.to_json()["notes"] == plan.notes


def test_a_note_is_not_added_when_requests_were_dropped_by_the_cap() -> None:
    plan = plan_slots(_perception(), None, 2)
    assert len(plan.slots) == 2 and plan.notes == []


# ---- freeplay checks, proposal and totals -----------------------------------------------


def _pick(pid: str = "a1", price: float = 100.0, **kw: Any) -> ProductCandidate:
    return ProductCandidate(
        name=f"Product {pid}", retailer="Shop", price=price, currency="USD",
        url=f"https://shop.example/products/{pid}", url_kind=kw.get("url_kind", "retailer_product"),
        product_id=pid, price_source="lookup", **{k: v for k, v in kw.items() if k != "url_kind"},
    )  # fmt: skip


def test_budget_checks_are_not_applicable_without_a_budget() -> None:
    items = [ProposalItem(slot="a", pick_id="a1", pick=_pick(price=999999.0))]
    results = check_budget(items, None, Config())
    assert [(r.status, r.message) for r in results] == [("n/a", "no budget set")]
    assert summarize(results) == {
        "budget": "n/a",
        "fit": "pass",
        "style": "pass",
        "grounded": "pass",
    }


def _retrieved(slots: dict[str, list[ProductCandidate]]) -> RetrievalResult:
    out = RetrievalResult(mode="shopping")
    from app.llm import Completion

    for name, cands in slots.items():
        out.slots.append(
            SlotResult(
                Slot(name, "why", "gap", None, tier="mid"), cands, [], "", Completion(text="")
            )
        )
    return out


def _run_proposal(monkeypatch: pytest.MonkeyPatch, client: ScriptedClient, slots: Any) -> Any:
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    return propose_and_check(
        _perception(), _retrieved(slots), budget_usd=None, style="warm", budget=Budget(Config())
    )


def test_freeplay_never_revises_for_the_budget_and_totals_have_no_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ScriptedClient(
        [
            response([text_block(proposal_payload({"sofa": "a1"}))]),
            response([text_block(style_payload({"sofa": "good"}))]),
        ]
    )
    run = _run_proposal(monkeypatch, client, {"sofa": [_pick(price=50000.0)]})
    p = run.proposal
    assert len(client.calls) == 2 and p.revised is False  # a huge price cannot fail anything
    assert p.check_summary["budget"] == "n/a" and not p.warnings
    assert p.totals.budget is None and p.totals.remaining is None
    request = client.calls[0]["messages"][0]["content"]
    assert "No budget set" in request and "Total budget" not in request
    assert client.calls[0]["system"] == load_prompt("proposal", "3").text


def test_freeplay_still_revises_on_a_failed_style_check(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ScriptedClient(
        [
            response([text_block(proposal_payload({"sofa": "a1"}))]),
            response([text_block(style_payload({"sofa": "poor"}))]),
            response([text_block(proposal_payload({"sofa": "a2"}))]),
            response([text_block(style_payload({"sofa": "good"}))]),
        ]
    )
    run = _run_proposal(monkeypatch, client, {"sofa": [_pick("a1"), _pick("a2")]})
    assert run.proposal.revised and run.proposal.items[0].pick_id == "a2"
    assert run.proposal.check_summary["budget"] == "n/a"


def test_the_freeplay_total_line_has_no_budget_or_remaining() -> None:
    proposal = {
        "items": [], "summary": "", "revised": False, "revision_changes": [], "warnings": [],
        "check_summary": {"budget": "n/a"},
        "totals": {
            "items_total": 300.0, "shipping_total": 20.0, "total": 320.0, "budget": None,
            "remaining": None, "item_count": 2, "shipping_known_count": 2, "shipping_note": "",
        },
    }  # fmt: skip
    text = format_final_list(proposal)
    assert "Total: $320.00 (items $300.00, shipping $20.00)" in text
    assert "remaining" not in text and "of $" not in text
    proposal["totals"]["shipping_known_count"] = 1  # type: ignore[index]
    unknown = format_final_list(proposal)
    assert (
        "Total: $320.00 (items $300.00, shipping known for 1 of 2 items, unknown excluded)"
        in unknown
    )


def test_the_mock_freeplay_summary_and_output_mention_no_budget(tmp_path: Path) -> None:
    result = run_spike(synthetic_room_image(), style="warm minimalist", config=Config())
    assert result.mode == "freeplay" and result.items == 5
    summary = result.proposal["summary"].lower()
    assert "budget" not in summary and "remaining" not in summary
    assert result.proposal["totals"]["budget"] is None
    table = (write_outputs(result, tmp_path) / "table.txt").read_text()
    total_line = next(x for x in table.splitlines() if x.startswith("Total: $"))
    assert "budget" not in total_line and "remaining" not in total_line
    assert "Checks: budget n/a" in table
    assert not any("over_budget" in k for k in result.proposal)
    for r in result.retrieval:
        assert r["over_budget_options"] == [] and r["slot"]["budget_usd"] is None
    assert result.steps <= 14


def test_the_over_budget_options_path_is_not_used_in_freeplay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ScriptedClient([response([text_block({"finalists": []})])])
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    out = retrieve(
        _empty_room("living room"), budget_usd=None, style="x", max_slots=1,
        budget=Budget(Config()), providers=(_Search(_prices(1000, 2000, 3000)), _Lookup({})),
    )  # fmt: skip
    assert out.slots[0].over_budget_options == [] and len(client.calls) == 1
    empty, _ = (
        retrieve(
            _empty_room("living room"), budget_usd=None, style="x", max_slots=1,
            budget=Budget(Config()), providers=(_Search([]), _Lookup({})),
        ).slots[0],
        None,
    )  # fmt: skip
    assert empty.candidates == [] and "no priced shopping results" in empty.note


# ---- lookup economy ---------------------------------------------------------------------


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
        out = self.details.get(product_id, ProductDetails())
        if isinstance(out, Exception):
            raise out
        return out


def _store(slug: str, price: float, shipping: float | None = 10.0) -> ProductDetails:
    total = price + (shipping or 0.0) if shipping is not None else price
    status = "unknown" if shipping is None else ("free" if shipping == 0 else "known")
    return ProductDetails(
        [
            Store(
                "Shop",
                f"https://shop.example/products/{slug}",
                price,
                shipping,
                total,
                None,
                shipping_status=status,
            )
        ]  # fmt: skip
    )


def _freeplay(
    monkeypatch: pytest.MonkeyPatch,
    results: list[SearchCandidate],
    lookup: _Lookup,
    ranked: list[str],
    config: Config | None = None,
) -> tuple[Any, Budget]:
    client = ScriptedClient(
        [response([text_block({"finalists": [{"id": i, "reason": "r"} for i in ranked]})])]
    )
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    budget = Budget(config or Config())
    out = retrieve(
        _empty_room("living room"), budget_usd=None, style="x", max_slots=1, budget=budget,
        providers=(_Search(results), lookup),
    )  # fmt: skip
    return out, budget


def test_one_lookup_per_slot_and_the_rest_are_unverified_alternates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = _Lookup({"p4": _store("a", 400)})
    out, budget = _freeplay(monkeypatch, TWELVE, lookup, ["p4", "p5"])
    (slot,) = out.slots
    assert lookup.ids == ["p4"]  # one lookup; the alternate was not looked up
    assert [c.product_id for c in slot.candidates] == ["p4"]
    assert slot.candidates[0].url_kind == "retailer_product"
    (alt,) = slot.unverified_alternates
    assert (
        alt.product_id == "p5" and alt.url_kind == "google_panel" and alt.price_source == "search"
    )
    assert alt.url.startswith("https://www.google.com/search") and alt.price == 600.0
    assert slot.summary["lookup_attempts"] == 1
    assert budget.provider_cost_usd == pytest.approx(
        Config().serper_usd_per_query + Config().serpapi_usd_per_search
    )


def test_a_failed_first_lookup_falls_back_to_the_second_finalist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = _Lookup({"p4": ProductDetails(note="no stores"), "p5": _store("b", 600)})
    out, budget = _freeplay(monkeypatch, TWELVE, lookup, ["p4", "p5", "p6"])
    slot = out.slots[0]
    assert lookup.ids == ["p4", "p5"]  # at most two attempts
    assert [c.product_id for c in slot.candidates] == ["p5"]
    assert {c.product_id for c in slot.unverified_alternates} == {"p4"} | set()
    lookup_rows = [c for c in slot.calls if c.kind == "lookup"]
    assert len(lookup_rows) == 2  # every attempt is a real row
    expected = Config().serper_usd_per_query + 2 * Config().serpapi_usd_per_search
    assert budget.provider_cost_usd == pytest.approx(expected)  # and counted in the cap
    assert {"title": "Product p4", "why": "no stores"} in slot.summary["lookup_issues"]


def test_a_lookup_error_also_triggers_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = _Lookup(
        {
            "p4": ProviderError("serpapi", "HTTP 429 rate limited", status=429),
            "p5": _store("b", 600),
        }
    )
    out, _ = _freeplay(monkeypatch, TWELVE, lookup, ["p4", "p5"])
    assert [c.product_id for c in out.slots[0].candidates] == ["p5"]
    assert out.slots[0].summary["lookup_errors"] == ["serpapi: HTTP 429 rate limited"]


def test_two_failed_attempts_leave_the_slot_empty_and_never_try_a_third(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = _Lookup({})
    out, _ = _freeplay(monkeypatch, TWELVE, lookup, ["p4", "p5", "p6"])
    slot = out.slots[0]
    assert lookup.ids == ["p4", "p5"] or lookup.ids == ["p4"]  # finalists_per_slot caps at 2
    assert slot.candidates == [] and slot.note.startswith("lookup returned no store for")


def test_a_pick_must_be_a_retailer_page_and_a_linkless_alternate_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = [_sc(f"p{i}", 10.0 * (i + 1)) for i in range(5)] + [_sc("p5", 60.0, panel=None)]
    lookup = _Lookup({"p4": _store("a", 50)})
    out, _ = _freeplay(monkeypatch, results, lookup, ["p4", "p5"])
    slot = out.slots[0]
    assert slot.candidates[0].url_kind == "retailer_product"
    assert slot.unverified_alternates == []  # no link of any kind: dropped


def test_unverified_alternates_are_not_proposal_candidates_but_are_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = _Lookup({"p4": _store("a", 400)})
    out, _ = _freeplay(monkeypatch, TWELVE, lookup, ["p4", "p5"])
    inputs = slot_inputs(out, _empty_room("living room"))
    assert list(inputs[0].finalists) == ["p4"] and len(inputs[0].unverified_alternates) == 1
    text = proposal_user_text(_empty_room("living room"), None, "x", inputs)
    assert "p5" not in text
    client = ScriptedClient(
        [
            response([text_block(proposal_payload({out.slots[0].slot.category: "p4"}))]),
            response([text_block(style_payload({out.slots[0].slot.category: "good"}))]),
        ]
    )
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    run = propose_and_check(
        _empty_room("living room"), out, budget_usd=None, style="x", budget=Budget(Config())
    )
    item = run.proposal.items[0]
    assert item.pick is not None and item.pick.url_kind == "retailer_product"
    assert [a.product_id for a in item.unverified_alternates] == ["p5"]
    assert item.alternates == []
    printed = format_final_list(run.proposal.model_dump())
    assert (
        "alternate (retailer link on request): Product p5" in printed
        and "[google_panel]" in printed
    )


def test_lookup_alternates_flag_looks_them_all_up(monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = _Lookup({"p4": _store("a", 400), "p5": _store("b", 600)})
    out, budget = _freeplay(
        monkeypatch, TWELVE, lookup, ["p4", "p5"], Config(lookup_alternates=True)
    )
    assert lookup.ids == ["p4", "p5"] and out.slots[0].unverified_alternates == []
    assert [c.product_id for c in out.slots[0].candidates] == ["p4", "p5"]
    expected = Config().serper_usd_per_query + 2 * Config().serpapi_usd_per_search
    assert budget.provider_cost_usd == pytest.approx(expected)


def test_lookups_per_slot_two_restores_the_concurrent_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = _Lookup({"p4": _store("a", 400), "p5": _store("b", 600)})
    out, _ = _freeplay(monkeypatch, TWELVE, lookup, ["p4", "p5"], Config(lookups_per_slot=2))
    assert sorted(lookup.ids) == ["p4", "p5"] and len(out.slots[0].candidates) == 2
    assert Config().lookups_for(True) == 1 and Config().lookups_for(False) == 2
    with pytest.raises(ValueError):
        Config(lookups_per_slot=0)


def test_on_demand_lookup_of_one_alternate_counts_in_the_cost_cap() -> None:
    alt = ProductCandidate(
        name="Alt", retailer="Shop", price=600.0, currency="USD",
        url="https://www.google.com/search?ibp=oshop&id=p5", url_kind="google_panel",
        product_id="p5", price_source="search",
    )  # fmt: skip
    budget = Budget(Config())
    found, record = lookup_alternate(alt, _Lookup({"p5": _store("b", 610, 15.0)}), budget)
    assert found is not None and found.url_kind == "retailer_product"
    assert (found.price, found.shipping, found.total) == (610.0, 15.0, 625.0)
    assert record.kind == "lookup" and record.estimated
    assert budget.provider_cost_usd == pytest.approx(Config().serpapi_usd_per_search)
    missing, _ = lookup_alternate(alt, _Lookup({}), budget)
    assert missing is None
    assert budget.provider_cost_usd == pytest.approx(2 * Config().serpapi_usd_per_search)
    tight = Budget(Config(max_provider_cost_usd=0.01))
    with pytest.raises(BudgetExceededError, match="provider cost cap"):
        lookup_alternate(alt, _Lookup({"p5": _store("b", 610)}), tight)


def test_every_lookup_counts_against_the_provider_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = _Lookup({"p4": ProductDetails(note="no stores"), "p5": _store("b", 600)})
    with pytest.raises(BudgetExceededError, match="provider cost cap"):
        _freeplay(monkeypatch, TWELVE, lookup, ["p4", "p5"], Config(max_provider_cost_usd=0.03))


def test_freeplay_caps_scale_with_the_number_of_items() -> None:
    result = run_spike(synthetic_room_image(), style="x", slots=9, config=Config())
    assert result.caps["max_provider_cost_usd"] == pytest.approx(0.54)  # 9 x 0.06 > 0.30
    assert result.steps <= 15
    budget_run = run_spike(synthetic_room_image(), budget_usd=800, style="x", config=Config())
    assert budget_run.caps["max_provider_cost_usd"] == 0.30  # budget mode keeps its cap


# ---- shipping status --------------------------------------------------------------------


def test_the_live_run_values_count_one_of_three_shipping_known() -> None:
    """Regression: sofa and media unit had shipping null with total == price (counted as known)."""
    sofa = _pick("sofa", 199.99, shipping=None, total=199.99)
    media = _pick("media", 143.99, shipping=None, total=143.99)
    side = _pick("side", 199.0, shipping=19.0, total=218.0)
    assert [item_shipping(c) for c in (sofa, media, side)] == [None, None, 19.0]
    assert [c.shipping_status for c in (sofa, media, side)] == ["unknown", "unknown", "known"]
    assert item_total(sofa) == (199.99, True) and item_total(side) == (218.0, False)
    from app.proposal import _totals

    items = [ProposalItem(slot=s, pick_id=c.product_id, pick=c) for s, c in
             (("sofa", sofa), ("media unit", media), ("side table", side))]  # fmt: skip
    totals = _totals(items, 800.0)
    assert (totals.item_count, totals.shipping_known_count) == (3, 1)
    assert totals.shipping_note.startswith("shipping known for 1 of 3 items")
    assert totals.shipping_total == 19.0 and totals.total == 561.98


def test_free_shipping_is_known_zero_and_distinct_from_unknown() -> None:
    free = _pick("free", 100.0, shipping=0.0, total=100.0)
    assert free.shipping_status == "free" and item_shipping(free) == 0.0
    items = [ProposalItem(slot="a", pick_id="free", pick=free)]
    from app.proposal import _totals

    totals = _totals(items, None)
    assert totals.shipping_known_count == 1 and totals.shipping_note == ""
    assert item_total(free) == (100.0, False)


@pytest.mark.parametrize(
    ("store", "price", "expected"),
    [
        ({"shipping_extracted": 59.0, "shipping": "+ $59.00"}, 249.0, (59.0, "known")),
        ({"shipping_extracted": 0.0}, 249.0, (0.0, "free")),
        ({"shipping": "Free"}, 249.0, (0.0, "free")),
        ({"shipping": "free delivery"}, 10.0, (0.0, "free")),
        ({"extracted_total": 199.99}, 199.99, (None, "unknown")),  # total == price: no evidence
        ({"extracted_total": 218.0}, 199.0, (19.0, "known")),  # derived from total above price
        ({}, 50.0, (None, "unknown")),
    ],
)
def test_parse_shipping_distinguishes_known_free_and_unknown(
    store: dict[str, Any], price: float, expected: tuple[float | None, str]
) -> None:
    assert parse_shipping(store, price) == expected


def test_a_lookup_store_with_no_shipping_data_is_unknown_not_free() -> None:
    payload = {
        "product_results": {
            "stores": [
                {"name": "S", "link": "https://s.example/p/1", "extracted_price": 199.99,
                 "extracted_total": 199.99},
            ]
        }
    }  # fmt: skip
    (store,) = parse_product_details(payload).stores
    assert (store.shipping, store.shipping_status, store.total) == (None, "unknown", 199.99)


def test_the_final_list_shows_free_and_unknown_shipping_apart() -> None:
    def item(slot: str, pick: dict[str, Any]) -> dict[str, Any]:
        return {
            "slot": slot, "pick": pick, "item_total": pick["price"], "rationale": "r",
            "fit_status": "unknown", "fit_note": "", "style_match": "good", "style_note": "",
            "warnings": [], "replaces": None,
        }  # fmt: skip

    base = {"name": "N", "retailer": "R", "currency": "USD", "total": None,
            "url": "https://r.example/products/1", "url_kind": "retailer_product"}  # fmt: skip
    proposal = {
        "items": [
            item("a", {**base, "price": 10.0, "shipping": 0.0, "shipping_status": "free"}),
            item("b", {**base, "price": 20.0, "shipping": None, "shipping_status": "unknown"}),
            item("c", {**base, "price": 30.0, "shipping": 5.0, "shipping_status": "known"}),
        ],
        "totals": {"items_total": 60.0, "shipping_total": 5.0, "total": 65.0, "budget": None,
                   "remaining": None, "item_count": 3, "shipping_known_count": 2,
                   "shipping_note": "x"},
        "summary": "", "check_summary": {}, "revised": False, "revision_changes": [],
        "warnings": [],
    }  # fmt: skip
    text = format_final_list(proposal)
    assert "$10.00 free shipping = $10.00" in text
    assert "$20.00 shipping unknown = $20.00" in text and "$30.00 + $5.00 shipping" in text
    assert "shipping known for 2 of 3 items, unknown excluded" in text


# ---- end to end -------------------------------------------------------------------------


def test_mock_freeplay_runs_end_to_end_with_one_lookup_per_slot(tmp_path: Path) -> None:
    result = run_spike(synthetic_room_image(), style="warm minimalist", config=Config())
    assert [r["slot"]["category"] for r in result.retrieval] == [
        "coffee table",
        "sofa",
        "area rug",
        "floor lamp",
        "bookshelf",
    ]
    lookups = [r for r in result.rows if r.kind == "lookup"]
    assert len(lookups) == 5 and all(r.cost_estimated for r in lookups)
    assert result.provider_cost_usd == pytest.approx(
        sum(r.cost_usd for r in result.rows if r.cost_estimated)
    )
    for r in result.retrieval:
        assert r["slot"]["tier"] == "mid" and r["summary"]["tier_band"]["tier"] == "mid"
        assert all(c["url_kind"] == "retailer_product" for c in r["candidates"])
        assert all(a["url_kind"] == "google_panel" for a in r["unverified_alternates"])
    out = write_outputs(result, tmp_path)
    table = (out / "table.txt").read_text()
    assert "alternate (retailer link on request)" in table and "freeplay tier=mid" in table
    data = json.loads((out / "result.json").read_text())
    assert data["mode"] == "freeplay" and data["slot_plan"]["tier_bands"]["sofa"]["tier"] == "mid"
    assert data["proposal"]["items"][0]["unverified_alternates"]


def test_fake_keys_appear_in_no_freeplay_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import tracing

    keys = ["SERPER-FAKE-KEY-fp001", "SERPAPI-FAKE-KEY-fp002"]
    monkeypatch.setenv("SERPER_API_KEY", keys[0])
    monkeypatch.setenv("SERPAPI_API_KEY", keys[1])
    monkeypatch.setattr(tracing, "TRACE_PATH", tmp_path / "out" / "trace.jsonl")
    result = run_spike(synthetic_room_image(), style="x", config=Config())
    out = write_outputs(result, tmp_path / "out")
    (tmp_path / "out" / "trace.jsonl").replace(out / "trace.jsonl")
    assert not any(k in "\n".join(p.read_text() for p in out.iterdir()) for k in keys)
