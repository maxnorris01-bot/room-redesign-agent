"""Shop Mode stage 2b: learning from the rated items, a wider pool, lookups only on Open.

Offline like the other Shop Mode tests (same doubles, same loopback-only socket guard).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.shop.models import (
    ImageReport,
    InferredAttribute,
    PreferenceProfileV2,
    ProfileEntry,
    QueryAdjustments,
    ShopResult,
    StoredProfile,
)
from app.shop.prefs import (
    Rated,
    alignment_counts,
    apply_adjustments,
    build_queries,
    guess_style_terms,
    merge_profile_v2,
    preference_alignment,
    price_band_entry,
    profile_summary,
    reapply_removed,
    remove_entry,
    select_image_rows,
    seller_affinity,
    text_matches,
    validate_inferred,
)
from app.shop.providers import PREMIUM_SELLER, build_shop_providers
from app.shop.server import STATIC_DIR, ShopApp
from app.shop.service import ApiError, ShopService
from app.shop.store import Session, SessionStore
from app.thumbnails import ThumbResult
from test_shop import (  # noqa: F401  (the autouse guard must be imported to apply here)
    FakeLookup,
    FakeSearch,
    RouterClient,
    _loopback_only,
    cand,
    config,
    make_service,
    query,
    retailer,
    use_client,
)

ROOT = Path(__file__).resolve().parents[1]


# ---- helpers ------------------------------------------------------------------------------------


def rated(
    rid: str,
    rating: str,
    title: str = "",
    seller: str = "Shop",
    comment: str = "",
    price: float = 100.0,
) -> Rated:
    return Rated(
        rid,
        rating,
        comment,
        title or f"Item {rid}",
        price,
        seller,
        f"https://img.example/{rid}.jpg",
    )


def rmap(*rows: Rated) -> dict[str, Rated]:
    return {r.id: r for r in rows}


def attr(
    kind: str, text: str, ids: list[str], pol: str = "like", conf: float = 0.8
) -> InferredAttribute:
    return InferredAttribute(kind=kind, text=text, polarity=pol, evidence=ids, confidence=conf)  # type: ignore[arg-type]


def adj0() -> QueryAdjustments:
    return QueryAdjustments(
        search_text="", style_terms=[], material_terms=[], must_have=[], avoid=[],
        price_max=None, price_note="",
    )  # fmt: skip


def proposal(
    inferred: list[InferredAttribute] | None = None,
    contradicted: list[Any] | None = None,
    likes: list[str] | None = None,
    dislikes: list[str] | None = None,
) -> PreferenceProfileV2:
    return PreferenceProfileV2(
        likes=likes or [], dislikes=dislikes or [], inferred=inferred or [],
        contradicted_guesses=contradicted or [], query_adjustments=adj0(),
    )  # fmt: skip


def mock_service(tmp_path: Path, **kw: Any) -> ShopService:
    cfg = config()
    search, lookup = build_shop_providers(cfg)
    return ShopService(cfg, SessionStore(tmp_path / "shop-data"), search, lookup, **kw)


COFFEE = "coffee tables with a mix of wood and metal with storage"


def pick(first: list[dict[str, Any]], *needles: str, premium: bool = True) -> dict[str, Any]:
    """The first result (from the premium seller, or not) whose title has every needle."""
    for r in first:
        if (r["seller"] == PREMIUM_SELLER) == premium and all(
            n.lower() in r["title"].lower() for n in needles
        ):
            return r
    raise AssertionError(f"no result with {needles} in the first batch")


def brass_scenario(svc: ShopService) -> tuple[str, list[dict[str, Any]]]:
    """The owner's mixed case, in mock form: loves a round walnut drum and a rectangular wood and
    black metal lift-top from one premium seller (no comments), likes a round and a rectangular
    item from other sellers, dislikes one. Shape comes out open. Returns (session id, first batch)."""
    view = svc.search(None, COFFEE)
    sid, first = view["session_id"], view["search"]["results"]
    svc.rate(sid, pick(first, "Round Walnut Drum")["id"], "love")
    svc.rate(sid, pick(first, "Rectangular Wood and Black Metal")["id"], "love")
    svc.rate(sid, pick(first, "Round ", premium=False)["id"], "like")
    svc.rate(sid, pick(first, "Rectangular ", premium=False)["id"], "like")
    svc.rate(sid, pick(first, "Oval ", premium=False)["id"], "dislike")
    return sid, first


def round_scenario(svc: ShopService) -> tuple[str, list[dict[str, Any]]]:
    """Scenario B: loves two round items from the premium seller, likes two rectangular ones from
    others, dislikes one. Round carries 75% of the like mass against about a third of what was
    shown, so shape converges."""
    view = svc.search(None, COFFEE)
    sid, first = view["session_id"], view["search"]["results"]
    svc.rate(sid, pick(first, "Round Walnut Drum")["id"], "love")
    svc.rate(sid, pick(first, "Round Brushed Brass Drum")["id"], "love")
    svc.rate(sid, pick(first, "Rectangular ", premium=False)["id"], "like")
    rect2 = [r for r in first if r["seller"] != PREMIUM_SELLER and "Rectangular" in r["title"]][1]
    svc.rate(sid, rect2["id"], "like")
    svc.rate(sid, pick(first, "Oval ", premium=False)["id"], "dislike")
    return sid, first


def as_results(rows: list[dict[str, Any]]) -> list[ShopResult]:
    return [ShopResult.model_validate(r) for r in rows]


# ---- learning from the items, with no comments --------------------------------------------------------


def test_two_loved_round_items_converge_shape_with_no_comment(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    sid, first = round_scenario(svc)
    again = svc.shop_again(sid)
    profile = again["profile"]
    assert profile["likes"] == [] and profile["dislikes"] == []  # nothing was said
    shape = [e for e in profile["inferred"] if e["dimension"] == "shape"]
    assert [(e["text"], e["status"], e["polarity"]) for e in shape] == [
        ("round", "converged", "like")
    ]
    assert shape[0]["share"] == 0.75 and shape[0]["lift"] > 1.3
    assert len(shape[0]["evidence"]) == 2 and shape[0]["weight"] == 6
    assert all("Round" in t for t in shape[0]["evidence_titles"])
    assert "round" in again["profile_summary"]
    assert not any(e["status"] == "open" and e["dimension"] == "shape" for e in profile["inferred"])


def test_the_mixed_scenario_shows_seller_affinity_open_shape_and_better_alignment(
    tmp_path: Path,
) -> None:
    svc = mock_service(tmp_path)
    sid, first = brass_scenario(svc)
    again = svc.shop_again(sid)
    profile = StoredProfile.model_validate(again["profile"])
    found = again["search"]
    seller = [e for e in profile.inferred if e.attr_kind == "seller"]
    assert [e.text for e in seller] == [f"seller: {PREMIUM_SELLER}"]
    shape = [e for e in profile.inferred if e.dimension == "shape"]
    assert [(e.status, sorted(e.values)) for e in shape] == [("open", ["rectangular", "round"])]
    assert any(PREMIUM_SELLER.lower() in q.lower() for q in found["queries"])  # the seller query
    assert any("round" in q.lower() for q in found["queries"])  # one phrasing per open value
    assert any("rectangular" in q.lower() for q in found["queries"])
    assert found["queries"][0] == "wood and metal coffee table with storage"  # typed: no seller
    assert (
        PREMIUM_SELLER in again["profile_summary"] and "Shape is open" in again["profile_summary"]
    )
    first_align = preference_alignment(as_results(first), profile)
    again_align = preference_alignment(as_results(found["results"]), profile)
    assert again_align > first_align
    assert (
        found["alignment"]["total"] == len(found["results"]) and found["alignment"]["matches"] > 0
    )
    premium_first = sum(1 for r in first if r["seller"] == PREMIUM_SELLER)
    premium_again = sum(1 for r in found["results"] if r["seller"] == PREMIUM_SELLER)
    assert premium_again > premium_first


def test_an_upper_price_band_shows_in_the_mock_scenario_and_leaves_the_cap_alone(
    tmp_path: Path,
) -> None:
    svc = mock_service(tmp_path)
    view = svc.search(None, "coffee tables under $1500")
    sid, first = view["session_id"], view["search"]["results"]
    top = sorted(first, key=lambda r: -r["price"])[:3]
    svc.rate(sid, top[0]["id"], "love")
    svc.rate(sid, top[1]["id"], "love")
    again = svc.shop_again(sid)
    band = [e for e in again["profile"]["inferred"] if e["attr_kind"] == "price_band"]
    assert (
        len(band) == 1 and band[0]["band"] == "upper" and "upper range, about $" in band[0]["text"]
    )
    assert "in the upper price range" in again["profile_summary"]
    assert again["search"]["query"]["price_max"] == 1500.0  # the typed cap is untouched
    assert again["search"]["used_query"]["price_max"] == 1500.0
    assert all(r["price"] <= 1500 for r in again["search"]["results"])


# ---- the rules the code enforces -------------------------------------------------------------------------


def test_seller_affinity_needs_two_loved_or_liked_items_and_no_dislike() -> None:
    one = rmap(rated("a", "love", seller="West Elm"))
    assert seller_affinity(one, []) is None
    two = rmap(rated("a", "love", seller="West Elm"), rated("b", "like", seller="west elm"))
    entry = seller_affinity(two, [])
    assert entry is not None and entry.text == "seller: West Elm" and entry.attr_kind == "seller"
    assert entry.weight == 4 and entry.evidence == ["a", "b"]
    spoiled = rmap(*two.values(), rated("c", "dislike", seller="West Elm"))
    assert seller_affinity(spoiled, []) is None
    assert seller_affinity(two, ["seller west elm"]) is None  # a deleted chip stays deleted
    only_dislikes = rmap(rated("a", "dislike", seller="X"), rated("b", "dislike", seller="X"))
    assert seller_affinity(only_dislikes, []) is None


def test_the_models_own_seller_and_price_proposals_are_ignored() -> None:
    data = rmap(rated("a", "love", seller="S1"), rated("b", "love", seller="S2"))
    out = validate_inferred(
        [attr("seller", "seller: s1", ["a", "b"]), attr("price_band", "upper range", ["a", "b"])],
        data, [], set(),
    )  # fmt: skip
    assert out == []


def session_with_prices(prices: list[float], rated_ids: list[str], rating: str = "love") -> Session:
    store_session = Session(session_id="x" * 16, created_at="t", updated_at="t")
    for i, price in enumerate(prices):
        store_session.results[f"r{i}"] = ShopResult(
            id=f"r{i}", title=f"Table {i}", price=price, currency="USD", seller=f"S{i}",
            thumbnail_url="", link="", link_kind="none", size_hint=None, product_id=f"r{i}",
            rank=i + 1, pool_position=i + 1,
        )  # fmt: skip
    return store_session


def test_price_band_is_computed_from_the_shown_prices_in_code() -> None:
    prices = [100.0, 150, 200, 250, 300, 350, 400, 450, 500, 700.0, 900.0, 1100.0]
    sess = session_with_prices(prices, [])
    data = rmap(rated("r9", "love", price=700.0), rated("r10", "like", price=900.0))
    entry = price_band_entry(sess, data, [])
    assert entry is not None and entry.band == "upper" and entry.attr_kind == "price_band"
    assert entry.text.startswith("upper range, about $") and entry.low and entry.high
    assert entry.low <= 700 and entry.high >= 1100 and entry.evidence == ["r9", "r10"]
    cheap = rmap(rated("r0", "love", price=100.0), rated("r1", "love", price=150.0))
    lower = price_band_entry(sess, cheap, [])
    assert lower is not None and lower.band == "lower"
    middle = rmap(rated("r4", "love", price=300.0), rated("r5", "love", price=350.0))
    assert price_band_entry(sess, middle, []) is None  # the middle says nothing
    assert price_band_entry(sess, rmap(rated("r9", "love", price=700.0)), []) is None  # one item
    few = session_with_prices([100.0, 200.0, 300.0], [])
    assert price_band_entry(few, data, []) is None  # too few shown prices


def test_a_price_band_never_changes_the_typed_cap_or_floor() -> None:
    base = query(category="table", price_min=50.0, price_max=1000.0, request="tables")
    band = ProfileEntry(
        text="upper range, about $600 to $1,000", attr_kind="price_band", polarity="like",
        low=600.0, high=1000.0, band="upper",
    )  # fmt: skip
    out = apply_adjustments(base, StoredProfile(inferred=[band]))
    assert (out.price_min, out.price_max) == (50.0, 1000.0)
    uncapped = apply_adjustments(query(request="tables"), StoredProfile(inferred=[band]))
    assert uncapped.price_min is None and uncapped.price_max is None


def test_evidence_from_two_items_is_needed_unless_a_comment_backs_it() -> None:
    data = rmap(
        rated("a", "love", title="Brushed Brass Table"),
        rated("b", "love", title="Brushed Brass Lamp"),
        rated("c", "like", title="Oak Desk", comment="love the oak"),
        rated("d", "love", title="Walnut Bench", comment="so nice"),
        rated("e", "like", title="Marble Stand"),
        rated("f", "love", title="Linen Chair"),
    )
    out = validate_inferred(
        [
            attr("color_finish", "brushed brass", ["a", "b"]),  # two items: kept
            attr("material", "marble", ["e"]),  # one liked item, no comment: dropped
            attr("material", "linen", ["f"]),  # one loved item, no comment: dropped
            attr("material", "oak", ["c"]),  # one item, but its comment names it: kept
            attr("material", "walnut", ["d"]),  # one loved item with a comment: kept
        ],
        data, [], set(),
    )  # fmt: skip
    assert [e.text for e in out] == ["brushed brass", "walnut", "oak"] or {e.text for e in out} == {
        "brushed brass",
        "oak",
        "walnut",
    }
    by_text = {e.text: e for e in out}
    assert by_text["brushed brass"].weight == 6 and by_text["oak"].weight == 1
    assert by_text["walnut"].weight == 3


def test_evidence_must_be_real_rated_results_of_the_right_polarity() -> None:
    data = rmap(rated("a", "love"), rated("b", "love"), rated("x", "dislike", title="Glass Top"))
    out = validate_inferred(
        [
            attr("material", "ghost", ["a", "nope"]),  # one real id only
            attr("material", "glass", ["a", "b"], pol="dislike"),  # loved items cannot show a dislike
            attr("shape", "round", ["a", "x"]),  # a like cannot cite a disliked item
        ],
        data, [], set(),
    )  # fmt: skip
    assert out == []


def test_a_like_needs_a_positive_net_weight_and_a_dislike_cannot_contradict_a_loved_item() -> None:
    data = rmap(
        rated("a", "like", title="Glass Table"),
        rated("b", "like", title="Glass Shelf"),
        rated("x", "dislike", title="Glass Lamp"),
        rated("y", "dislike", title="Glass Bowl"),
        rated("z", "dislike", title="Glass Vase"),
        rated("p", "love", title="Wicker Chair"),
        rated("q", "dislike", title="Wicker Stool"),
        rated("r", "dislike", title="Wicker Bench"),
    )
    out = validate_inferred(
        [
            attr("material", "glass", ["a", "b"]),  # weight 2, three disliked glass items: dropped
            attr("material", "wicker", ["q", "r"], pol="dislike"),  # a loved wicker chair: dropped
        ],
        data, [], set(),
    )  # fmt: skip
    assert out == []


def test_a_disliked_attribute_needs_two_disliked_items_or_a_comment() -> None:
    data = rmap(
        rated("a", "dislike", title="Flimsy Table", comment="too flimsy"),
        rated("b", "dislike", title="Glass Table"),
        rated("c", "dislike", title="Glass Desk"),
        rated("d", "dislike", title="Chrome Lamp"),
    )
    out = validate_inferred(
        [
            attr("frame_or_legs", "flimsy", ["a"], pol="dislike"),
            attr("material", "glass", ["b", "c"], pol="dislike"),
            attr("material", "chrome", ["d"], pol="dislike"),
        ],
        data, [], set(),
    )  # fmt: skip
    assert {e.text for e in out} == {"flimsy", "glass"}
    assert all(e.polarity == "dislike" for e in out)


def test_a_quality_cue_must_be_visible_in_a_title_an_image_or_the_band() -> None:
    data = rmap(
        rated("a", "love", title="Solid Walnut Table"),
        rated("b", "love", title="Solid Walnut Desk"),
        rated("c", "love", title="Plain Chair"),
        rated("d", "love", title="Plain Stool"),
    )
    cues = [
        attr("quality_cue", "solid walnut", ["a", "b"]),  # in the titles
        attr("quality_cue", "hand finished", ["c", "d"]),  # nothing shows it
    ]
    assert [e.text for e in validate_inferred(cues, data, [], set())] == ["solid walnut"]
    with_image = validate_inferred(cues, data, [], {"c"})  # an image of one of them was sent
    assert {e.text for e in with_image} == {"solid walnut", "hand finished"}
    price_cue = [attr("quality_cue", "higher price", ["c", "d"])]
    assert validate_inferred(price_cue, data, [], set()) == []
    assert len(validate_inferred(price_cue, data, [], set(), upper_band=True)) == 1


def test_inferred_entries_are_bounded_deduplicated_and_skip_deleted_phrases() -> None:
    names = [f"finish number {i}" for i in range(15)]
    data = rmap(rated("a", "love"), rated("b", "love"))
    many = [attr("color_finish", n, ["a", "b"]) for n in names + names[:3]]
    out = validate_inferred(many, data, ["finish number 0"], set())
    assert len(out) == 10 and len({e.text for e in out}) == 10
    assert "finish number 0" not in [e.text for e in out]


# ---- guesses ----------------------------------------------------------------------------------------------


def test_style_terms_the_shopper_did_not_say_are_guesses() -> None:
    q = query(
        request="coffee tables with a mix of wood and metal with storage",
        style_terms=["industrial", "modern"],
    )
    assert guess_style_terms(q) == ["industrial", "modern"]
    typed = query(request="modern boho curtains", style_terms=["modern", "boho", "bohemian"])
    assert guess_style_terms(typed) == ["bohemian"]  # "bohemian" is not in the request
    mixed = query(request="boho curtains", style_terms=["boho", "mid-century"])
    assert guess_style_terms(mixed) == ["mid-century"]


def test_a_guess_is_shown_marked_and_not_counted_as_a_like(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    view = svc.search(None, "modern couches under $1000")
    sid = view["session_id"]
    session = svc.store.load(sid)
    base = session.searches[0].query
    base = base.model_copy(update={"style_terms": ["modern", "industrial"]})
    assert guess_style_terms(base) == ["industrial"]
    res = view["search"]["results"]
    svc.rate(sid, res[0]["id"], "like")
    session = svc.store.load(sid)
    merged = merge_profile_v2(None, proposal(), session, base, ImageReport(), set())
    assert merged.guesses == ["industrial"]
    assert merged.likes == [] and all(e.text != "industrial" for e in merged.inferred)
    assert alignment_counts(as_results(res), merged) == (0, len(res))  # a guess matches nothing


def test_a_guess_is_dropped_when_a_loved_item_contradicts_it() -> None:
    sess = Session(session_id="x" * 16, created_at="t", updated_at="t")
    for rid, title in (("a", "Brushed Brass Drum Table"), ("b", "Walnut Lift-Top Table")):
        sess.results[rid] = ShopResult(
            id=rid, title=title, price=500.0, currency="USD", seller="S", thumbnail_url="",
            link="", link_kind="none", size_hint=None, product_id=rid, rank=1, pool_position=1,
        )  # fmt: skip
    from app.shop.store import RatingRecord

    sess.ratings["a"] = RatingRecord(rating="love", rated_at="1")
    sess.ratings["b"] = RatingRecord(rating="like", rated_at="2")
    base = query(request="coffee tables", style_terms=["industrial", "drum"])
    from app.shop.models import ContradictedGuess

    flagged = [
        ContradictedGuess(text="industrial", evidence=["a"]),
        ContradictedGuess(text="drum", evidence=["a"]),
    ]
    merged = merge_profile_v2(
        None, proposal(contradicted=flagged), sess, base, ImageReport(), set()
    )
    # "industrial" is not shown by any loved or liked title: dropped. "drum" is in a loved title:
    # the model's claim is refused, so the guess stays (and is still only a guess).
    assert merged.dropped_guesses == ["industrial"] and merged.guesses == ["drum"]
    out = apply_adjustments(base, merged)
    assert "industrial" not in out.style_terms and "drum" in out.style_terms
    unrated = merge_profile_v2(
        None, proposal(contradicted=[ContradictedGuess(text="industrial", evidence=["zzz"])]), sess, base,
        ImageReport(), set(),
    )  # fmt: skip
    assert unrated.guesses == ["industrial", "drum"]  # evidence must be a loved or liked result
    again = merge_profile_v2(merged, proposal(), sess, base, ImageReport(), set())
    assert again.guesses == ["drum"] and again.dropped_guesses == ["industrial"]  # stays dropped


def test_a_guess_chip_can_be_removed_and_stays_removed() -> None:
    profile = StoredProfile(guesses=["industrial", "modern"])
    out = remove_entry(profile, "guess", "Industrial")
    assert out is not None and out.guesses == ["modern"] and out.removed == ["industrial"]
    assert remove_entry(profile, "guess", "boho") is None
    base = query(request="tables", style_terms=["industrial", "modern"])
    sess = Session(session_id="x" * 16, created_at="t", updated_at="t")
    merged = merge_profile_v2(out, proposal(), sess, base, ImageReport(), set())
    assert merged.guesses == ["modern"]  # a removed guess is never re-added
    assert "industrial" not in apply_adjustments(base, merged).style_terms


def test_a_guess_never_reaches_the_ranker_as_a_like(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = RouterClient(parse={
        "search_text": "coffee table", "category": "coffee table", "price_min": None,
        "price_max": None, "style_terms": ["industrial"], "material_terms": ["wood"],
        "must_have": [], "avoid": [],
    })  # fmt: skip
    use_client(monkeypatch, client)
    search = FakeSearch([[cand(f"a{i}", 50.0 + i, title=f"Wood Table {i}") for i in range(30)]])
    svc = make_service(tmp_path, search, FakeLookup())
    first = svc.search(None, "coffee tables with wood")
    sid = first["session_id"]
    svc.rate(sid, first["search"]["results"][0]["id"], "like")  # (a loved one would drop the guess)
    search.sequence = [[cand(f"n{i}", 50.0, title=f"Wood Table N{i}") for i in range(30)]]
    again = svc.shop_again(sid)
    rank_text = client.calls[-1]["messages"][0]["content"]
    assert "Style: none" in rank_text  # the guess is not listed as a style
    guess_line = next(x for x in rank_text.splitlines() if x.startswith("Style guessed"))
    assert guess_line.endswith("industrial") and "do not treat as likes" in guess_line
    assert again["profile"]["guesses"] == ["industrial"]
    assert "industrial" not in " ".join(e["text"] for e in again["profile"]["inferred"])


# ---- deleted chips -------------------------------------------------------------------------------------------


def test_deleted_chips_never_come_back(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    sid, _ = brass_scenario(svc)
    one = svc.shop_again(sid)
    texts = [e["text"] for e in one["profile"]["inferred"]]
    assert f"seller: {PREMIUM_SELLER}" in texts and "shape: open" in texts
    for text in (f"seller: {PREMIUM_SELLER}", "shape: open"):
        out = svc.remove_preference(sid, "inferred", text)
        assert text not in [e["text"] for e in out["profile"]["inferred"]]
    two = svc.shop_again(sid)
    after = [e["text"] for e in two["profile"]["inferred"]]
    assert f"seller: {PREMIUM_SELLER}" not in after and "shape: open" not in after
    assert (
        PREMIUM_SELLER not in two["profile_summary"]
        and "Shape is open" not in two["profile_summary"]
    )
    assert all(PREMIUM_SELLER.lower() not in q.lower() for q in two["search"]["queries"])


def test_reapply_removed_filters_every_part_of_the_profile() -> None:
    adj = QueryAdjustments(
        search_text="walnut table", style_terms=["walnut"], material_terms=["walnut", "oak"],
        must_have=[], avoid=["glare"], price_max=None, price_note="",
    )  # fmt: skip
    profile = StoredProfile(
        likes=["walnut finish"], dislikes=["glare"], guesses=["industrial"], adjustments=adj,
        inferred=[ProfileEntry(text="walnut grain", attr_kind="material", polarity="like")],
    )  # fmt: skip
    out = reapply_removed(profile, ["walnut", "glare", "industrial"])
    assert out.likes == [] and out.dislikes == [] and out.guesses == [] and out.inferred == []
    assert out.adjustments.material_terms == ["oak"] and out.adjustments.search_text == ""
    assert out.removed == ["walnut", "glare", "industrial"]


def test_evidence_tooltips_are_plain_text_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evil = '<img src=x onerror=alert(1)> Brushed Brass "Table"'
    items = [
        cand(f"e{i}", 90.0 + i, title=f"{evil} {i}", seller="Evil <b>Shop</b>") for i in range(8)
    ]
    use_client(monkeypatch, RouterClient())
    search = FakeSearch([items])
    svc = make_service(tmp_path, search, FakeLookup())
    first = svc.search(None, "tables")
    sid = first["session_id"]
    for r in first["search"]["results"][:2]:
        svc.rate(sid, r["id"], "love")
    search.sequence = [[cand(f"z{i}", 80.0) for i in range(6)]]
    view = svc.shop_again(sid)
    entries = view["profile"]["inferred"]
    assert entries and all(isinstance(t, str) for e in entries for t in e["evidence_titles"])
    assert any("<img" in t for e in entries for t in e["evidence_titles"])  # kept as data
    code = (STATIC_DIR / "app.js").read_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in code
    assert "tip-line" in code and "role: 'tooltip'" in code
    tip_block = code[code.index("kids.push(h('span', { class: 'tip'") :][:400]
    assert "[line]" in tip_block  # each line becomes a text node


# ---- images --------------------------------------------------------------------------------------------------------


class FailingThumbs:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def fetch(self, url: str) -> ThumbResult:
        self.urls.append(url)
        return ThumbResult(False, "timed out", "img.example")


def test_image_selection_limits_and_order() -> None:
    sess = Session(session_id="x" * 16, created_at="t", updated_at="t")
    from app.shop.store import RatingRecord

    plan = [("love", 8), ("like", 8), ("dislike", 6)]
    n = 0
    for rating, count in plan:
        for _ in range(count):
            n += 1
            rid = f"{rating}{n}"
            sess.results[rid] = ShopResult(
                id=rid, title=f"T {rid}", price=10.0, currency="USD", seller="S",
                thumbnail_url=f"https://img.example/{rid}.jpg", link="", link_kind="none",
                size_hint=None, product_id=rid, rank=1, pool_position=1,
            )  # fmt: skip
            sess.ratings[rid] = RatingRecord(rating=rating, rated_at=f"{n:04d}")  # type: ignore[arg-type]
    sess.results["http1"] = sess.results["love1"].model_copy(
        update={"id": "http1", "thumbnail_url": "http://insecure.example/x.jpg"}
    )
    sess.ratings["http1"] = RatingRecord(rating="love", rated_at="9999")
    sess.results["none1"] = sess.results["love1"].model_copy(
        update={"id": "none1", "thumbnail_url": ""}
    )
    sess.ratings["none1"] = RatingRecord(rating="love", rated_at="9998")
    rows = select_image_rows(sess)
    assert [r.rating for r in rows].count("love") == 6
    assert [r.rating for r in rows].count("like") == 6
    assert [r.rating for r in rows].count("dislike") == 4
    order = [r.rating for r in rows]
    assert order == sorted(order, key={"love": 0, "like": 1, "dislike": 2}.get)  # type: ignore[arg-type]
    assert all(r.thumbnail_url.startswith("https://") for r in rows)  # http and empty ones skipped
    assert rows[0].id == "love8"  # newest rating first within a rating (http1 and none1 skipped)


def test_images_are_attached_when_they_can_be_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = RouterClient()
    use_client(monkeypatch, client)
    search = FakeSearch([[cand(f"a{i}", 40.0 + i, title=f"Walnut Table {i}") for i in range(10)]])
    svc = make_service(tmp_path, search, FakeLookup())
    first = svc.search(None, "tables")
    sid = first["session_id"]
    for r in first["search"]["results"][:3]:
        svc.rate(sid, r["id"], "love")
    search.sequence = [[cand(f"n{i}", 40.0, title=f"Walnut N{i}") for i in range(10)]]
    again = svc.shop_again(sid)
    prefs_call = next(c for c in client.calls if "PreferenceTags" in json.dumps(c["output_config"]))
    blocks = prefs_call["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["image", "image", "image", "text"]  # images first
    assert again["profile"]["images"] == {"offered": 3, "used": 3, "failed": 0, "reasons": []}
    assert "image 1" in blocks[-1]["text"] and "image 3" in blocks[-1]["text"]
    cues = [e["text"] for e in again["profile"]["inferred"] if e["attr_kind"] == "quality_cue"]
    assert cues == ["visible grain"]  # a cue only the photos show, and three loved items show it


def test_a_failed_image_fetch_falls_back_to_the_title_and_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = RouterClient()
    use_client(monkeypatch, client)
    search = FakeSearch([[cand(f"a{i}", 40.0 + i, title=f"Walnut Table {i}") for i in range(10)]])
    thumbs = FailingThumbs()
    svc = make_service(tmp_path, search, FakeLookup())
    svc.thumbnailer = thumbs
    first = svc.search(None, "tables")
    sid = first["session_id"]
    for r in first["search"]["results"][:3]:
        svc.rate(sid, r["id"], "love")
    search.sequence = [[cand(f"n{i}", 40.0, title=f"Walnut N{i}") for i in range(10)]]
    again = svc.shop_again(sid)
    prefs_call = next(c for c in client.calls if "PreferenceTags" in json.dumps(c["output_config"]))
    content = prefs_call["messages"][0]["content"]
    assert isinstance(content, str) and "image none" in content  # title only
    images = again["profile"]["images"]
    assert (images["offered"], images["used"], images["failed"]) == (3, 0, 3)
    assert images["reasons"] and "timed out" in images["reasons"][0]
    assert "https://" not in json.dumps(images)  # reasons name a host and a reason, never a url
    assert len(thumbs.urls) == 3 and all(u.startswith("https://") for u in thumbs.urls)
    assert again["search"]["kind"] == "shop_again" and again["search"]["results"]  # carried on
    assert not any(
        e["attr_kind"] == "quality_cue" for e in again["profile"]["inferred"]
    )  # no photo


def test_the_image_fetch_uses_the_guarded_thumbnailer() -> None:
    import app.shop.prefs as prefs
    from app.thumbnails import HttpThumbnailer

    code = (ROOT / "src" / "app" / "shop" / "prefs.py").read_text()
    assert "fetch_traced(thumbnailer" in code and "requests." not in code and "httpx" not in code
    assert prefs.fetch_traced.__module__ == "app.thumbnails"
    hit = HttpThumbnailer(resolver=lambda host: ["10.0.0.5"]).fetch("https://img.example/a.jpg")
    assert not hit.ok and hit.reason == "address not public"  # the guard is the existing one
    assert not HttpThumbnailer().fetch("http://img.example/a.jpg").ok


# ---- queries, the pool, the caps -----------------------------------------------------------------------------


def entry(kind: str, text: str, weight: int = 3, pol: str = "like") -> ProfileEntry:
    dim = (
        kind
        if kind in ("shape", "material", "color_finish", "frame_or_legs", "frame_color", "style")
        else ""
    )
    return ProfileEntry(
        text=text, attr_kind=kind, polarity=pol, weight=weight, dimension=dim,
        values=[text] if dim else [],
    )  # type: ignore[arg-type]  # fmt: skip


def test_three_phrasings_adjusted_original_attributes_and_seller() -> None:
    base = query(
        request="coffee tables with storage", search_text="coffee table with storage",
        category="coffee table", must_have=["storage"],
    )  # fmt: skip
    profile = StoredProfile(
        inferred=[
            entry("color_finish", "brushed brass", 6),
            entry("material", "walnut", 3),
            entry("shape", "drum", 3),
            entry("shape", "lift-top", 1),
            entry("style", "modern", 9),  # not a material, finish or shape: not in the phrasing
            entry("seller", "seller: West Elm", 6),
        ]
    )
    adjusted = apply_adjustments(base, profile)
    queries = build_queries(base, profile, adjusted)
    assert queries == [
        "coffee table with storage",
        "coffee table brushed brass drum walnut",
        "West Elm coffee table storage",
    ]
    assert build_queries(base, StoredProfile(), base) == ["coffee table with storage"]
    no_seller = StoredProfile(inferred=[entry("material", "walnut")])
    assert build_queries(base, no_seller, base) == [
        "coffee table with storage",
        "coffee table walnut",
    ]


def test_phrasings_never_carry_price_words_or_repeat() -> None:
    base = query(request="sofa under $500", search_text="sofa", category="sofa")
    profile = StoredProfile(inferred=[entry("material", "velvet under $300")])
    out = build_queries(base, profile, base)
    assert out == ["sofa", "sofa velvet"]
    same = StoredProfile(adjustments=QueryAdjustments(
        search_text="Sofa", style_terms=[], material_terms=[], must_have=[], avoid=[],
        price_max=None, price_note="",
    ))  # fmt: skip
    assert build_queries(base, same, apply_adjustments(base, same)) == ["Sofa"]


def two_loved_brass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ShopService, FakeSearch, RouterClient, str]:
    client = RouterClient()
    use_client(monkeypatch, client)
    items = [cand(f"a{i}", 40.0 + i, title=f"Brushed Brass Table {i}") for i in range(30)]
    search = FakeSearch([items])
    svc = make_service(tmp_path, search, FakeLookup())
    first = svc.search(None, "coffee tables")
    sid = first["session_id"]
    for r in first["search"]["results"][:2]:
        svc.rate(sid, r["id"], "love")
    return svc, search, client, sid


SHAPES_CYCLE = ["Round", "Rectangular", "Oval"]
MATERIALS_CYCLE = ["Walnut", "Glass", "Marble", "Black Steel", "Oak"]


def pages_of(size: int, count: int = 7) -> list[list[Any]]:
    """Pages of varied candidates (three shapes, four materials), so no guard trims the list."""
    return [
        [
            cand(
                f"{p}{i}",
                60.0 + i,
                title=f"{SHAPES_CYCLE[i % 3]} {MATERIALS_CYCLE[i % 5]} Table {p.upper()}{i}",
                seller=f"S{i % 9}",
            )
            for i in range(size)
        ]
        for p in "pqrstuvwx"[:count]
    ]


def test_extra_pages_stop_once_the_pool_reaches_sixty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, search, client, sid = two_loved_brass(tmp_path, monkeypatch)
    calls_before = len(search.calls)
    search.sequence = pages_of(20)
    again = svc.shop_again(sid)
    assert len(search.calls) - calls_before == 3  # three pages of 20: 60 candidates, then stop
    rank_text = client.calls[-1]["messages"][0]["content"]
    pool_lines = [x for x in rank_text.splitlines() if x.startswith("- id: ")]
    assert len(pool_lines) == 60 and len(again["search"]["results"]) == 24
    assert again["search"]["serper_calls"] == 3 and again["search"]["serper_credits"] == 6
    assert len({q for q, _ in search.calls[calls_before:]}) == 1  # nothing converged: one phrasing


def test_a_thin_pool_uses_all_four_serper_calls_and_never_a_fifth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, search, client, sid = two_loved_brass(tmp_path, monkeypatch)
    calls_before = len(search.calls)
    search.sequence = pages_of(12)
    again = svc.shop_again(sid)
    assert len(search.calls) - calls_before == 4 and again["search"]["serper_calls"] == 4
    assert again["search"]["serper_credits"] == 8
    assert again["search"]["provider_usd"] == pytest.approx(0.008)
    assert len(search.sequence) == 3  # four of the seven pages were fetched
    rank_text = client.calls[-1]["messages"][0]["content"]
    assert len([x for x in rank_text.splitlines() if x.startswith("- id: ")]) == 48
    assert len(again["search"]["results"]) == 24


def test_the_rank_pool_is_capped_at_eighty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config(shop_again_max_serper_calls=6)
    client = RouterClient()
    use_client(monkeypatch, client)
    search = FakeSearch(
        [[cand(f"a{i}", 40.0 + i, title=f"Brushed Brass Table {i}") for i in range(30)]]
    )
    svc = make_service(tmp_path, search, FakeLookup(), cfg)
    first = svc.search(None, "coffee tables")
    sid = first["session_id"]
    for r in first["search"]["results"][:2]:
        svc.rate(sid, r["id"], "love")
    search.sequence = pages_of(45, 4)  # more candidates than the ranker is shown
    svc.shop_again(sid)
    rank_text = client.calls[-1]["messages"][0]["content"]
    assert len([x for x in rank_text.splitlines() if x.startswith("- id: ")]) == 80


def test_the_call_cap_is_configurable_and_never_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(shop_again_max_serper_calls=2)
    use_client(monkeypatch, RouterClient())
    search = FakeSearch([[cand(f"a{i}", 40.0 + i, title=f"Brass Table {i}") for i in range(30)]])
    svc = make_service(tmp_path, search, FakeLookup(), cfg)
    first = svc.search(None, "tables")
    sid = first["session_id"]
    for r in first["search"]["results"][:2]:
        svc.rate(sid, r["id"], "love", "the brass")
    search.sequence = [
        [cand(f"{p}{i}", 50.0, title=f"Brass {p}{i}") for i in range(20)] for p in "pqrs"
    ]
    again = svc.shop_again(sid)
    assert again["search"]["serper_calls"] == 2
    assert Config().shop_again_max_serper_calls == 4


def test_the_call_cap_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_SHOP_AGAIN_MAX_SERPER_CALLS", raising=False)
    assert Config.from_env().shop_again_max_serper_calls == 4
    monkeypatch.setenv("APP_SHOP_AGAIN_MAX_SERPER_CALLS", "3")
    assert Config.from_env().shop_again_max_serper_calls == 3
    with pytest.raises(ValueError):
        Config(shop_again_max_serper_calls=0)


def test_a_repeated_phrasing_starts_after_the_pages_already_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_client(monkeypatch, RouterClient())
    search = FakeSearch([[cand(f"a{i}", 40.0, title=f"Table {i}") for i in range(10)], []])
    svc = make_service(tmp_path, search, FakeLookup())
    first = svc.search(None, "boho curtains")
    sid = first["session_id"]
    assert [p for _, p in search.calls] == [1, 2]  # the typed search took two pages
    svc.rate(sid, first["search"]["results"][0]["id"], "love")
    search.sequence = [[cand(f"n{i}", 40.0, title=f"New {i}") for i in range(5)]]
    svc.shop_again(sid)
    assert [p for _, p in search.calls][2:] == [3, 4]  # the same words: pages 3 and on, not 1 again
    stored = svc.store.load(sid).searches
    assert stored[0].pages == {"boho curtains": 2} and stored[1].pages == {"boho curtains": 4}


def test_seller_cap_is_four_normally_half_for_an_affinity_seller_and_most_when_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_client(monkeypatch, RouterClient())
    shown = [cand(f"f{i}", 60.0, title=f"Brass Shown {i}", seller="FavShop") for i in range(2)]
    shown += [cand(f"o{i}", 60.0, title=f"Other Shown {i}", seller=f"Other{i}") for i in range(10)]
    search = FakeSearch([shown])
    svc = make_service(tmp_path, search, FakeLookup())
    first = svc.search(None, "tables")
    sid = first["session_id"]
    by_title = {r["title"]: r["id"] for r in first["search"]["results"]}
    svc.rate(sid, by_title["Brass Shown 0"], "love")
    svc.rate(sid, by_title["Brass Shown 1"], "love")

    def fresh(tag: str) -> list[Any]:
        out = [
            cand(f"{tag}f{i}", 60.0, title=f"Fav {tag} {i}", seller="FavShop") for i in range(22)
        ]
        return out + [
            cand(f"{tag}o{i}", 60.0, title=f"Other {tag} {i}", seller="OtherShop")
            for i in range(12)
        ]

    search.sequence = [fresh("a")]
    again = svc.shop_again(sid)
    sellers = [r["seller"] for r in again["search"]["results"]]
    assert sellers.count("FavShop") == 12  # an affinity seller: 50% of a batch of 24
    assert sellers.count("OtherShop") == 4  # everyone else: four
    assert again["search"]["seller_share"] == {"seller": "FavShop", "share": 0.75}
    stored = svc.store.load(sid).profile
    assert stored is not None and any(
        e.attr_kind == "seller" and not e.pinned for e in stored.inferred
    )
    # pin the seller: it may now take 80% of a batch (19 of 24); the pin is stored in the session
    pinned = svc.pin_preference(sid, "seller: FavShop")
    assert pinned["pinned"] is True
    assert svc.store.load(sid).profile.pinned_sellers == ["favshop"]  # type: ignore[union-attr]
    assert svc.session_view(sid)["profile"]["inferred"][0]["pinned"] is True
    search.sequence = [fresh("b")]
    again2 = svc.shop_again(sid)
    sellers2 = [r["seller"] for r in again2["search"]["results"]]
    assert sellers2.count("FavShop") == 19 and sellers2.count("OtherShop") == 4
    assert svc.store.load(sid).profile.pinned_sellers == ["favshop"]  # type: ignore[union-attr]
    unpinned = svc.pin_preference(sid, "seller: FavShop")  # clicking again unpins
    assert unpinned["pinned"] is False and svc.store.load(sid).profile.pinned_sellers == []  # type: ignore[union-attr]
    with pytest.raises(ApiError) as err:
        svc.pin_preference(sid, "seller: Nobody")
    assert err.value.status == 404


# ---- the summary line, the measure -------------------------------------------------------------------------------


def test_summary_line_reflects_inferred_likes_seller_and_band() -> None:
    band = ProfileEntry(
        text="upper range, about $600 to $1,000",
        attr_kind="price_band",
        polarity="like",
        band="upper",
    )
    profile = StoredProfile(
        inferred=[
            entry("color_finish", "brushed brass"),
            entry("material", "wood and metal"),
            entry("seller", "seller: West Elm"),
            band,
            entry("color_finish", "dark stain", pol="dislike"),
        ]
    )
    assert profile_summary(profile) == (
        "Looking for more brushed brass and wood and metal pieces from West Elm in the upper price "
        "range, fewer dark stain."
    )
    only_seller = StoredProfile(inferred=[entry("seller", "seller: West Elm")])
    assert profile_summary(only_seller) == "Looking for more pieces from West Elm."
    lower = ProfileEntry(
        text="lower range, about $0 to $100", attr_kind="price_band", polarity="like", band="lower"
    )
    assert profile_summary(StoredProfile(inferred=[lower])).endswith("in the lower price range.")
    stated = StoredProfile(
        likes=["storage"], inferred=[entry("material", "walnut")], dislikes=["glare"]
    )
    assert profile_summary(stated) == "Looking for more storage and walnut, fewer glare."
    assert profile_summary(StoredProfile()) == "" and profile_summary(None) == ""


def result(rid: str, title: str, seller: str = "S", price: float | None = 100.0) -> ShopResult:
    return ShopResult(
        id=rid, title=title, price=price, currency="USD", seller=seller, thumbnail_url="",
        link="", link_kind="none", size_hint=None, product_id=rid, rank=1, pool_position=1,
    )  # fmt: skip


def test_alignment_counts_inferred_likes_by_title_seller_or_price_band() -> None:
    profile = StoredProfile(
        inferred=[
            entry("color_finish", "brass"),
            entry("seller", "seller: West Elm"),
            ProfileEntry(
                text="upper range, about $600 to $1,000",
                attr_kind="price_band",
                polarity="like",
                low=600.0,
                high=1000.0,
                band="upper",
            ),
            entry("material", "glass", pol="dislike"),
        ],
        dislikes=["flimsy"],
    )
    results = [
        result("1", "Brushed Brass Table"),  # title
        result("2", "Plain Table", seller="west elm"),  # seller
        result("3", "Plain Table", price=800.0),  # band
        result("4", "Plain Table", price=300.0),  # nothing
        result("5", "Glass Brushed Brass Table"),  # a like but also a dislike
        result("6", "Flimsy Table", seller="West Elm"),  # a like but a stated dislike
        result("7", "Brass Table"),  # the finish value brass
    ]
    assert alignment_counts(results, profile) == (4, 7)
    assert preference_alignment(results, profile) == pytest.approx(4 / 7, abs=1e-4)


def test_alignment_ignores_guesses_stated_likes_and_empty_profiles() -> None:
    results = [result("1", "Industrial Walnut Table")]
    only_stated = StoredProfile(likes=["walnut"], guesses=["industrial"])
    assert alignment_counts(results, only_stated) == (0, 1)
    assert preference_alignment([], StoredProfile(inferred=[entry("material", "walnut")])) == 0.0
    assert preference_alignment(results, None) == 0.0
    assert alignment_counts([], None) == (0, 0)


def test_text_matching_needs_both_words_of_a_two_word_phrase() -> None:
    assert text_matches("brushed brass", "Brushed Brass Table")
    assert not text_matches("brushed brass", "Brass Table")
    assert text_matches("walnut", "Solid Walnut Desk")
    assert not text_matches("", "anything")
    assert text_matches("lift-top", "Lift Top Coffee Table")


# ---- lookups only on Open ----------------------------------------------------------------------------------------------


def test_loving_runs_no_lookup_and_open_runs_one_cached_lookup(tmp_path: Path) -> None:
    cfg = config()
    search, lookup = build_shop_providers(cfg)
    svc = ShopService(cfg, SessionStore(tmp_path / "shop-data"), search, lookup)
    view = svc.search(None, COFFEE)
    sid, res = view["session_id"], view["search"]["results"]
    svc.rate(sid, res[0]["id"], "love")
    svc.rate(sid, res[1]["id"], "like")
    svc.shop_again(sid)
    assert lookup.product_ids == []  # loving, liking and Shop Again looked nothing up
    assert svc.session_view(sid)["lookups_used"] == 0
    opened = svc.resolve(sid, res[0]["id"])
    assert opened["lookups_used"] == 1 and len(lookup.product_ids) == 1
    svc.resolve(sid, res[0]["id"])  # cached: never twice
    assert len(lookup.product_ids) == 1
    loved = svc.session_view(sid)
    assert loved["resolved"][res[0]["id"]]["link_kind"] in ("retailer_product", "google_panel")


def test_a_loved_item_keeps_its_google_link_until_opened(tmp_path: Path) -> None:
    cfg = config()
    search, lookup = build_shop_providers(cfg)
    svc = ShopService(cfg, SessionStore(tmp_path / "shop-data"), search, lookup)
    view = svc.search(None, COFFEE)
    sid, res = view["session_id"], view["search"]["results"]
    svc.rate(sid, res[0]["id"], "love")
    saved = svc.session_view(sid)["saved"][0]
    assert saved["link_kind"] == "google_panel" and saved["link"].startswith(
        "https://www.google.com/"
    )
    assert res[0]["id"] not in svc.session_view(sid)["resolved"]


def test_the_page_calls_resolve_only_from_open() -> None:
    code = (STATIC_DIR / "app.js").read_text()
    start = code.index("function setRating(")
    end = code.index("function saveComment(")
    assert "/api/resolve" not in code[start:end] and "resolve(" not in code[start:end]
    open_fn = code[code.index("function openResult(") : code.index("function renderCard(")]
    assert "resolve(id" in open_fn  # Open is where the lookup happens


def test_the_live_cost_text_says_only_open_looks_things_up() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("shop_script", ROOT / "scripts" / "shop.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = "\n".join(module.cost_lines(config()))
    assert "none when you love a result" in text and "Only Open runs one SerpApi lookup" in text
    assert "Or Love" not in text and "or Love" not in text
    assert "Shop Again: up to 4 Serper calls (8 credits, $0.0080)" in text
    assert "16 small product photos" in text and "up to 80 candidates" in text
    for doc in ("SPEC.md", "docs/adr/0012-two-modes-shop-first.md"):
        body = (ROOT / doc).read_text()
        assert "only on Open or Love" not in body, doc


# ---- cost, validation, security -----------------------------------------------------------------------------------------------


def test_images_add_input_tokens_to_the_preference_call(tmp_path: Path) -> None:
    def run(sub: str, thumbs: Any) -> float:
        svc = mock_service(tmp_path / sub)
        svc.thumbnailer = thumbs
        sid, _ = brass_scenario(svc)
        return float(svc.shop_again(sid)["search"]["anthropic_usd"])

    with_images = run("a", svc_thumbs())
    without = run("b", None)
    assert with_images > without > 0  # the mock bills 90 input tokens per image


def svc_thumbs() -> Any:
    from app.thumbnails import MockThumbnailer

    return MockThumbnailer()


def test_remove_endpoint_accepts_the_four_kinds_and_rejects_others(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    app = ShopApp(svc, port=8765)
    hdrs = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}

    def post(payload: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        return app.handle(
            "POST", "/api/preference/remove", headers or hdrs, json.dumps(payload).encode()
        )

    sid, _ = brass_scenario(svc)
    view = svc.shop_again(sid)
    texts = [e["text"] for e in view["profile"]["inferred"]]
    assert post({"session_id": sid, "kind": "love", "phrase": texts[0]}).status == 400
    assert post({"session_id": sid, "kind": "inferred", "phrase": "no such"}).status == 404
    assert post({"session_id": sid, "kind": "inferred", "phrase": "x" * 81}).status == 400
    assert post({"session_id": sid, "kind": "guess", "phrase": "nothing"}).status == 404
    assert post({"session_id": sid, "kind": "inferred", "phrase": texts[0]}).status == 200
    assert post({"session_id": sid, "kind": "inferred", "phrase": texts[0]}).status == 404  # gone
    evil = post({"session_id": sid, "kind": "inferred", "phrase": "a"}, {"Host": "evil.example"})
    assert evil.status == 403
    from app.shop.server import CSP

    assert evil.all_headers()["Content-Security-Policy"] == CSP


def test_cost_numbers_for_a_full_shop_again(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    sid, first = brass_scenario(svc)
    found = svc.shop_again(sid)["search"]
    assert found["serper_calls"] == 4 and found["serper_credits"] == 8
    assert found["provider_usd"] == pytest.approx(0.008)
    assert 0 < found["anthropic_usd"] < 0.01  # a preference call (with images) plus a rank call
    assert found["total_usd"] == pytest.approx(found["anthropic_usd"] + 0.008)
    view = svc.session_view(sid)
    assert view["lookups_used"] == 0
    assert view["session_usd"] == pytest.approx(first_total(svc, sid), abs=1e-6)


def first_total(svc: ShopService, sid: str) -> float:
    s = svc.store.load(sid)
    return sum(r.total_usd for r in s.searches) + s.lookup_usd


def test_shop_again_still_works_when_the_model_returns_nonsense(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = RouterClient(prefs="not json")
    use_client(monkeypatch, client)
    search = FakeSearch([[cand(f"a{i}", 40.0 + i) for i in range(30)]])
    svc = make_service(tmp_path, search, FakeLookup())
    first = svc.search(None, "tables")
    sid = first["session_id"]
    svc.rate(sid, first["search"]["results"][0]["id"], "love")
    search.sequence = [[cand(f"n{i}", 45.0) for i in range(30)]]
    again = svc.shop_again(sid)
    assert len(again["search"]["results"]) == 24
    assert any("Could not read the items" in w for w in again["search"]["warnings"])
    assert again["profile"]["inferred"] == []


def test_old_profiles_without_the_new_fields_still_load(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    sid, _ = brass_scenario(svc)
    svc.shop_again(sid)
    path = tmp_path / "shop-data" / f"{sid}.json"
    raw = json.loads(path.read_text())
    for key in ("inferred", "guesses", "dropped_guesses", "images"):
        del raw["profile"][key]
    for rec in raw["searches"]:
        for key in ("queries", "pages", "alignment_matches", "alignment_total"):
            rec.pop(key, None)
    path.write_text(json.dumps(raw))
    view = svc.session_view(sid)
    assert view["profile"]["inferred"] == [] and view["search"]["alignment"] is None
    assert view["search"]["queries"] == []
    again = svc.shop_again(sid)  # and Shop Again carries on from it
    assert again["search"]["kind"] == "shop_again"


def test_input_validation_for_shop_again_is_unchanged(tmp_path: Path) -> None:
    app = ShopApp(mock_service(tmp_path), port=8765)
    hdrs = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}
    assert app.handle("POST", "/api/shop-again", hdrs, b"{}").status == 400
    assert (
        app.handle("POST", "/api/shop-again", hdrs, b'{"session_id": "' + b"a" * 20 + b'"}').status
        == 404
    )
    assert app.handle("POST", "/api/shop-again", {**hdrs, "Host": "x"}, b"{}").status == 403
    assert app.handle("POST", "/api/shop-again", hdrs, b"x" * 9000).status == 413
    with pytest.raises(ApiError):
        mock_service(tmp_path / "b").shop_again("a" * 20)
