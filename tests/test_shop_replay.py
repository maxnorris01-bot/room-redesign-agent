"""Shop Mode stage 2d: the owner's real session replayed offline, the preference call's failure paths,
the coverage and price rules, the guards and the "How I decided" trace.

Offline like the other Shop Mode tests (same loopback-only socket guard).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.shop.decide import (
    FALLBACK_NOTE,
    KNOWN_COVERAGE_MIN,
    classify_dimension,
    decide,
    typed_dimensions,
)
from app.shop.models import ShopQuery, StoredProfile
from app.shop.prefs import estimate_item_tokens, prefs_max_tokens, rated_map, split_groups
from app.shop.providers import build_shop_providers
from app.shop.replay import load_replay, seed_session
from app.shop.search import RESULT_COUNT, build_results
from app.shop.service import ShopService
from app.shop.store import RatingRecord, SessionStore
from app.shopping import SearchCandidate
from test_shop import (  # noqa: F401  (the autouse guard must be imported to apply here)
    FakeLookup,
    FakeSearch,
    RouterClient,
    _loopback_only,
    cand,
    config,
    make_service,
    query,
    use_client,
)
from test_shop_decide import (
    make_session,
    model_tags,
    shape_world,
    tagged,
)

ROOT = Path(__file__).resolve().parents[1]


def replay_service(tmp_path: Path, **kw: Any) -> tuple[ShopService, str]:
    cfg = config()
    search, lookup = build_shop_providers(cfg)
    store = SessionStore(tmp_path / "shop-data")
    svc = ShopService(cfg, store, search, lookup)
    session = seed_session(store, **kw)
    return svc, session.session_id


def dims(view: dict[str, Any]) -> dict[str, Any]:
    return view["profile"]["trace"]["dimensions"]


def chips(view: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(e["attr_kind"], e["text"]): e for e in view["profile"]["inferred"]}


# ---- the owner's session -------------------------------------------------------------------------------


def test_the_replay_fixture_is_the_owners_session() -> None:
    data = load_replay()
    rated = data["rated"]
    assert [r["rating"] for r in rated].count("love") == 2 and [r["rating"] for r in rated].count(
        "like"
    ) == 8
    assert sum(1 for r in rated if r["seller"] == "West Elm") == 8
    assert min(r["price"] for r in rated) == 143.99 and max(r["price"] for r in rated) == 799.0
    assert [r["price"] for r in rated if r["rating"] == "love"] == [699.0, 699.0]
    assert len(data["shown_extras"]) == 30


def test_the_replay_decides_shape_open_price_mixed_and_no_industrial_or_round(
    tmp_path: Path,
) -> None:
    svc, sid = replay_service(tmp_path)
    view = svc.shop_again(sid)
    found = chips(view)
    shape = dims(view)["shape"]
    assert shape["decision"] == "open"
    mass = shape["like_mass"]
    assert mass["round"] == 6 and mass["rectangular"] == 8 and mass["(unknown)"] == 0
    assert round(mass["round"] / shape["total_mass"], 2) == 0.43  # round about 43% of like mass
    assert round(mass["rectangular"] / shape["total_mass"], 2) == 0.57  # rectangular about 57%
    assert ("open", "shape: open") in found and found[("open", "shape: open")]["values"] == [
        "rectangular",
        "round",
    ]
    assert not [
        e for e in view["profile"]["inferred"] if e["attr_kind"] == "shape"
    ]  # no "round" like
    assert not [
        e for e in view["profile"]["inferred"] if e["attr_kind"] == "style"
    ]  # no "industrial"
    assert "industrial" not in json.dumps([e["text"] for e in view["profile"]["inferred"]])
    price = found[("price_mixed", "price: mixed")]
    assert price["status"] == "mixed" and price["detail"] == "loved $699, liked $144 to $799"
    trace_price = view["profile"]["trace"]["price"]
    assert trace_price["decision"] == "mixed" and trace_price["liked_in_band"] == 0.75
    assert (
        "would hold 86% of the like mass and 75% of the liked items, under 80%"
        in trace_price["reason"]
    )
    assert view["search"]["query"]["material_terms"] == ["wood", "metal"]  # request chips untouched
    assert not any(
        "storage" in e["text"] for e in view["profile"]["inferred"]
    )  # storage not learned
    assert "Shape is open: showing both rectangular and round." in view["profile_summary"]
    assert "Price is mixed." in view["profile_summary"]


def test_the_replay_seller_affinity_for_west_elm_and_its_numbers(tmp_path: Path) -> None:
    svc, sid = replay_service(tmp_path)  # West Elm is 20 of the 40 shown results
    view = svc.shop_again(sid)
    seller = chips(view)[("seller", "seller: West Elm")]
    assert seller["share"] == pytest.approx(0.8571, abs=1e-4)  # 12 of 14 like mass
    assert seller["exposure"] == 0.5 and seller["lift"] == pytest.approx(1.714, abs=1e-3)
    top = view["profile"]["trace"]["seller"]["candidates"][0]
    assert top["seller"] == "West Elm" and top["items"] == 8 and top["verdict"] == "chip"
    assert top["like_mass"] == 12 and top["dislikes"] == 0


def test_the_replay_seller_chip_is_missing_only_when_the_lift_legitimately_fails(
    tmp_path: Path,
) -> None:
    svc, sid = replay_service(tmp_path, west_elm_extras=30)  # West Elm becomes 38 of 40 shown
    view = svc.shop_again(sid)
    assert ("seller", "seller: West Elm") not in chips(view)
    trace = view["profile"]["trace"]["seller"]
    assert trace["decision"] == "none"
    west = trace["candidates"][0]
    assert (
        west["seller"] == "West Elm"
        and west["exposure"] == 0.95
        and west["lift"] == pytest.approx(0.902, abs=1e-3)
    )
    assert (
        "lift 0.902 is under 1.3" in trace["reason"]
        and "86% against 95% of what was shown" in trace["reason"]
    )


def test_the_replay_in_title_only_fallback_mode_decides_no_attribute(tmp_path: Path) -> None:
    from app.shop.models import ImageReport
    from app.shop.prefs import merge_profile_v3

    store = SessionStore(tmp_path / "shop-data")
    session = seed_session(store)
    base = session.searches[0].query
    prof = merge_profile_v3(None, None, session, base, ImageReport(), set())
    kinds = {(e.attr_kind, e.text) for e in prof.inferred}
    assert ("seller", "seller: West Elm") in kinds and ("price_mixed", "price: mixed") in kinds
    for dim in ("shape", "material", "color_finish", "frame_color", "frame_or_legs", "style"):
        assert not [e for e in prof.inferred if e.dimension == dim], dim  # no attribute chip at all
    assert not [e for e in prof.inferred if e.attr_kind in ("open", "quality_cue")]
    assert prof.tagging["fallback"] is True and prof.trace["mode"] == "fallback"
    assert all(d["decision"] == "none" for d in prof.trace["dimensions"].values())
    assert all("fallback" in d["reason"] for d in prof.trace["dimensions"].values())


def test_the_old_bias_is_gone_four_keyword_titles_and_four_silent_ones() -> None:
    # stage 2c read titles by keyword: 4 titles said round or drum, 4 rectangular titles said nothing
    items = [tagged(f"a{i}", "love" if i < 2 else "like", f"Drum Table {i}") for i in range(4)]
    items += [tagged(f"b{i}", "like", f"Agnelli Table {i}") for i in range(4)]  # silent: no shape
    res = classify_dimension(
        "shape", items, {"shape": {"round": 0.4, "rectangular": 0.6}}, {"shape": 0.9}
    )
    assert res.status == "none" and res.trace["like_mass"] == {"round": 8, "(unknown)": 4}
    assert res.trace["known_coverage"] == pytest.approx(0.6667, abs=1e-3)
    assert "known coverage 67% is under 70%" in res.trace["reason"]
    assert res.shares == {"round": 0.6667}  # a share of ALL mass, not of the known items only


def test_title_only_fallback_cannot_converge_even_with_obvious_titles() -> None:
    sess = make_session(
        shape_world(4, 4), {"r0": ("love", ""), "r1": ("love", ""), "x0": ("like", "")}
    )
    out = decide(
        rated_map(sess), model_tags(sess), list(sess.results.values()), [], [], fallback=True
    )
    assert [
        e.attr_kind
        for e in out.entries
        if e.attr_kind not in ("seller", "price_band", "price_mixed")
    ] == []
    assert out.trace["mode"] == "fallback"


# ---- coverage: unknowns count against every value --------------------------------------------------------------


def test_a_dimension_converges_only_with_known_coverage_of_at_least_seventy_percent() -> None:
    low = {"shape": {"round": 0.3, "rectangular": 0.7}}
    cov = {"shape": 1.0}
    known = [tagged(f"a{i}", "like", f"Round Table {i}") for i in range(7)]
    silent = [tagged(f"s{i}", "like", f"Plain Table {i}") for i in range(3)]
    ok = classify_dimension("shape", known + silent, low, cov)  # 7 of 10 known, all round: 70%
    assert ok.status == "converged" and ok.share == 0.7 and ok.trace["known_coverage"] == 0.7
    short = classify_dimension(
        "shape", known[:6] + silent + [tagged("s9", "like", "Plain 9")], low, cov
    )
    assert short.status == "none" and short.trace["known_coverage"] == 0.6
    assert KNOWN_COVERAGE_MIN == 0.70


def test_the_lift_test_needs_keywords_in_seventy_percent_of_what_was_shown() -> None:
    items = [tagged(f"a{i}", "love", f"Round Table {i}") for i in range(3)]
    thin = classify_dimension(
        "shape", items, {"shape": {"round": 0.2, "rectangular": 0.8}}, {"shape": 0.69}
    )
    assert thin.status == "none" and "the lift test cannot run" in thin.trace["reason"]
    enough = classify_dimension(
        "shape", items, {"shape": {"round": 0.2, "rectangular": 0.8}}, {"shape": 0.70}
    )
    assert enough.status == "converged"
    assert thin.trace["exposure_coverage"] == 0.69 and enough.trace["lift"] == 5.0


def test_open_uses_the_same_all_mass_shares() -> None:
    items = [
        tagged("a", "love", "Round A"),
        tagged("b", "love", "Rectangular B"),
        tagged("c", "love", "Plain C"),
    ]
    res = classify_dimension(
        "shape", items, {"shape": {"round": 0.5, "rectangular": 0.5}}, {"shape": 1.0}
    )
    assert res.status == "open" and res.shares == {"round": 0.3333, "rectangular": 0.3333}
    thin = classify_dimension(
        "shape", [tagged("a", "love", "Round A"), tagged("b", "like", "Rectangular B")] + [tagged(f"p{i}", "love", f"Plain {i}") for i in range(3)],
        {"shape": {"round": 0.5, "rectangular": 0.5}}, {"shape": 1.0},
    )  # fmt: skip
    assert thin.status == "none"  # unknowns pull round to 3/13 and rectangular to 1/13


def test_typed_dimensions_are_the_ones_the_request_names() -> None:
    base = ShopQuery(
        request="coffee tables with a mix of wood and metal with storage",
        search_text="x",
        category="c",
        material_terms=["wood", "metal"],
    )
    assert typed_dimensions(base) == {"material"}
    round_one = ShopQuery(request="round walnut coffee table", search_text="x", category="c")
    assert {"shape", "color_finish"} <= typed_dimensions(round_one)
    assert (
        typed_dimensions(ShopQuery(request="coffee tables", search_text="x", category="c")) == set()
    )


# ---- the preference call: tokens, retry, fallback -----------------------------------------------------------------------


def test_the_token_budget_comes_from_the_schema_with_twice_the_headroom() -> None:
    per_item = estimate_item_tokens()
    assert 60 <= per_item <= 140  # a worst-case compact item, from the schema
    for n in (4, 10, 16, 40):
        needed = 450 + n * per_item
        got = prefs_max_tokens(n)
        assert got >= min(12000, 2 * needed) or got == 12000
        assert got >= 3000  # never the 2000 that stage 2c ran out of
    assert prefs_max_tokens(16) >= 2 * (450 + 16 * per_item)
    assert prefs_max_tokens(500) == 12000


def test_the_first_call_is_given_the_computed_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = RouterClient()
    use_client(monkeypatch, client)
    search = FakeSearch(
        [
            [
                cand(f"a{i}", 40.0 + i, title=f"Round Walnut Table {i}", seller=f"S{i}")
                for i in range(30)
            ]
        ]
    )
    svc = make_service(tmp_path, search, FakeLookup())
    first = svc.search(None, "tables")
    sid = first["session_id"]
    for r in first["search"]["results"][:3]:
        svc.rate(sid, r["id"], "love")
    svc.rate(sid, first["search"]["results"][5]["id"], "dislike")
    search.sequence = [
        [cand(f"n{i}", 50.0, title=f"Oval Oak Table N{i}", seller=f"T{i}") for i in range(30)]
    ]
    svc.shop_again(sid)
    call = next(c for c in client.calls if "PreferenceTags" in json.dumps(c["output_config"]))
    assert call["max_tokens"] == prefs_max_tokens(4)
    assert call["max_tokens"] >= 2 * (450 + 4 * estimate_item_tokens())


def comment_on(svc: ShopService, sid: str, rid: str, text: str) -> None:
    svc.rate(sid, rid, comment=text)


def test_a_cut_off_call_is_retried_once_in_two_parts_and_succeeds(tmp_path: Path) -> None:
    svc, sid = replay_service(tmp_path)
    svc.rate(sid, "X20", "dislike")  # a disliked item makes the split loved+liked / disliked
    comment_on(svc, sid, "R1", "MOCK_TRUNCATE_FIRST")  # the unsplit call is cut off
    view = svc.shop_again(sid)
    tagging = view["profile"]["tagging"]
    assert tagging["retried"] is True and tagging["fallback"] is False
    assert tagging["model_calls"] == 3  # the full call, then the two parts
    assert tagging["items_tagged"] == tagging["items_total"] == 11
    assert tagging["failures"] and "max_tokens" in tagging["failures"][0]
    assert view["profile"]["trace"]["mode"] == "model"  # the attributes were decided from the parts
    assert chips(view)[("open", "shape: open")]["values"] == ["rectangular", "round"]
    assert view["search"]["tagging"]["retried"] is True and not view["search"]["warnings"][:1] == [
        FALLBACK_NOTE
    ]


def test_without_a_disliked_item_the_retry_splits_loved_from_liked(tmp_path: Path) -> None:
    svc, sid = replay_service(tmp_path)
    session = svc.store.load(sid)
    assert [sorted(g) for g in split_groups(session)] == [
        ["R1", "R2"],
        sorted(f"R{i}" for i in range(3, 11)),
    ]
    session.ratings["X1"] = RatingRecord(rating="dislike", comment="", rated_at="9")
    groups = split_groups(session)
    assert len(groups) == 2 and "X1" in groups[1] and groups[0] == {f"R{i}" for i in range(1, 11)}


def test_when_every_part_fails_the_profile_falls_back_to_seller_and_price(tmp_path: Path) -> None:
    svc, sid = replay_service(tmp_path)
    svc.rate(sid, "X20", "dislike")
    comment_on(svc, sid, "R1", "MOCK_TRUNCATE_ALL")  # the loved/liked part fails again
    comment_on(svc, sid, "X20", "MOCK_TRUNCATE_ALL")  # and so does the disliked part
    view = svc.shop_again(sid)
    tagging = view["profile"]["tagging"]
    assert (
        tagging["retried"] is True and tagging["fallback"] is True and tagging["items_tagged"] == 0
    )
    assert tagging["model_calls"] == 3  # one retry (two parts), never more
    assert FALLBACK_NOTE in view["search"]["warnings"]
    assert FALLBACK_NOTE == "Could not read item details; only seller and price were learned."
    kinds = {e["attr_kind"] for e in view["profile"]["inferred"]}
    assert kinds <= {"seller", "price_mixed", "price_band"} and "seller" in kinds
    assert view["profile"]["trace"]["mode"] == "fallback"
    assert view["search"]["kind"] == "shop_again" and view["search"]["results"]  # still searched


def test_one_part_failing_leaves_its_items_untagged_but_not_the_fallback(tmp_path: Path) -> None:
    svc, sid = replay_service(tmp_path)
    svc.rate(sid, "X20", "dislike")
    comment_on(svc, sid, "X20", "MOCK_TRUNCATE_ALL")  # only the disliked part is cut off
    view = svc.shop_again(sid)
    tagging = view["profile"]["tagging"]
    assert (
        tagging["fallback"] is False
        and tagging["items_tagged"] == 10
        and tagging["items_total"] == 11
    )
    assert tagging["model_calls"] == 3 and tagging["retried"] is True
    assert "Read details for 10 of 11 rated items." in view["search"]["warnings"]
    assert dims(view)["shape"]["decision"] == "open"  # the positive items were read


def test_invalid_json_gets_the_same_one_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = RouterClient(prefs="{not json")
    use_client(monkeypatch, client)
    search = FakeSearch(
        [
            [
                cand(f"a{i}", 40.0 + i, title=f"Round Walnut Table {i}", seller=f"S{i}")
                for i in range(30)
            ]
        ]
    )
    svc = make_service(tmp_path, search, FakeLookup())
    first = svc.search(None, "tables")
    sid = first["session_id"]
    for r in first["search"]["results"][:2]:
        svc.rate(sid, r["id"], "love")
    svc.rate(sid, first["search"]["results"][5]["id"], "dislike")
    search.sequence = [
        [cand(f"n{i}", 50.0, title=f"Oval Oak Table N{i}", seller=f"T{i}") for i in range(30)]
    ]
    view = svc.shop_again(sid)
    prefs_calls = [c for c in client.calls if "PreferenceTags" in json.dumps(c["output_config"])]
    assert len(prefs_calls) == 3  # the full call and one retry in two parts, no more
    assert (
        view["profile"]["tagging"]["fallback"] is True
        and FALLBACK_NOTE in view["search"]["warnings"]
    )


def test_the_footer_text_comes_from_the_tagging_record(tmp_path: Path) -> None:
    from app.shop.server import STATIC_DIR

    svc, sid = replay_service(tmp_path)
    tagging = svc.shop_again(sid)["search"]["tagging"]
    assert (
        tagging["items_tagged"] == 10
        and tagging["images_used"] == 8
        and tagging["images_offered"] == 8
    )
    code = (STATIC_DIR / "app.js").read_text()
    assert "tagged ' + tg.items_tagged + ' of ' + tg.items_total + ' items'" in code
    assert "'retried'" in code and "'fallback'" in code and "'no photos'" in code


# ---- price: a band must hold the liked mass ----------------------------------------------------------------------------------


def test_a_price_band_that_leaves_liked_items_out_is_mixed(tmp_path: Path) -> None:
    svc, sid = replay_service(tmp_path)
    trace = svc.shop_again(sid)["profile"]["trace"]["price"]
    assert trace["band"] == "upper $450 to $800"  # what stage 2c would have called the band
    assert trace["mass_in_band"] == pytest.approx(0.8571, abs=1e-4)  # 12 of 14 like mass inside
    assert trace["liked_in_band"] == 0.75  # but only 6 of the 8 liked items
    assert trace["decision"] == "mixed"
    assert svc.store.load(sid).profile.trace["price"]["decision"] == "mixed"  # type: ignore[union-attr]


# ---- the diversity guard -----------------------------------------------------------------------------------------------------------------


def candidate(i: int, title: str, seller: str = "S") -> SearchCandidate:
    return SearchCandidate(
        f"c{i}", title, seller, 100.0, "USD", None, None, f"https://www.google.com/p/{i}"
    )


def test_open_and_shape_are_capped_at_sixty_percent_and_converged_at_eighty() -> None:
    pool = [candidate(i, f"Round Table {i}", f"S{i}") for i in range(40)]
    pool += [candidate(100 + i, f"Oval Table {i}", f"O{i}") for i in range(10)]
    index = {c.product_id: n for n, c in enumerate(pool, 1)}
    ordered = [(c, "") for c in pool]
    sixty, _, skipped60 = build_results(
        ordered, query(request="t"), index, guard_caps={"shape": 0.6}
    )
    eighty, _, skipped80 = build_results(
        ordered, query(request="t"), index, guard_caps={"shape": 0.8}
    )
    rounds = lambda rs: sum(1 for r in rs if r.title.startswith("Round"))  # noqa: E731
    assert len(sixty) == len(eighty) == RESULT_COUNT
    assert rounds(sixty) == 14 == int(0.6 * RESULT_COUNT) and skipped60 == 26
    assert rounds(eighty) == 19 == int(0.8 * RESULT_COUNT) and skipped80 == 21


def test_only_shape_and_open_dimensions_are_guarded_not_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_client(monkeypatch, RouterClient())
    first_items = [
        cand(f"a{i}", 100.0 + i, title=f"Plain Table {i}", seller=f"S{i}") for i in range(30)
    ]
    search = FakeSearch([first_items])
    svc = make_service(tmp_path, search, FakeLookup())
    first = svc.search(None, "tables")
    sid = first["session_id"]
    svc.rate(sid, first["search"]["results"][0]["id"], "like")
    # a pool that is all wood: stage 2c capped and warned; now material is not guarded
    search.sequence = [
        [cand(f"n{i}", 100.0 + i, title=f"Walnut Table N{i}", seller=f"T{i}") for i in range(40)]
    ]
    found = svc.shop_again(sid)["search"]
    assert len(found["results"]) == 24
    assert not any("material" in w for w in found["warnings"])
    assert "material" not in found["diversity"]


def test_a_dimension_the_typed_request_names_is_never_warned_about(tmp_path: Path) -> None:
    from app.shop.again import again_context
    from app.shop.models import ProfileEntry

    svc, sid = replay_service(tmp_path)
    session = svc.store.load(sid)
    open_material = ProfileEntry(
        text="material: open", attr_kind="open", polarity="like", dimension="material",
        status="open", values=["wood", "metal"], weight=6,
    )  # fmt: skip
    open_shape = open_material.model_copy(
        update={"text": "shape: open", "dimension": "shape", "values": ["round", "rectangular"]}
    )
    ctx = again_context(
        session, StoredProfile(inferred=[open_material, open_shape]), config(), ["x"]
    )
    assert "material" in ctx.typed_dims  # the request says "wood and metal"
    assert "material" not in ctx.guard_caps  # so the guard leaves it alone
    assert ctx.guard_caps == {"shape": 0.6}  # shape (open) at 60%


def test_a_converged_dimension_is_guarded_at_eighty_percent(tmp_path: Path) -> None:
    from app.shop.again import again_context
    from app.shop.models import ProfileEntry

    svc, sid = replay_service(tmp_path)
    session = svc.store.load(sid)
    converged = ProfileEntry(
        text="round", attr_kind="shape", polarity="like", dimension="shape", status="converged",
        values=["round"], weight=6,
    )  # fmt: skip
    finish = converged.model_copy(
        update={
            "text": "warm wood",
            "attr_kind": "color_finish",
            "dimension": "color_finish",
            "values": ["warm wood"],
        }
    )
    ctx = again_context(session, StoredProfile(inferred=[converged, finish]), config(), ["x"])
    assert ctx.guard_caps == {"shape": 0.8, "color_finish": 0.8}


def test_warnings_state_the_real_shares_and_the_limit() -> None:
    from app.shop.search import AgainContext, run_query

    class OneRound:
        name = "serper"

        def search(
            self, text: str, gl: str = "us", hl: str = "en", page: int = 1
        ) -> list[SearchCandidate]:
            return (
                [candidate(i + 100 * page, f"Round Table {i}", f"S{i}") for i in range(30)]
                if page == 1
                else []
            )

    from app.llm import Budget

    cfg = config()
    import app.llm as llm_mod
    from app.mock_llm import MockAnthropicClient

    llm_mod.get_client.cache_clear()
    warnings: list[str] = []
    ctx = AgainContext(guard_caps={"shape": 0.6}, max_calls=1)
    out = run_query(
        query(request="t", search_text="t"), OneRound(), cfg, Budget(cfg), warnings, 0.0, ctx
    )
    assert len(out.results) == 14
    assert (
        "Could not keep shape mixed: the pool is mostly round (100% of these results, limit 60%)."
        in out.warnings
    )
    ctx2 = AgainContext(guard_caps={"shape": 0.8}, max_calls=1)
    out2 = run_query(
        query(request="t", search_text="t"), OneRound(), cfg, Budget(cfg), [], 0.0, ctx2
    )
    assert len(out2.results) == 19
    assert (
        "Could not keep shape under 80%: the pool is mostly round (100% of these results, limit 80%)."
        in out2.warnings
    )
    assert MockAnthropicClient is not None


# ---- the "How I decided" trace -----------------------------------------------------------------------------------------------------------


def test_the_trace_has_the_numbers_for_every_dimension_seller_and_price(tmp_path: Path) -> None:
    svc, sid = replay_service(tmp_path)
    view = svc.shop_again(sid)
    trace = view["profile"]["trace"]
    assert (
        trace["mode"] == "model"
        and trace["rated"] == {"love": 2, "like": 8, "dislike": 0}
        and trace["shown"] == 40
    )
    assert set(trace["dimensions"]) == {
        "shape",
        "material",
        "color_finish",
        "frame_or_legs",
        "frame_color",
        "style",
        "size_class",
    }
    shape = trace["dimensions"]["shape"]
    for key in (
        "like_mass",
        "total_mass",
        "known_coverage",
        "exposure_coverage",
        "exposure",
        "decision",
        "reason",
    ):
        assert key in shape
    assert (
        shape["total_mass"] == 14
        and shape["known_coverage"] == 1.0
        and shape["exposure_coverage"] >= 0.7
    )
    assert set(shape["exposure"]) == {"round", "rectangular", "oval", "square"}
    assert (
        "each hold at least 30%" in shape["reason"]
        and "top value rectangular holds 57%" in shape["reason"]
    )
    frame = trace["dimensions"]["frame_color"]
    assert frame["decision"] == "none" and "known coverage 36% is under 70%" in frame["reason"]
    assert "only 8% of shown titles have a keyword" in frame["reason"]
    style = trace["dimensions"]["style"]
    assert style["like_mass"]["industrial"] == 2 and "known coverage 14%" in style["reason"]
    finish = trace["dimensions"]["color_finish"]
    assert "top value warm wood holds 64% of all like mass, under 70%" in finish["reason"]
    material = trace["dimensions"]["material"]
    assert (
        material["decision"] == "none" and "lift 1.286 for wood is under 1.3" in material["reason"]
    )
    assert trace["seller"]["candidates"][0]["lift"] == pytest.approx(1.714, abs=1e-3)
    assert trace["price"]["decision"] == "mixed"
    assert view["search"]["trace"] == trace  # the response carries it
    session_file = json.loads((tmp_path / "shop-data" / f"{sid}.json").read_text())
    assert (
        session_file["profile"]["trace"] == trace and session_file["searches"][-1]["trace"] == trace
    )
    assert session_file["profile"]["tagging"]["items_tagged"] == 10


def test_a_converged_value_the_request_already_says_is_explained_in_the_trace(
    tmp_path: Path,
) -> None:
    svc, sid = replay_service(tmp_path, west_elm_extras=12)
    trace = svc.shop_again(sid)["profile"]["trace"]
    assert trace["dimensions"]["material"]["decision"] in ("none", "converged")
    # wood is typed ("wood and metal"): even when it converges, no learned chip repeats it
    session = svc.store.load(sid)
    assert not [e for e in session.profile.inferred if e.dimension == "material"]  # type: ignore[union-attr]


def test_the_panel_is_text_only_closed_by_default_and_remembered() -> None:
    from app.shop.server import STATIC_DIR

    page = (STATIC_DIR / "index.html").read_text()
    assert 'id="why"' in page and 'id="why-toggle"' in page
    assert 'id="why" class="why" aria-label="How the preferences were decided" hidden' in page
    assert 'aria-expanded="false"' in page.split('id="why-toggle"')[1][:120]
    code = (STATIC_DIR / "app.js").read_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in code
    block = code[code.index("function whyTable") : code.index("function removePref")]
    assert "createTextNode" not in block and "String(c)" in block and "[c]" in block  # text via h()
    assert (
        "shop.whyOpen" in code
        and "storeGet(KEY_WHY_OPEN)" in code
        and "storeSet(KEY_WHY_OPEN" in code
    )
    assert "Could not read item details; only seller and price were learned." in code
    assert FALLBACK_NOTE in code
    for number in (
        "like mass",
        "known coverage",
        "of shown titles have a keyword",
        "lift",
        "mean position",
    ):
        assert number in block


def test_trace_text_with_markup_stays_data(tmp_path: Path) -> None:
    from app.shop.models import ImageReport
    from app.shop.prefs import merge_profile_v3

    svc, sid = replay_service(tmp_path)
    session = svc.store.load(sid)
    session.results["R1"] = session.results["R1"].model_copy(
        update={"title": "<img src=x onerror=alert(1)> Agnelli", "seller": "<b>West Elm</b>"}
    )
    prof = merge_profile_v3(None, None, session, session.searches[0].query, ImageReport(), set())
    blob = json.dumps(prof.trace)
    assert "<b>West Elm</b>" in blob or "West Elm" in blob  # present as plain strings in JSON
    assert isinstance(prof.trace["seller"]["candidates"][0]["seller"], str)


# ---- unchanged behaviour -----------------------------------------------------------------------------------------------------------------


def test_the_replay_keeps_pin_request_chips_and_cost_behaviour(tmp_path: Path) -> None:
    svc, sid = replay_service(tmp_path)
    view = svc.shop_again(sid)
    typed = svc.store.load(sid).searches[0].query.model_dump(mode="json")
    assert view["search"]["query"] == typed  # request chips unchanged
    found = view["search"]
    assert found["serper_credits"] == found["serper_calls"] * 2
    assert found["provider_usd"] == pytest.approx(found["serper_calls"] * 0.002)
    assert found["anthropic_usd"] > 0 and found["total_usd"] == pytest.approx(
        found["anthropic_usd"] + found["provider_usd"]
    )
    pinned = svc.pin_preference(sid, "seller: West Elm")
    assert pinned["pinned"] is True
    again = svc.shop_again(sid)
    assert again["profile"]["pinned_sellers"] == ["westelm"]


def test_a_retried_call_costs_more_than_a_clean_one_and_stays_under_a_cent(tmp_path: Path) -> None:
    clean_svc, clean_sid = replay_service(tmp_path / "a")
    clean = clean_svc.shop_again(clean_sid)["search"]["anthropic_usd"]
    retry_svc, retry_sid = replay_service(tmp_path / "b")
    retry_svc.rate(retry_sid, "X20", "dislike")
    comment_on(retry_svc, retry_sid, "R1", "MOCK_TRUNCATE_FIRST")
    retried = retry_svc.shop_again(retry_sid)["search"]["anthropic_usd"]
    assert (
        retried > clean > 0
    )  # the failed full call is paid for, then two parts, then the rank call
    assert retried < 0.01


def test_a_session_view_rejects_nothing_new_and_keeps_the_csp(tmp_path: Path) -> None:
    from app.shop.server import CSP, ShopApp

    svc, sid = replay_service(tmp_path)
    svc.shop_again(sid)
    app = ShopApp(svc, port=8765)
    hdrs = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}
    ok = app.handle("GET", f"/api/session?id={sid}", hdrs)
    assert ok.status == 200 and ok.all_headers()["Content-Security-Policy"] == CSP
    body = json.loads(ok.body)
    assert (
        body["profile"]["trace"]["mode"] == "model"
        and body["search"]["tagging"]["items_tagged"] == 10
    )
    assert app.handle("GET", f"/api/session?id={sid}", {"Host": "evil.example"}).status == 403
