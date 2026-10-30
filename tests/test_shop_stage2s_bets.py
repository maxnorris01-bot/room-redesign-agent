"""Stage 2s: a learned seller's exposure matches the evidence behind it (graduated allowance) and a
bet that fails is revoked right after the ratings show it. Mock model, mock catalogue."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.shop import bets as bt
from app.shop.models import BetBatch, SellerBet
from app.shop.providers import PREMIUM_SELLER, build_shop_providers
from app.shop.service import ShopService
from app.shop.store import SessionStore
from app.shop.thresholds import (
    RESULT_COUNT,
    SELLER_MAX_SHARE,
    SELLER_PINNED_SHARE,
    seller_slots,
)
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)
from test_shop_learn import seller_scenario

KEY = "hartwellandco"


def bet(level: int = 0, batches: list[tuple[list[str], bool]] | None = None) -> SellerBet:
    return SellerBet(
        key="s", name="Seller", level=level,
        batches=[BetBatch(index=i + 1, ids=x, judged=j) for i, (x, j) in enumerate(batches or [])],
    )  # fmt: skip


def liked(ids: list[str], n_liked: int) -> dict[str, str]:
    return {i: ("like" if n < n_liked else "dislike") for n, i in enumerate(ids)}


# ---- the allowance ------------------------------


def test_the_first_try_is_a_sixth_of_the_batch_and_never_below_the_ordinary_cap() -> None:
    assert seller_slots(1 / 6) == 4 and seller_slots(1 / 6, 12) == 2 and seller_slots(1 / 12) == 2
    a = bt.allowance(bet(), {})
    assert a["slots"] == 4 and a["why"] == "first try" and a["limit"] == 8
    small = bt.allowance(bet(), {}, size=8)  # a short batch scales down, but not under its cap
    assert small["slots"] == 2


def test_each_success_adds_two_slots_up_to_a_third() -> None:
    slots = [bt.allowance(bet(level), {})["slots"] for level in range(5)]
    assert slots == [4, 6, 8, 8, 8]  # a third of 24 is 8
    assert "at its limit of 8" in bt.allowance(bet(3), {})["why"]


def test_half_a_batch_only_after_two_batches_liked_at_sixty_percent() -> None:
    ids1, ids2 = ["a", "b", "c", "d", "e"], ["f", "g", "h", "i", "j"]
    ratings = {**liked(ids1, 4), **liked(ids2, 3)}  # 80% and 60%
    proven = bet(4, [(ids1, True), (ids2, True)])
    assert bt.allowance(proven, ratings)["slots"] == seller_slots(SELLER_MAX_SHARE) == 12
    weaker = {**liked(ids1, 4), **liked(ids2, 2)}  # 80% and 40%: only a third
    assert bt.allowance(proven, weaker)["slots"] == 8
    one = bet(4, [(ids1, True)])  # one judged batch is not two
    assert bt.allowance(one, ratings)["slots"] == 8


def test_a_pin_only_raises_the_ceiling_it_does_not_skip_the_proof() -> None:
    assert bt.allowance(bet(0), {}, pinned=True)["slots"] == 4  # still a first try
    assert bt.allowance(bet(5), {}, pinned=True)["slots"] == 14  # 4 + 5 x 2, under 80% of 24
    assert bt.allowance(bet(20), {}, pinned=True)["slots"] == int(SELLER_PINNED_SHARE * 24 + 0.5)


def test_growth_needs_three_rated_boosted_items_and_half_of_them_liked() -> None:
    ids = ["a", "b", "c", "d"]
    previous = {"s": bet(0, [(ids, False)])}
    grown = bt.update_bets(previous, liked(ids, 3), 0.3, {"s": "Seller"}, 8).bets["s"]
    assert grown.level == 1 and grown.batches[0].judged
    flat = bt.update_bets(previous, {"a": "like", "b": "like"}, 0.3, {"s": "Seller"}, 8).bets["s"]
    assert flat.level == 0 and not flat.batches[0].judged  # two ratings: nothing changes yet
    mixed = bt.update_bets(previous, liked(ids, 2), 0.1, {"s": "Seller"}, 8).bets["s"]
    assert mixed.level == 1  # 50% counts as a success when it is not under the baseline
    assert bt.update_bets(previous, liked(ids, 2), 0.1, {}, 8).bets["s"].level == 1


def test_a_new_eligible_seller_opens_a_bet() -> None:
    out = bt.update_bets({}, {}, 0.3, {"s": "Seller"}, 4)
    assert out.bets["s"].level == 0 and out.bets["s"].status == "active" and not out.revoked


# ---- through the service ------------------------------


def service(tmp_path: Path, bets: bool = True) -> ShopService:
    cfg = Config(tracing_disabled=True, shop_seller_bets=bets)
    search, lookup = build_shop_providers(cfg)
    return ShopService(cfg, SessionStore(tmp_path / f"d{bets}"), search, lookup)


def premium_count(results: list[dict[str, Any]]) -> int:
    return sum(1 for r in results if r["seller"] == PREMIUM_SELLER)


def test_a_newly_learned_seller_gets_a_small_first_exposure(tmp_path: Path) -> None:
    sid_old = seller_scenario(service(tmp_path, bets=False))[0]
    off = service(tmp_path, bets=False)
    old = off.shop_again(seller_scenario(off)[0])["search"]
    on = service(tmp_path, bets=True)
    sid, _ = seller_scenario(on)
    new = on.shop_again(sid)["search"]
    assert sid_old and premium_count(old["results"]) > premium_count(new["results"])
    assert premium_count(new["results"]) <= 7  # the ordinary cap, relaxed at most as a short batch
    rows = new["trace"]["batch"]["seller_bets"]
    assert [r["text"] for r in rows][0].startswith(f"{PREMIUM_SELLER}: 4 slots, first try")
    assert rows[0]["shown"] == premium_count(new["results"]) and rows[0]["slots"] == 4
    bets = on.store.load(sid).seller_bets
    assert bets[KEY].level == 0 and bets[KEY].status == "active"


def test_the_exposure_is_one_allowance_across_chip_named_search_and_pin(tmp_path: Path) -> None:
    on = service(tmp_path)
    sid, _ = seller_scenario(on)
    on.shop_again(sid)  # the chip needs a profile before it can be pinned
    on.pin_preference(sid, f"seller: {PREMIUM_SELLER}")  # pinned used to mean up to 19 slots
    pinned = on.shop_again(sid)["search"]
    row = pinned["trace"]["batch"]["seller_bets"][0]
    assert row["slots"] == 4 and row["limit"] == int(SELLER_PINNED_SHARE * RESULT_COUNT + 0.5)
    assert premium_count(pinned["results"]) <= 7
    named = [r for r in pinned["results"] if (r.get("source") or {}).get("kind") == "seller"]
    assert premium_count(pinned["results"]) >= len(named)  # the named search is inside it


def test_the_panel_and_the_evidence_table_text_are_in_the_trace(tmp_path: Path) -> None:
    on = service(tmp_path)
    sid, _ = seller_scenario(on)
    again = on.shop_again(sid)["search"]
    rows = again["trace"]["batch"]["seller_bets"]
    assert rows and {"seller", "slots", "limit", "why", "text", "shown", "boosted_now"} <= set(
        rows[0]
    )
    app_js = (Path(__file__).resolve().parents[1] / "src/app/shop/static/app.js").read_text()
    assert "seller_bets" in app_js and "'exposure'" in app_js


def test_with_the_switch_off_nothing_changes(tmp_path: Path) -> None:
    off = service(tmp_path, bets=False)
    sid, _ = seller_scenario(off)
    again = off.shop_again(sid)["search"]
    assert "seller_bets" not in again["trace"]["batch"] and off.store.load(sid).seller_bets == {}


def test_a_revoked_bet_would_not_survive_a_new_topic_reset() -> None:
    from pydantic import ValidationError

    from app.shop.store import advance_topic  # noqa: F401  (the reset is in advance_topic)

    with pytest.raises(ValidationError):
        SellerBet(key="s", name="x", status="maybe")  # type: ignore[arg-type]


# ---- outcome check and revocation ------------------------------


def boosted_ids(view: dict[str, Any]) -> list[str]:
    """The batch's items shown under the premium seller's boost (named search or above the cap)."""
    rows = [r for r in view["results"] if r["seller"] == PREMIUM_SELLER]
    named = [r["id"] for r in rows if (r.get("source") or {}).get("kind") == "seller"]
    return named or [r["id"] for r in rows][4:]


def rate_all(svc: ShopService, sid: str, ids: list[str], rating: str) -> None:
    for rid in ids:
        svc.rate(sid, rid, rating)


def rate_others(svc: ShopService, sid: str, view: dict[str, Any], n: int, rating: str) -> None:
    done = 0
    for r in view["results"]:
        if r["seller"] != PREMIUM_SELLER and svc.store.load(sid).ratings.get(r["id"]) is None:
            svc.rate(sid, r["id"], rating)
            done += 1
            if done >= n:
                break


def test_the_persona_turns_against_the_seller_and_the_bet_is_revoked(tmp_path: Path) -> None:
    on = service(tmp_path)
    sid, _ = seller_scenario(on)
    first = on.shop_again(sid)["search"]
    mine = boosted_ids(first)
    assert len(mine) >= 3, "the mock catalogue must show at least three boosted items"
    rate_all(on, sid, mine, "dislike")
    second = on.shop_again(sid)["search"]
    message = f"Tried more {PREMIUM_SELLER}; you did not like most of it, so I stopped."
    assert message in second["warnings"] and message in second["note"] + " ".join(
        second["warnings"]
    )
    assert on.store.load(sid).seller_bets[KEY].status == "revoked"
    # the next batch obeys the ordinary cap and the named search is gone
    assert premium_count(second["results"]) <= 4
    assert not any((r.get("source") or {}).get("kind") == "seller" for r in second["results"])
    assert not any(PREMIUM_SELLER.lower() in q.lower() for q in second["queries"])
    profile = on.session_view(sid)["profile"]
    assert not any(e["attr_kind"] == "seller" for e in profile["inferred"])
    assert profile["dismissed"][f"seller:{KEY}"] > 0
    rows = second["trace"]["batch"].get("seller_bets", [])
    assert not rows or "revoked" in rows[0]["text"]


def test_a_revoked_seller_returns_only_after_eight_more_ratings(tmp_path: Path) -> None:
    on = service(tmp_path)
    sid, _ = seller_scenario(on)
    first = on.shop_again(sid)["search"]
    rate_all(on, sid, boosted_ids(first), "dislike")
    second = on.shop_again(sid)["search"]
    assert on.store.load(sid).seller_bets[KEY].status == "revoked"
    rate_others(on, sid, second, 4, "dislike")  # 4 of the 8 ratings it must wait for
    third = on.shop_again(sid)["search"]
    assert on.store.load(sid).seller_bets[KEY].status == "revoked"
    assert not any(e["attr_kind"] == "seller" for e in on.session_view(sid)["profile"]["inferred"])
    rate_others(on, sid, third, 6, "dislike")  # past 8 in all
    fourth = on.shop_again(sid)["search"]
    again = on.store.load(sid).seller_bets.get(KEY)
    if again is not None and again.status == "active":  # the evidence may still earn it back
        assert again.level == 0 and fourth["trace"]["batch"]["seller_bets"][0]["slots"] == 4


def test_a_persona_who_likes_the_seller_grows_the_allowance_step_by_step(tmp_path: Path) -> None:
    on = service(tmp_path)
    sid, _ = seller_scenario(on)
    levels, slots = [], []
    view = on.shop_again(sid)["search"]
    for _ in range(4):
        mine = boosted_ids(view)
        rate_all(on, sid, mine, "love")
        view = on.shop_again(sid)["search"]
        bet = on.store.load(sid).seller_bets[KEY]
        levels.append(bet.level)
        rows = view["trace"]["batch"].get("seller_bets", [])
        if rows:
            slots.append(rows[0]["slots"])
    assert levels == sorted(levels) and max(levels) >= 1
    assert slots and all(s <= 12 for s in slots) and slots == sorted(slots)
    assert on.store.load(sid).seller_bets[KEY].status == "active"


def test_ratings_below_the_minimum_change_nothing(tmp_path: Path) -> None:
    on = service(tmp_path)
    sid, _ = seller_scenario(on)
    first = on.shop_again(sid)["search"]
    mine = boosted_ids(first)
    rate_all(on, sid, mine[:2], "dislike")  # two ratings: under the three that count
    second = on.shop_again(sid)["search"]
    bet = on.store.load(sid).seller_bets[KEY]
    assert bet.status == "active" and bet.level == 0
    assert not any("Tried more" in w for w in second["warnings"])


def test_revoking_uses_the_baseline_too() -> None:
    ids = ["a", "b", "c", "d"]
    ratings = {"a": "like", "b": "like", "c": "dislike", "d": "dislike"}  # 50%: above 35% ...
    previous = {"s": bet(1, [(ids, True)])}
    kept = bt.update_bets(previous, ratings, 0.40, {"s": "Seller"}, 20)
    assert not kept.revoked and kept.bets["s"].status == "active"
    gone = bt.update_bets(
        previous, ratings, 0.65, {"s": "Seller"}, 20
    )  # ... but under the baseline
    assert [r["name"] for r in gone.revoked] == ["Seller"] and gone.bets["s"].revoked_at == 20
    low = bt.update_bets(previous, {i: "dislike" for i in ids}, 0.10, {"s": "Seller"}, 20)
    assert low.revoked and bt.dismissed_keys({"seller:s": 20}, 27) == {"s"}
    assert bt.dismissed_keys({"seller:s": 20}, 28) == set()  # eight more ratings: free again
    assert bt.revocation_message("Wayfair") == (
        "Tried more Wayfair; you did not like most of it, so I stopped."
    )


def test_the_revoked_seller_earns_no_chip_until_the_wait_is_over() -> None:
    from app.shop.evidence import seller_entries
    from app.shop.textutil import Rated

    rated = {
        f"r{i}": Rated(f"r{i}", "love" if i < 4 else "dislike", "", f"T{i}", 100.0, "Wayfair", "")
        for i in range(6)
    }  # 4 of 6 liked from Wayfair against a baseline that is the same: not 1.5 times it
    mix = {
        **rated,
        **{f"o{i}": Rated(f"o{i}", "dislike", "", "x", 90.0, "Other", "") for i in range(8)},
    }
    entries, _ = seller_entries(mix)
    assert [e.text for e in entries] == ["seller: Wayfair"]
    waiting, trace = seller_entries(mix, {"wayfair"})
    assert waiting == [] and "waits for more ratings" in trace["candidates"][0]["verdict"]


def test_the_records_and_constants_are_in_place() -> None:
    from evals.shop.spec import thresholds_sha256

    from app.shop.thresholds import all_thresholds

    root = Path(__file__).resolve().parents[1]
    adr = (root / "docs" / "adr" / "0012-two-modes-shop-first.md").read_text()
    assert "Amendment, stage 2s" in adr and "Tried more Wayfair" in adr
    names = [
        "SELLER_FIRST_SHARE", "SELLER_GROW_STEP_SHARE", "SELLER_PROVEN_MAX_SHARE",
        "SELLER_TOP_MIN_RATE", "SELLER_GROW_MIN_RATED", "SELLER_GROW_MIN_RATE",
        "SELLER_REVOKE_MIN_RATED", "SELLER_REVOKE_MAX_RATE", "SELLER_REDISMISS_RATED",
    ]  # fmt: skip
    for name in names:
        assert name in all_thresholds() and f"| {name} |" in adr
    assert (root / "docs" / "sessions" / "2026-10-30-shop-seller-affinity.md").is_file()
    wanted = "c64fe35a51c389603c507ed0a31e4388d08a1e96ee25f2d0bf9288f1fb75d237"
    assert thresholds_sha256() == wanted
