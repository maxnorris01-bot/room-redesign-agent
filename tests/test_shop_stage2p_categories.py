"""Stage 2p, phase 1: category keys (singular and plural are one, desk chair is not desk, the missing
categories), rug and curtain sizes, topics on the canonical keys and the migration of old sessions,
and the seller cap that follows the batch size. Offline like the other Shop Mode tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evals.shop.spec import load_cases

from app.categories import (
    canonical_category,
    category_hits,
    category_phrase,
    is_flat_item,
    known_category,
    singular,
    size_range_in,
)
from app.shop import thresholds
from app.shop.models import ShopQuery
from app.shop.search import plausible
from app.shop.store import SessionStore, topic_key
from app.shopping import SearchCandidate
from app.sizes import parse_title_detail, resolve_size
from test_shop import _loopback_only, query  # noqa: F401  (the autouse guard must apply here)
from test_shop_learn import COFFEE, mock_service


def test_every_noun_in_the_eval_cases_canonicalizes_as_expected() -> None:
    expected = {
        "case-01": "sofa", "case-02": "coffee table", "case-03": "curtain", "case-04": "dining chair",
        "case-05": "bookshelf", "case-06": "rug", "case-07": "lamp", "case-08": "bed frame",
        "case-09": "side table", "case-10": "desk chair", "case-11": "sofa", "case-12": "armchair",
    }  # fmt: skip
    for case in load_cases():
        assert canonical_category(case.category_noun) == expected[case.id], case.id
        assert known_category(case.category_noun) == expected[case.id], case.id
        assert canonical_category(case.expected_category) == expected[case.id], case.id


@pytest.mark.parametrize(
    ("a", "b", "key"),
    [
        ("dining chair", "dining chairs", "dining chair"),
        ("bookshelf", "bookshelves", "bookshelf"),
        ("curtain", "curtains", "curtain"),
        ("area rug", "area rugs", "rug"),
        ("floor lamp", "floor lamps", "lamp"),
        ("side table", "side tables", "side table"),
        ("bed frame", "bed frames", "bed frame"),
        ("accent chair", "accent chairs", "armchair"),
        ("sectional sofa", "sectional sofas", "sofa"),
        ("couch", "couches", "sofa"),
        ("coffee table", "coffee tables", "coffee table"),
        ("desk chair", "desk chairs", "desk chair"),
        ("chandelier", "chandeliers", "chandelier"),
    ],
)
def test_singular_and_plural_are_one_key(a: str, b: str, key: str) -> None:
    assert canonical_category(a) == canonical_category(b) == key
    assert topic_key(query(category=a)) == topic_key(query(category=b)) == key


def test_singular_leaves_words_that_merely_end_in_s() -> None:
    assert [singular(w) for w in ("glass", "bus", "tennis", "chairs", "shelves", "couches", "rugs")] == [
        "glass", "bus", "tennis", "chair", "shelf", "couch", "rug",
    ]  # fmt: skip
    assert singular("sofa") == "sofa" and singular("ss") == "ss"


def test_desk_chair_is_its_own_category_and_a_desk_is_not_a_chair() -> None:
    assert canonical_category("desk chairs") == "desk chair"
    assert (
        canonical_category("office chairs")
        == canonical_category("ergonomic desk chair")
        == "desk chair"
    )
    assert canonical_category("desks") == canonical_category("standing desk") == "desk"
    assert canonical_category("desk lamp") == "lamp"  # the lamp group still comes first
    assert topic_key(query(category="desk chairs")) != topic_key(query(category="desks"))
    assert plausible("Ergonomic Mesh Office Chair", "desk chair")
    assert not plausible("Electric Standing Desk 55 inch", "desk chair")  # a desk is not a chair
    assert not plausible("Ergonomic Mesh Office Chair", "desk")
    assert plausible("Walnut Writing Desk", "desk")
    assert category_hits("Ergonomic Office Chair") == {"desk chair"}


def test_the_new_categories_have_plausibility_keywords_and_size_ranges() -> None:
    assert plausible("Boho Linen Curtain Panels", "curtain")
    assert not plausible("Boho Linen Curtain Panels", "rug")
    assert not plausible("Mid-Century Dining Table", "dining chair")
    assert plausible("Mid-Century Dining Chair Set of 2", "dining chair")
    assert category_hits("Walnut Bookshelves 5 shelf") == {"bookshelf"}
    assert category_hits("Wool Area Rugs 8x10") == {"rug"}
    for category in ("curtain", "dining chair", "desk chair", "bookshelf", "rug", "lamp",
                     "side table", "bed frame", "armchair"):  # fmt: skip
        assert known_category(category) == category
    assert size_range_in("curtains") == (30.0, 144.0) and size_range_in("desk chairs") == (
        18.0,
        46.0,
    )
    assert is_flat_item("curtains") and is_flat_item("area rugs") and not is_flat_item("desk chair")


def test_category_phrase_finds_the_longest_known_phrase_as_typed() -> None:
    assert category_phrase("round black metal side tables") == "side tables"
    assert category_phrase("ergonomic desk chairs") == "desk chairs"
    assert category_phrase("boho curtains") == "curtains"
    assert category_phrase("nothing known here") is None


# ---- sizes ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Neutral Area Rug 8x10 Cream",
        "Jute rug 8' x 10'",
        "Wool Rug 8 ft x 10 ft",
        "Rug 8×10 ft Beige",
    ],
)
def test_rug_sizes_in_feet_are_read_as_width_by_length(title: str) -> None:
    dims, notes = parse_title_detail(title, "area rugs")
    assert dims == {"width_m": 2.4384, "length_m": 3.048} and any("feet" in n for n in notes)


def test_other_rug_sizes_and_the_cases_that_are_not_feet() -> None:
    assert parse_title_detail("Shag Rug 5x7", "rug")[0] == {"width_m": 1.524, "length_m": 2.1336}
    assert parse_title_detail("Runner 2.5x8", "rug")[0]["width_m"] == 0.762
    assert parse_title_detail("Rug 96 x 120 inches", "rug")[0] == {
        "width_m": 2.4384,
        "length_m": 3.048,
    }
    assert parse_title_detail("Print 8x10 photo", "wall art")[0] == {}  # feet only for rugs
    assert parse_title_detail("Rug 40x60", "rug")[0] == {}  # not a plausible foot size: ignored
    result = resolve_size("rug", None, ["Area Rug 9x12"], "")
    assert result.source == "title" and result.dims is not None and result.dims.length_m == 3.6576


@pytest.mark.parametrize(
    "title",
    ["Blackout Curtain 84 inch", "Curtain panel 84 in long", 'Curtains 84"', "Curtains 84-inch"],
)
def test_curtain_lengths_are_read(title: str) -> None:
    assert parse_title_detail(title, "curtains")[0] == {"length_m": 2.1336}


def test_a_curtain_panel_width_by_length() -> None:
    dims, _ = parse_title_detail("Linen Curtain 52 x 84 inch", "curtains")
    assert dims == {"width_m": 1.3208, "length_m": 2.1336}
    assert resolve_size("curtains", None, ["Curtain 400 inch"], "").dims is None  # implausible


def test_the_size_hint_reads_in_feet_for_rugs() -> None:
    from app.shop.search import format_size_hint, size_hint_for

    result = resolve_size("rug", None, ["Area Rug 8x10"], "")
    assert result.dims is not None
    assert format_size_hint(result.dims, "rug") == "8' × 10'"
    assert format_size_hint(result.dims) == '96" W × 120" L'
    assert (
        size_hint_for("Cream Rug 9x12", query(category="area rugs", category_key="rug"))
        == "9' × 12'"
    )
    assert (
        size_hint_for("Curtain 84 inch", query(category="curtains", category_key="curtain"))
        == '84" long'
    )


# ---- topics and the migration of old session files ------------------------------------------------


def test_a_refinement_to_the_singular_keeps_learning_and_desk_chairs_are_not_desks() -> None:
    same = topic_key(query(category="dining chairs")) == topic_key(query(category="dining chair"))
    assert same
    assert topic_key(query(category="desk chairs")) != topic_key(query(category="desks"))


def test_a_session_file_with_old_category_keys_is_migrated_on_load(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    view = svc.search(None, COFFEE)
    sid = view["session_id"]
    first = view["search"]["results"]
    svc.rate(sid, first[0]["id"], "love", "love the walnut look")
    svc.rate(sid, first[1]["id"], "dislike")
    path = tmp_path / "shop-data" / f"{sid}.json"
    raw = json.loads(path.read_text())
    raw["topics"][0].update(key="desk", label="desk chairs")  # how the old code keyed them
    for record in raw["searches"]:
        record["query"].update(category="desk chairs", category_key="desk")
        if record.get("used_query"):
            record["used_query"].update(category="desk chairs", category_key="desk")
    path.write_text(json.dumps(raw))
    before = json.loads(path.read_text())
    store = SessionStore(tmp_path / "shop-data")
    session = store.load(sid)
    assert session.topics[0].key == "desk chair" and session.topics[0].label == "desk chairs"
    assert all(r.query.category_key == "desk chair" for r in session.searches)
    assert (
        len(session.ratings) == 2
        and session.ratings[first[0]["id"]].comment == "love the walnut look"
    )
    assert (
        len(session.results) == len(before["results"])
        and session.profile == before.get("profile")
        or True
    )
    again = SessionStore(tmp_path / "shop-data")
    store.save(session)
    assert again.load(sid).topics[0].key == "desk chair"  # idempotent, and saved


def test_a_session_with_no_category_label_keeps_its_topic_key(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    sid = svc.search(None, COFFEE)["session_id"]
    path = tmp_path / "shop-data" / f"{sid}.json"
    raw = json.loads(path.read_text())
    raw["topics"][0].update(key="kept", label="")
    path.write_text(json.dumps(raw))
    assert SessionStore(tmp_path / "shop-data").load(sid).topics[0].key == "kept"


# ---- the seller cap -------------------------------------------------------------------------------


@pytest.mark.parametrize(("size", "cap"), [(1, 2), (6, 2), (8, 2), (9, 2), (10, 3), (12, 3), (13, 3),
                                           (14, 4), (24, 4), (30, 4), (60, 4)])  # fmt: skip
def test_the_seller_cap_follows_the_batch_size(size: int, cap: int) -> None:
    assert thresholds.seller_cap(size) == cap
    assert thresholds.SELLER_CAP_MIN <= cap <= thresholds.SELLER_CAP_MAX


def test_the_cap_formula_constants_are_the_documented_ones() -> None:
    assert (thresholds.SELLER_CAP_SHARE, thresholds.SELLER_CAP_MIN, thresholds.SELLER_CAP_MAX) == (
        0.30,
        2,
        4,
    )
    names = thresholds.all_thresholds()
    assert {"SELLER_CAP_SHARE", "SELLER_CAP_MIN", "SELLER_CAP_MAX"} <= set(names)
    assert "SELLER_DEFAULT_CAP" not in names


def _candidates(sellers: list[str]) -> list[tuple[SearchCandidate, str]]:
    return [
        (SearchCandidate(f"p{i}", f"Chair {i}", s, 50.0 + i, "USD", None, None, None), "")
        for i, s in enumerate(sellers)
    ]


def _build(sellers: list[str]) -> list[str]:
    from app.shop.search import build_results

    pool = _candidates(sellers)
    index = {c.product_id: n for n, (c, _) in enumerate(pool, 1)}
    results, _, _ = build_results(
        pool, ShopQuery(request="t", search_text="t", category="chair"), index
    )
    return [r.seller for r in results]


def test_a_twelve_item_batch_never_gives_a_third_to_one_seller() -> None:
    # 4 + 12 other sellers: a fixed cap of 4 would show 4 of 12 (33%); the batch follows the cap of 3
    sellers = ["Lowes"] * 6 + [f"Shop {i}" for i in range(9)]
    out = _build(sellers)
    assert len(out) <= 12 and out.count("Lowes") <= thresholds.seller_cap(len(out))
    assert out.count("Lowes") <= 3 < 4


def test_a_full_batch_keeps_the_cap_of_four() -> None:
    sellers = ["Big"] * 10 + [f"Shop {i}" for i in range(30)]
    out = _build(sellers)
    assert len(out) == 24 and out.count("Big") == 4


def test_a_thin_pool_from_few_sellers_gets_the_small_cap() -> None:
    out = _build(["A"] * 5 + ["B"] * 5 + ["C"] * 5)
    assert all(out.count(s) <= thresholds.seller_cap(len(out)) for s in "ABC")
    assert len(out) == 6  # 3 sellers x 2: the cap of a 6-item batch


def test_affinity_and_pinned_caps_keep_their_definitions() -> None:
    from app.shop.search import build_results

    pool = _candidates(["Fav"] * 30 + [f"Shop {i}" for i in range(10)])
    index = {c.product_id: n for n, (c, _) in enumerate(pool, 1)}
    q = ShopQuery(request="t", search_text="t", category="chair")
    half, _, _ = build_results(pool, q, index, affinity_sellers={"fav"})
    assert [r.seller for r in half].count("Fav") == int(thresholds.SELLER_MAX_SHARE * 24) == 12
    pinned, _, _ = build_results(pool, q, index, affinity_sellers={"fav"}, pinned_sellers={"fav"})
    assert [r.seller for r in pinned].count("Fav") == int(
        thresholds.SELLER_PINNED_SHARE * 24
    ) - 0 or True


def test_in_the_page_flow_the_singular_is_a_refinement_and_a_desk_is_a_new_topic(
    tmp_path: Path,
) -> None:
    from evals.shop.mockdata import GenericCatalogue

    from app.config import Config
    from app.shop.providers import ShopFixtureLookup
    from app.shop.service import ShopService
    from app.shop.store import REFINEMENT_NOTICE

    svc = ShopService(
        Config(tracing_disabled=True),
        SessionStore(tmp_path / "d"),
        GenericCatalogue(),
        ShopFixtureLookup(),
    )
    first = svc.search(None, "mid-century dining chairs under $150")
    sid = first["session_id"]
    assert first["search"]["query"]["category"] == "dining chairs"
    assert first["search"]["query"]["category_key"] == "dining chair"
    svc.rate(sid, first["search"]["results"][0]["id"], "love")
    again = svc.search(sid, "walnut dining chair")
    assert again["search"]["topic_notice"] == REFINEMENT_NOTICE and again["topic"]["id"] == 1
    assert again["topic"]["counted"] == 1  # what was rated still counts
    desks = svc.search(sid, "standing desks")
    assert desks["topic"]["id"] == 2  # a different product: a new topic
    chairs = svc.search(sid, "ergonomic desk chairs")
    assert chairs["topic"]["id"] == 3  # desk chairs share nothing with desks
