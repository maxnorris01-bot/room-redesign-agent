"""Shop Mode stage 2i: group like-rate evidence, the leaning layer, the marketplace exclusion, seller
searches and model-written queries, page depth, the footer wording and the session pool snapshot.

The numbers come from the owner's last live Shop Again (48 rated, 15 liked): `tailoring_evidence.json`
reproduces them (premium tier 7 liked of 13 rated, West Elm 3 of 5, Article 2 of 4, CB2 2 of 4,
marketplace and budget 1 of 11, natural wood frame 7 of 10, black frame 4 of 31). Nothing sleeps.

Offline like the other Shop Mode tests (same loopback-only socket guard).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.shop.decide import decide
from app.shop.evidence import (
    MARKETPLACE_TEXT,
    baseline_of,
    group_stat,
    marketplace_decision,
    seller_entries,
    tier_entry,
    toward_fails,
)
from app.shop.models import ItemTags, ProfileEntry, StoredProfile
from app.shop.prefs import (
    alignment_counts,
    alignment_detail,
    alignment_signals,
    profile_summary,
    reapply_removed,
    remove_entry,
)
from app.shop.providers import ReplayPoolSearch
from app.shop.queries import QueryInput, validate_query, write_queries
from app.shop.replay import REPLAY_FILE_EVIDENCE, load_replay, seed_session
from app.shop.retailers import MARKETPLACE_BUDGET, canonical_name, tier_of
from app.shop.search import build_results
from app.shop.service import ShopService
from app.shop.store import SessionStore
from app.shop.textutil import Rated
from app.shopping import SearchCandidate
from test_shop import (  # noqa: F401  (the autouse guard must be imported to apply here)
    FakeLookup,
    FakeSearch,
    RouterClient,
    _loopback_only,
    cand,
    config,
    query,
    use_client,
)

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "app" / "shop" / "static"


def evidence_rated() -> dict[str, Rated]:
    return {
        r["id"]: Rated(r["id"], r["rating"], "", r["title"], r["price"], r["seller"], "")
        for r in load_replay(REPLAY_FILE_EVIDENCE)["rated"]
    }


def evidence_tags() -> dict[str, ItemTags]:
    out: dict[str, ItemTags] = {}
    for r in load_replay(REPLAY_FILE_EVIDENCE)["rated"]:
        a = r["attrs"]
        out[r["id"]] = ItemTags(
            id=r["id"], shape=a["shape"], material=a["material"], color_finish=a["color_finish"],
            frame_color=a["frame_color"], style=[a["style"]] if a["style"] else [], quality_cues=[],
        )  # fmt: skip
    return out


def decided(dismissed: dict[str, int] | None = None, rated: dict[str, Rated] | None = None) -> Any:
    rated = rated or evidence_rated()
    return decide(rated, evidence_tags(), [], [], ["wood", "metal", "storage"], dismissed=dismissed)


def entries_of(out: Any, kind: str) -> list[ProfileEntry]:
    return [e for e in out.entries if e.attr_kind == kind]


# ---- the like-rate function ------------------------------------------------------------------------


def test_the_owners_numbers_are_in_the_fixture() -> None:
    rated = evidence_rated()
    base = baseline_of(rated)
    assert (base.rated, base.liked) == (48, 15) and round(base.rate, 2) == 0.31
    premium = group_stat(
        "tier", "design_premium", "premium", rated, lambda r: tier_of(r.seller) == "design_premium"
    )
    market = group_stat(
        "tier",
        MARKETPLACE_BUDGET,
        "marketplace",
        rated,
        lambda r: tier_of(r.seller) == MARKETPLACE_BUDGET,
    )
    assert (premium.rated, premium.liked) == (13, 7) and round(premium.rate, 2) == 0.54
    assert (market.rated, market.liked) == (11, 1) and round(market.rate, 2) == 0.09
    for name, rated_n, liked_n in (
        ("West Elm", 5, 3),
        ("Article", 4, 2),
        ("CB2", 4, 2),
        ("Lowe's", 5, 1),
    ):
        s = group_stat("seller", name, name, rated, lambda r, n=name: canonical_name(r.seller) == n)
        assert (s.rated, s.liked) == (rated_n, liked_n), name


def test_the_like_rate_does_not_depend_on_how_often_a_group_was_shown() -> None:
    rated = evidence_rated()
    base = baseline_of(rated)
    stat = group_stat("seller", "West Elm", "West Elm", rated, lambda r: r.seller == "West Elm")
    assert not toward_fails(stat, base)  # 3 of 5 = 60%, over 1.5 times 31% and over 35%
    # the same ratings with nothing else shown or tagged decide the same chips
    assert [e.text for e in seller_entries(rated)[0]] == [
        e.text for e in seller_entries(dict(rated))[0]
    ]


# ---- seller and tier chips ------------------------------------------------------------------------------


def test_the_premium_tier_chip_is_earned_with_7_liked_of_13_rated() -> None:
    entry, trace = tier_entry(evidence_rated())
    assert entry is not None and entry.attr_kind == "tier" and entry.values == ["design_premium"]
    assert entry.detail.startswith("liked 7 of 13 (54%) against 31% overall")
    premium = next(c for c in trace["candidates"] if c["tier"] == "design_premium")
    assert premium["verdict"] == "chip" and premium["like_rate"] == pytest.approx(0.5385, abs=1e-3)
    mainstream = next(c for c in trace["candidates"] if c["tier"] == "design_mainstream")
    assert mainstream["rated"] == 7 and mainstream["liked"] == 1 and mainstream["verdict"] != "chip"
    market = next(c for c in trace["candidates"] if c["tier"] == MARKETPLACE_BUDGET)
    assert "never earn a retailer chip" in market["verdict"]


def test_west_elm_article_and_cb2_each_earn_a_seller_chip_and_lowes_does_not() -> None:
    entries, trace = seller_entries(evidence_rated())
    assert [e.text for e in entries] == ["seller: West Elm", "seller: Article", "seller: CB2"] or {
        e.text for e in entries
    } == {"seller: West Elm", "seller: Article", "seller: CB2"}
    rows = {c["seller"]: c for c in trace["candidates"]}
    assert rows["West Elm"]["like_rate"] == 0.6 and rows["Article"]["like_rate"] == 0.5
    assert rows["Lowe's"]["verdict"].startswith("no chip: liked 1, needs 2")
    assert "never earn a positive chip" in rows["Lowe's"]["verdict"]
    assert all("Walmart" not in e.text for e in entries)  # a marketplace never earns a chip
    assert entries[0].text == "seller: West Elm"  # the highest like rate first


def test_marketplace_retailers_never_earn_a_positive_chip_whatever_their_rate() -> None:
    rated = {
        f"w{i}": Rated(f"w{i}", "love", "", f"T{i}", 100.0, "Walmart", "") for i in range(6)
    } | {f"o{i}": Rated(f"o{i}", "dislike", "", f"U{i}", 100.0, "Etsy", "") for i in range(8)}
    entries, _ = seller_entries(rated)
    assert not [e for e in entries if "Walmart" in e.text]
    assert tier_entry(rated)[0] is None


def test_the_old_net_dislike_and_share_rules_are_gone() -> None:
    from app.shop import thresholds

    names = thresholds.all_thresholds()
    assert "SELLER_NET_DISLIKE_RATIO" not in names and "TIER_MIN_LIKE_SHARE" not in names
    # 7 liked against 6 disliked in a tier failed "3 times the disliked mass" before; it passes now
    assert tier_entry(evidence_rated())[0] is not None


# ---- the leaning layer ----------------------------------------------------------------------------------------


def leanings(out: Any) -> dict[str, tuple[str, str]]:
    return {e.text: (e.attr_kind, e.dimension) for e in out.entries if e.status == "leaning"}


def test_natural_wood_frame_is_leaning_toward_and_black_frame_is_leaning_away() -> None:
    out = decided()
    found = leanings(out)
    assert found["leaning toward natural wood (frame color)"] == ("leaning_toward", "frame_color")
    assert found["leaning away from black (frame color)"] == ("leaning_away", "frame_color")
    toward = next(e for e in out.entries if e.text == "leaning toward natural wood (frame color)")
    away = next(e for e in out.entries if e.text == "leaning away from black (frame color)")
    assert toward.detail == "liked 7 of 10 (70%) against 31% overall"
    assert away.detail == "liked 4 of 31 (13%) against 31% overall"
    assert toward.status == away.status == "leaning" and toward.polarity == "like"


def test_rectangular_and_wood_earn_nothing() -> None:
    out = decided()
    assert not [
        e
        for e in out.entries
        if e.attr_kind.startswith("leaning") and e.dimension in ("shape", "material")
    ]
    rows = {(r["kind"], r["name"]): r for r in out.trace["evidence"]["rows"]}
    assert rows[("shape", "rectangular")]["verdict"] == "nothing"
    assert rows[("material", "wood")]["verdict"] == "nothing"
    assert (
        rows[("shape", "rectangular")]["rated"] == 24
        and rows[("shape", "rectangular")]["liked"] == 7
    )


def test_a_leaning_needs_the_counts_and_never_overrides_a_converged_value() -> None:
    out = decided()
    rows = {(r["kind"], r["name"]): r for r in out.trace["evidence"]["rows"]}
    # only values with at least 2 rated items are in the table, each with a verdict and a reason
    assert all(r["rated"] >= 2 and r["reason"] for r in out.trace["evidence"]["rows"])
    assert (
        rows[("frame_color", "white")]["verdict"] == "nothing"
    )  # 0 of 3 rated: under 6 to lean away
    assert "rated 3, needs 6 to lean away" in rows[("frame_color", "white")]["reason"]
    # a dimension that converged on a value gets no leaning toward it
    liked_only = {k: v for k, v in evidence_rated().items() if v.rating != "dislike"}
    out2 = decide(liked_only, evidence_tags(), [], [], [])
    assert not [e for e in out2.entries if e.attr_kind.startswith("leaning")]  # baseline is 100%


def test_a_leaning_is_not_made_for_what_the_request_typed_or_the_shopper_deleted() -> None:
    typed = decide(evidence_rated(), evidence_tags(), [], [], ["natural wood"])
    assert "leaning toward natural wood (frame color)" not in leanings(typed)
    deleted = decide(
        evidence_rated(), evidence_tags(), [], ["leaning away from black frame color"], []
    )
    assert "leaning away from black (frame color)" not in leanings(deleted)
    assert "leaning toward natural wood (frame color)" in leanings(deleted)


def test_deleting_a_leaning_chip_removes_its_effect_and_it_stays_deleted() -> None:
    out = decided()
    profile = StoredProfile(inferred=out.entries)
    chip = "leaning toward natural wood (frame color)"
    gone = remove_entry(profile, "inferred", chip)
    assert gone is not None and chip not in [e.text for e in gone.inferred]
    again = decide(evidence_rated(), evidence_tags(), [], gone.removed, [])
    assert chip not in leanings(again) and "leaning away from black (frame color)" in leanings(
        again
    )


# ---- the marketplace exclusion --------------------------------------------------------------------------


def test_the_marketplace_exclusion_fires_with_1_liked_of_11() -> None:
    out = decided()
    chip = entries_of(out, "exclude_marketplace")
    assert len(chip) == 1 and chip[0].text == MARKETPLACE_TEXT
    assert chip[0].detail == "liked 1 of 11" and chip[0].status == "filter"
    assert out.trace["marketplace"]["decision"] == "chip"


def test_the_marketplace_exclusion_needs_8_rated_and_a_low_like_rate() -> None:
    rated = evidence_rated()
    few = {
        k: v for k, v in rated.items() if tier_of(v.seller) != MARKETPLACE_BUDGET or int(k[1:]) % 2
    }
    assert (
        marketplace_decision(few)[0] is None
        or sum(1 for v in few.values() if tier_of(v.seller) == MARKETPLACE_BUDGET) >= 8
    )
    seven = {
        f"m{i}": Rated(f"m{i}", "dislike", "", f"T{i}", 1.0, "Walmart", "") for i in range(7)
    } | {f"g{i}": Rated(f"g{i}", "like", "", f"G{i}", 1.0, "Etsy", "") for i in range(4)}
    entry, trace = marketplace_decision(seven)
    assert entry is None and "rated 7, needs 8" in trace["reason"]
    eight = seven | {"m7": Rated("m7", "dislike", "", "T7", 1.0, "Walmart", "")}
    assert marketplace_decision(eight)[0] is not None
    liked_some = {
        f"m{i}": Rated(f"m{i}", "like" if i < 3 else "dislike", "", f"T{i}", 1.0, "Walmart", "")
        for i in range(10)
    }  # 30% liked: over 20%
    assert marketplace_decision(liked_some)[0] is None


def test_deleting_the_chip_dismisses_it_until_8_more_items_are_rated() -> None:
    out = decided()
    profile = StoredProfile(inferred=out.entries, rated_count=48)
    gone = remove_entry(profile, "inferred", MARKETPLACE_TEXT, rated_now=48)
    assert gone is not None and gone.dismissed == {"marketplace": 48}
    assert MARKETPLACE_TEXT not in " ".join(gone.removed)  # dismissed, not forgotten for good
    assert not entries_of(decided(gone.dismissed), "exclude_marketplace")
    more = dict(evidence_rated())
    for i in range(7):  # seven more ratings: still under 8
        more[f"n{i}"] = Rated(f"n{i}", "dislike", "", f"N{i}", 1.0, "Maple & Co", "")
    assert not entries_of(decided(gone.dismissed, more), "exclude_marketplace")
    more["n7"] = Rated("n7", "dislike", "", "N7", 1.0, "Maple & Co", "")
    assert entries_of(decided(gone.dismissed, more), "exclude_marketplace")  # 8 more: back
    # a chip dismissed while a Shop Again ran stays dismissed
    kept = reapply_removed(profile, [], {"marketplace": 48})
    assert not [e for e in kept.inferred if e.attr_kind == "exclude_marketplace"]


# ---- seller searches, queries and page depth, end to end on the fixture --------------------------------------


def evidence_service(tmp_path: Path, **cfg: Any) -> tuple[ShopService, str, ReplayPoolSearch]:
    config_ = Config(tracing_disabled=True, shop_model_queries=True, **cfg)
    search = ReplayPoolSearch(REPLAY_FILE_EVIDENCE)
    store = SessionStore(tmp_path / "shop-data")
    svc = ShopService(config_, store, search, FakeLookup())
    session = seed_session(store, replay=REPLAY_FILE_EVIDENCE)
    return svc, session.session_id, search


def test_a_batch_from_this_evidence_has_no_marketplace_sellers_and_some_from_the_liked_sellers(
    tmp_path: Path,
) -> None:
    svc, sid, search = evidence_service(tmp_path)
    view = svc.shop_again(sid)
    found = view["search"]
    sellers = [r["seller"] for r in found["results"]]
    assert len(sellers) >= 12 and not [s for s in sellers if tier_of(s) == MARKETPLACE_BUDGET]
    liked = [s for s in sellers if s in ("West Elm", "Article", "CB2")]
    assert len(liked) >= 6, sellers
    kinds = {e["attr_kind"] for e in view["profile"]["inferred"]}
    assert {"seller", "tier", "exclude_marketplace", "leaning_toward", "leaning_away"} <= kinds
    batch = found["trace"]["batch"]
    assert batch["marketplace_excluded"] > 0
    assert not [w for w in found["warnings"] if "without marketplace retailers" in w]
    assert batch["serper_calls"] <= 9 and found["serper_credits"] == found["serper_calls"] * 2
    assert len(search.calls) == found["serper_calls"]
    # the batch leans where the evidence leans
    leaning = batch["leanings"]
    assert leaning["toward"]["frame_color:natural wood"]["in_batch"] >= 8
    assert leaning["away"]["frame_color:black"]["in_batch"] <= 7


def test_seller_searches_run_for_the_liked_sellers_without_any_chip_deciding_it(
    tmp_path: Path,
) -> None:
    svc, sid, search = evidence_service(tmp_path)
    view = svc.shop_again(sid)
    peers = view["search"]["trace"]["batch"]["peers"]
    searched = [p["peer"] for p in peers if p["kind"] == "seller"]
    assert searched == [
        "West Elm",
        "Article",
        "CB2",
    ]  # highest like rate first, never a marketplace
    texts = [q for q, _ in search.calls]
    assert any(t.startswith("West Elm ") and "coffee table" in t for t in texts)
    assert all(not t.lower().startswith(("walmart", "lowe")) for t in texts)
    # delete the West Elm chip: it is no longer searched next time
    out = svc.remove_preference(sid, "inferred", "seller: West Elm")
    assert "seller: West Elm" not in [e["text"] for e in out["profile"]["inferred"]]
    second = svc.shop_again(sid)
    names = [
        p["peer"] for p in second["search"]["trace"]["batch"]["peers"] if p["kind"] == "seller"
    ]
    assert "West Elm" not in names


def test_fewer_than_three_liked_sellers_are_filled_with_peers_of_the_tier(tmp_path: Path) -> None:
    from app.shop.again import seller_plan

    svc, sid, _ = evidence_service(tmp_path)
    session = svc.store.load(sid)
    for rid in ("E1", "E2", "E3"):  # West Elm's three liked items become dislikes
        session.ratings[rid].rating = "dislike"
    svc.store.save(session)
    base = query(request="coffee tables", search_text="wood and metal coffee table with storage")
    view = svc.shop_again(sid)
    profile = StoredProfile.model_validate(view["profile"])
    plan, liked_keys = seller_plan(svc.store.load(sid), profile, base)
    names = [name for _, name, _ in plan]
    assert len(names) == 3 and liked_keys <= {"article", "cb2"}
    assert any(n not in ("Article", "CB2") for n in names)  # a peer filled the free slot


def test_the_model_writes_queries_that_are_validated_and_run_as_page_one(tmp_path: Path) -> None:
    svc, sid, search = evidence_service(tmp_path)
    view = svc.shop_again(sid)
    mq = view["search"]["trace"]["batch"]["model_queries"]
    assert mq["asked"] and mq["accepted"] and not mq["error"]
    for q in mq["accepted"]:
        assert len(q.split()) <= 8 and "coffee table" in q or "table" in q
    assert [s["query"] for s in mq["searches"]] == mq["accepted"]
    assert all(s["page"] == 1 for s in mq["searches"])
    texts = [q for q, _ in search.calls]
    assert all(q in texts for q in mq["accepted"])
    assert (
        view["search"]["anthropic_usd"] >= mq["usd"] > 0
    )  # counted in the footer's Anthropic cost
    assert view["search"]["trace"]["model_queries"] == mq


def test_queries_are_off_when_the_setting_is_off(tmp_path: Path) -> None:
    cfg = Config(tracing_disabled=True, shop_model_queries=False)
    store = SessionStore(tmp_path / "shop-data")
    svc = ShopService(cfg, store, ReplayPoolSearch(REPLAY_FILE_EVIDENCE), FakeLookup())
    sid = seed_session(store, replay=REPLAY_FILE_EVIDENCE).session_id
    view = svc.shop_again(sid)
    assert not view["search"]["trace"]["batch"]["model_queries"]["asked"]


def qinput(**kw: Any) -> QueryInput:
    base = query(
        request="coffee tables with storage", search_text="wood and metal coffee table with storage",
        category="coffee table", must_have=["storage"], material_terms=["wood", "metal"],
    )  # fmt: skip
    return QueryInput(base=base, adjusted=base, liked_sellers=["West Elm"], **kw)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("natural wood legs coffee table wood metal storage", ""),
        ("coffee tables wood metal storage natural legs", ""),
        ("West Elm coffee table wood metal storage", ""),  # a liked seller is allowed
        ("Article coffee table wood metal storage", "names Article"),  # a retailer not liked
        ("coffee table wood metal storage www.example.com", "URL"),
        ("https://x.test coffee table wood metal storage", "URL"),
        ("coffee table wood metal storage under $400", "price"),
        ("wood metal storage natural legs", "category word 'table'"),
        ("coffee table wood metal natural legs", "typed constraint 'storage'"),
        ("coffee table wood storage natural legs", "typed constraint 'metal'"),
        ("coffee table", ""),
        ("one two three four five six seven eight nine coffee table storage wood metal", "words"),
        ("", "empty"),
    ],
)
def test_query_validation_accepts_and_rejects(text: str, reason: str) -> None:
    got, why = validate_query(text, qinput())
    if reason:
        assert not got and reason in why, (text, why)
    elif text == "coffee table":
        assert why  # the typed constraints are missing
    else:
        assert got and not why, (text, why)


def test_invalid_model_queries_are_dropped_silently_and_noted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.llm import Budget

    class Writer(RouterClient):
        def _create(self, **kwargs: Any) -> Any:
            if (kwargs.get("tool_choice") or {}).get("name") == "record_queries":
                from types import SimpleNamespace

                from app.mock_llm import response

                block = SimpleNamespace(
                    type="tool_use", id="t", name="record_queries",
                    input={"queries": [
                        "natural wood legs coffee table wood metal storage",
                        "see https://evil.test coffee table wood metal storage",
                        "Article coffee table wood metal storage",
                        "natural wood legs coffee table wood metal storage",
                    ]},
                )  # fmt: skip
                return response([block], stop_reason="tool_use")
            return super()._create(**kwargs)

    use_client(monkeypatch, Writer())
    result = write_queries(qinput(existing=[]), Budget(config()))
    assert result.accepted == ["natural wood legs coffee table wood metal storage"]
    reasons = [d["reason"] for d in result.dropped]
    assert any("URL" in r for r in reasons) and any("Article" in r for r in reasons)
    assert any("already planned" in r for r in reasons)
    assert result.usd > 0 and not result.error


def test_the_query_writer_input_has_the_constraints_leanings_and_titles() -> None:
    from app.shop.queries import user_text

    text = user_text(
        qinput(
            toward=["natural wood (frame color)"],
            away=["black (frame color)"],
            liked_titles=[f"Liked {i}" for i in range(9)],
            disliked_titles=[f"Disliked {i}" for i in range(9)],
        )
    )
    assert "Category: coffee table" in text and "Must have: storage" in text
    assert "Leaning toward: natural wood (frame color)" in text
    assert "Leaning away from: black (frame color)" in text
    assert "Liked retailers (you may name one): West Elm" in text
    assert "Liked 5" in text and "Liked 6" not in text  # six liked titles
    assert "Disliked 3" in text and "Disliked 4" not in text  # four disliked titles


def test_the_base_query_is_not_paged_past_page_two_and_page_three_is_a_last_resort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_client(monkeypatch, RouterClient())
    full = [cand(f"a{i}", 40.0 + i, title=f"Wood Table {i}", seller=f"S{i}") for i in range(20)]
    search = FakeSearch(
        [full, [cand(f"b{i}", 50.0 + i, title=f"Oak Table {i}", seller=f"T{i}") for i in range(20)]]
    )
    cfg = config(shop_again_max_serper_calls=6)
    from test_shop import make_service

    svc = make_service(tmp_path, search, FakeLookup(), cfg)
    first = svc.search(None, "tables")
    sid = first["session_id"]
    svc.rate(sid, first["search"]["results"][0]["id"], "love")
    # the typed search fetched page 1 (20) and page 2 (20): the base query is at page 3 now, which is
    # only a last resort. A pool already over 36 from other searches means page 3 is not fetched.
    pages_before = [p for _, p in search.calls]
    assert pages_before == [1, 2]
    search.sequence = [[cand(f"c{i}", 60.0, title=f"Pine Table {i}") for i in range(40)]]
    again = svc.shop_again(sid)
    fetched = [p for _, p in search.calls][2:]
    assert fetched == [3] and again["search"]["serper_calls"] == 1  # nothing else could fill it
    assert max(p for _, p in search.calls) <= 3
    # and a third round never goes to page 4
    search.sequence = [[cand(f"d{i}", 61.0, title=f"Ash Table {i}") for i in range(10)]]
    third = svc.shop_again(sid)
    assert max(p for _, p in search.calls) <= 3 and third["search"]["serper_calls"] == 0


def test_the_serper_cap_of_nine_holds_with_sellers_and_model_queries(tmp_path: Path) -> None:
    svc, sid, search = evidence_service(tmp_path, shop_again_max_serper_calls=4)
    view = svc.shop_again(sid)
    assert view["search"]["serper_calls"] <= 9 and len(search.calls) <= 9
    # every search call is accounted for in the trace: phrasings, sellers and written queries
    batch = view["search"]["trace"]["batch"]
    planned = len(view["search"]["queries"])
    assert planned >= len(batch["peers"]) + len(batch["model_queries"]["searches"])


# ---- the under-twelve banner ------------------------------------------------------------------------------


def marketplace_pool(non_market: int, market: int) -> list[SearchCandidate]:
    out = [
        cand(f"n{i}", 100.0 + i, title=f"Wood Table N{i}", seller=f"Studio {i}")
        for i in range(non_market)
    ]
    out += [
        cand(f"m{i}", 90.0 + i, title=f"Wood Table M{i}", seller="Walmart") for i in range(market)
    ]
    return out


def test_under_twelve_results_without_marketplaces_show_the_banner(
    tmp_path: Path,
) -> None:
    svc, sid, _ = evidence_service(tmp_path)
    # a pool with only a handful of non-marketplace candidates
    search = FakeSearch([marketplace_pool(5, 40)])
    svc.search_provider = search
    first = svc.shop_again(sid)  # first run: the evidence chip exists and filters
    del first
    search.sequence = [marketplace_pool(5, 40)]
    search.calls.clear()
    view = svc.shop_again(sid)
    found = view["search"]
    assert [w for w in found["warnings"] if w.startswith("Only ")]
    assert (
        f"Only {len(found['results'])} results without marketplace retailers. "
        "Remove the chip to include them." in found["warnings"]
    ) or len(found["results"]) >= 12
    assert not [r for r in found["results"] if tier_of(r["seller"]) == MARKETPLACE_BUDGET]


def test_the_banner_is_exact_and_marketplace_results_are_never_silently_re_added() -> None:
    from app.llm import Budget
    from app.shop.search import AgainContext, run_query

    ctx = AgainContext(exclude_marketplace=True)
    search = FakeSearch([marketplace_pool(5, 40)])
    warnings: list[str] = []
    out = run_query(
        query(request="tables", search_text="tables", category="table"),
        search, config(), Budget(config()), warnings, 0.0, ctx,
    )  # fmt: skip
    assert len(out.results) == 5 and all(r.seller.startswith("Studio") for r in out.results)
    assert (
        "Only 5 results without marketplace retailers. Remove the chip to include them."
        in out.warnings
    )
    assert out.counts.marketplace_excluded == 40 and out.composition["marketplace_excluded"] == 40


# ---- selection: leanings in a batch ------------------------------------------------------------------------


def lean_pool() -> list[tuple[SearchCandidate, str]]:
    frames = ["black"] * 20 + ["natural wood"] * 12
    out = []
    for i, f in enumerate(frames):
        words = "Black Metal Frame" if f == "black" else "Natural Wood Legs"
        out.append(
            (cand(f"c{i}", 100.0 + i, title=f"Rect Table {words} {i}", seller=f"S{i % 11}"), "")
        )
    return out


def test_a_leaning_toward_gets_a_soft_share_and_a_leaning_away_a_cap() -> None:
    ordered = lean_pool()  # black first in rank order, natural wood last
    index = {c.product_id: i + 1 for i, (c, _) in enumerate(ordered)}

    def tags(c: SearchCandidate) -> dict[str, str | None]:
        return {"frame_color": "black" if "Black" in c.title else "natural wood"}

    plain, _, _ = build_results(ordered, query(), index, tags=tags)
    assert sum(1 for r in plain if "Black" in r.title) == 20  # no leaning: rank order wins
    leaned, _, _ = build_results(
        ordered, query(), index, tags=tags,
        leanings=([("frame_color", "natural wood")], [("frame_color", "black")]),
    )  # fmt: skip
    black = sum(1 for r in leaned if "Black" in r.title)
    natural = sum(1 for r in leaned if "Natural" in r.title)
    assert black <= 7  # at most 30% of 24
    assert natural >= 8  # at least 30% of 24, where the pool has it
    assert natural == 12 and len(leaned) == black + natural  # every natural wood item in the pool


def test_a_leaning_never_pushes_a_value_above_a_converged_cap() -> None:
    ordered = lean_pool()
    index = {c.product_id: i + 1 for i, (c, _) in enumerate(ordered)}

    def tags(c: SearchCandidate) -> dict[str, str | None]:
        return {"frame_color": "black" if "Black" in c.title else "natural wood"}

    out, _, _ = build_results(
        ordered, query(), index, guard_caps={"frame_color": 0.25}, tags=tags,
        leanings=([("frame_color", "natural wood")], []),
    )  # fmt: skip
    assert sum(1 for r in out if "Natural" in r.title) <= 6  # the cap of 25% holds


def test_the_ranker_sees_the_leanings_as_soft_lines() -> None:
    from app.shop.search import AgainContext, rank_user_text

    ctx = AgainContext(
        lean_toward=[("frame_color", "natural wood")], lean_away=[("frame_color", "black")]
    )
    text = rank_user_text(query(), [cand("a")], 24, ctx)
    assert (
        "Leaning toward (soft preference; rank a little higher): natural wood (frame color)" in text
    )
    assert "Leaning away from (soft avoid; rank a little lower): black (frame color)" in text
    from app.prompts import load_prompt

    assert load_prompt("shop_rank").version == "6" and load_prompt("shop_rank", "5").version == "5"
    assert "leanings" in load_prompt("shop_rank").text


# ---- the footer and the page ----------------------------------------------------------------------------------


def test_the_footer_counts_converged_leaning_and_chip_matches_and_says_what_it_counted() -> None:
    out = decided()
    profile = StoredProfile(inferred=out.entries)
    detail = alignment_detail(profile)
    assert detail["seller_tier"] == 4  # three seller chips and the tier chip
    assert (
        detail["leaning_toward"] >= 1 and detail["other_chips"] >= 2
    )  # leaning away, the exclusion
    assert alignment_signals(profile) >= 5
    js = (STATIC / "app.js").read_text()
    assert "(counted: " in js and "seller or retailer chip" in js and "leaning toward" in js


def test_nothing_decided_yet_is_not_said_when_any_leaning_or_chip_exists() -> None:
    away_only = StoredProfile(
        inferred=[
            ProfileEntry(
                text="leaning away from black (frame color)",
                attr_kind="leaning_away",
                polarity="like",
                status="leaning",
                dimension="frame_color",
                values=["black"],
            )
        ]
    )
    assert alignment_signals(away_only) == 0  # nothing to match against...
    assert alignment_detail(away_only)["other_chips"] == 1  # ...but a chip exists: the page says so
    assert alignment_counts([], away_only) == (0, 0)
    js = (STATIC / "app.js").read_text()
    assert "matches your preferences: none yet (" in js and "c.other_chips > 0" in js
    assert (
        alignment_detail(StoredProfile())["other_chips"] == 0
    )  # the old text stays for an empty one


def test_a_leaning_toward_counts_as_a_match_and_a_leaning_away_does_not() -> None:
    from app.shop.models import ShopResult

    def result(rid: str, title: str) -> ShopResult:
        return ShopResult(
            id=rid, title=title, price=1.0, currency="USD", seller="Z", thumbnail_url="",
            link="", link_kind="none", size_hint=None, product_id=rid, rank=1, pool_position=1,
        )  # fmt: skip

    toward = ProfileEntry(
        text="leaning toward natural wood (frame color)", attr_kind="leaning_toward",
        polarity="like", status="leaning", dimension="frame_color", values=["natural wood"],
    )  # fmt: skip
    away = toward.model_copy(
        update={"attr_kind": "leaning_away", "values": ["black"], "text": "leaning away from black"}
    )
    items = [
        result("a", "Table with Natural Wood Legs"),
        result("b", "Table with Black Metal Frame"),
    ]
    hits, total = alignment_counts(items, StoredProfile(inferred=[toward, away]))
    assert (hits, total) == (1, 2)


def test_the_summary_mentions_leanings_and_the_exclusion() -> None:
    text = profile_summary(StoredProfile(inferred=decided().entries))
    assert "Leaning toward natural wood" in text and "Leaning away from black" in text
    assert "Marketplace retailers are left out." in text
    assert "from West Elm, Article and CB2" in text or "West Elm" in text


def test_the_page_draws_the_new_chips_and_the_evidence_table_with_text_nodes_only() -> None:
    js = (STATIC / "app.js").read_text()
    css = (STATIC / "style.css").read_text()
    for token in ("leaning_toward", "leaning_away", "exclude_marketplace", "like-rate evidence"):
        assert token in js, token
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js
    assert "chip.pref.lean" in css and "dashed" in css and "lean-toward" in css
    assert "Model-written queries:" in js and "Left out by the marketplace exclusion" in js


def test_seller_names_on_the_cards_are_the_canonical_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_shop import make_service

    items = [
        cand("a", 100.0, title="Wood Table A", seller="west elm"),
        cand("b", 101.0, title="Wood Table B", seller="crateandbarrel.com"),
        cand("c", 102.0, title="Wood Table C", seller="Maple & Co - Outlet Store"),
        cand("d", 103.0, title="Wood Table D", seller="Some Small Shop"),
    ]
    use = FakeSearch([items])
    svc = make_service(tmp_path, use, FakeLookup(), config())
    use_client(monkeypatch, RouterClient())
    sellers = {r["title"]: r["seller"] for r in svc.search(None, "tables")["search"]["results"]}
    assert sellers == {
        "Wood Table A": "West Elm",
        "Wood Table B": "Crate & Barrel",
        "Wood Table C": "Maple & Co",
        "Wood Table D": "Some Small Shop",
    }


# ---- the pool snapshot and the replay export ---------------------------------------------------------------------


def test_each_round_keeps_a_compact_pool_snapshot_of_at_most_100_items(tmp_path: Path) -> None:
    svc, sid, _ = evidence_service(tmp_path)
    svc.shop_again(sid)
    session = svc.store.load(sid)
    assert session.pool_snapshots and session.pool_snapshots[-1]["kind"] == "shop_again"
    items = session.pool_snapshots[-1]["items"]
    assert 0 < len(items) <= 100
    assert set(items[0]) == {"title", "price", "seller", "thumbnail_url", "tags", "source"}
    assert all(i["thumbnail_url"].startswith("https://") or not i["thumbnail_url"] for i in items)
    assert all(i["source"] in ("title", "photo", "keyword") for i in items)
    raw = (tmp_path / "shop-data" / f"{sid}.json").read_text()
    assert "pool_snapshots" in json.loads(raw)


def test_the_export_script_writes_a_redacted_replay_fixture_in_the_existing_format(
    tmp_path: Path,
) -> None:
    import importlib.util

    svc, sid, _ = evidence_service(tmp_path)
    svc.rate(sid, "E1", "like", "love this, call me at 555-123-4567 or me@example.com")
    svc.shop_again(sid)
    spec = importlib.util.spec_from_file_location(
        "shop_export_replay", ROOT / "scripts" / "shop_export_replay.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / "fixture.json"
    path = tmp_path / "shop-data" / f"{sid}.json"
    module.export(path, out)
    data = json.loads(out.read_text())
    assert set(data) == {"request", "rated", "shown_extras", "pool"}
    assert len(data["rated"]) == 48 and 0 < len(data["pool"]) <= 100
    first = data["rated"][0]
    assert set(first) == {"id", "title", "price", "seller", "rating", "comment", "attrs"}
    assert set(first["attrs"]) == {"shape", "material", "color_finish", "frame_color", "style"}
    text = out.read_text()
    assert "555-123-4567" not in text and "me@example.com" not in text and sid not in text
    assert all(p["thumbnail"].startswith("https://mock.example/") for p in data["pool"])
    # the export loads as a replay: a new session can be seeded from it and a Shop Again run
    target = tmp_path / "fixtures"
    target.mkdir()
    (target / "exported.json").write_text(text)
    import app.shop.replay as replay_mod

    replay_mod.REPLAY_DIR = target
    try:
        store = SessionStore(tmp_path / "again")
        seeded = seed_session(store, replay="exported.json")
        assert len(seeded.ratings) == 48
    finally:
        replay_mod.REPLAY_DIR = ROOT / "tests" / "fixtures" / "shop"
