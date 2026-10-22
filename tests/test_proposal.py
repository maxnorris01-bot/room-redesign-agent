from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app import llm
from app.config import BudgetExceededError, Config
from app.images import synthetic_room_image
from app.llm import Budget, Completion
from app.mock_llm import (
    MOCK_INVENTED_ID,
    ScriptedClient,
    mock_perception_payload,
    proposal_payload,
    response,
    style_payload,
    text_block,
)
from app.proposal import propose_and_check, slot_inputs, validate_proposal
from app.retrieval import RetrievalResult, Slot, SlotResult
from app.schemas import (
    DimensionsM,
    ProductCandidate,
    ProposalItem,
    ProposalOutput,
    RoomPerception,
    SchemaError,
    parse_model,
)
from app.selfcheck import (
    apply_results,
    check_budget,
    check_fit,
    check_grounded,
    item_total,
    summarize,
)
from app.spike import SpikeResult, StageRow, format_final_list, run_spike, write_outputs


def _cand(pid: str, price: float, **kw: Any) -> ProductCandidate:
    return ProductCandidate(
        name=kw.get("name", f"Product {pid}"),
        retailer=kw.get("retailer", "Shop"),
        price=price,
        currency="USD",
        url=kw.get("url", f"https://shop.example/products/{pid}"),
        url_kind=kw.get("url_kind", "retailer_product"),
        shipping=kw.get("shipping", 0.0),
        total=kw.get("total"),
        dimensions_m=kw.get("dims"),
        product_id=pid,
        price_source=kw.get("price_source", "lookup"),
        rating=4.5,
        rating_count=10,
    )


def _retrieved(slots: dict[str, list[ProductCandidate]], note: str = "") -> RetrievalResult:
    out = RetrievalResult(mode="shopping")
    for name, cands in slots.items():
        out.slots.append(
            SlotResult(
                Slot(name, f"why {name}", "gap", 100.0), cands, [], note, Completion(text="")
            )
        )
    return out


def _perception(conf: float = 0.9, width: float = 4.0, length: float = 5.0) -> RoomPerception:
    payload = copy.deepcopy(mock_perception_payload())
    payload["dimensions"].update(width_m=width, length_m=length, confidence=conf)
    return RoomPerception.model_validate(payload)


def _item(slot: str, cand: ProductCandidate | None, **kw: Any) -> ProposalItem:
    return ProposalItem(
        slot=slot,
        pick_id=cand.product_id if cand else "",
        pick=cand,
        item_total=item_total(cand)[0] if cand else None,
        **kw,
    )


def _scripted(*payloads: dict[str, Any]) -> ScriptedClient:
    return ScriptedClient([response([text_block(p)]) for p in payloads])


def _run(
    monkeypatch: pytest.MonkeyPatch,
    client: ScriptedClient,
    slots: dict[str, list[ProductCandidate]],
    *,
    budget_usd: float = 1000.0,
    config: Config | None = None,
    perception: RoomPerception | None = None,
) -> Any:
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    return propose_and_check(
        perception or _perception(),
        _retrieved(slots),
        budget_usd=budget_usd,
        style="warm minimalist",
        budget=Budget(config or Config()),
    )


# ---- proposal: ids, real data ----------------------------------------------------------------


def test_invented_pick_and_alternate_ids_are_dropped_and_never_trusted() -> None:
    slots = slot_inputs(_retrieved({"sofa": [_cand("a1", 100), _cand("a2", 200)]}))
    out = ProposalOutput.model_validate(
        proposal_payload({"sofa": "zzz"}, alternates={"sofa": ["a2", "bogus", "zzz", "a2"]})
    )
    (item,), dropped = validate_proposal(out, slots)
    assert item.pick is None and item.pick_id == "" and "unknown product" in item.note
    assert [a.product_id for a in item.alternates] == ["a2"]  # valid, unique
    assert {"slot": "sofa", "id": "zzz", "reason": "id_not_in_finalists"} in dropped
    assert {"slot": "sofa", "id": "bogus", "reason": "id_not_in_finalists"} in dropped


def test_ids_are_scoped_to_their_own_slot() -> None:
    slots = slot_inputs(_retrieved({"sofa": [_cand("a1", 100)], "lamp": [_cand("b1", 50)]}))
    out = ProposalOutput.model_validate(proposal_payload({"sofa": "b1", "lamp": "b1"}))
    items, dropped = validate_proposal(out, slots)
    assert items[0].pick is None and items[1].pick_id == "b1"
    assert dropped == [{"slot": "sofa", "id": "b1", "reason": "id_not_in_finalists"}]


def test_items_are_built_from_real_candidate_data() -> None:
    dims = DimensionsM(width_m=0.58, depth_m=0.39, height_m=0.32)
    real = _cand("a1", 249.0, shipping=59.0, total=308.0, dims=dims, retailer="Article",
                 url="https://www.article.com/pla/1/torme", name="Torme side table")  # fmt: skip
    slots = slot_inputs(_retrieved({"table": [real, _cand("a2", 10.0)]}))
    out = ProposalOutput.model_validate(
        proposal_payload({"table": "a1"}, alternates={"table": ["a2"]})
    )
    (item,), dropped = validate_proposal(out, slots)
    assert item.pick == real and item.item_total == 308.0 and dropped == []
    assert item.pick is not None and item.pick.dimensions_m == dims
    assert item.alternates[0].product_id == "a2"


def test_the_model_cannot_supply_prices_sellers_or_urls() -> None:
    payload = proposal_payload({"sofa": "a1"})
    payload["slots"][0].update(price=1.0, url="https://evil.example", seller="Evil")
    with pytest.raises(SchemaError, match="proposal"):
        parse_model(ProposalOutput, json.dumps(payload), what="proposal")


def test_unknown_slot_duplicate_slot_and_unaddressed_slot() -> None:
    slots = slot_inputs(_retrieved({"sofa": [_cand("a1", 1)], "lamp": [_cand("b1", 2)]}))
    payload = proposal_payload({"Sofa ": "a1", "rug": "x"})
    payload["slots"].append(proposal_payload({"sofa": None})["slots"][0])  # duplicate: ignored
    items, dropped = validate_proposal(ProposalOutput.model_validate(payload), slots)
    assert items[0].pick_id == "a1"  # case/whitespace-insensitive match, first entry wins
    assert items[1].pick is None and "did not address" in items[1].note
    assert {"slot": "rug", "reason": "unknown_slot"} in dropped


def test_a_slot_left_empty_by_the_model_and_a_slot_with_no_finalists_are_carried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _scripted(proposal_payload({"sofa": None, "lamp": None}), style_payload({}))
    slots = {"sofa": [_cand("a1", 1)], "lamp": []}
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    run = propose_and_check(
        _perception(), _retrieved(slots, note="nothing within budget"),
        budget_usd=500, style="x", budget=Budget(Config()),
    )  # fmt: skip
    sofa, lamp = run.proposal.items
    assert sofa.pick is None and sofa.note == "left empty by the proposal"
    assert lamp.pick is None and lamp.note == "nothing within budget"
    assert run.proposal.totals.total == 0 and run.proposal.check_summary["budget"] == "pass"
    assert len(client.calls) == 1  # no pick, so no style call either


def test_no_finalists_anywhere_makes_no_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ScriptedClient([])
    run = _run(monkeypatch, client, {"sofa": [], "lamp": []})
    assert client.calls == [] and run.proposal.revised is False
    assert [i.note for i in run.proposal.items] == ["no finalists found"] * 2


# ---- budget check -------------------------------------------------------------------------------


def test_budget_total_pass_fail_and_exact_boundary() -> None:
    items = [_item("a", _cand("1", 300)), _item("b", _cand("2", 200))]
    cfg = Config(item_cap_fraction=1.0)
    assert check_budget(items, 500, cfg)[0].status == "pass"  # exactly at budget
    over = check_budget(items, 499.99, cfg)[0]
    assert over.status == "fail" and over.item is None and "over" in over.message


def test_per_item_cap_applies_with_several_items_but_not_to_a_single_item() -> None:
    cfg = Config()  # 0.6
    two = [_item("a", _cand("1", 700)), _item("b", _cand("2", 100))]
    results = check_budget(two, 1000, cfg)
    by_item = {r.item: r for r in results if r.item}
    assert by_item["a"].status == "fail" and "per-item cap $600.00" in by_item["a"].message
    assert by_item["b"].status == "pass"
    one = check_budget([_item("a", _cand("1", 900))], 1000, cfg)
    assert [r.status for r in one] == ["pass", "pass"] and "only item" in one[1].message
    assert check_budget([_item("a", _cand("1", 1200))], 1000, cfg)[0].status == "fail"


def test_unknown_shipping_counts_as_zero_and_sets_a_warning() -> None:
    cand = _cand("1", 100, shipping=None, total=None)
    assert item_total(cand) == (100.0, True)
    items = [_item("a", cand), _item("b", _cand("2", 50, shipping=10.0))]
    results = check_budget(items, 1000, Config())
    warned = [r for r in results if r.warning]
    assert [r.item for r in warned] == ["a"] and "shipping unknown" in (warned[0].warning or "")
    assert results[0].message.startswith("total $160.00")  # 100 + (50 + 10)


def test_stated_total_wins_over_price_plus_shipping() -> None:
    assert item_total(_cand("1", 100, shipping=10.0, total=130.0)) == (130.0, False)


def test_no_picks_passes_budget() -> None:
    (r,) = check_budget([_item("a", None)], 100, Config())
    assert r.status == "pass" and r.message == "no items picked"


# ---- fit check ----------------------------------------------------------------------------------


def _dims(w: float | None, d: float | None) -> DimensionsM:
    return DimensionsM(width_m=w, depth_m=d)


def test_fit_pass_fail_by_side_fail_by_footprint_and_unknown() -> None:
    room = _perception(conf=0.9, width=4.0, length=5.0)  # shorter side 4 m, floor 20 m2
    cfg = Config()
    ok = _item("sofa", _cand("1", 1, dims=_dims(2.0, 0.9)))  # 2.0 <= 2.4; 1.8 <= 2.4
    long_side = _item("console", _cand("2", 1, dims=_dims(3.0, 0.5)))  # 3.0 > 2.4
    big_footprint = _item("bed", _cand("3", 1, dims=_dims(2.3, 1.2)))  # 2.76 m2 > 2.4
    no_dims = _item("lamp", _cand("4", 1))
    half = _item("desk", _cand("5", 1, dims=_dims(1.0, None)))  # depth missing: not enough
    results = {
        r.item: r for r in check_fit([ok, long_side, big_footprint, no_dims, half], room, cfg)
    }
    assert results["sofa"].status == "pass" and "low-confidence" not in results["sofa"].message
    assert (
        results["console"].status == "fail" and "longer side 3.00 m" in results["console"].message
    )
    assert results["bed"].status == "fail" and "footprint 2.76 m2" in results["bed"].message
    assert results["lamp"].status == "unknown"
    assert results["lamp"].message == "no size found; check the product page"
    assert results["desk"].status == "unknown"


def test_fit_boundaries_are_inclusive() -> None:
    room = _perception(width=4.0, length=5.0)
    edge = _item("x", _cand("1", 1, dims=_dims(2.4, 1.0)))  # longer side exactly 60% of 4 m
    assert check_fit([edge], room, Config())[0].status == "pass"


def test_low_confidence_room_size_is_stated_on_pass_and_fail() -> None:
    room = _perception(conf=0.3)
    ok = _item("sofa", _cand("1", 1, dims=_dims(2.0, 0.9)))
    bad = _item("console", _cand("2", 1, dims=_dims(3.5, 2.0)))
    results = check_fit([ok, bad], room, Config())
    assert all("room size is a low-confidence estimate" in r.message for r in results)
    assert [r.status for r in results] == ["pass", "fail"]
    assert "low-confidence" not in check_fit([ok], _perception(conf=0.5), Config())[0].message


def test_fit_uses_only_the_room_dimensions_not_existing_furniture() -> None:
    crowded = _perception()
    assert crowded.furniture  # the mock room has furniture; it must not change the verdict
    ok = _item("sofa", _cand("1", 1, dims=_dims(2.0, 0.9)))
    assert check_fit([ok], crowded, Config())[0].status == "pass"


# ---- grounded check -----------------------------------------------------------------------------


def _grounded(cand: ProductCandidate, ids: set[str] | None = None) -> Any:
    ids = ids if ids is not None else {cand.product_id}
    return check_grounded([_item("sofa", cand)], {"sofa": ids})[0]


def test_grounded_pass_and_google_panel_warns_but_none_fails() -> None:
    assert _grounded(_cand("1", 10)).status == "pass"
    panel = _grounded(
        _cand("1", 10, url="https://www.google.com/search?ibp=oshop", url_kind="google_panel")
    )
    assert panel.status == "pass"
    assert panel.warning == "link is a Google Shopping page, not the retailer"
    none = _grounded(_cand("1", 10, url="", url_kind="none"))
    assert none.status == "fail" and "no link" in none.message


def test_grounded_fails_on_listing_urls_foreign_ids_and_model_prices() -> None:
    listing = _grounded(_cand("1", 10, url="https://shop.example/collections/sofas"))
    assert listing.status == "fail" and "product page" in listing.message
    assert _grounded(_cand("1", 10), ids={"other"}).status == "fail"
    model_price = _grounded(_cand("1", 10, price_source="model"))
    assert model_price.status == "fail" and "provider data" in model_price.message
    assert _grounded(_cand("1", 10, price_source="search")).status == "pass"


def test_summarize_reports_the_worst_status_per_check() -> None:
    results = [
        *check_budget([_item("a", _cand("1", 900)), _item("b", _cand("2", 50))], 1000, Config()),
        *check_fit([_item("a", _cand("1", 1))], _perception(), Config()),
    ]
    assert summarize(results) == {
        "budget": "fail", "fit": "unknown", "style": "pass", "grounded": "pass",
    }  # fmt: skip


def test_apply_results_never_drops_a_failure() -> None:
    items = [_item("sofa", _cand("1", 900)), _item("lamp", _cand("2", 50))]
    results = check_budget(items, 1000, Config())
    warnings = apply_results(items, results)
    assert any("per-item cap" in w for w in items[0].warnings)
    assert items[1].warnings == [] and warnings == []  # the total itself passed (950 <= 1000)
    over = apply_results(items, check_budget(items, 500, Config()))
    assert any(w.startswith("budget: total $950.00 is over") for w in over)


# ---- orchestration: style, revision ----------------------------------------


TWO_SLOTS = {
    "sofa": [_cand("a1", 300), _cand("a2", 100)],
    "lamp": [_cand("b1", 80), _cand("b2", 40)],
}


def test_clean_proposal_makes_two_calls_and_no_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _scripted(
        proposal_payload({"sofa": "a2", "lamp": "b2"}),
        style_payload({"sofa": "good", "lamp": "ok"}),
    )
    run = _run(monkeypatch, client, TWO_SLOTS)
    p = run.proposal
    assert len(client.calls) == 2 and p.revised is False and p.revision_changes == []
    assert p.check_summary == {
        "budget": "pass",
        "fit": "unknown",
        "style": "pass",
        "grounded": "pass",
    }
    assert [i.style_match for i in p.items] == ["good", "ok"]
    assert p.totals.total == 140.0 and p.totals.remaining == 860.0 and p.warnings == []
    assert [c.label for c in run.calls] == ["proposal", "self-check / code", "self-check / style"]
    assert {c.group for c in run.calls if c.kind in ("code_check", "style_check")} == {"selfcheck"}


def test_a_poor_style_match_triggers_one_revision_that_fixes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _scripted(
        proposal_payload({"sofa": "a1", "lamp": "b1"}),
        style_payload({"sofa": "poor", "lamp": "good"}),
        proposal_payload({"sofa": "a2", "lamp": "b1"}),
        style_payload({"sofa": "good", "lamp": "good"}),
    )
    run = _run(monkeypatch, client, TWO_SLOTS)
    p = run.proposal
    assert len(client.calls) == 4 and p.revised is True
    assert p.items[0].pick_id == "a2" and p.items[0].style_match == "good"
    assert p.check_summary["style"] == "pass" and p.items[0].warnings == []
    assert p.revision_changes == ["sofa: Product a1 -> Product a2"]
    revision_request = client.calls[2]["messages"][0]["content"]
    assert "Previous proposal:" in revision_request and "[style] sofa: poor" in revision_request
    assert [c.label for c in run.calls][4:] == [
        "revision / self-check / code", "revision / self-check / style",
    ]  # fmt: skip
    assert {c.group for c in run.calls if c.label.startswith("revision /")} == {"recheck"}


def test_revision_fixes_an_over_budget_first_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _scripted(
        proposal_payload({"sofa": "a1", "lamp": "b1"}),  # 300 + 80 = 380 > 300
        style_payload({"sofa": "good", "lamp": "good"}),
        proposal_payload({"sofa": "a2", "lamp": "b2"}),  # 100 + 40 = 140
        style_payload({"sofa": "good", "lamp": "good"}),
    )
    run = _run(
        monkeypatch, client, TWO_SLOTS, budget_usd=300.0, config=Config(item_cap_fraction=1.0)
    )
    p = run.proposal
    assert p.revised and p.totals.total == 140.0 and p.check_summary["budget"] == "pass"
    assert p.warnings == [] and "total $380.00 is over" in client.calls[2]["messages"][0]["content"]


def test_a_proposal_that_still_fails_is_shown_with_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _scripted(
        proposal_payload({"sofa": "a1", "lamp": "b1"}),
        style_payload({"sofa": "poor", "lamp": "good"}),
        proposal_payload({"sofa": "a1", "lamp": "b1"}),  # the revision changes nothing
        style_payload({"sofa": "poor", "lamp": "good"}),
    )
    run = _run(monkeypatch, client, TWO_SLOTS)
    p = run.proposal
    assert len(client.calls) == 4  # exactly one revision, never a second
    assert p.revised and p.revision_changes == ["revision changed no picks"]
    assert p.check_summary["style"] == "fail"
    assert any(w.startswith("style: poor") for w in p.items[0].warnings)
    text = format_final_list(p.model_dump())
    assert "WARNING style: poor" in text and "Revised: yes" in text  # never hidden


def test_revisions_zero_disables_the_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _scripted(
        proposal_payload({"sofa": "a1", "lamp": "b1"}),
        style_payload({"sofa": "poor", "lamp": "good"}),
    )
    run = _run(monkeypatch, client, TWO_SLOTS, config=Config(max_revisions=0))
    assert len(client.calls) == 2 and run.proposal.revised is False
    assert run.proposal.check_summary["style"] == "fail"
    assert any("style: poor" in w for w in run.proposal.items[0].warnings)


def test_unknown_alone_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _scripted(proposal_payload({"sofa": "a1"}), style_payload({"sofa": "good"}))
    run = _run(monkeypatch, client, {"sofa": [_cand("a1", 100)]})
    assert run.proposal.check_summary["fit"] == "unknown" and run.proposal.revised is False
    assert run.proposal.items[0].fit_status == "unknown"
    assert run.proposal.items[0].fit_note == "no size found; check the product page"


def test_a_revision_that_makes_things_worse_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _scripted(
        proposal_payload({"sofa": "a1", "lamp": "b1"}),
        style_payload({"sofa": "poor", "lamp": "good"}),  # 1 failure
        proposal_payload({"sofa": "a2", "lamp": "b2"}),
        style_payload({"sofa": "poor", "lamp": "poor"}),  # 2 failures
    )
    p = _run(monkeypatch, client, TWO_SLOTS).proposal
    assert p.revised and p.items[0].pick_id == "a1"  # the original is kept
    assert p.revision_changes and "made things worse (1 -> 2" in p.revision_changes[0]
    assert p.check_summary["style"] == "fail"


def test_a_garbled_revision_keeps_the_previous_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ScriptedClient(
        [
            response([text_block(proposal_payload({"sofa": "a1", "lamp": "b1"}))]),
            response([text_block(style_payload({"sofa": "poor", "lamp": "good"}))]),
            response([text_block("not json")]),
        ]
    )
    p = _run(monkeypatch, client, TWO_SLOTS).proposal
    assert p.revised and p.items[0].pick_id == "a1"
    assert any("revision failed" in w for w in p.warnings)


def test_a_failed_style_call_is_unknown_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ScriptedClient(
        [
            response([text_block(proposal_payload({"sofa": "a1"}))]),
            response([text_block("not json")]),
        ]
    )
    p = _run(monkeypatch, client, {"sofa": [_cand("a1", 100)]}).proposal
    assert p.check_summary["style"] == "unknown" and p.items[0].style_match == "unknown"
    assert "style check failed to run" in p.items[0].style_note


def test_style_request_is_text_only_with_titles_and_retailers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _scripted(proposal_payload({"sofa": "a1"}), style_payload({"sofa": "good"}))
    _run(monkeypatch, client, {"sofa": [_cand("a1", 100, name="Oak sofa", retailer="Acme")]})
    style_call = client.calls[1]
    text = style_call["messages"][0]["content"]
    assert isinstance(text, str)  # no image blocks
    assert "Style goal: warm minimalist" in text
    assert "- slot: sofa | Oak sofa | retailer: Acme | category: sofa" in text
    assert style_call["model"] == Config().model_stylecheck
    assert style_call["output_config"]["format"]["schema"]["title"] == "StyleCheckOutput"
    proposal_call = client.calls[0]
    assert proposal_call["model"] == Config().model_proposal == "claude-sonnet-5-5"
    assert proposal_call["output_config"]["effort"] == "low"
    assert "tools" not in proposal_call and "tools" not in style_call


# ---- result shape, timing, caps ------------------------------------------------------------------


def test_critical_path_sums_the_stages_in_order() -> None:
    result = SpikeResult(
        run_id="r", llm_mode="mock", started_at="", photo_name="p", budget_usd=1.0, style="s",
        prompt_versions={}, models={}, image={},
    )  # fmt: skip

    def row(stage: str, seconds: float, **kw: Any) -> StageRow:
        return StageRow(stage, "m", 0, 0, 0, 0.0, seconds, **kw)

    result.rows = [
        row("prepare_image", 0.1),
        row("perception", 9.0),
        row("retrieval: a / search", 1.0, slot="a", kind="search"),
        row("retrieval: a / rank", 2.0, slot="a", kind="rank"),
        row("retrieval: a / lookup 1", 3.0, slot="a", kind="lookup"),
        row("retrieval: a / lookup 2", 5.0, slot="a", kind="lookup"),  # slowest: chain = 8
        row("retrieval: b / search", 1.0, slot="b", kind="search"),
        row("retrieval: b / lookup 1", 2.0, slot="b", kind="lookup"),  # chain = 3
        row("proposal", 6.0),
        row("self-check / code", 0.01, group="selfcheck"),
        row("self-check / style", 3.0, group="selfcheck"),  # parallel: 3.0 counts
        row("revision", 5.0),
        row("revision / self-check / code", 0.01, group="recheck"),
        row("revision / self-check / style", 2.5, group="recheck"),
    ]
    assert result.critical_path_seconds == pytest.approx(0.1 + 9.0 + 8.0 + 6.0 + 3.0 + 5.0 + 2.5)
    assert result.total_seconds == pytest.approx(0.1 + 9 + 1 + 2 + 3 + 5 + 1 + 2 + 6 + 0.01 + 3 + 5
                                                 + 0.01 + 2.5)  # fmt: skip
    assert result.perception_retrieval_seconds == pytest.approx(9.0 + 8.0)


def test_separate_cost_caps_trip_independently() -> None:
    img = synthetic_room_image()
    # provider cap only: default mock run spends about $0.15 of provider dollars
    with pytest.raises(BudgetExceededError, match="provider cost cap"):
        run_spike(img, budget_usd=800, style="x", config=Config(max_provider_cost_usd=0.04))
    # Anthropic cap only: proposal and self-check push Anthropic dollars past a small cap
    with pytest.raises(BudgetExceededError) as info:
        run_spike(img, budget_usd=800, style="x", config=Config(max_cost_usd=0.018))
    assert "provider" not in str(info.value) and "cost cap" in str(info.value)
    # a provider total far over the Anthropic cap is fine when its own cap allows it
    roomy = Config(max_provider_cost_usd=5.0, serpapi_usd_per_search=0.2)
    result = run_spike(img, budget_usd=800, style="x", config=roomy)
    assert result.provider_cost_usd > roomy.max_cost_usd
    assert result.anthropic_cost_usd < roomy.max_cost_usd


def test_step_cap_for_a_three_slot_run_with_a_revision_fits_the_default() -> None:
    result = run_spike(synthetic_room_image(), budget_usd=800, style="x", config=Config())
    assert result.proposal["revised"] is True  # the default mock run exercises the revision
    # perception 1 + 3 ranks + proposal + style + revision + style = 8, under max_steps 12
    assert result.steps == 8 <= Config().max_steps
    # a revision with web_search retrieval adds no extra LLM steps beyond its own calls
    web = run_spike(
        synthetic_room_image(), budget_usd=800, style="x",
        config=Config(retrieval_mode="web_search"),
    )  # fmt: skip
    assert web.steps <= Config().max_steps


def test_result_json_proposal_shape(tmp_path: Path) -> None:
    result = run_spike(synthetic_room_image(), budget_usd=800, style="x", config=Config())
    out = write_outputs(result, tmp_path)
    data = json.loads((out / "result.json").read_text())
    p = data["proposal"]
    assert set(p) == {
        "items", "totals", "summary", "checks", "check_summary", "revised",
        "revision_changes", "warnings",
    }  # fmt: skip
    assert set(p["totals"]) == {"items_total", "shipping_total", "total", "budget", "remaining"}
    assert set(p["check_summary"]) == {"budget", "fit", "style", "grounded"}
    item = p["items"][0]
    assert {
        "slot", "pick", "alternates", "rationale", "fit_status", "fit_note", "style_match",
        "style_note", "warnings", "item_total", "pick_id",
    } <= set(item)  # fmt: skip
    assert {"price", "shipping", "total", "url", "url_kind", "dimensions_m"} <= set(item["pick"])
    assert {c["check"] for c in p["checks"]} == {"budget", "fit", "style", "grounded"}
    assert data["totals"]["anthropic_cost_usd"] + data["totals"]["provider_cost_usd_estimated"] == (
        pytest.approx(data["totals"]["cost_usd"])
    )
    assert data["caps"] == {"max_cost_usd": 0.25, "max_provider_cost_usd": 0.3}
    assert data["proposal_dropped"] and data["proposal_dropped"][0]["id"] == MOCK_INVENTED_ID


def test_end_to_end_mock_prints_the_final_list_and_both_subtotals(tmp_path: Path) -> None:
    result = run_spike(synthetic_room_image(), budget_usd=800, style="x", config=Config())
    table = (write_outputs(result, tmp_path) / "table.txt").read_text()
    assert "FINAL LIST" in table and "MOCK DATA" in table
    assert "Total: $" in table and "Checks: budget pass" in table and "Revised: yes" in table
    assert "Anthropic cost: $" in table and "Provider cost (estimated): $" in table
    assert "self-check / style" in table and "revision / self-check / style" in table
    assert table.index("FINAL LIST") > table.index("TOTAL")


def test_the_mock_style_hook_lets_a_poor_match_flow_through_the_whole_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mock_llm import MockAnthropicClient

    client = MockAnthropicClient()
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    p = propose_and_check(
        _perception(), _retrieved({"sofa": [_cand("a1", 50, name="POOR-STYLE gilded sofa")]}),
        budget_usd=500, style="minimalist", budget=Budget(Config()),
    ).proposal  # fmt: skip
    assert p.revised and p.check_summary["style"] == "fail"  # the mock revision can't fix a title
    assert any("style: poor" in w for w in p.items[0].warnings)


# ---- cli and secrets -----------------------------------------------------


def _cli() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "spike_cli", Path(__file__).resolve().parents[1] / "scripts" / "spike.py"
    )
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def test_cli_flags_for_the_proposal_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_LLM_MODE", "mock")
    cli = _cli()
    photo = tmp_path / "room.jpg"
    photo.write_bytes(synthetic_room_image())
    base = [str(photo), "--budget", "800", "--style", "calm", "--slots", "1"]

    assert cli.main([*base, "--no-proposal"]) == 0
    out = capsys.readouterr().out
    assert "FINAL LIST" not in out and "self-check" not in out and "proposal" not in out.lower()

    assert cli.main([*base, "--revisions", "0"]) == 0
    out = capsys.readouterr().out
    assert "FINAL LIST" in out and "Revised: no" in out and "revision /" not in out

    assert (
        cli.main([*base, "--proposal-model", "claude-haiku-5-5", "--proposal-effort", "high"]) == 0
    )
    capsys.readouterr()
    traces = [
        json.loads(line)
        for run in (tmp_path / "spike-output").iterdir()
        for line in (run / "trace.jsonl").read_text().splitlines()
        if '"llm.proposal"' in line
    ]
    assert "claude-haiku-5-5" in {t["model"] for t in traces}


def test_fake_keys_appear_in_no_output_of_the_full_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import tracing

    keys = ["SERPER-FAKE-KEY-dddd4444", "SERPAPI-FAKE-KEY-eeee5555", "sk-ant-FAKE-ffff6666"]
    monkeypatch.setenv("SERPER_API_KEY", keys[0])
    monkeypatch.setenv("SERPAPI_API_KEY", keys[1])
    monkeypatch.setenv("ANTHROPIC_API_KEY", keys[2])
    monkeypatch.setattr(tracing, "TRACE_PATH", tmp_path / "out" / "trace.jsonl")
    result = run_spike(synthetic_room_image(), budget_usd=800, style="x", config=Config())
    out_dir = write_outputs(result, tmp_path / "out")
    (tmp_path / "out" / "trace.jsonl").replace(out_dir / "trace.jsonl")
    text = "\n".join(p.read_text() for p in out_dir.iterdir())
    assert not any(k in text for k in keys)
    assert "proposal" in json.loads((out_dir / "result.json").read_text())
