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
    Exposure,
    TaggedItem,
    build_exposure,
    classify_dimension,
    decide,
    dislike_values,
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
from app.shop.vocab import normalize, title_tags, title_value
from app.shopping import SearchCandidate
from test_shop import (
    FakeLookup,
    FakeSearch,
    RouterClient,
    cand,
    config,
    make_service,
    query,
    rank_call,  # noqa: F401  (the autouse guard must be imported to apply here)
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
    return TaggedItem(r, {**title_tags(title), **values})


def model_tags(sess: Session) -> dict[str, ItemTags]:
    """Tags a careful model would give: what each title shows by keyword, null for the rest."""
    tags: dict[str, ItemTags] = {}
    for rid in sess.ratings:
        got = title_tags(sess.results[rid].title)
        tags[rid] = ItemTags(
            id=rid, shape=got.get("shape"), material=got.get("material"),
            color_finish=got.get("color_finish"), frame_color=got.get("frame_color"),
            style=[got["style"]] if got.get("style") else [], quality_cues=[],
        )  # fmt: skip
    return tags


def title_exposure(sess: Session) -> Exposure:
    """The baseline from a model that tagged every shown title as far as its keywords go."""
    shown = list(sess.results.values())
    return build_exposure(shown, {r.id: title_tags(r.title) for r in shown})


def merged(existing: StoredProfile | None, sess: Session, base: Any) -> StoredProfile:
    """`merge_profile_v3` with a model answer built from the titles (so attributes are decided)."""
    from app.shop.models import PreferenceTags, QueryAdjustments

    answer = PreferenceTags(
        items=list(model_tags(sess).values()), likes=[], dislikes=[], contradicted_guesses=[],
        query_adjustments=QueryAdjustments(
            search_text="", style_terms=[], material_terms=[], must_have=[], avoid=[],
            price_max=None, price_note="",
        ),
    )  # fmt: skip
    return merge_profile_v3(
        existing, answer, sess, base, ImageReport(), set(), exposure=title_exposure(sess)
    )


def entries_by(dim: str, decisions: Any) -> list[ProfileEntry]:
    return [e for e in decisions.entries if e.dimension == dim]


def decide_session(
    sess: Session, base_terms: list[str] | None = None, removed: list[str] | None = None
) -> Any:
    return decide(
        rated_map(sess),
        model_tags(sess),
        list(sess.results.values()),
        removed or [],
        base_terms or [],
        exposure=title_exposure(sess),
    )


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
    assert t["SHAPE_CONVERGE_SHARE"] == 0.80 and t["SOFT_CONVERGE_SHARE"] == 0.60
    assert t["KNOWN_COVERAGE_MIN"] == 0.60 and t["OPEN_SHARE"] == 0.30 and t["LIFT_MIN"] == 1.3
    assert t["MIN_AFFINITY_ITEMS"] == 2 and "SELLER_MIN_LIKE_SHARE" not in t  # replaced in 2i
    assert t["GROUP_MIN_RATED"] == 4 and t["GROUP_MIN_LIKED"] == 2
    assert t["GROUP_RATE_RATIO"] == 1.5 and t["GROUP_MIN_RATE"] == 0.35
    assert t["LEAN_AWAY_MIN_RATED"] == 6 and t["LEAN_AWAY_MAX_RATIO"] == 0.5
    assert t["LEAN_TOWARD_MIN_SHARE"] == 0.30 and t["LEAN_AWAY_MAX_SHARE"] == 0.30
    assert (
        t["MARKET_MIN_RATED"] == 8 and t["MARKET_MAX_RATIO"] == 0.5 and t["MARKET_MAX_RATE"] == 0.20
    )
    assert t["MARKET_MIN_RESULTS"] == 12 and t["MARKET_REDISMISS_RATED"] == 8
    assert t["MINORITY_POOL_SHARE"] == 0.40 and t["OPEN_STRATUM_FLOOR"] == 0.25
    assert t["MIN_SUPPORT_ITEMS"] == 2 and t["DISLIKE_MIN_ITEMS"] == 2
    assert t["LOVE_WEIGHT"] == 3 and t["LIKE_WEIGHT"] == 1
    assert t["DIVERSITY_MAX_SHARE"] == 0.60 and t["CONVERGED_MAX_SHARE"] == 0.80
    assert t["SELLER_MAX_SHARE"] == 0.50 and t["SELLER_PINNED_SHARE"] == 0.80
    assert t["PRICE_MIXED_LIKED_SPAN"] == pytest.approx(0.6667, abs=1e-4)
    assert t["RESULT_COUNT"] == RESULT_COUNT == 24
    docs = (
        Path(__file__).resolve().parents[1] / "docs" / "adr" / "0012-two-modes-shop-first.md"
    ).read_text()
    for name in t:
        assert name in docs, f"{name} is not in the ADR 0012 thresholds table"


# ---- lexicon and exposure -------------------------------------------------------------------------------


def test_the_title_reader_is_a_fallback_that_leaves_unknowns_out() -> None:
    assert title_tags('Round Walnut Drum Storage Coffee Table 36"') == {
        "shape": "round", "material": "wood", "color_finish": "warm wood",
    }  # fmt: skip
    got = title_tags("Rectangular Wood and Black Metal Lift-Top Coffee Table")
    assert got == {"shape": "rectangular", "material": "wood", "frame_color": "black"}
    assert title_tags('Coffee Table with Storage 40"') == {}  # nothing to read
    assert title_tags("Brushed Brass Drum Table")["frame_color"] == "brass or gold"
    assert title_value("shape", "ROUND table") == "round"
    assert title_value("shape", "background") is None
    assert title_value("style", "Mid-Century Modern desk") == "mid-century"  # earliest word wins
    assert title_value("frame_color", "Black Steel Base") == "black"


def test_free_text_folds_into_the_fixed_vocabularies() -> None:
    assert normalize("material", "walnut") == "wood"
    assert normalize("color_finish", "Walnut") == "warm wood"
    for words in ("warm wood", "brown wood", "rustic brown", "walnut", "oak brown finished"):
        assert normalize("color_finish", words) == "warm wood", words
    assert normalize("color_finish", "dark warm wood") == "warm wood"
    assert normalize("color_finish", "espresso") == "dark wood"
    assert normalize("color_finish", "brushed brass") == "metallic"
    assert normalize("frame_color", "black metal") == "black"
    assert normalize("frame_color", "black powder-coated steel") == "black"
    assert normalize("frame_color", "gold metal") == "brass or gold"
    assert normalize("frame_color", "natural wood") == "natural wood"
    # a frame described only as metal says nothing about colour: one value, never split in two
    assert (
        normalize("frame_color", "metal base")
        == normalize("frame_color", "metal frame")
        == normalize("frame_color", "metal legs")
        == "other"
    )
    assert normalize("shape", "Round") == "round" and normalize("shape", "other") == "other"
    assert normalize("shape", "kidney bean") == "other"  # read, but not a value to learn
    assert normalize("shape", None) is None and normalize("material", "  ") is None
    assert normalize("style", "Mid-Century Modern") == "mid-century"
    assert (
        normalize("style", "Scandinavian") == "modern"
        and normalize("style", "rustic") == "farmhouse"
    )
    assert normalize("style", "quirky cabin vibes") == "other"
    assert normalize("size_class", "large") is None  # not a decided dimension any more


def test_exposure_is_the_share_among_shown_results_that_have_a_value() -> None:
    sess = make_session(
        [
            (f"t{i}", t, 100.0, "S")
            for i, t in enumerate(
                ["Round A", "Round B", "Round C", "Rectangular D", "Plain E", "Plain F"]
            )
        ],
        {},
    )
    shown = list(sess.results.values())
    exp = build_exposure(shown, {r.id: title_tags(r.title) for r in shown})
    assert exp.shares["shape"] == {"round": 0.75, "rectangular": 0.25}  # two plain: unknown
    assert exp.coverage["shape"] == pytest.approx(4 / 6) and exp.tagged == 6 and exp.total == 6
    assert "color_finish" not in exp.shares  # nothing shows it: no baseline at all
    assert exp.usable
    keywords = build_exposure(shown, None, keywords=True)  # the tagging call failed
    assert keywords.source == "keywords" and not keywords.usable and keywords.tagged == 0


def test_tagging_uses_the_model_only_and_unknowns_stay_unknown() -> None:
    r = Rated("a", "love", "", "Round Walnut Table", 100.0, "S", "")
    model = ItemTags(
        id="a", shape=None, material="steel", color_finish=None, frame_color="brass",
        style=["modern"], quality_cues=["Solid Wood", "storage"],
    )  # fmt: skip
    item = tag_item(r, model)
    assert item.values["material"] == "metal"  # the model's reading, folded into the vocabulary
    assert "shape" not in item.values and "color_finish" not in item.values  # null stays unknown:
    # the title says "Round Walnut" but title keywords never fill what the model left out
    assert item.values["frame_color"] == "brass or gold" and "size_class" not in item.values
    assert item.cues == ("solid wood", "storage")
    assert tag_item(r, None).values == {}  # an item the model did not tag is unknown everywhere


# ---- one dimension: converged, open, none -----------------------------------------------------------------


def exposure_of(round_share: float, coverage: float = 1.0) -> Exposure:
    return Exposure(
        shares={"shape": {"round": round_share, "rectangular": 1 - round_share}},
        coverage={"shape": coverage},
    )


def test_one_round_and_one_rectangular_love_make_shape_open_with_no_pressure() -> None:
    items = [tagged("a", "love", "Round Table"), tagged("b", "love", "Rectangular Table")]
    res = classify_dimension("shape", items, exposure_of(0.5))
    assert res.status == "open" and sorted(res.values) == ["rectangular", "round"]
    assert res.shares == {"round": 0.5, "rectangular": 0.5}
    # through the whole pipeline: an open chip, nothing converged, no shape in any phrasing or context
    sess = make_session(shape_world(6, 6), {"r0": ("love", ""), "x0": ("love", "")})
    prof = merged(None, sess, query(request="tables", category="table"))
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
    prof = merged(None, sess, query(request="tables"))
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
    prof = merged(None, sess, query(request="tables"))
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


def test_shape_converges_at_eighty_percent_and_soft_dimensions_at_sixty() -> None:
    low = exposure_of(0.2)

    def items(spec: list[tuple[str, str]]) -> list[TaggedItem]:
        return [
            tagged(f"i{n}", rating, f"{shape} Table {n}") for n, (rating, shape) in enumerate(spec)
        ]

    # 4 of 5 = 80%: shape (the structural dimension) converges exactly at its share
    eighty = items([("love", "Round"), ("like", "Round"), ("like", "Rectangular")])
    assert classify_dimension("shape", eighty, low).status == "converged"
    # 3 of 5 = 60% and 2 of 5 = 40%: both hold 30%, so shape is open, not converged
    sixty = items([("love", "Round"), ("like", "Rectangular"), ("like", "Rectangular")])
    res = classify_dimension("shape", sixty, low)
    assert res.status == "open" and res.values == ["round", "rectangular"]
    # 6 of 8 = 75% round and 25% rectangular: neither converged nor open
    seventy_five = items(
        [("love", "Round"), ("love", "Round"), ("like", "Rectangular"), ("like", "Rectangular")]
    )
    none = classify_dimension("shape", seventy_five, low)
    assert none.status == "none" and "75% of all like mass, under 80%" in none.trace["reason"]
    # a soft dimension converges at 60%: three liked wood items against two liked glass ones
    soft = Exposure(shares={"material": {"wood": 0.2, "glass": 0.8}}, coverage={"material": 1.0})
    mixed = [tagged(f"w{n}", "like", "Table", material="wood") for n in range(3)]
    mixed += [tagged(f"g{n}", "like", "Table", material="glass") for n in range(2)]
    got = classify_dimension("material", mixed, soft)
    assert got.status == "converged" and got.value == "wood" and got.share == 0.6
    # the same 60% for shape is open; just under 60% is open for material too
    under = mixed + [tagged("g9", "like", "Table", material="glass")]  # 3 of 6 = 50%
    assert classify_dimension("material", under, soft).status == "open"
    assert thresholds.SHAPE_CONVERGE_SHARE == 0.80 and thresholds.SOFT_CONVERGE_SHARE == 0.60
    assert (
        classify_dimension("shape", sixty, low).trace["converge_share"] == 0.8
        and got.trace["converge_share"] == 0.6
    )


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


def test_a_dimension_with_no_exposure_baseline_cannot_converge() -> None:
    items = [
        tagged("a", "love", "Table", color_finish="warm wood"),
        tagged("b", "love", "Chair", color_finish="warm wood"),
    ]
    res = classify_dimension("color_finish", items, Exposure())  # nothing shown was tagged for it
    assert res.status == "none" and "lift test cannot run" in res.trace["reason"]
    assert res.trace["exposure_coverage"] == 0.0 and res.lift is None
    # tagged on 60% of what was shown: the lift test may run (and a 100% like share passes it)
    ok = Exposure(
        shares={"color_finish": {"warm wood": 0.4, "dark wood": 0.6}},
        coverage={"color_finish": 0.6},
    )
    assert classify_dimension("color_finish", items, ok).status == "converged"
    thin = Exposure(shares=ok.shares, coverage={"color_finish": 0.59})
    assert classify_dimension("color_finish", items, thin).status == "none"
    # the shown results could not be tagged (title keywords only): nothing converges at all
    keywords = Exposure(shares=ok.shares, coverage=ok.coverage, source="keywords")
    blocked = classify_dimension("color_finish", items, keywords)
    assert blocked.status == "none" and "title keywords only" in blocked.trace["reason"]


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
        ("b", "Oval Black Metal Coffee Table", 320.0, "S2"),
        ("c", "Round Natural Wood Frame Coffee Table", 310.0, "S3"),
        ("d", "Rectangular White Frame Coffee Table", 290.0, "S4"),
        ("e", "Square Chrome Coffee Table", 280.0, "S5"),
        ("f", "Plain Table", 270.0, "S6"),
    ]
    sess = make_session(shown, {"a": ("love", ""), "b": ("love", "")})
    got = entries_by("frame_color", decide_session(sess))
    assert [(e.text, e.status) for e in got] == [("black", "converged")]
    assert got[0].lift == pytest.approx(2.5)  # 2 of 5 known frame colours were black


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
            id=rid, shape=None, material=None, color_finish=None, frame_color=None, style=[],
            quality_cues=["solid walnut", "storage", "lift-top storage"],
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
    sess, rated = priced_world([720.0, 749], [780.0, 820, 860], POOL)
    entry = price_entry(rated, list(sess.results.values()))
    assert entry is not None and entry.attr_kind == "price_band" and entry.band == "upper"
    assert entry.status == "converged" and entry.text.startswith("upper range, about $")
    sess2, rated2 = priced_world([150.0, 190], [160.0, 210], POOL)
    low = price_entry(rated2, list(sess2.results.values()))
    assert low is not None and low.band == "lower"


def test_price_says_nothing_when_loved_is_not_clustered_and_liked_is_narrow() -> None:
    sess, rated = priced_world([440.0, 480], [450.0, 470], POOL)
    assert price_entry(rated, list(sess.results.values())) is None  # a narrow mid-range cluster
    few = make_session(
        [("a", "T", 100.0, "S"), ("b", "T2", 200.0, "S")], {"a": ("love", ""), "b": ("love", "")}
    )
    assert price_entry(rated_map(few), list(few.results.values())) is None  # too few shown prices


def test_a_mixed_price_chip_is_neutral_and_the_ranker_hears_it_is_only_a_hint() -> None:
    from app.shop.again import again_context
    from app.shop.search import rank_user_text

    sess, rated = priced_world([699.0, 749, 829, 899], [140.0, 300, 520, 800], POOL)
    prof = merged(None, sess, query(request="tables"))
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


def west_elm_session() -> tuple[
    list[tuple[str, str, float | None, str]], dict[str, tuple[str, str]]
]:
    """Four West Elm items liked and six others disliked: 4 of 10 liked, West Elm 4 of 4."""
    shown = [(f"w{i}", f"Walnut Round Table {i}", 600.0 + i, "West Elm") for i in range(4)]
    shown += [(f"d{i}", f"Plain Table {i}", 100.0 + i, f"Seller{i}") for i in range(6)]
    ratings = {f"w{i}": ("love" if i < 2 else "like", "") for i in range(4)}
    ratings |= {f"d{i}": ("dislike", "") for i in range(6)}
    return shown, ratings


def test_seller_affinity_ignores_how_often_the_seller_was_shown() -> None:
    shown, ratings = west_elm_session()
    rated = rated_map(make_session(shown, ratings))
    flooded = [
        ShopResult(
            id=f"s{i}", title="T", price=1.0, currency="USD", seller="West Elm", thumbnail_url="",
            link="", link_kind="none", size_hint=None, product_id=f"s{i}", rank=1, pool_position=1,
        )
        for i in range(90)
    ]  # fmt: skip
    for seen in ([], flooded):  # 4 of 4 liked against 4 of 10: the same chip either way
        got = seller_affinity_entry(rated, seen)
        assert got is not None and got.text == "seller: West Elm"
        assert got.detail == "liked 4 of 4 (100%) against 40% overall" and got.lift is None


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
    cfg = config(shop_again_max_serper_calls=2, shop_again_max_serper_calls_total=2)
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
    text = rank_call(client)["messages"][0]["content"]
    assert (
        "Open dimensions (the shopper likes several values; keep them mixed, no pressure): shape: rectangular, round"
        in text
    )
    assert "Converged preferences (learned from the rated items; apply them): none" in text
    assert rank_call(client)["system"].startswith("You order real shopping results")
    assert (
        "keep them mixed" in rank_call(client)["system"]
        or "open dimensions" in rank_call(client)["system"]
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
    shapes = [title_value("shape", r.title) for r in results]
    assert len(results) == 24 and shapes.count("round") == 14
    assert shapes.count("round") <= int(0.6 * RESULT_COUNT) == 14
    assert skipped == 16  # the 16 round items past the 14th were passed over
    assert (
        shapes.count("rectangular") + shapes.count(None) == 24 - 14
    )  # the next best of other values
    # replacing keeps rank order inside each value
    kept_round = [r.title for r in results if title_value("shape", r.title) == "round"]
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
        "Could not keep shape mixed: the pool is mostly round (100% of these results, limit 60%)."
        in w
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
    assert "shape" in div and div["shape"]["share"] <= 0.6
    assert "material" not in div  # material is no longer always guarded (stage 2d)
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
    first = merged(None, make_session(shown, ratings), base)
    reordered = dict(reversed(list(ratings.items())))
    second = merged(None, make_session(shown, reordered), base)
    dump = lambda p: json.dumps([e.model_dump() for e in p.inferred], sort_keys=True)  # noqa: E731
    assert dump(first) == dump(second) and first.inferred  # the same, and not empty
    # recomputing from the same ratings again changes nothing (no incremental drift)
    third = merged(first, make_session(shown, ratings), base)
    assert dump(third) == dump(first)


def test_deleted_chips_stay_deleted_across_recomputes() -> None:
    shown = shape_world(3, 4, 3) + [
        (f"w{i}", f"Walnut Round Table {i}", 650.0 + i, "West Elm") for i in range(4)
    ]
    ratings = {f"w{i}": ("love", "") for i in range(4)}
    ratings |= {f"o{i}": ("dislike", "") for i in range(3)} | {
        f"x{i}": ("dislike", "") for i in range(3)
    }
    sess = make_session(shown, ratings)
    base = query(request="tables")
    prof = merged(None, sess, base)
    texts = [e.text for e in prof.inferred]
    assert "round" in texts and "seller: West Elm" in texts
    gone = remove_entry(prof, "inferred", "round")
    assert gone is not None
    again = merged(gone, sess, base)
    assert "round" not in [e.text for e in again.inferred] and "round" in again.removed
    gone2 = remove_entry(again, "inferred", "seller: West Elm")
    assert gone2 is not None
    final = merged(gone2, sess, base)
    assert "seller: West Elm" not in [e.text for e in final.inferred]
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
        + [(f"w{i}", f"Walnut Table {i}", 650.0 + i, "West Elm") for i in range(4)],
        {f"w{i}": ("love", "") for i in range(4)}
        | {f"o{i}": ("dislike", "") for i in range(3)}
        | {f"x{i}": ("dislike", "") for i in range(3)},
    )
    base = query(request="tables")
    prof = merged(None, sess, base)
    chip = next(e for e in prof.inferred if e.attr_kind == "seller")
    assert chip.pinned is False and prof.pinned_sellers == []
    pinned = pin_seller(prof, chip.text)
    assert pinned is not None and pinned.pinned_sellers == ["westelm"]
    assert next(e for e in pinned.inferred if e.attr_kind == "seller").pinned is True
    again = merged(pinned, sess, base)
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
    prof = merged(None, sess, query(request="tables"))
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
    for r in premium[:4]:  # 4 rated and liked, against four disliked results from other sellers
        svc.rate(sid, r["id"], "love")
    for r in [r for r in first if r["seller"] != "Hartwell & Co"][:4]:
        svc.rate(sid, r["id"], "dislike")
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
    assert view2["profile"]["pinned_sellers"] == ["hartwellandco"]
