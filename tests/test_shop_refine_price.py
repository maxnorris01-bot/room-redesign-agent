"""Price min/max as a refine filter: a typed range for the NEXT Shop Again.

Fakes only (same doubles and loopback-only guard as the other Shop Mode tests)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.shop.models import RefineChip, ShopQuery
from app.shop.refine import apply_to_query, build_set
from app.shop.refine_filters import MAX_ADDED_WORDS, targeted_text
from app.shop.refine_filters import chip_of as filter_chip
from app.shop.refine_price import (
    PriceError,
    chip_of,
    parse_price,
    price_of,
    price_words,
    with_price,
)
from app.shop.server import ShopApp
from app.shop.service import UNSET, ApiError, ShopService
from app.shopping import SearchCandidate
from test_shop import (
    _loopback_only,  # noqa: F401  (the autouse guard must apply here)
    cand,
)
from test_shop_again import app, hdr, post, searched  # noqa: F401  (the `app` fixture)
from test_shop_refine_filters import GRAY_BLUE, ByText, rugs, service, titles

PRICE_FIELDS = {
    "id": "price", "kind": "price", "strength": "focus", "approximate": False, "text": "",
    "note_mode": "",
}  # fmt: skip


def priced(prefix: str, prices: list[float | None], title: str = "Area Rug") -> list[Any]:
    return [cand(f"{prefix}{i}", p, title=f"{title} {prefix}{i}") for i, p in enumerate(prices)]


def searched_svc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, search: Any
) -> tuple[ShopService, str]:
    svc = service(tmp_path, monkeypatch, search)
    return svc, svc.search(None, "area rug")["session_id"]


def prices_of(view: dict[str, Any]) -> list[float | None]:
    return [r["price"] for r in view["search"]["results"]]


def shown_page() -> list[SearchCandidate]:
    return priced("s", [150.0] * 24, "Shown Rug")  # a full first page


# ---- the pure pieces ------------------------------------------------------------------------


def test_parse_price_forms_and_clearing() -> None:
    assert parse_price({"min": 50, "max": 200}) == (50.0, 200.0)
    assert parse_price({"max": 200}) == (None, 200.0)
    assert parse_price({"min": 50, "max": None}) == (50.0, None)
    assert parse_price({"min": 0, "max": 0}) == (0.0, 0.0)  # zero is a number; min == max is fine
    assert parse_price({}) is None
    assert parse_price({"min": None, "max": None}) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"min": -1},
        {"max": -0.5},
        {"min": 300, "max": 200},
        {"min": "50"},
        {"max": True},
        {"max": float("inf")},
        {"max": float("nan")},
        [],
        "50",
        None,
    ],
)
def test_parse_price_rejects_everything_else(bad: Any) -> None:
    with pytest.raises(PriceError):
        parse_price(bad)


def test_chip_target_and_label_forms() -> None:
    both = chip_of((50.0, 200.0))
    assert both.model_dump(mode="json") == {
        **PRICE_FIELDS, "target": "price:50-200", "label": "Price: $50-$200",
    }  # fmt: skip
    under, over = chip_of((None, 200.0)), chip_of((50.0, None))
    assert (under.target, under.label) == ("price:-200", "Under $200")
    assert (over.target, over.label) == ("price:50-", "Over $50")
    assert chip_of((49.5, 200.0)).label == "Price: $49.50-$200"  # cents only when there are some
    for price in [(50.0, 200.0), (None, 200.0), (50.0, None), (0.0, 0.0), (49.5, 99.99)]:
        assert price_of([chip_of(price)]) == price  # the target reads back


def test_with_price_replaces_clears_and_keeps_other_chips() -> None:
    other = filter_chip("color", ["gray"])
    first = with_price([other], (50.0, 200.0))
    assert [c.id for c in first] == ["filter:color", "price"]
    second = with_price(first, (None, 80.0))
    assert [c.id for c in second] == ["filter:color", "price"] and second[1].target == "price:-80"
    assert [c.id for c in with_price(second, None)] == ["filter:color"]


def test_price_words_and_the_four_word_cap_with_price_first() -> None:
    assert price_words((None, 200.0)) == ["under", "$200"]
    assert price_words((50.0, None)) == ["over", "$50"]
    assert price_words((50.0, 200.0)) == ["$50", "to", "$200"]
    f = {"color": ["gray", "blue"], "style": ["vintage", "boho"]}
    assert targeted_text("area rug", f, (None, 100.0)) == "area rug under $100 gray blue"
    assert targeted_text("area rug", f, (50.0, 100.0)) == "area rug $50 to $100 gray"
    for price in [(None, 100.0), (50.0, None), (50.0, 100.0)]:
        assert len(targeted_text("area rug", f, price).split()) - 2 <= MAX_ADDED_WORDS
    assert targeted_text("area rug", f) == "area rug gray blue vintage boho"  # unchanged
    assert targeted_text("rug under $100", {}, (None, 100.0)) == "rug under $100"  # no repeats


def test_a_typed_price_overrides_the_parsed_budget_and_a_text_refinement() -> None:
    query = ShopQuery(request="r", search_text="rug", category="rug", price_min=10, price_max=50)
    text_chip = RefineChip(
        id="t1", kind="price_max", target="400", strength="hard", label="under $400"
    )
    got = apply_to_query(query, build_set([text_chip, chip_of((60.0, 120.0))]))
    assert (got.price_min, got.price_max) == (60.0, 120.0)  # both ends replaced
    only_max = apply_to_query(query, build_set([text_chip, chip_of((None, 90.0))]))
    assert (only_max.price_min, only_max.price_max) == (None, 90.0)  # the parsed floor goes too
    no_typed = apply_to_query(query, build_set([text_chip]))
    assert (no_typed.price_min, no_typed.price_max) == (10, 400)  # without a typed price: as before
    assert build_set([text_chip]).typed_price is None


# ---- the endpoint: stored, replaced, cleared, validated, removed -------------------------------


def test_price_is_stored_replaced_cleared_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, sid = searched_svc(tmp_path, monkeypatch, ByText([("rug", [rugs("a", 6, "Rug")])]))
    out = svc.refine(sid, None, UNSET, {"min": 50, "max": 200})
    assert out["refine"] == [{**PRICE_FIELDS, "target": "price:50-200", "label": "Price: $50-$200"}]
    assert svc.session_view(sid)["refine"][0]["id"] == "price"
    replaced = svc.refine(sid, None, UNSET, {"max": 80})
    assert [(c["id"], c["target"], c["label"]) for c in replaced["refine"]] == [
        ("price", "price:-80", "Under $80")
    ]  # REPLACED, not merged
    assert svc.refine(sid, None, UNSET, {})["refine"] == []  # {} clears it
    svc.refine(sid, None, UNSET, {"min": 5})
    assert svc.refine(sid, None, UNSET, {"min": None, "max": None})["refine"] == []
    svc.refine(sid, None, UNSET, {"min": 5})
    assert svc.remove_refine(sid, "price")["refine"] == []  # the existing remove endpoint


def test_price_sits_beside_filters_and_each_replaces_only_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, sid = searched_svc(tmp_path, monkeypatch, ByText([("rug", [rugs("a", 6, "Rug")])]))
    out = svc.refine(sid, None, GRAY_BLUE, {"max": 100})
    assert [c["id"] for c in out["refine"]] == ["filter:color", "price"]
    out = svc.refine(sid, None, [])  # clearing the filters keeps the price
    assert [c["id"] for c in out["refine"]] == ["price"]
    svc.refine(sid, None, GRAY_BLUE)
    out = svc.refine(sid, None, UNSET, {"max": 70})  # a new price keeps the filters
    assert [(c["id"], c["target"]) for c in out["refine"]] == [
        ("filter:color", "color:gray,blue"),
        ("price", "price:-70"),
    ]


@pytest.mark.parametrize("bad", [{"min": -1}, {"max": -1}, {"min": 9, "max": 8}, {"min": "x"}, 5])
def test_price_validation_is_a_400_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: Any
) -> None:
    svc, sid = searched_svc(tmp_path, monkeypatch, ByText([("rug", [rugs("a", 6, "Rug")])]))
    with pytest.raises(ApiError) as err:
        svc.refine(sid, None, GRAY_BLUE, bad)  # the filters in the same call are not stored either
    assert err.value.status == 400
    assert svc.session_view(sid)["refine"] == []


def test_price_endpoint_and_a_new_category_clears_it(app: ShopApp) -> None:  # noqa: F811
    sid, _ = searched(app)
    out = post(app, "/api/refine", {"session_id": sid, "price": {"min": 50, "max": 200}})
    assert out.status == 200
    assert json.loads(out.body)["refine"][0]["target"] == "price:50-200"
    assert (
        post(app, "/api/refine", {"session_id": sid, "price": {"min": 3, "max": 2}}).status == 400
    )
    assert post(app, "/api/refine", {"session_id": sid, "price": {"min": -3}}).status == 400
    removed = post(app, "/api/refine/remove", {"session_id": sid, "id": "price"})
    assert removed.status == 200 and json.loads(removed.body)["refine"] == []
    post(app, "/api/refine", {"session_id": sid, "price": {"max": 9}})
    moved = json.loads(post(app, "/api/search", {"session_id": sid, "request": "sofa"}).body)
    assert moved["refine"] == []  # a new product category starts a new topic: the price is gone


# ---- the Shop Again --------------------------------------------------------------------------


def test_strict_drop_inclusive_bounds_missing_price_and_removed_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page2 = [
        *priced("lo", [49.99, 50.0, 51.0], "Cheap Rug"),
        *priced("mid", [100.0, 150.0, 199.99, 200.0], "Mid Rug"),
        *priced("hi", [200.01, 350.0], "Dear Rug"),
        *priced("np", [None], "Unpriced Rug"),
        *priced("many", [60.0 + i for i in range(30)], "Many Rug"),  # a full batch in range
    ]
    search = ByText([("rug", [shown_page(), page2])])
    svc, sid = searched_svc(tmp_path, monkeypatch, search)
    svc.refine(sid, None, UNSET, {"min": 50, "max": 200})
    again = svc.shop_again(sid)
    got = prices_of(again)
    assert got and all(p is not None and 50.0 <= p <= 200.0 for p in got)
    assert 50.0 in got and 200.0 in got  # inclusive at both ends
    assert not [t for t in titles(again) if "Dear" in t or "Unpriced" in t or t.endswith("lo0")]
    pf = again["search"]["price_filter"]
    assert (pf["min"], pf["max"]) == (50.0, 200.0)
    assert pf["removed"] == 4  # 49.99, 200.01, 350 and the one with no price
    assert not [c for c in search.calls if "$50" in c[0]]  # a full batch: no extra page


def test_the_extra_page_only_when_the_filter_leaves_a_short_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    short = priced("a", [90.0, 95.0, 300.0, 400.0, None], "Short Rug")
    more = priced("m", [80.0, 85.0, 500.0], "More Rug")
    search = ByText([("under $100", [more]), ("rug", [shown_page(), short])])
    svc, sid = searched_svc(tmp_path, monkeypatch, search)
    svc.refine(sid, None, UNSET, {"max": 100})
    again = svc.shop_again(sid)
    assert [c for c in search.calls if "under $100" in c[0]] == [("area rug under $100", 1)]
    assert sorted(p for p in prices_of(again) if p is not None) == [80.0, 85.0, 90.0, 95.0]
    assert None not in prices_of(again)  # still short: never padded
    assert again["search"]["price_filter"]["removed"] == 4  # 300, 400, no price, and 500


def test_the_extra_page_failing_is_a_warning_not_a_failed_shop_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.shopping import ProviderError

    class Failing(ByText):
        def search(self, query: str, gl: str = "us", hl: str = "en", page: int = 1) -> Any:
            if "under $100" in query:
                with self._lock:
                    self.calls.append((query, page))
                raise ProviderError("serper", "search failed")
            return super().search(query, gl, hl, page)

    search = Failing([("rug", [shown_page(), priced("a", [90.0, 500.0], "Short Rug")])])
    svc, sid = searched_svc(tmp_path, monkeypatch, search)
    svc.refine(sid, None, UNSET, {"max": 100})
    again = svc.shop_again(sid)
    assert prices_of(again) == [90.0]
    assert any("price was unavailable" in w for w in again["search"]["warnings"])
    assert len([c for c in search.calls if "under $100" in c[0]]) == 1  # no retry


def test_price_with_filters_adds_price_words_to_the_targeted_fetch_and_tiers_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    targeted = [
        *priced("g", [60.0, 70.0, 500.0], "Gray Vintage Area Rug"),
        *priced("r", [65.0, 75.0, 800.0], "Red Persian Area Rug"),
    ]
    search = ByText([("gray", [targeted]), ("rug", [shown_page(), rugs("b", 2, "Green Rug")])])
    svc, sid = searched_svc(tmp_path, monkeypatch, search)
    svc.refine(sid, None, GRAY_BLUE, {"max": 100})
    again = svc.shop_again(sid)
    text = "area rug under $100 gray blue"  # price words first, then the filter words
    assert [c for c in search.calls if "gray" in c[0]] == [(text, 1), (text, 2)]  # + the one extra
    out = titles(again)
    assert all(p is not None and p <= 100 for p in prices_of(again))
    assert out[:2] == ["Gray Vintage Area Rug g0", "Gray Vintage Area Rug g1"]  # filter tier first
    assert not [t for t in out if t.endswith(("g2", "r2"))]  # the dear ones never show
    assert again["search"]["filters_match"]["satisfied_all"] == 2


def test_a_price_applies_to_every_following_shop_again_until_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page2 = priced("p", [40.0, 500.0, 60.0], "P Rug")
    page3 = priced("q", [70.0, 900.0], "Q Rug")
    search = ByText([("rug", [shown_page(), page2, page3, []])])
    svc, sid = searched_svc(tmp_path, monkeypatch, search)
    svc.refine(sid, None, UNSET, {"max": 100})
    for _ in range(2):
        view = svc.shop_again(sid)
        assert view["search"]["price_filter"]["max"] == 100.0
        assert all(p is not None and p <= 100 for p in prices_of(view))
    svc.remove_refine(sid, "price")
    assert svc.shop_again(sid)["search"]["price_filter"] is None


def test_no_price_behaves_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(root: Path, touch: bool) -> tuple[dict[str, Any], list[tuple[str, int]]]:
        page2 = priced("p", [40.0, 500.0, 60.0, None], "P Rug") + rugs("b", 12, "Green Rug")
        search = ByText([("rug", [shown_page(), page2, []])])
        svc, sid = searched_svc(root, monkeypatch, search)
        if touch:  # set a price and clear it again: the session must behave as if never touched
            svc.refine(sid, None, UNSET, {"min": 1, "max": 2})
            svc.refine(sid, None, UNSET, {})
        return svc.shop_again(sid), search.calls

    plain, plain_calls = run(tmp_path / "plain", False)
    touched, touched_calls = run(tmp_path / "touched", True)
    assert plain["search"]["price_filter"] is None and touched["search"]["price_filter"] is None
    assert titles(plain) == titles(touched) and prices_of(plain) == prices_of(touched)
    assert plain_calls == touched_calls  # the same fetches, in the same order
    assert plain["search"]["serper_calls"] == touched["search"]["serper_calls"]


def test_a_first_search_has_no_price_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service(tmp_path, monkeypatch, ByText([("rug", [rugs("a", 6, "Rug")])]))
    view = svc.search(None, "area rug")
    assert view["search"]["price_filter"] is None and view["search"]["kind"] == "search"
