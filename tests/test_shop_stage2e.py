"""Shop Mode stage 2e: candidate tagging by the model, fixed vocabularies, per-dimension thresholds,
the seller 40% rule, supply for an open dimension's minority value, stratified selection and the
batch composition in the trace. The owner's second real session is replayed offline.

Offline like the other Shop Mode tests (same loopback-only socket guard).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.llm import Budget
from app.shop.candidates import TagBook, Taggable, parse_rows, tag_results
from app.shop.decide import Exposure, build_exposure, decide, seller_decision
from app.shop.models import ShopResult
from app.shop.prefs import rated_map
from app.shop.providers import build_shop_providers
from app.shop.replay import REPLAY_FILE_2, load_replay, seed_session
from app.shop.search import (
    RESULT_COUNT,
    AgainContext,
    build_results,
    run_query,
    strata_quotas,
)
from app.shop.service import ShopService
from app.shop.store import Session, SessionStore
from app.shop.textutil import Rated
from app.shop.vocab import DIMENSIONS, VOCAB, normalize, vocab_text
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
from test_shop_decide import make_session, tagged

ROOT = Path(__file__).resolve().parents[1]


def tag_calls(client: RouterClient) -> list[dict[str, Any]]:
    return [
        c
        for c in client.calls
        if c["output_config"]["format"]["schema"]["title"] == "CandidateTags"
    ]


def rows_in(call: dict[str, Any]) -> list[str]:
    content = call["messages"][0]["content"]
    text = (
        content
        if isinstance(content, str)
        else " ".join(b["text"] for b in content if b.get("type") == "text")
    )
    return re.findall(r"^\d+ \| .*$", text, flags=re.MULTILINE)


def replay2(tmp_path: Path, **kw: Any) -> tuple[ShopService, str]:
    cfg = config()
    search, lookup = build_shop_providers(cfg)
    store = SessionStore(tmp_path / "shop-data")
    session = seed_session(store, replay=REPLAY_FILE_2, **kw)
    return ShopService(cfg, store, search, lookup), session.session_id


# ---- the fixed vocabularies and the normalization map ------------------------------------------------------


def test_the_vocabularies_are_defined_once_and_passed_into_both_prompts() -> None:
    assert VOCAB["shape"] == ("round", "oval", "rectangular", "square", "other")
    assert VOCAB["material"] == ("wood", "metal", "glass", "stone", "upholstered", "other")
    assert VOCAB["color_finish"] == (
        "light wood", "warm wood", "dark wood", "painted", "metallic", "glass", "other",
    )  # fmt: skip
    assert VOCAB["frame_color"] == (
        "black", "brass or gold", "silver or chrome", "natural wood", "white", "other",
    )  # fmt: skip
    assert VOCAB["style"] == (
        "modern", "mid-century", "industrial", "farmhouse", "traditional", "boho", "glam", "other",
    )  # fmt: skip
    assert DIMENSIONS == ("shape", "material", "color_finish", "frame_color", "style")
    from app.shop.candidates import candidate_user_text
    from app.shop.prefs import prefs_user_text

    text = vocab_text()
    assert "shape: round, oval, rectangular, square, other" in text
    batch = [Taggable("a", "Round Walnut Table", 100.0, "S")]
    assert text in candidate_user_text(batch)
    sess = make_session([("a", "Round Table", 100.0, "S")], {"a": ("love", "")})
    assert text in prefs_user_text(sess, query(request="tables"))


def test_synonyms_fold_into_one_value_and_unknowns_become_other_or_null() -> None:
    frame = {normalize("frame_color", w) for w in ("metal base", "metal frame", "metal legs")}
    assert frame == {"other"}  # never split in two: a frame with no colour named is one value
    for words in ("warm wood", "brown wood", "rustic brown", "walnut", "natural oak"):
        assert normalize("color_finish", words) == "warm wood"
    assert normalize("color_finish", "Dark Walnut") == "warm wood"  # walnut is a warm wood here
    assert normalize("color_finish", "espresso") == "dark wood"
    assert normalize("frame_color", "black or brass metal") == "black"  # the earliest colour named
    assert normalize("frame_color", "brass") == "brass or gold"
    assert normalize("frame_color", "chrome legs") == "silver or chrome"
    assert normalize("material", "solid mango wood") == "wood"
    assert (
        normalize("material", "marble") == "stone"
        and normalize("material", "velvet") == "upholstered"
    )
    assert normalize("shape", "circular") == "round" and normalize("shape", "racetrack") == "oval"
    assert normalize("style", "mid century modern") == "mid-century"
    assert normalize("style", "unclassifiable") == "other" and normalize("shape", "") is None
    assert normalize("shape", None) is None and normalize("size_class", "large") is None


def test_candidate_rows_are_parsed_and_folded_and_bad_rows_are_dropped() -> None:
    rows = [
        "1|round|wood|warm wood|black|modern",
        "2|-|-|-|-|-",
        "3|kidney|walnut|brown wood|metal base|quirky",
        "oops",
        "9|round|wood|warm wood|black|modern",  # outside the batch
        "1|square|-|-|-|-",  # a repeated number
        "4|round|wood",  # too few values
    ]
    got = parse_rows(rows, 4)
    assert sorted(got) == [1, 2, 3]
    assert got[1] == {
        "shape": "round", "material": "wood", "color_finish": "warm wood",
        "frame_color": "black", "style": "modern",
    }  # fmt: skip
    assert got[2] == dict.fromkeys(DIMENSIONS)
    assert got[3] == {
        "shape": "other", "material": "wood", "color_finish": "warm wood",
        "frame_color": "other", "style": "other",
    }  # fmt: skip


# ---- candidate tagging: cached, batched, falling back ----------------------------------------------------------


def taggables(n: int, start: int = 0) -> list[Taggable]:
    return [
        Taggable(f"r{i}", f"Round Walnut Table {i}", 100.0 + i, f"S{i}")
        for i in range(start, start + n)
    ]


def test_candidate_tags_are_cached_by_result_id_and_never_repeated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RouterClient()
    use_client(monkeypatch, client)
    budget = Budget(config())
    first = TagBook()
    tag_results(taggables(10), first, budget)
    assert len(tag_calls(client)) == 1 and len(rows_in(tag_calls(client)[0])) == 10
    assert first.requested == 10 and first.tagged == 10 and len(first.new) == 10
    assert first.new["r0"]["shape"] == "round" and first.new["r0"]["color_finish"] == "warm wood"
    # the session cache holds them; a later run asks only for the results it has not seen
    second = TagBook(cache=dict(first.new))
    tag_results(taggables(15), second, budget)
    assert len(tag_calls(client)) == 2
    assert [r.split(" | ")[1] for r in rows_in(tag_calls(client)[1])] == [
        f"Round Walnut Table {i}" for i in range(10, 15)
    ]
    assert second.requested == 5 and second.tagged == 5
    third = TagBook(cache={**first.new, **second.new})
    tag_results(taggables(15), third, budget)
    assert len(tag_calls(client)) == 2 and third.calls == 0  # nothing new: no call at all


def test_a_big_pool_is_split_into_calls_of_a_hundred_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RouterClient()
    use_client(monkeypatch, client)
    book = TagBook()
    tag_results(taggables(230), book, Budget(config()))
    assert [len(rows_in(c)) for c in tag_calls(client)] == [100, 100, 30]
    assert book.calls == 3 and book.tagged == 230 and not book.failed


def test_a_titles_only_call_sends_no_image_and_no_link(monkeypatch: pytest.MonkeyPatch) -> None:
    client = RouterClient()
    use_client(monkeypatch, client)
    tag_results([Taggable("a", "Round Table", 99.0, "West Elm")], TagBook(), Budget(config()))
    content = tag_calls(client)[0]["messages"][0]["content"]
    assert isinstance(content, str)  # text only: no image block
    assert "https://" not in content and "West Elm" in content and "$99" in content


def test_a_failed_tagging_call_is_recorded_and_leaves_the_results_untagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_client(monkeypatch, RouterClient(tags="not json at all"))
    book = TagBook()
    tag_results(taggables(5), book, Budget(config()))
    assert book.failed and book.tagged == 0 and book.new == {} and book.failures
    use_client(monkeypatch, RouterClient(tags={"rows": ["garbage"]}))
    unusable = TagBook()
    tag_results(taggables(5), unusable, Budget(config()))
    assert unusable.failed and "no usable row" in unusable.failures[0]


# ---- the second real session, replayed -------------------------------------------------------------------------


def test_the_second_replay_fixture_is_the_owners_second_session() -> None:
    data = load_replay(REPLAY_FILE_2)
    rated = data["rated"]
    assert [r["rating"] for r in rated].count("love") == 4
    assert [r["rating"] for r in rated].count("like") == 4
    assert [r["rating"] for r in rated].count("dislike") == 1
    assert [r["price"] for r in rated if r["rating"] == "love"] == [199.0, 93.99, 444.99, 449.99]
    assert sum(1 for r in rated if r["seller"] == "Home Depot") == 2
    assert [r["seller"] for r in rated if r["rating"] == "dislike"] == ["Wayfair"]


def test_replay_two_shape_open_no_home_depot_chip_and_a_warm_wood_finish(tmp_path: Path) -> None:
    svc, sid = replay2(tmp_path)
    view = svc.shop_again(sid)
    chips = {(e["attr_kind"], e["text"]): e for e in view["profile"]["inferred"]}
    dims = view["profile"]["trace"]["dimensions"]
    assert dims["shape"]["decision"] == "open"
    assert (
        dims["shape"]["like_mass"]["rectangular"] == 11 and dims["shape"]["like_mass"]["round"] == 5
    )
    assert ("open", "shape: open") in chips  # about 69/31
    # Home Depot is 2 items but 25% of the like mass: under the 40% rule, so no seller chip
    assert not [e for e in view["profile"]["inferred"] if e["attr_kind"] == "seller"]
    home = next(
        c for c in view["profile"]["trace"]["seller"]["candidates"] if c["seller"] == "Home Depot"
    )
    assert home["items"] == 2 and home["like_share"] == 0.25
    assert "like share 25% is under 40%" in home["verdict"]
    assert "more pieces from" not in view["profile_summary"]
    assert "Home Depot" not in view["profile_summary"]
    # finish: warm wood holds 81% of the like mass, 54% of what was shown, and the lift passes
    assert dims["color_finish"]["decision"] == "converged"
    assert ("color_finish", "warm wood") in chips
    assert dims["color_finish"]["lift"] >= 1.3 and dims["color_finish"]["exposure_coverage"] >= 0.6
    # material: wood is 100% of the like mass but also most of what was shown: lift under 1.3
    assert dims["material"]["decision"] == "none"
    assert "lift 1.25 for wood is under 1.3" in dims["material"]["reason"]
    # frame colour: black holds 44%, and the shown titles rarely name a frame colour: the trace says so
    frame = dims["frame_color"]
    assert frame["decision"] == "none" and "under 60%" in frame["reason"]
    assert view["profile"]["trace"]["price"]["decision"] == "mixed"
    assert set(dims) == set(DIMENSIONS)  # size class and legs decide nothing any more


def test_replay_two_batch_has_both_shapes_within_the_caps_and_a_composition(tmp_path: Path) -> None:
    svc, sid = replay2(tmp_path)
    view = svc.shop_again(sid)
    batch = view["search"]["trace"]["batch"]
    assert batch["batch"]["size"] == 24
    shapes = batch["batch"]["shape"]
    assert shapes["rectangular"] >= 6 and shapes["round"] >= 6  # both shapes, the 25% floor
    assert max(shapes.values()) <= int(0.6 * RESULT_COUNT)
    assert max(batch["batch"]["seller"].values()) <= 4  # the default seller cap
    assert batch["pool_before"]["size"] >= 24 and "shape" in batch["pool_before"]
    assert batch["serper_calls"] == view["search"]["serper_calls"]
    assert batch["strata"]["dimension"] == "shape"
    assert sum(batch["strata"]["quota"].values()) == RESULT_COUNT
    tagging = view["search"]["tagging"]
    assert tagging["candidates_tagged"] == tagging["candidates_total"] > 0
    assert tagging["shown_tagged"] == tagging["shown_total"] == 35
    assert view["search"]["trace"]["shown_tags"]["source"] == "model"


def test_replay_two_candidate_tags_are_cached_in_the_session_and_not_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, sid = replay2(tmp_path)
    client = RouterClient()
    use_client(monkeypatch, client)
    svc.shop_again(sid)
    first = [r for c in tag_calls(client) for r in rows_in(c)]
    cache = json.loads((tmp_path / "shop-data" / f"{sid}.json").read_text())["tag_cache"]
    assert len(first) >= 35 + 24 and len(cache) == len(first)  # shown results, then the pool
    assert cache["Q2"]["shape"] == "round" and cache["Q1"]["shape"] is None  # titles only
    n_calls = len(tag_calls(client))
    # the first batch is now shown, and the same pool is searched again: what was tagged before is
    # never sent again, so a second Shop Again asks only for results it has not seen
    svc.shop_again(sid)
    again = [r for c in tag_calls(client)[n_calls:] for r in rows_in(c)]
    first_titles = {r.split(" | ")[1] for r in first}
    assert len(again) < len(first) and len(set(again)) == len(again)
    assert not {r.split(" | ")[1] for r in again} & first_titles
    cache2 = json.loads((tmp_path / "shop-data" / f"{sid}.json").read_text())["tag_cache"]
    assert len(cache2) == len(cache) + len(again)


def test_replay_two_tagging_failure_falls_back_conservatively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, sid = replay2(tmp_path)
    use_client(monkeypatch, RouterClient(tags="the model rambled"))
    view = svc.shop_again(sid)
    assert len(view["search"]["results"]) > 0  # still searches, ranks and shows a batch
    trace = view["profile"]["trace"]
    assert trace["shown_tags"]["source"] == "keywords" and trace["shown_tags"]["tagged"] == 0
    for dim in DIMENSIONS:  # no attribute converges on a baseline read from title keywords
        assert trace["dimensions"][dim]["decision"] != "converged", dim
    assert "title keywords only" in trace["dimensions"]["color_finish"]["reason"]
    assert not [
        e
        for e in view["profile"]["inferred"]
        if e["status"] == "converged" and e["attr_kind"] not in ("seller", "price_band")
    ]
    tagging = view["search"]["tagging"]
    assert tagging["candidates_tagged"] == 0 and tagging["tag_failures"]
    assert any("could not be tagged" in w for w in view["search"]["warnings"])
    assert not json.loads((tmp_path / "shop-data" / f"{sid}.json").read_text())["tag_cache"]


def test_the_first_replay_still_decides_what_it_did(tmp_path: Path) -> None:
    cfg = config()
    search, lookup = build_shop_providers(cfg)
    store = SessionStore(tmp_path / "shop-data")
    svc = ShopService(cfg, store, search, lookup)
    view = svc.shop_again(seed_session(store).session_id)
    chips = {(e["attr_kind"], e["text"]) for e in view["profile"]["inferred"]}
    assert ("open", "shape: open") in chips and ("price_mixed", "price: mixed") in chips
    assert ("seller", "seller: West Elm") in chips
    assert not [e for e in view["profile"]["inferred"] if e["attr_kind"] in ("style", "shape")]
    assert "Looking for more pieces from West Elm." in view["profile_summary"]
    dims = view["profile"]["trace"]["dimensions"]
    assert all(d["decision"] in ("open", "none") for d in dims.values())  # nothing guessed


# ---- seller affinity: two items AND 40% ----------------------------------------------------------------------


def rated_sellers(spec: list[tuple[str, str, str]]) -> dict[str, Rated]:
    """(rating, seller, id) rows as rated items."""
    return {
        rid: Rated(rid, rating, "", f"Table {rid}", 100.0, seller, "")
        for rating, seller, rid in spec
    }


def shown_sellers(counts: dict[str, int]) -> list[ShopResult]:
    out: list[ShopResult] = []
    for seller, n in counts.items():
        for i in range(n):
            out.append(
                ShopResult(
                    id=f"{seller}{i}",
                    title="T",
                    price=1.0,
                    currency="USD",
                    seller=seller,
                    thumbnail_url="",
                    link="",
                    link_kind="none",
                    size_hint=None,
                    product_id=f"{seller}{i}",
                    rank=1,
                    pool_position=1,
                )  # fmt: skip
            )
    return out


def test_a_seller_needs_two_items_and_forty_percent_of_the_like_mass() -> None:
    shown = shown_sellers({"A": 5, "B": 30, "C": 30, "D": 30})
    # 2 of 5 like items (40% of the like mass): passes, and the lift (0.4 / 5%) is high
    ok = rated_sellers(
        [
            ("like", "A", "1"),
            ("like", "A", "2"),
            ("like", "B", "3"),
            ("like", "C", "4"),
            ("like", "D", "5"),
        ]
    )
    entry, trace = seller_decision(ok, shown)
    assert entry is not None and entry.text == "seller: A" and entry.share == 0.4
    # 2 of 6 (33%): two items but under 40%
    under = rated_sellers(
        [("like", "A", "1"), ("like", "A", "2")]
        + [("like", s, f"x{i}") for i, s in enumerate("BCDB")]
    )
    entry, trace = seller_decision(under, shown)
    assert entry is None
    row = next(c for c in trace["candidates"] if c["seller"] == "A")
    assert row["like_share"] == pytest.approx(0.3333, abs=1e-3)
    assert row["verdict"] == "no chip: like share 33% is under 40%"
    # one loved item is 3 of 4 = 75% of the mass, but one item is not enough
    one = rated_sellers([("love", "A", "1"), ("like", "B", "2")])
    entry, trace = seller_decision(one, shown)
    assert (
        entry is None and "1 loved or liked item(s), needs 2" in trace["candidates"][0]["verdict"]
    )
    # a dislike from the seller still blocks it, and every failed rule is listed
    both = rated_sellers(
        [("like", "A", "1"), ("like", "A", "2"), ("dislike", "A", "3"), ("like", "B", "4")]
    )
    entry, trace = seller_decision(both, shown)
    verdict = next(c for c in trace["candidates"] if c["seller"] == "A")["verdict"]
    assert entry is None and "1 disliked item(s) from this seller" in verdict
    # the lift test is unchanged: 40% of the like mass but 70% of what was shown is no signal
    flooded = shown_sellers({"A": 70, "B": 10, "C": 10, "D": 10})
    entry, trace = seller_decision(ok, flooded)
    assert entry is None and "lift 0.571 is under 1.3" in trace["candidates"][0]["verdict"]


def test_the_trace_lists_every_sellers_like_share_and_the_failed_rule() -> None:
    rated = rated_sellers(
        [
            ("love", "A", "1"),
            ("like", "A", "2"),
            ("like", "B", "3"),
            ("like", "C", "4"),
            ("dislike", "C", "5"),
        ]
    )
    _, trace = seller_decision(rated, shown_sellers({"A": 4, "B": 4, "C": 4}))
    by = {c["seller"]: c for c in trace["candidates"]}
    assert set(by) == {"A", "B", "C"}
    assert by["A"]["like_share"] == pytest.approx(0.6667, abs=1e-3) and by["A"]["verdict"] == "chip"
    assert by["B"]["verdict"].startswith(
        "no chip: 1 loved or liked item(s), needs 2; like share 17% is under 40%"
    )
    assert "disliked" in by["C"]["verdict"]


# ---- the pool: supply for the minority value of an open dimension ------------------------------------------------


def make_again(book: TagBook, max_calls: int = 1, max_open: int = 6, **kw: Any) -> AgainContext:
    return AgainContext(
        open_dims={"shape": ["rectangular", "round"]},
        open_mass={"shape": {"rectangular": 11, "round": 5}},
        open_loved={"shape": {"rectangular", "round"}},
        guard_caps={"shape": 0.6},
        max_calls=max_calls,
        max_calls_open=max_open,
        tag_book=book,
        **kw,
    )


def pool_page(prefix: str, shape: str, n: int, start: int = 0) -> list[SearchCandidate]:
    return [
        cand(
            f"{prefix}{i}",
            100.0 + i,
            title=f"{shape} Wood Coffee Table {prefix}{i}",
            seller=f"{prefix}S{i}",
        )
        for i in range(start, start + n)
    ]


def run_open(
    monkeypatch: pytest.MonkeyPatch, sequence: list[Any], cfg: Config | None = None, **kw: Any
) -> tuple[Any, FakeSearch, RouterClient]:
    client = RouterClient()
    use_client(monkeypatch, client)
    search = FakeSearch([])
    search.sequence = sequence
    cfg = cfg or config()
    again = make_again(TagBook(), **kw)
    outcome = run_query(
        query(request="coffee tables", search_text="coffee table", category="coffee table"),
        search, cfg, Budget(cfg), [], time.perf_counter(), again,
    )  # fmt: skip
    return outcome, search, client


def test_a_thin_minority_triggers_targeted_searches_for_that_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the first search finds 30 rectangular tables and no round one: round is 0% of the pool
    outcome, search, _ = run_open(
        monkeypatch,
        [
            pool_page("a", "Rectangular", 30),
            pool_page("b", "Round", 10),
            pool_page("c", "Round", 10),
        ],
    )
    assert [q for q, _ in search.calls] == [
        "coffee table",
        "round coffee table",
        "round coffee table",
    ]
    assert [p for _, p in search.calls] == [1, 1, 2]  # the next page of the same targeted search
    assert outcome.serper_calls == 3 and outcome.serper_credits == 6
    supply = outcome.composition["supply"]
    assert [s["value"] for s in supply] == ["round", "round"]
    assert supply[0]["value_before"] == "0 of 30" and supply[1]["value_before"] == "10 of 40"
    assert outcome.composition["pool_before"]["shape"]["rectangular"] == 30
    assert outcome.composition["pool_after"]["shape"]["round"] == 20
    assert outcome.composition["serper_calls"] == 3 and outcome.composition["serper_credits"] == 6
    assert "round coffee table" in outcome.queries
    shapes = outcome.composition["batch"]["shape"]
    assert shapes["round"] >= 6 and shapes["rectangular"] <= 14  # both shapes, within the caps
    assert not [w for w in outcome.warnings if "Could not keep shape" in w]


def test_no_supply_when_the_minority_value_is_forty_percent_or_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, search, _ = run_open(
        monkeypatch, [pool_page("a", "Rectangular", 12) + pool_page("b", "Round", 8)]
    )
    assert len(search.calls) == 1 and outcome.serper_calls == 1  # 8 of 20 is exactly 40%
    assert outcome.composition["supply"] == []


def test_supply_stops_at_the_open_cap_of_six_calls_and_the_warning_has_real_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence: list[Any] = [pool_page("a", "Rectangular", 30)] + [
        pool_page(f"z{n}", "Rectangular", 10) for n in range(10)
    ]  # the targeted searches keep finding rectangular tables only
    outcome, search, _ = run_open(monkeypatch, sequence)
    assert (
        len(search.calls) == 6 and outcome.serper_calls == 6
    )  # APP_SHOP_AGAIN_MAX_SERPER_CALLS_OPEN
    assert outcome.serper_credits == 12  # the extra credits are reported
    warning = next(w for w in outcome.warnings if "Could not keep shape mixed" in w)
    assert "mostly rectangular" in warning and "6 searches were run" in warning
    assert "The pool of 80 had 80 rectangular" in warning  # the real counts, after the supply step
    assert outcome.composition["batch"]["shape"]["rectangular"] == 14  # held to the 60% cap


def test_the_open_call_cap_is_configurable_and_never_below_the_normal_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config(shop_again_max_serper_calls_open=4)
    sequence: list[Any] = [pool_page("a", "Rectangular", 30)] + [
        pool_page(f"z{n}", "Rectangular", 10) for n in range(10)
    ]
    outcome, search, _ = run_open(monkeypatch, sequence, cfg=cfg, max_open=4)
    assert len(search.calls) == 4 and outcome.serper_calls == 4
    with pytest.raises(ValueError):
        config(shop_again_max_serper_calls=4, shop_again_max_serper_calls_open=3)
    assert Config().shop_again_max_serper_calls_open == 6


def test_supply_does_not_run_for_a_typed_search_or_a_converged_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RouterClient()
    use_client(monkeypatch, client)
    search = FakeSearch([pool_page("a", "Rectangular", 30)])
    cfg = config()
    typed = run_query(
        query(request="coffee tables", search_text="coffee table", category="coffee table"),
        search, cfg, Budget(cfg), [], time.perf_counter(),
    )  # fmt: skip
    assert typed.composition == {} and not tag_calls(client)  # a typed search tags nothing
    converged = AgainContext(converged={"shape": "round"}, max_calls=1, tag_book=TagBook())
    search2 = FakeSearch([pool_page("a", "Rectangular", 30)])
    out = run_query(
        query(request="coffee tables", search_text="coffee table", category="coffee table"),
        search2, cfg, Budget(cfg), [], time.perf_counter(), converged,
    )  # fmt: skip
    assert len(search2.calls) == 1 and out.composition["supply"] == []


def test_the_supplied_candidates_survive_the_pool_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    # 90 rectangular candidates fill the 80-candidate pool; the round ones fetched for the open
    # dimension must not be cut off the end of it
    outcome, _, client = run_open(
        monkeypatch,
        [
            pool_page("a", "Rectangular", 45),
            pool_page("b", "Rectangular", 45),
            pool_page("c", "Round", 20),
        ],
        max_calls=2,
    )
    rank_calls = [
        c
        for c in client.calls
        if c["output_config"]["format"]["schema"]["title"] == "RankedFinalists"
    ]
    ranked_text = rank_calls[0]["messages"][0]["content"]
    assert ranked_text.count("Round Wood Coffee Table") >= 10
    assert outcome.composition["batch"]["shape"].get("round", 0) >= 6


# ---- stratified selection -------------------------------------------------------------------------------------


def test_slots_follow_the_like_mass_within_the_cap_and_the_floor() -> None:
    both = {"rectangular", "round"}
    assert strata_quotas(["rectangular", "round"], {"rectangular": 11, "round": 5}, both) == {
        "rectangular": 14,
        "round": 10,
    }  # 16.5 and 7.5 slots: the 60% cap (14) holds, the rest goes to the other value
    assert strata_quotas(["rectangular", "round"], {"rectangular": 15, "round": 1}, both) == {
        "rectangular": 14,
        "round": 10,
    }  # a loved value gets its floor (at least 6) and the cap leaves the rest to it
    assert strata_quotas(["a", "b", "c"], {"a": 6, "b": 6, "c": 6}, set()) == {
        "a": 8,
        "b": 8,
        "c": 8,
    }
    got = strata_quotas(["a", "b"], {"a": 1, "b": 1}, set())
    assert got == {"a": 12, "b": 12} and sum(got.values()) == RESULT_COUNT
    floor = strata_quotas(["a", "b", "c"], {"a": 20, "b": 1, "c": 20}, {"b"})
    assert floor["b"] >= 6 and sum(floor.values()) == RESULT_COUNT and max(floor.values()) <= 14
    assert 6 == __import__("math").ceil(0.25 * RESULT_COUNT)


def ordered_pool(parts: list[tuple[str, str, int]]) -> list[tuple[SearchCandidate, str]]:
    out: list[tuple[SearchCandidate, str]] = []
    for prefix, shape, n in parts:
        out += [(c, "") for c in pool_page(prefix, shape, n)]
    return out


def test_stratified_selection_reserves_slots_for_each_value_in_ranker_order() -> None:
    # the ranker put 20 titles that name no shape first, then rectangular, then round
    pool = ordered_pool([("u", "Plain", 20), ("r", "Rectangular", 14), ("o", "Round", 14)])
    index = {c.product_id: n for n, (c, _) in enumerate(pool, 1)}
    q = query(request="t")
    plain, _, _ = build_results(pool, q, index, guard_caps={"shape": 0.6})
    shapes = [r.title.split()[0] for r in plain]
    assert shapes.count("Round") == 0 and shapes.count("Rectangular") == 4  # round never reached
    results, _, _ = build_results(
        pool,
        q,
        index,
        guard_caps={"shape": 0.6},
        strata=("shape", {"rectangular": 14, "round": 10}),
    )
    shapes = [r.title.split()[0] for r in results]
    assert len(results) == 24 and shapes.count("Round") == 10 and shapes.count("Rectangular") == 14
    assert shapes.count("Plain") == 0  # the 24 slots are spoken for; none left for unknown titles
    ids = [int(r.title.split()[-1][1:]) for r in results if r.title.startswith("Round")]
    assert ids == sorted(ids)  # best ranked first inside a value
    assert [r.rank for r in results] == list(range(1, 25))
    # merged back into ranker order: rectangular (ranked earlier) before round
    assert [s for s in shapes if s != "Plain"] == ["Rectangular"] * 14 + ["Round"] * 10


def test_stratified_selection_still_holds_every_cap() -> None:
    pool = ordered_pool([("r", "Rectangular", 30), ("o", "Round", 30)])
    for n, (c, r) in enumerate(pool):  # three sellers only: the default cap of 4 per seller bites
        pool[n] = (
            SearchCandidate(
                c.product_id, c.title, f"S{n % 3}", c.price, "USD", None, None, c.google_panel_url
            ),
            r,
        )
    index = {c.product_id: n for n, (c, _) in enumerate(pool, 1)}
    results, capped, _ = build_results(
        pool, query(request="t"), index, guard_caps={"shape": 0.6},
        strata=("shape", {"rectangular": 14, "round": 10}),
    )  # fmt: skip
    assert len(results) == 12 and capped > 0  # 3 sellers x 4
    shape_counts = {
        s: sum(1 for r in results if r.title.startswith(s)) for s in ("Round", "Rectangular")
    }
    assert max(shape_counts.values()) <= 14
    affinity, _, _ = build_results(
        pool, query(request="t"), index, affinity_sellers={"s0"}, guard_caps={"shape": 0.6},
        strata=("shape", {"rectangular": 14, "round": 10}),
    )  # fmt: skip
    assert sum(1 for r in affinity if r.seller == "S0") == 12  # an affinity seller: up to half


def test_a_value_with_no_candidates_leaves_its_slots_to_the_others() -> None:
    pool = ordered_pool([("r", "Rectangular", 30)])
    index = {c.product_id: n for n, (c, _) in enumerate(pool, 1)}
    results, _, skipped = build_results(
        pool, query(request="t"), index, guard_caps={"shape": 0.6},
        strata=("shape", {"rectangular": 14, "round": 10}),
    )  # fmt: skip
    assert len(results) == 14 and skipped > 0  # the 60% cap still holds: no more than 14


# ---- the exposure baseline and the guard from model tags -----------------------------------------------------------


def test_the_lift_test_runs_on_model_tags_not_title_keywords() -> None:
    shown = [
        ShopResult(
            id=f"s{i}", title=f"Coffee Table {i}", price=1.0, currency="USD", seller="S",
            thumbnail_url="", link="", link_kind="none", size_hint=None, product_id=f"s{i}",
            rank=1, pool_position=1,
        )
        for i in range(10)
    ]  # fmt: skip
    # no title says anything about shape, but the model tagged all ten: 3 round, 7 rectangular
    tags = {r.id: {"shape": "round" if n < 3 else "rectangular"} for n, r in enumerate(shown)}
    exp = build_exposure(shown, tags)
    assert (
        exp.shares["shape"] == {"round": 0.3, "rectangular": 0.7} and exp.coverage["shape"] == 1.0
    )
    items = [
        tagged("a", "love", "Table", shape="round"),
        tagged("b", "love", "Table", shape="round"),
    ]
    from app.shop.decide import classify_dimension

    res = classify_dimension("shape", items, exp)
    assert res.status == "converged" and res.lift == pytest.approx(3.333, abs=1e-3)
    assert build_exposure(shown, None).coverage == {}  # keywords alone read none of these titles


def test_old_decisions_and_old_session_files_still_load() -> None:
    old = {
        "schema_version": 1, "session_id": "a" * 16, "created_at": "t", "updated_at": "t",
        "profile": {
            "inferred": [
                {"text": "hairpin legs", "attr_kind": "frame_or_legs", "polarity": "like",
                 "dimension": "frame_or_legs", "status": "converged", "values": ["hairpin legs"]},
                {"text": "medium", "attr_kind": "size", "polarity": "like", "dimension": "size_class"},
            ],
            "trace": {"mode": "model", "dimensions": {"size_class": {"decision": "none", "reason": "old"}}},
        },
    }  # fmt: skip
    session = Session.model_validate(old)
    assert session.tag_cache == {} and session.profile is not None
    assert [e.dimension for e in session.profile.inferred] == ["frame_or_legs", "size_class"]


def test_size_class_and_legs_no_longer_decide_anything() -> None:
    sess = make_session(
        [(f"i{i}", f"Table {i}", 100.0, f"S{i}") for i in range(4)],
        {f"i{i}": ("love", "") for i in range(4)},
    )
    rated = rated_map(sess)
    from app.shop.models import ItemTags

    tags = {
        rid: ItemTags.model_validate(
            {"id": rid, "shape": None, "material": None, "color_finish": None, "frame_color": None,
             "style": None, "quality_cues": []}
        )
        for rid in rated
    }  # fmt: skip
    out = decide(rated, tags, list(sess.results.values()), [], [], exposure=Exposure())
    assert set(out.trace["dimensions"]) == set(DIMENSIONS)
    assert not [e for e in out.entries if e.dimension in ("size_class", "frame_or_legs")]
    with pytest.raises(ValueError):  # the schema the model fills no longer has them
        ItemTags.model_validate(
            {"id": "a", "shape": None, "material": None, "color_finish": None, "frame_color": None,
             "style": None, "quality_cues": [], "size_class": "large", "frame_or_legs": None}
        )  # fmt: skip


# ---- cost, prompts, security --------------------------------------------------------------------------------------


def test_candidate_tagging_cost_is_in_the_shop_again_total_and_under_a_cent(tmp_path: Path) -> None:
    svc, sid = replay2(tmp_path)
    view = svc.shop_again(sid)
    found = view["search"]
    tagging = found["tagging"]
    assert tagging["tag_calls"] >= 1 and 0 < tagging["tag_usd"] < 0.005
    assert tagging["tag_usd"] < found["anthropic_usd"] < 0.01  # included in the Anthropic total
    assert found["total_usd"] == pytest.approx(found["anthropic_usd"] + found["provider_usd"])
    assert found["serper_calls"] == view["search"]["trace"]["batch"]["serper_calls"]


def test_the_prompts_are_versioned_and_the_old_ones_kept() -> None:
    from app.prompts import load_prompt

    assert load_prompt("shop_prefs").version == "5"
    for v in ("1", "2", "3", "4"):
        assert load_prompt("shop_prefs", v).version == v
    tags = load_prompt("shop_candidate_tags")
    assert tags.version == "1" and "Allowed values" in tags.text and "no photo" in tags.text
    assert "frame_or_legs" not in load_prompt("shop_prefs").text
    assert "size_class" not in load_prompt("shop_prefs").text
    assert "frame_or_legs" in load_prompt("shop_prefs", "4").text  # v4 is untouched


def test_the_page_renders_the_composition_with_text_nodes_only() -> None:
    from app.shop.server import STATIC_DIR

    code = (STATIC_DIR / "app.js").read_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in code
    block = code[code.index("function batchBlock") : code.index("function removePref")]
    assert "Batch makeup" in block and "supply" in block and "tagged" in block
    assert "candidates tagged" in code  # the footer says candidates tagged N of M


def test_input_validation_and_the_csp_are_unchanged(tmp_path: Path) -> None:
    from app.shop.server import CSP, ShopApp

    svc, sid = replay2(tmp_path)
    app = ShopApp(svc, port=8765)
    hdrs = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}
    bad = app.handle("POST", "/api/shop-again", hdrs, json.dumps({"session_id": "../etc"}).encode())
    assert bad.status in (400, 404)
    ok = app.handle("POST", "/api/shop-again", hdrs, json.dumps({"session_id": sid}).encode())
    assert ok.status == 200 and ok.all_headers()["Content-Security-Policy"] == CSP
    evil = app.handle("POST", "/api/shop-again", {"Host": "evil.example"}, b"{}")
    assert evil.status == 403
