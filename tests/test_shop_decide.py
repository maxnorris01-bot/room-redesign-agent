"""Shop Mode stage 2c: the model describes, code decides.

Per-item tags from the model, preferences from `app.shop.decide`: converged, open or none per
dimension, with an exposure baseline from what was shown; dislike discipline; price as a spread; seller
and diversity limits; request chips that never change. Offline like the other Shop Mode tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.shop import thresholds
from app.shop.decide import (
    FEATURE_WORDS,
    TaggedItem,
    canon_value,
    classify_dimension,
    decide,
    dislike_values,
    exposure_shares,
    lexicon_tags,
    lexicon_value,
    price_entry,
    seller_affinity_entry,
    tag_item,
)
from app.shop.models import ImageReport, ItemTags, ProfileEntry, ShopResult, StoredProfile
from app.shop.prefs import (
    apply_adjustments,
    build_phrasings,
    merge_profile_v3,
    open_dimension,
    pin_seller,
    profile_summary,
    rated_map,
    remove_entry,
)
from app.shop.search import RESULT_COUNT, build_results
from app.shop.store import RatingRecord, Session
from app.shop.textutil import Rated
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

# ---- helpers ---------------------------------------------------------------------------------------


def make_session(
    shown: list[tuple[str, str, float | None, str]],
    ratings: dict[str, tuple[str, str]],
) -> Session:
    """A session whose results are `shown` (id, title, price, seller) and whose ratings are
    `ratings` (id -> (rating, comment)). Ratings get increasing timestamps in dict order."""
    sess = Session(session_id="x" * 16, created_at="t", updated_at="t")
    for rid, title, price, seller in shown:
        sess.results[rid] = ShopResult(
            id=rid, title=title, price=price, currency="USD", seller=seller, thumbnail_url="",
            link="", link_kind="none", size_hint=None, product_id=rid, rank=1, pool_position=1,
        )  # fmt: skip
    for n, (rid, (rating, comment)) in enumerate(ratings.items()):
        sess.ratings[rid] = RatingRecord(rating=rating, comment=comment, rated_at=f"{n:04d}")  # type: ignore[arg-type]
    return sess


def tagged(
    rid: str, rating: str, title: str, seller: str = "S", comment: str = "", **values: str
) -> TaggedItem:
    r = Rated(rid, rating, comment, title, 100.0, seller, "")
    return TaggedItem(r, {**lexicon_tags(title), **values})


def entries_by(dim: str, decisions: Any) -> list[ProfileEntry]:
    return [e for e in decisions.entries if e.dimension == dim]


def decide_session(
    sess: Session, base_terms: list[str] | None = None, removed: list[str] | None = None
) -> Any:
    return decide(rated_map(sess), {}, list(sess.results.values()), removed or [], base_terms or [])


def shape_world(
    n_round: int, n_rect: int, n_other: int = 0
) -> list[tuple[str, str, float | None, str]]:
    shown: list[tuple[str, str, float | None, str]] = []
    for i in range(n_round):
        shown.append((f"r{i}", f"Round Table {i}", 100.0 + i, f"Seller{i % 5}"))
    for i in range(n_rect):
        shown.append((f"x{i}", f"Rectangular Table {i}", 100.0 + i, f"Seller{i % 5}"))
    for i in range(n_other):
        shown.append((f"o{i}", f"Plain Table {i}", 100.0 + i, f"Seller{i % 5}"))
    return shown


# ---- the thresholds, in one place ---------------------------------------------------------------------


def test_every_threshold_is_in_one_module_with_the_documented_values() -> None:
    t = thresholds.all_thresholds()
    assert t["CONVERGE_SHARE"] == 0.70 and t["OPEN_SHARE"] == 0.30 and t["LIFT_MIN"] == 1.3
    assert t["MIN_SUPPORT_ITEMS"] == 2 and t["DISLIKE_MIN_ITEMS"] == 2
    assert t["LOVE_WEIGHT"] == 3 and t["LIKE_WEIGHT"] == 1
    assert t["DIVERSITY_MAX_SHARE"] == 0.60
    assert t["SELLER_MAX_SHARE"] == 0.50 and t["SELLER_PINNED_SHARE"] == 0.80
    assert t["PRICE_MIXED_LIKED_SPAN"] == pytest.approx(0.6667, abs=1e-4)
    assert t["RESULT_COUNT"] == RESULT_COUNT == 24
    docs = (
        Path(__file__).resolve().parents[1] / "docs" / "adr" / "0012-two-modes-shop-first.md"
    ).read_text()
    for name in t:
        assert name in docs, f"{name} is not in the ADR 0012 thresholds table"


# ---- lexicon and exposure -------------------------------------------------------------------------------


def test_the_lexicon_reads_titles_and_leaves_unknowns_out() -> None:
    assert lexicon_tags('Round Walnut Drum Storage Coffee Table 36"') == {
        "shape": "round", "material": "wood", "color_finish": "warm wood",
    }  # fmt: skip
    got = lexicon_tags("Rectangular Wood and Black Metal Lift-Top Coffee Table")
    assert got["shape"] == "rectangular" and got["material"] == "wood"
    assert got["color_finish"] == "dark" and got["frame_color"] == "dark or brass metal"
    assert lexicon_tags('Coffee Table with Storage 40"') == {}  # nothing to read
    assert lexicon_tags("Brushed Brass Drum Table")["frame_color"] == "dark or brass metal"
    assert (
        lexicon_value("shape", "ROUND table") == "round"
        and lexicon_value("shape", "background") is None
    )
    assert (
        lexicon_value("style", "Mid-Century Modern desk") == "mid-century"
    )  # the earliest word wins
    assert lexicon_value("frame_color", "Powder-Coated Steel Base") == "dark or brass metal"


def test_model_tags_are_mapped_into_the_lexicon_value_space() -> None:
    assert canon_value("material", "walnut") == "wood"
    assert canon_value("color_finish", "Walnut") == "warm wood"
    assert canon_value("frame_color", "black metal") == "dark or brass metal"
    assert canon_value("shape", "Round") == "round" and canon_value("shape", "other") is None
    assert canon_value("shape", None) is None and canon_value("material", "  ") is None
    assert (
        canon_value("size_class", "Large") == "large" and canon_value("size_class", "huge") is None
    )
    assert (
        canon_value("style", "quirky cabin vibes") == "quirky cabin vibes"
    )  # unknown stays as text


def test_exposure_is_the_share_among_titles_that_have_a_value() -> None:
    titles = ["Round A", "Round B", "Round C", "Rectangular D", "Plain E", "Plain F"]
    shares = exposure_shares(titles)
    assert shares["shape"] == {
        "round": 0.75,
        "rectangular": 0.25,
    }  # the two plain titles are unknown
    assert "color_finish" not in exposure_shares(titles)  # no title shows it: no baseline at all
    shown = [r for _, t, _, _ in shape_world(2, 1) for r in [t]]
    assert exposure_shares(shown)["shape"]["round"] == pytest.approx(2 / 3)


def test_tagging_prefers_the_model_and_falls_back_to_the_title() -> None:
    r = Rated("a", "love", "", "Round Walnut Table", 100.0, "S", "")
    model = ItemTags(
        id="a", shape=None, material="steel", color_finish=None, frame_or_legs=None,
        frame_color="brass", style="modern", size_class="small", quality_cues=["Solid Wood", "storage"],
    )  # fmt: skip
    item = tag_item(r, model)
    assert item.values["material"] == "metal"  # the model's reading wins
    assert (
        item.values["shape"] == "round" and item.values["color_finish"] == "warm wood"
    )  # from the title
    assert (
        item.values["frame_color"] == "dark or brass metal" and item.values["size_class"] == "small"
    )
    assert item.cues == ("solid wood", "storage")
    assert tag_item(r, None).values["material"] == "wood"  # no model answer: the lexicon alone


# ---- one dimension: converged, open, none -----------------------------------------------------------------


def exposure_of(round_share: float) -> dict[str, dict[str, float]]:
    return {"shape": {"round": round_share, "rectangular": 1 - round_share}}


def test_one_round_and_one_rectangular_love_make_shape_open_with_no_pressure() -> None:
    items = [tagged("a", "love", "Round Table"), tagged("b", "love", "Rectangular Table")]
    res = classify_dimension("shape", items, exposure_of(0.5))
    assert res.status == "open" and sorted(res.values) == ["rectangular", "round"]
    assert res.shares == {"round": 0.5, "rectangular": 0.5}
    # through the whole pipeline: an open chip, nothing converged, no shape in any phrasing or context
    sess = make_session(shape_world(6, 6), {"r0": ("love", ""), "x0": ("love", "")})
    prof = merge_profile_v3(
        None, None, sess, query(request="tables", category="table"), ImageReport(), set()
    )
    shape = [e for e in prof.inferred if e.dimension == "shape"]
    assert [(e.status, e.attr_kind, e.text, sorted(e.values)) for e in shape] == [
        ("open", "open", "shape: open", ["rectangular", "round"])
    ]
    assert shape[0].detail == "liked rectangular and round"
    base = query(request="tables", search_text="tables", category="table")
    queries, dropped = build_phrasings(base, prof, apply_adjustments(base, prof), 4)
    assert queries[0] == "tables" and dropped == []
    assert [q for q in queries if "round" in q.lower() or "rectangular" in q.lower()] == [
        "table rectangular",
        "table round",
    ]  # one phrasing per value; never one value in every phrasing
    assert "Shape is open: showing both rectangular and round." in profile_summary(prof)


def test_two_round_loves_from_different_sellers_converge_shape() -> None:
    shown = shape_world(3, 4, 3)  # a third of what was shown is round
    sess = make_session(shown, {"r0": ("love", ""), "r1": ("love", "")})
    prof = merge_profile_v3(None, None, sess, query(request="tables"), ImageReport(), set())
    shape = [e for e in prof.inferred if e.dimension == "shape"]
    assert [(e.status, e.text, e.polarity) for e in shape] == [("converged", "round", "like")]
    assert (
        shape[0].share == 1.0
        and shape[0].exposure == 0.4286
        and shape[0].lift == pytest.approx(2.333, abs=1e-3)
    )
    assert {r.seller for r in rated_map(sess).values()} == {"Seller0", "Seller1"}


def test_the_exposure_feedback_loop_does_not_become_a_preference() -> None:
    # 80% of what was shown is round; liking two round items is just what was on the page
    shown = shape_world(8, 2)
    sess = make_session(shown, {"r0": ("like", ""), "r1": ("like", "")})
    prof = merge_profile_v3(None, None, sess, query(request="tables"), ImageReport(), set())
    assert [e for e in prof.inferred if e.dimension == "shape"] == []
    res = classify_dimension(
        "shape", [tagged("a", "like", "Round A"), tagged("b", "like", "Round B")], exposure_of(0.8)
    )
    assert res.status == "none" and res.shares == {"round": 1.0}
    lifted = classify_dimension(
        "shape", [tagged("a", "like", "Round A"), tagged("b", "like", "Round B")], exposure_of(0.7)
    )
    assert lifted.status == "converged" and lifted.lift == pytest.approx(
        1.4286, abs=1e-3
    )  # 1.3 and over


def test_the_seventy_percent_and_thirty_percent_thresholds() -> None:
    low = exposure_of(0.2)

    def items(spec: list[tuple[str, str]]) -> list[TaggedItem]:
        return [
            tagged(f"i{n}", rating, f"{shape} Table {n}") for n, (rating, shape) in enumerate(spec)
        ]

    seventy = items(
        [
            ("love", "Round"),
            ("like", "Round"),
            ("like", "Round"),
            ("like", "Round"),
            ("like", "Round"),
            ("like", "Rectangular"),
            ("like", "Rectangular"),
            ("like", "Rectangular"),
        ]
    )
    assert classify_dimension("shape", seventy, low).status == "converged"  # 7 of 10 = 70%
    sixty_nine = items(
        [
            ("love", "Round"),
            ("like", "Round"),
            ("like", "Round"),
            ("like", "Round"),
            ("like", "Rectangular"),
            ("like", "Rectangular"),
            ("like", "Rectangular"),
        ]
    )
    res = classify_dimension("shape", sixty_nine, low)  # 6 of 9 = 66.7%, 33.3% rectangular
    assert res.status == "open" and res.values == ["round", "rectangular"]
    twenty_nine = items(
        [("love", "Round")] * 1 + [("like", "Round")] * 4 + [("like", "Rectangular")] * 2
    )
    assert classify_dimension("shape", twenty_nine, low).status == "converged"  # 7 of 9 = 77%
    barely_open = items(
        [
            ("love", "Round"),
            ("love", "Round"),
            ("like", "Rectangular"),
            ("like", "Rectangular"),
            ("like", "Rectangular"),
        ]
    )
    assert classify_dimension("shape", barely_open, low).status == "open"  # 6/9 and 3/9 = 33%
    only_twenty_five = items(
        [("love", "Round"), ("love", "Round"), ("love", "Round"), ("like", "Rectangular")]
    )
    assert classify_dimension("shape", only_twenty_five, low).status == "converged"  # 9 of 10: 90%
    under = items([("love", "Round"), ("love", "Round"), ("love", "Oval"), ("like", "Rectangular")])
    res3 = classify_dimension("shape", under, low)  # 6/10 round, 3/10 oval, 1/10 rectangular
    assert res3.status == "open" and res3.values == [
        "round",
        "oval",
    ]  # rectangular (10%) is below 30%


def test_two_supporting_items_are_needed_unless_a_loved_comment_names_it() -> None:
    low = exposure_of(0.3)
    one = [tagged("a", "love", "Round Table")]
    assert classify_dimension("shape", one, low).status == "none"  # one item is not a pattern
    commented = [tagged("a", "love", "Round Table", comment="love how round it is")]
    res = classify_dimension("shape", commented, low)
    assert (
        res.status == "converged" and res.value == "round"
    )  # the comment stands in for a 2nd item
    liked = [tagged("a", "like", "Round Table", comment="round is nice")]
    assert classify_dimension("shape", liked, low).status == "none"  # only a LOVED item may do it
    unrelated = [tagged("a", "love", "Round Table", comment="great price")]
    assert classify_dimension("shape", unrelated, low).status == "none"


def test_a_dimension_with_no_known_exposure_skips_the_lift_test() -> None:
    items = [
        tagged("a", "love", "Table", size_class="large"),
        tagged("b", "love", "Chair", size_class="large"),
    ]
    res = classify_dimension("size_class", items, {})  # size class has no lexicon, so no baseline
    assert res.status == "converged" and res.value == "large" and res.lift is None


# ---- dislike discipline -----------------------------------------------------------------------------------


def test_one_dislike_gives_no_attribute_dislike_two_sharing_a_value_do() -> None:
    shown = shape_world(3, 3) + [
        ("v0", "Oval Table 0", 100.0, "S"),
        ("v1", "Oval Table 1", 100.0, "S"),
    ]
    one = make_session(shown, {"r0": ("love", ""), "x0": ("love", ""), "v0": ("dislike", "")})
    assert [
        e for e in decide_session(one).entries if e.polarity == "dislike"
    ] == []  # excludes that item only
    two = make_session(
        shown,
        {"r0": ("love", ""), "x0": ("love", ""), "v0": ("dislike", ""), "v1": ("dislike", "")},
    )
    dislikes = [e for e in decide_session(two).entries if e.polarity == "dislike"]
    assert [(e.dimension, e.text, e.weight) for e in dislikes] == [("shape", "oval", 2)]
    assert dislikes[0].share == 1.0 and dislikes[0].lift == pytest.approx(
        4.0
    )  # 25% of what was shown
    # a liked oval item: no dislike chip, however many were disliked
    contradicted = make_session(
        shown + [("v2", "Oval Table 2", 100.0, "S")],
        {"r0": ("love", ""), "v0": ("dislike", ""), "v1": ("dislike", ""), "v2": ("like", "")},
    )
    assert [e for e in decide_session(contradicted).entries if e.polarity == "dislike"] == []
    # two dislikes sharing "round" when round is most of what was shown: no lift, no chip
    flooded = make_session(
        shape_world(9, 1), {"r0": ("dislike", ""), "r1": ("dislike", ""), "x0": ("love", "")}
    )
    assert [e for e in decide_session(flooded).entries if e.polarity == "dislike"] == []


def test_a_dislike_chip_is_dropped_when_a_loved_or_liked_item_has_the_value() -> None:
    disliked = [tagged("a", "dislike", "Oval Table A"), tagged("b", "dislike", "Oval Table B")]
    assert [v[0] for v in dislike_values("shape", disliked, [], exposure_of(0.9))] == ["oval"]
    positive = [tagged("c", "like", "Oval Table C")]
    assert dislike_values("shape", disliked, positive, exposure_of(0.9)) == []
    sess = make_session(
        shape_world(2, 2)
        + [("v0", "Oval A", 90.0, "S"), ("v1", "Oval B", 90.0, "S"), ("v2", "Oval C", 90.0, "S")],
        {
            "v0": ("dislike", ""),
            "v1": ("dislike", ""),
            "r0": ("love", ""),
            "x0": ("love", ""),
            "v2": ("like", ""),
        },
    )
    assert not [e for e in decide_session(sess).entries if e.polarity == "dislike"]


def test_sellers_are_still_deprioritized_only_after_two_dislikes_and_no_like() -> None:
    from app.shop.store import sellers_to_deprioritize

    sess = make_session(
        [
            ("a", "Table A", 90.0, "Bad"),
            ("b", "Table B", 90.0, "Bad"),
            ("c", "Table C", 90.0, "Good"),
        ],
        {"a": ("dislike", ""), "b": ("dislike", ""), "c": ("love", "")},
    )
    assert sellers_to_deprioritize(sess) == {"bad"}
    assert thresholds.SELLER_DISLIKES_TO_DEPRIORITIZE == 2


# ---- finish, frame colour, quality cues ---------------------------------------------------------------------------


def test_frame_color_converges_as_a_finish_signal() -> None:
    shown = [
        ("a", "Round Black Steel Coffee Table", 300.0, "S1"),
        ("b", "Oval Brushed Brass Coffee Table", 320.0, "S2"),
        ("c", "Round Natural Wood Frame Coffee Table", 310.0, "S3"),
        ("d", "Rectangular White Frame Coffee Table", 290.0, "S4"),
        ("e", "Square Chrome Coffee Table", 280.0, "S5"),
        ("f", "Plain Table", 270.0, "S6"),
    ]
    sess = make_session(shown, {"a": ("love", ""), "b": ("love", "")})
    got = entries_by("frame_color", decide_session(sess))
    assert [(e.text, e.status) for e in got] == [("dark or brass metal", "converged")]
    assert got[0].lift == pytest.approx(2.5)  # 2 of 5 known frame colours were dark or brass


def test_a_typed_request_term_is_never_written_back_as_a_learned_chip() -> None:
    shown = [(f"w{i}", f"Walnut Table {i}", 100.0 + i, f"S{i}") for i in range(2)] + [
        (f"g{i}", f"Glass Table {i}", 100.0 + i, f"S{i}") for i in range(6)
    ]
    sess = make_session(shown, {"w0": ("love", ""), "w1": ("love", "")})
    assert [e.text for e in entries_by("material", decide_session(sess))] == ["wood"]
    typed = decide_session(sess, base_terms=["wood"])
    assert entries_by("material", typed) == []  # the shopper already said it


def test_quality_cues_need_half_the_positives_visible_evidence_and_are_never_features() -> None:
    shown = [(f"i{i}", f"Solid Walnut Table {i}", 100.0 + i, f"S{i}") for i in range(6)]
    rated = rated_map(
        make_session(shown, {"i0": ("love", ""), "i1": ("love", ""), "i2": ("like", "")})
    )
    tags = {
        rid: ItemTags(
            id=rid, shape=None, material=None, color_finish=None, frame_or_legs=None, frame_color=None,
            style=None, size_class=None, quality_cues=["solid walnut", "storage", "lift-top storage"],
        )
        for rid in rated
    }  # fmt: skip
    out = decide(rated, tags, [], [], [], image_ids=set())
    cues = [e.text for e in out.entries if e.attr_kind == "quality_cue"]
    assert cues == ["solid walnut"]  # storage and lift-top are features
    assert {"storage", "drawer", "drawers"} <= FEATURE_WORDS
    hidden = {
        rid: t.model_copy(update={"quality_cues": ["hand finished"]}) for rid, t in tags.items()
    }
    assert [
        e for e in decide(rated, hidden, [], [], []).entries if e.attr_kind == "quality_cue"
    ] == []
    assert [
        e.text
        for e in decide(rated, hidden, [], [], [], image_ids={"i0"}).entries
        if e.attr_kind == "quality_cue"
    ] == ["hand finished"]  # a photo was sent: the cue may be visible there


# ---- price as a spread -------------------------------------------------------------------------------------------------


def priced_world(
    loved: list[float], liked: list[float], pool: list[float]
) -> tuple[Session, dict[str, Rated]]:
    shown = [(f"p{i}", f"Table {i}", price, f"S{i}") for i, price in enumerate(pool)]
    ratings: dict[str, tuple[str, str]] = {}
    for n, price in enumerate(loved):
        shown.append((f"l{n}", f"Loved Table {n}", price, "WestElm"))
        ratings[f"l{n}"] = ("love", "")
    for n, price in enumerate(liked):
        shown.append((f"k{n}", f"Liked Table {n}", price, f"K{n}"))
        ratings[f"k{n}"] = ("like", "")
    sess = make_session(shown, ratings)
    return sess, rated_map(sess)


POOL = [
    140.0,
    180,
    220,
    260,
    300,
    340,
    380,
    420,
    460,
    500,
    540,
    580,
    620,
    660,
    700,
    740,
    780,
    820,
    860,
    900,
]


def test_price_is_mixed_when_loved_is_upper_but_liked_spans_the_range() -> None:
    sess, rated = priced_world([699.0, 749, 829, 899], [140.0, 300, 520, 800], POOL)
    entry = price_entry(rated, list(sess.results.values()))
    assert entry is not None and entry.status == "mixed" and entry.attr_kind == "price_mixed"
    assert entry.text == "price: mixed"
    assert entry.detail == "loved $699 to $899, liked $140 to $800"
    base = query(request="tables", price_min=100.0, price_max=1000.0)
    prof = StoredProfile(inferred=[entry])
    out = apply_adjustments(base, prof)
    assert (out.price_min, out.price_max) == (100.0, 1000.0)  # the typed cap never changes
    assert "Price is mixed." in profile_summary(prof)
    assert [q for q in build_phrasings(base, prof, out, 4)[0] if "$" in q or "699" in q] == []


def test_price_is_a_band_when_loved_and_liked_both_cluster() -> None:
    sess, rated = priced_world([699.0, 749], [780.0, 820, 860], POOL)
    entry = price_entry(rated, list(sess.results.values()))
    assert entry is not None and entry.attr_kind == "price_band" and entry.band == "upper"
    assert entry.status == "converged" and entry.text.startswith("upper range, about $")
    sess2, rated2 = priced_world([150.0, 190], [160.0, 210], POOL)
    low = price_entry(rated2, list(sess2.results.values()))
    assert low is not None and low.band == "lower"


def test_price_says_nothing_when_loved_is_not_clustered_and_liked_is_narrow() -> None:
    sess, rated = priced_world([140.0, 900.0], [440.0, 480], POOL)
    assert price_entry(rated, list(sess.results.values())) is None
    few = make_session(
        [("a", "T", 100.0, "S"), ("b", "T2", 200.0, "S")], {"a": ("love", ""), "b": ("love", "")}
    )
    assert price_entry(rated_map(few), list(few.results.values())) is None  # too few shown prices


def test_a_mixed_price_chip_is_neutral_and_the_ranker_hears_it_is_only_a_hint() -> None:
    from app.shop.again import again_context
    from app.shop.search import rank_user_text

    sess, rated = priced_world([699.0, 749, 829, 899], [140.0, 300, 520, 800], POOL)
    prof = merge_profile_v3(None, None, sess, query(request="tables"), ImageReport(), set())
    assert any(e.status == "mixed" for e in prof.inferred)
    ctx = again_context(sess, prof, config(), ["tables"])
    assert ctx.price_line.startswith("mixed (loved $699 to $899, liked $140 to $800)")
    text = rank_user_text(
        query(request="tables"),
        [SearchCandidate("p1", "T", "S", 100.0, "USD", None, None, None)],
        24,
        ctx,
    )
    assert (
        "Price: mixed (loved $699 to $899, liked $140 to $800) (a hint about taste, never a limit"
        in text
    )


# ---- seller affinity with the lift test ---------------------------------------------------------------------------------


def test_seller_affinity_also_needs_a_lift_over_what_was_shown() -> None:
    rated = rated_map(
        make_session(
            [("a", "T", 1.0, "WestElm"), ("b", "T", 1.0, "WestElm")],
            {"a": ("love", ""), "b": ("love", "")},
        )
    )
    mostly_one = [
        ShopResult(
            id=f"s{i}",
            title="T",
            price=1.0,
            currency="USD",
            seller="WestElm",
            thumbnail_url="",
            link="",
            link_kind="none",
            size_hint=None,
            product_id=f"s{i}",
            rank=1,
            pool_position=1,
        )
        for i in range(9)
    ]
    others = [
        r.model_copy(update={"id": f"o{i}", "seller": f"Other{i}"})
        for i, r in enumerate(mostly_one[:3])
    ]
    assert (
        seller_affinity_entry(rated, mostly_one + others[:1]) is None
    )  # 90% of what was shown anyway
    spread = [
        r.model_copy(update={"id": f"z{i}", "seller": f"Z{i}"}) for i, r in enumerate(mostly_one)
    ]
    got = seller_affinity_entry(rated, spread + others[:1] + mostly_one[:1])
    assert (
        got is not None
        and got.text == "seller: WestElm"
        and got.lift is not None
        and got.lift >= 1.3
    )


# ---- phrasings: converged only, one per open value, the call cap ------------------------------------------------------------


def conv(dim: str, value: str, weight: int = 3) -> ProfileEntry:
    return ProfileEntry(
        text=value, attr_kind=dim, polarity="like", dimension=dim, values=[value], weight=weight
    )


def opened(dim: str, values: list[str], weight: int = 6) -> ProfileEntry:
    return ProfileEntry(
        text=f"{dim}: open", attr_kind="open", polarity="like", dimension=dim, status="open",
        values=values, weight=weight, detail="liked " + " and ".join(values),
    )  # fmt: skip


def seller_chip(name: str = "West Elm") -> ProfileEntry:
    return ProfileEntry(
        text=f"seller: {name}", attr_kind="seller", polarity="like", dimension="seller", weight=9
    )


BASE = query(
    request="coffee tables with storage",
    search_text="coffee table with storage",
    category="coffee table",
    must_have=["storage"],
)


def test_phrasings_use_converged_values_only_and_one_per_open_value() -> None:
    prof = StoredProfile(
        inferred=[
            conv("material", "wood"),
            conv("color_finish", "warm wood"),
            opened("shape", ["round", "rectangular"]),
            seller_chip(),
        ]
    )
    queries, dropped = build_phrasings(BASE, prof, BASE, 5)
    assert dropped == []
    assert queries == [
        "coffee table with storage",  # the typed query: no learned value, no seller name
        "West Elm coffee table storage",  # the seller
        "coffee table round walnut wood",  # one phrasing per value of the open dimension,
        "coffee table rectangular walnut wood",  # each carrying the converged words
    ]
    assert not any("West Elm" in q for q in (queries[0], *queries[2:]))  # look-alikes from anywhere
    assert (
        len([q for q in queries if "round" in q]) == 1
    )  # one open value is never in every phrasing
    assert (
        "round" not in queries[0] and "walnut" not in queries[0]
    )  # the typed phrasing is unbiased


def test_without_open_values_the_converged_words_get_their_own_phrasing() -> None:
    prof = StoredProfile(inferred=[conv("material", "wood"), conv("shape", "round"), seller_chip()])
    queries, dropped = build_phrasings(BASE, prof, BASE, 4)
    assert queries == [
        "coffee table with storage",
        "coffee table round wood",
        "West Elm coffee table storage",
    ]
    assert dropped == []


def test_when_the_cap_bites_the_lowest_priority_phrasing_is_dropped_and_recorded() -> None:
    prof = StoredProfile(
        inferred=[
            conv("material", "wood"),
            opened("shape", ["round", "rectangular"]),
            seller_chip(),
        ]
    )
    four, dropped4 = build_phrasings(BASE, prof, BASE, 4)
    assert len(four) == 4 and dropped4 == []  # typed, seller, and one phrasing per open value
    assert (
        any("West Elm" in q for q in four)
        and any("round" in q for q in four)
        and any("rectangular" in q for q in four)
    )
    three, dropped3 = build_phrasings(BASE, prof, BASE, 3)
    # the open values (kept whole or not at all) do not fit: the converged words alone take their place
    assert three == [
        "coffee table with storage",
        "coffee table wood",
        "West Elm coffee table storage",
    ]
    assert dropped3 == ["coffee table round wood", "coffee table rectangular wood"]
    two, dropped2 = build_phrasings(BASE, prof, BASE, 2)
    assert two == ["coffee table with storage", "West Elm coffee table storage"]
    assert any("round" in q for q in dropped2) and any("rectangular" in q for q in dropped2)
    one, dropped1 = build_phrasings(BASE, prof, BASE, 1)
    assert one == ["coffee table with storage"] and "West Elm coffee table storage" in dropped1


def test_an_open_dimension_without_search_words_adds_no_phrasing() -> None:
    prof = StoredProfile(
        inferred=[
            opened("style", ["modern", "boho"]),
            opened("frame_color", ["dark or brass metal", "wood"]),
        ]
    )
    assert open_dimension(prof) is None
    assert build_phrasings(BASE, prof, BASE, 4) == (["coffee table with storage"], [])
    heavier = StoredProfile(
        inferred=[opened("material", ["wood", "metal"], 9), opened("shape", ["round", "oval"], 6)]
    )
    chosen = open_dimension(heavier)
    assert chosen is not None and chosen.dimension == "material"  # the most like mass


def test_the_service_records_dropped_phrasings_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(shop_again_max_serper_calls=2)
    use_client(monkeypatch, RouterClient())
    shown = [
        cand(
            f"s{i}",
            100.0 + i,
            title=f"{'Round' if i % 2 else 'Rectangular'} Table {i}",
            seller="West Elm" if i < 2 else f"S{i}",
        )
        for i in range(12)
    ]
    search = FakeSearch([shown])
    svc = make_service(tmp_path, search, FakeLookup(), cfg)
    first = svc.search(None, "coffee tables")
    sid = first["session_id"]
    by_title = {r["title"]: r["id"] for r in first["search"]["results"]}
    svc.rate(sid, by_title["Round Table 1"], "love")
    svc.rate(sid, by_title["Rectangular Table 0"], "love")
    search.sequence = [[cand(f"n{i}", 100.0, title=f"Plain Table N{i}") for i in range(10)]]
    again = svc.shop_again(sid)
    assert again["search"]["serper_calls"] <= 2
    assert again["search"]["dropped_phrasings"]
    assert any(
        "search phrasing(s) to stay within the 2-call cap" in w for w in again["search"]["warnings"]
    )


# ---- ranking context ----------------------------------------------------------------------------------------------------------


def test_the_ranker_is_told_what_converged_what_is_open_and_how_to_treat_guesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = RouterClient()
    use_client(monkeypatch, client)
    mixed = [
        cand(f"s{i}", 100.0 + i, title=f"{'Round' if i % 2 else 'Rectangular'} Walnut Table {i}")
        for i in range(12)
    ]
    search = FakeSearch([mixed])
    svc = make_service(tmp_path, search, FakeLookup())
    first = svc.search(None, "coffee tables")
    sid = first["session_id"]
    for title in ("Round Walnut Table 1", "Rectangular Walnut Table 0"):
        svc.rate(sid, {r["title"]: r["id"] for r in first["search"]["results"]}[title], "love")
    search.sequence = [
        [
            cand(f"n{i}", 90.0, title=f"{'Round' if i % 2 else 'Rectangular'} Oak Table N{i}")
            for i in range(30)
        ]
    ]
    svc.shop_again(sid)
    text = client.calls[-1]["messages"][0]["content"]
    assert (
        "Open dimensions (the shopper likes several values; keep them mixed, no pressure): shape: rectangular, round"
        in text
    )
    assert "Converged preferences (learned from the rated items; apply them): none" in text
    assert client.calls[-1]["system"].startswith("You order real shopping results")
    assert (
        "keep them mixed" in client.calls[-1]["system"]
        or "open dimensions" in client.calls[-1]["system"]
    )


# ---- the diversity guard and the seller caps -------------------------------------------------------------------------------------


def candidate(i: int, title: str, seller: str = "S") -> SearchCandidate:
    return SearchCandidate(
        f"c{i}", title, seller, 100.0, "USD", None, None, f"https://www.google.com/p/{i}"
    )


def ordered(candidates: list[SearchCandidate]) -> list[tuple[SearchCandidate, str]]:
    return [(c, "") for c in candidates]


def test_no_single_known_value_may_exceed_sixty_percent() -> None:
    pool = [candidate(i, f"Round Table {i}", f"S{i}") for i in range(30)]
    pool += [candidate(100 + i, f"Rectangular Table {i}", f"R{i}") for i in range(12)]
    pool += [candidate(200 + i, f"Plain Table {i}", f"P{i}") for i in range(6)]  # unknown shape
    index = {c.product_id: n for n, c in enumerate(pool, 1)}
    results, capped, skipped = build_results(
        ordered(pool), query(request="t"), index, guard_dims=["shape"]
    )
    shapes = [lexicon_value("shape", r.title) for r in results]
    assert len(results) == 24 and shapes.count("round") == 14
    assert shapes.count("round") <= int(0.6 * RESULT_COUNT) == 14
    assert skipped == 16  # the 16 round items past the 14th were passed over
    assert (
        shapes.count("rectangular") + shapes.count(None) == 24 - 14
    )  # the next best of other values
    # replacing keeps rank order inside each value
    kept_round = [r.title for r in results if lexicon_value("shape", r.title) == "round"]
    assert kept_round == [f"Round Table {i}" for i in range(14)]


def test_a_converged_dimension_is_not_capped() -> None:
    pool = [candidate(i, f"Round Table {i}", f"S{i}") for i in range(30)]
    index = {c.product_id: n for n, c in enumerate(pool, 1)}
    unguarded, _, skipped = build_results(ordered(pool), query(request="t"), index, guard_dims=[])
    assert len(unguarded) == 24 and skipped == 0  # converged on round: round fills the list
    guarded, _, skipped2 = build_results(
        ordered(pool), query(request="t"), index, guard_dims=["shape"]
    )
    assert len(guarded) == 14 and skipped2 == 16  # a pool of one value cannot satisfy the guard


def test_the_guard_that_cannot_be_met_says_so_and_returns_what_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_client(monkeypatch, RouterClient())
    first_items = [
        cand(f"a{i}", 100.0 + i, title=f"Round Walnut Table {i}", seller=f"S{i}") for i in range(30)
    ]
    search = FakeSearch([first_items])
    svc = make_service(tmp_path, search, FakeLookup())
    first = svc.search(None, "coffee tables")
    sid = first["session_id"]
    svc.rate(sid, first["search"]["results"][0]["id"], "like")
    search.sequence = [
        [cand(f"n{i}", 100.0 + i, title=f"Round Oak Table N{i}", seller=f"T{i}") for i in range(40)]
    ]
    again = svc.shop_again(sid)
    found = again["search"]
    assert len(found["results"]) == 14  # what exists under the guard
    assert found["diversity"]["shape"] == {"value": "round", "share": 1.0}
    assert any(
        "Could not keep shape mixed: the pool is mostly round (100% of these results)." in w
        for w in found["warnings"]
    )


def test_the_largest_share_per_dimension_is_logged(
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
    fresh = []
    for i in range(30):
        shape = ["Round", "Rectangular", "Oval"][i % 3]
        material = ["Walnut", "Glass", "Marble", "Steel"][i % 4]
        fresh.append(
            cand(f"n{i}", 100.0 + i, title=f"{shape} {material} Table N{i}", seller=f"T{i % 8}")
        )
    search.sequence = [fresh]
    again = svc.shop_again(sid)
    div = again["search"]["diversity"]
    assert (
        set(div) >= {"shape", "material"}
        and div["shape"]["share"] <= 0.6
        and div["material"]["share"] <= 0.6
    )
    assert again["search"]["seller_share"]["share"] <= 0.5


def test_the_seller_caps_are_four_half_and_four_fifths() -> None:
    pool = [candidate(i, f"Table {i}", "Fav") for i in range(30)] + [
        candidate(100 + i, f"Other {i}", "Shop") for i in range(10)
    ]
    index = {c.product_id: n for n, c in enumerate(pool, 1)}
    plain, _, _ = build_results(ordered(pool), query(request="t"), index)
    assert [r.seller for r in plain].count("Fav") == 4 and [r.seller for r in plain].count(
        "Shop"
    ) == 4
    half, _, _ = build_results(ordered(pool), query(request="t"), index, affinity_sellers={"fav"})
    assert [r.seller for r in half].count("Fav") == 12 == int(thresholds.SELLER_MAX_SHARE * 24)
    pinned, _, _ = build_results(
        ordered(pool), query(request="t"), index, affinity_sellers={"fav"}, pinned_sellers={"fav"}
    )
    assert [r.seller for r in pinned].count("Fav") == 19 == int(thresholds.SELLER_PINNED_SHARE * 24)


# ---- request chips, recompute, deleted chips --------------------------------------------------------------------------------------------


def test_request_chips_are_unchanged_after_two_shop_agains(tmp_path: Path) -> None:
    from app.shop.providers import build_shop_providers
    from app.shop.service import ShopService
    from app.shop.store import SessionStore

    cfg = config()
    search, lookup = build_shop_providers(cfg)
    svc = ShopService(cfg, SessionStore(tmp_path / "shop-data"), search, lookup)
    view = svc.search(None, "coffee tables with a mix of wood and metal with storage")
    sid, first = view["session_id"], view["search"]["results"]
    typed_query = view["search"]["query"]
    svc.rate(sid, first[0]["id"], "love", "love the walnut look, too dark though")
    svc.rate(sid, first[1]["id"], "love")
    svc.rate(sid, first[2]["id"], "like")
    one = svc.shop_again(sid)
    two = svc.shop_again(sid)
    for again in (one, two):
        assert again["search"]["query"] == typed_query  # what the chips show
        assert again["search"]["query"]["style_terms"] == typed_query["style_terms"]
        assert again["search"]["query"]["material_terms"] == ["wood", "metal"]
        assert again["search"]["query"]["must_have"] == ["storage"]
    used = two["search"]["used_query"]
    assert (
        "dark" in used["avoid"] and "dark" not in two["search"]["query"]["avoid"]
    )  # learned: used, not shown
    assert svc.store.load(sid).searches[-1].query == svc.store.load(sid).searches[0].query


def test_the_same_ratings_give_the_same_profile_in_any_order() -> None:
    shown = shape_world(4, 4, 4) + [
        (f"w{i}", f"Walnut Round Table {i}", 600.0 + i, "WestElm") for i in range(3)
    ]
    ratings = {
        "w0": ("love", ""),
        "w1": ("love", "walnut"),
        "r1": ("like", ""),
        "x1": ("like", ""),
        "x2": ("dislike", ""),
        "o1": ("dislike", ""),
    }
    base = query(request="tables")
    first = merge_profile_v3(None, None, make_session(shown, ratings), base, ImageReport(), set())
    reordered = dict(reversed(list(ratings.items())))
    second = merge_profile_v3(
        None, None, make_session(shown, reordered), base, ImageReport(), set()
    )
    dump = lambda p: json.dumps([e.model_dump() for e in p.inferred], sort_keys=True)  # noqa: E731
    assert dump(first) == dump(second) and first.inferred  # the same, and not empty
    # recomputing from the same ratings again changes nothing (no incremental drift)
    third = merge_profile_v3(first, None, make_session(shown, ratings), base, ImageReport(), set())
    assert dump(third) == dump(first)


def test_deleted_chips_stay_deleted_across_recomputes() -> None:
    shown = shape_world(3, 4, 3) + [
        ("w0", "Walnut Round Table", 650.0, "WestElm"),
        ("w1", "Walnut Round Table 2", 690.0, "WestElm"),
    ]
    ratings = {"w0": ("love", ""), "w1": ("love", "")}
    sess = make_session(shown, ratings)
    base = query(request="tables")
    prof = merge_profile_v3(None, None, sess, base, ImageReport(), set())
    texts = [e.text for e in prof.inferred]
    assert "round" in texts and "seller: WestElm" in texts
    gone = remove_entry(prof, "inferred", "round")
    assert gone is not None
    again = merge_profile_v3(gone, None, sess, base, ImageReport(), set())
    assert "round" not in [e.text for e in again.inferred] and "round" in again.removed
    gone2 = remove_entry(again, "inferred", "seller: WestElm")
    assert gone2 is not None
    final = merge_profile_v3(gone2, None, sess, base, ImageReport(), set())
    assert "seller: WestElm" not in [e.text for e in final.inferred]
    open_chip = ProfileEntry(
        text="shape: open",
        attr_kind="open",
        polarity="like",
        dimension="shape",
        status="open",
        values=["round", "oval"],
    )
    removed_open = remove_entry(StoredProfile(inferred=[open_chip]), "inferred", "shape: open")
    assert removed_open is not None and removed_open.removed == ["shape open"]


def test_the_pin_toggles_and_is_carried_across_recomputes() -> None:
    sess = make_session(
        shape_world(3, 4, 3)
        + [("w0", "Walnut Table", 650.0, "West Elm"), ("w1", "Walnut Table 2", 690.0, "West Elm")],
        {"w0": ("love", ""), "w1": ("love", "")},
    )
    base = query(request="tables")
    prof = merge_profile_v3(None, None, sess, base, ImageReport(), set())
    chip = next(e for e in prof.inferred if e.attr_kind == "seller")
    assert chip.pinned is False and prof.pinned_sellers == []
    pinned = pin_seller(prof, chip.text)
    assert pinned is not None and pinned.pinned_sellers == ["westelm"]
    assert next(e for e in pinned.inferred if e.attr_kind == "seller").pinned is True
    again = merge_profile_v3(pinned, None, sess, base, ImageReport(), set())
    assert (
        again.pinned_sellers == ["westelm"]
        and next(e for e in again.inferred if e.attr_kind == "seller").pinned
    )
    off = pin_seller(again, chip.text)
    assert off is not None and off.pinned_sellers == []
    assert pin_seller(prof, "seller: Nobody") is None


# ---- open chip tooltips and the page ------------------------------------------------------------------------------------------------------


def test_open_chip_evidence_is_plain_text_and_the_page_builds_it_from_text_nodes() -> None:
    from app.shop.server import STATIC_DIR

    evil = "<img src=x onerror=alert(1)> Round Table"
    sess = make_session(
        [("a", evil, 100.0, "S1"), ("b", "Rectangular Table", 100.0, "S2")] + shape_world(3, 3),
        {"a": ("love", ""), "b": ("love", "")},
    )
    prof = merge_profile_v3(None, None, sess, query(request="tables"), ImageReport(), set())
    open_chip = next(e for e in prof.inferred if e.status == "open")
    assert all(isinstance(t, str) for t in open_chip.evidence_titles)
    assert any("<img" in t for t in open_chip.evidence_titles)  # stored as data, never as markup
    code = (STATIC_DIR / "app.js").read_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in code
    block = code[code.index("function inferredChip") : code.index("function pinSeller")]
    assert (
        "tip = ['Open:" in block and "evidence_titles" in block
    )  # lines go through h(...), i.e. text nodes
    chips = code[code.index("function prefChip") : code.index("function inferredChip")]
    assert "[line]" in chips  # each tooltip line is a text node built by h()


def test_the_saved_panel_wraps_into_a_grid_with_no_sideways_scroll() -> None:
    from app.shop.server import STATIC_DIR

    css = (STATIC_DIR / "style.css").read_text()
    row = css[css.index(".saved-row {") : css.index("}", css.index(".saved-row {"))]
    assert (
        "display: grid" in row
        and "auto-fill" in row
        and "overflow-x" not in row
        and "flex" not in row
    )
    assert "flex: 0 0 230px" not in css


# ---- the owner's real case -------------------------------------------------------------------------------------------------------------------


def owner_session() -> Session:
    """Four loved West Elm tables ($699 to $899, all with storage): a round walnut drum, a round
    brushed brass drum, a rectangular wood and black metal lift-top, a rectangular walnut with
    drawers. Eight liked across other sellers ($140 to $800, both shapes, warm wood). One disliked
    rectangular-with-drawers table. Plus what else was shown."""
    shown: list[tuple[str, str, float | None, str]] = [
        ("L1", 'Round Walnut Drum Storage Coffee Table 36"', 749.0, "West Elm"),
        ("L2", 'Round Brushed Brass Drum Storage Coffee Table 34"', 829.0, "West Elm"),
        (
            "L3",
            'Rectangular Wood and Black Metal Lift-Top Storage Coffee Table 46"',
            699.0,
            "West Elm",
        ),
        ("L4", 'Rectangular Walnut Storage Coffee Table with Drawers 48"', 899.0, "West Elm"),
        ("K1", 'Round Oak Coffee Table with Storage 34"', 140.0, "IKEA"),
        ("K2", 'Rectangular Walnut Coffee Table 44"', 250.0, "Target"),
        ("K3", 'Round Walnut Coffee Table with Shelf 32"', 320.0, "Wayfair"),
        ("K4", 'Rectangular Oak Coffee Table with Drawers 46"', 410.0, "Article"),
        ("K5", 'Round Walnut Storage Coffee Table 36"', 520.0, "Crate & Barrel"),
        ("K6", 'Rectangular Walnut Lift-Top Coffee Table 48"', 640.0, "Joss & Main"),
        ("K7", 'Round Oak Drum Coffee Table 30"', 720.0, "Etsy"),
        ("K8", 'Rectangular Oak Storage Coffee Table 50"', 800.0, "Overstock"),
        ("D1", 'Rectangular Black Steel Coffee Table with Drawers 48"', 430.0, "Walmart"),
    ]
    shown += [
        (f"x{i}", t, p, s)
        for i, (t, p, s) in enumerate(
            [
                ('Round Glass Top Black Metal Coffee Table 32"', 180.0, "IKEA"),
                ('Oval Marble Coffee Table 44"', 880.0, "Wayfair"),
                ('Rectangular Glass Coffee Table 46"', 200.0, "Target"),
                ('Square Black Steel Coffee Table 30"', 160.0, "Walmart"),
                ('Round White Marble Coffee Table 36"', 560.0, "Article"),
                ('Rectangular Marble Coffee Table 52"', 760.0, "Overstock"),
                ('Oval Glass Coffee Table 40"', 300.0, "Etsy"),
                ('Round Glass Coffee Table 34"', 150.0, "Walmart"),
                ('Round Gray Coffee Table 36"', 240.0, "Home Depot"),
                ('Oval White Coffee Table 42"', 350.0, "Article"),
                ('Rectangular Gray Coffee Table 48"', 380.0, "Wayfair"),
                ('Square White Coffee Table 30"', 210.0, "IKEA"),
                ('Round Chrome Coffee Table 32"', 190.0, "Walmart"),
                ('Oval Silver Metal Coffee Table 44"', 340.0, "Target"),
                ('Rectangular White Frame Coffee Table 46"', 260.0, "Wayfair"),
                ('Round Natural Wood Frame Coffee Table 34"', 310.0, "Etsy"),
                ('Square Wood Legs Coffee Table 30"', 230.0, "Overstock"),
                ('Coffee Table with Storage 40"', 270.0, "Home Depot"),
                ('Rectangular Black Steel Coffee Table 44"', 330.0, "Target"),
            ]
        )
    ]
    ratings = {
        "L1": ("love", ""), "L2": ("love", ""), "L3": ("love", ""), "L4": ("love", ""),
        **{f"K{i}": ("like", "") for i in range(1, 9)},
        "D1": ("dislike", ""),
    }  # fmt: skip
    return make_session(shown, ratings)


def storage_tags(sess: Session) -> dict[str, ItemTags]:
    """A model that (wrongly, as a feature) offers "storage" as a quality cue for every loved item."""
    return {
        rid: ItemTags(
            id=rid, shape=None, material=None, color_finish=None, frame_or_legs=None,
            frame_color=None, style=None, size_class=None, quality_cues=["storage"],
        )
        for rid in sess.ratings
    }  # fmt: skip


def test_the_owners_real_case() -> None:
    sess = owner_session()
    base = query(
        request="coffee tables with a mix of wood and metal with storage",
        search_text="coffee table with storage",
        material_terms=["wood", "metal"], must_have=["storage"], category="coffee table",
    )  # fmt: skip
    rated = rated_map(sess)
    decisions = decide(
        rated,
        storage_tags(sess),
        list(sess.results.values()),
        [],
        [],
        image_ids=set(),
    )
    by_kind = {(e.attr_kind, e.text): e for e in decisions.entries}
    # shape is open: both round and rectangular were loved and liked; nothing says "round" or "rectangular"
    shape = [e for e in decisions.entries if e.dimension == "shape"]
    assert [(e.status, e.text, sorted(e.values)) for e in shape] == [
        ("open", "shape: open", ["rectangular", "round"])
    ]
    assert not any(e.polarity == "dislike" and e.dimension == "shape" for e in decisions.entries)
    # the seller converged (and can be pinned)
    seller = [e for e in decisions.entries if e.attr_kind == "seller"]
    assert (
        [e.text for e in seller] == ["seller: West Elm"]
        and seller[0].lift is not None
        and seller[0].lift >= 1.3
    )
    # price is mixed: loved $699 to $899, liked $140 to $800
    price = [e for e in decisions.entries if e.attr_kind.startswith("price")]
    assert [(e.status, e.detail) for e in price] == [
        ("mixed", "loved $699 to $899, liked $140 to $800")
    ]
    # wood converged, a warm wood finish converged, and so did dark or brass metal frames
    wood = by_kind[("material", "wood")]
    warm = by_kind[("color_finish", "warm wood")]
    frame = by_kind[("frame_color", "dark or brass metal")]
    assert wood.status == warm.status == frame.status == "converged"
    assert (
        wood.share and wood.share >= 0.7 and warm.share and warm.share >= 0.7 and frame.share == 1.0
    )
    assert min(wood.lift or 9, warm.lift or 9, frame.lift or 9) >= thresholds.LIFT_MIN
    # storage is not a learned like (a typed term, and a feature, not build or finish)
    assert not any("storage" in e.text for e in decisions.entries)
    assert not any(e.attr_kind == "quality_cue" for e in decisions.entries)
    # the whole profile: the summary mentions open shape and mixed price, and the chips would not push round
    prof = merge_profile_v3(None, None, sess, base, ImageReport(), set())
    text = profile_summary(prof)
    assert (
        "Shape is open: showing both rectangular and round." in text and "Price is mixed." in text
    )
    assert "from West Elm" in text and "round" not in text.split("Shape is open")[0]
    pinned = pin_seller(prof, "seller: West Elm")
    assert pinned is not None and pinned.pinned_sellers == ["westelm"]
    queries, dropped = build_phrasings(base, prof, apply_adjustments(base, prof), 4)
    assert queries[0].startswith("coffee table") and dropped == []
    assert sum("round" in q for q in queries) == 1 and sum("rectangular" in q for q in queries) == 1
    assert sum("West Elm" in q for q in queries) == 1 and "mixed" not in " ".join(queries)


def test_the_owners_case_without_the_model_still_gets_the_same_decisions() -> None:
    sess = owner_session()
    with_model = decide(rated_map(sess), storage_tags(sess), list(sess.results.values()), [], [])
    lexicon_only = decide(rated_map(sess), {}, list(sess.results.values()), [], [])
    keys = lambda d: sorted((e.attr_kind, e.text, e.status) for e in d.entries)  # noqa: E731
    assert keys(with_model) == keys(
        lexicon_only
    )  # titles alone carry this case; the model adds nothing here


# ---- the pin endpoint, validation and security ----------------------------------------------------------------------------------------------------------


def test_the_pin_endpoint_validates_pins_and_keeps_the_security_behaviour(tmp_path: Path) -> None:
    from app.shop.providers import build_shop_providers
    from app.shop.server import CSP, ShopApp
    from app.shop.service import ShopService
    from app.shop.store import SessionStore

    cfg = config()
    search, lookup = build_shop_providers(cfg)
    svc = ShopService(cfg, SessionStore(tmp_path / "shop-data"), search, lookup)
    app = ShopApp(svc, port=8765)
    hdrs = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}

    def post(payload: Any, headers: dict[str, str] | None = None) -> Any:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return app.handle("POST", "/api/preference/pin", headers or hdrs, body)

    view = svc.search(None, "coffee tables with a mix of wood and metal with storage")
    sid, first = view["session_id"], view["search"]["results"]
    premium = [r for r in first if r["seller"] == "Hartwell & Co"]
    svc.rate(sid, premium[0]["id"], "love")
    svc.rate(sid, premium[1]["id"], "love")
    again = svc.shop_again(sid)
    chip = next(e for e in again["profile"]["inferred"] if e["attr_kind"] == "seller")
    assert post({"session_id": sid}).status == 400  # no phrase
    assert post({"session_id": sid, "phrase": 5}).status == 400
    assert post({"session_id": sid, "phrase": "x" * 81}).status == 400
    assert post({"session_id": "bad", "phrase": chip["text"]}).status == 400
    assert post({"session_id": "a" * 20, "phrase": chip["text"]}).status == 404
    assert post({"session_id": sid, "phrase": "seller: Nobody"}).status == 404
    assert (
        post({"session_id": sid, "phrase": "shape: open"}).status == 404
    )  # only the seller chip pins
    assert app.handle("GET", "/api/preference/pin", hdrs).status == 405
    assert post({"session_id": sid, "phrase": chip["text"]}, {"Host": "evil.example"}).status == 403
    assert post(b"x" * 9000).status == 413
    assert post(b"{bad").status == 400
    ok = post({"session_id": sid, "phrase": chip["text"]})
    body = json.loads(ok.body)
    assert ok.status == 200 and body["pinned"] is True
    assert (
        next(e for e in body["profile"]["inferred"] if e["attr_kind"] == "seller")["pinned"] is True
    )
    assert ok.all_headers()["Content-Security-Policy"] == CSP
    view2 = json.loads(app.handle("GET", f"/api/session?id={sid}", hdrs).body)
    assert view2["profile"]["pinned_sellers"] == ["hartwellco"]
