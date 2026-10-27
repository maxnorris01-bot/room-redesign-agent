"""Shop Mode stage 2m: customer ratings on the cards (display only) and the "More photos" gallery,
which shares one product lookup per result with Open.

Offline like the other Shop Mode tests (same loopback-only socket guard).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.shop.providers import build_shop_providers
from app.shop.search import build_results
from app.shop.service import ApiError, ShopService
from app.shop.store import SessionStore
from app.shopping import ProductDetails, ProviderError, parse_product_details
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
from test_shop_learn import COFFEE, mock_service

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "app" / "shop" / "static"
FIXTURES = ROOT / "tests" / "fixtures"


def shown_ids(view: dict[str, Any]) -> list[str]:
    return [r["id"] for r in view["search"]["results"]]


# ---- what the providers give for ratings ---------------------------------------------------------


def gallery_service(tmp_path: Path, **kw: Any) -> ShopService:
    """The gallery is dormant by default (stage 2o); these tests run with it on."""
    cfg = config(shop_gallery=True)
    search, lookup = build_shop_providers(cfg)
    return ShopService(cfg, SessionStore(tmp_path / "shop-data"), search, lookup, **kw)


def test_the_serper_fixtures_carry_rating_and_review_count_for_most_results() -> None:
    for name in ("serper_coffee_tables", "serper_couches", "serper_curtains"):
        data = json.loads((FIXTURES / "shop" / f"{name}.json").read_text())["shopping"]
        rated = [
            r for r in data if r.get("rating") is not None and r.get("ratingCount") is not None
        ]
        assert len(rated) >= len(data) * 0.7, name  # a few have none, on purpose
        assert any("rating" not in r or r.get("rating") is None for r in data) or len(rated) == len(
            data
        )
    # Serper's field names are `rating` and `ratingCount`; SerpApi's shopping search says `reviews`
    assert "ratingCount" in (ROOT / "src" / "app" / "shopping.py").read_text()
    assert 'item.get("reviews")' in (ROOT / "src" / "app" / "shopping.py").read_text()


def test_a_product_lookup_may_carry_a_rating_a_review_count_and_photos() -> None:
    payload = json.loads((FIXTURES / "shopping" / "serpapi_product_article.json").read_text())
    details = parse_product_details(payload)
    assert details.rating == 4.6 and details.rating_count == 1203
    assert len(details.images) == 6 and all(u.startswith("https://") for u in details.images)
    assert "video.mp4" not in " ".join(details.images)  # only images
    bare = parse_product_details({"product_results": {"title": "x", "stores": []}})
    assert bare.rating is None and bare.rating_count is None and bare.images == []
    bad = parse_product_details(
        {
            "product_results": {
                "rating": 9, "reviews": "many",
                "media": [{"type": "image", "link": "http://insecure.test/a.jpg"},
                          {"type": "image", "link": "https://ok.test/a.jpg"},
                          {"type": "image", "link": "https://ok.test/a.jpg"},
                          {"link": "javascript:alert(1)"}],
            }
        }
    )  # fmt: skip
    assert (
        bad.rating is None and bad.rating_count is None and bad.images == ["https://ok.test/a.jpg"]
    )


# ---- on the result -----------------------------------------------------------------------------------


def test_results_carry_the_rating_and_count_the_search_gave_and_nothing_invented() -> None:
    pool = [
        (cand("a", rating=4.6, rating_count=1203, title="A"), ""),
        (cand("b", rating=None, rating_count=None, title="B"), ""),
        (cand("c", rating=0.0, rating_count=-5, title="C"), ""),
        (cand("d", rating=7.5, rating_count=10, title="D"), ""),
        (cand("e", rating=4.0, rating_count=None, title="E"), ""),
    ]
    index = {c.product_id: i + 1 for i, (c, _) in enumerate(pool)}
    out, _, _ = build_results(pool, query(), index)
    by = {r.title: r for r in out}
    assert (by["A"].rating, by["A"].rating_count) == (4.6, 1203)
    assert (by["B"].rating, by["B"].rating_count) == (None, None)
    assert (by["C"].rating, by["C"].rating_count) == (None, None)  # 0 stars and -5 are not data
    assert (by["D"].rating, by["D"].rating_count) == (None, 10)  # 7.5 of 5 is not a rating
    assert (by["E"].rating, by["E"].rating_count) == (4.0, None)


def test_a_search_view_reports_how_many_shown_results_came_with_ratings(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    view = svc.search(None, COFFEE)
    found = view["search"]
    with_rating = [r for r in found["results"] if r["rating"] is not None]
    assert found["trace"]["reviews"] == {"with_rating": len(with_rating), "total": 24}
    assert with_rating and all(0 < r["rating"] <= 5 for r in with_rating)
    assert all(r["rating_count"] is None or r["rating_count"] >= 0 for r in found["results"])


def test_the_open_lookup_fills_in_a_missing_rating_and_never_overwrites_one(tmp_path: Path) -> None:
    import app.shop.providers as providers

    use = FakeSearch(
        [
            [
                cand("a1", 50.0, title="No Rating Table", rating=None, rating_count=None),
                cand("a2", 51.0, title="Rated Table", rating=3.2, rating_count=7),
            ]
        ]
    )
    svc = make_service(tmp_path, use, providers.ShopFixtureLookup(), config())
    view = svc.search(None, "tables")
    sid = view["session_id"]
    by = {r["title"]: r for r in view["search"]["results"]}
    assert by["No Rating Table"]["rating"] is None
    out = svc.resolve(sid, by["No Rating Table"]["id"])
    assert out["rating"] == {"rating": 4.6, "rating_count": 1203}  # the lookup's, not an estimate
    kept = svc.resolve(sid, by["Rated Table"]["id"])
    assert kept["rating"] == {"rating": 3.2, "rating_count": 7}  # the search's stays
    saved = svc.store.load(sid)
    assert saved.results[by["No Rating Table"]["id"]].rating == 4.6
    assert svc.session_view(sid)["search"]["results"][0]["rating"] in (4.6, 3.2)


def test_ratings_are_display_only_they_change_no_cap_or_order() -> None:
    ordered = [
        (cand(f"p{i}", 50.0 + i, title=f"Oak Table {i}", seller=f"S{i}"), "") for i in range(30)
    ]
    index = {c.product_id: i + 1 for i, (c, _) in enumerate(ordered)}
    plain, _, _ = build_results(ordered, query(), index)
    starred = [
        (c.__class__(**{**c.__dict__, "rating": 1.0 + i % 4, "rating_count": i}), r)
        for i, (c, r) in enumerate(ordered)
    ]
    again, _, _ = build_results(starred, query(), index)
    assert [r.product_id for r in plain] == [r.product_id for r in again]
    import app.shop.decide as decide
    import app.shop.evidence as evidence

    for module in (evidence, decide):
        assert "rating_count" not in Path(module.__file__ or "").read_text()


# ---- the page --------------------------------------------------------------------------------------------


def test_the_page_shows_stars_with_accessible_text_and_nothing_when_there_is_no_rating() -> None:
    js = (STATIC / "app.js").read_text()
    css = (STATIC / "style.css").read_text()
    assert "function ratingLine" in js and "rating-row" in js
    assert "'rated ' + shown + ' out of 5'" in js and "' reviews'" in js
    assert "toLocaleString('en-US')" in js and "role: 'img'" in js and "'aria-hidden': 'true'" in js
    assert "el.hidden = true" in js  # no rating, no line
    assert "data.rating" in js and ".rating-row" in css
    assert "innerHTML" not in js
    assert "Customer ratings:" in js  # the trace line


# ---- more photos (stage 2m, part 2) ---------------------------------------------------------------------


def first_with(view: dict[str, Any], ends_with_nine: bool = False) -> dict[str, Any]:
    for r in view["search"]["results"]:
        if r["product_id"].endswith("9") == ends_with_nine:
            return r
    raise AssertionError("no such result")


def test_the_gallery_returns_up_to_six_https_photos_from_one_shared_lookup(tmp_path: Path) -> None:
    svc = gallery_service(tmp_path)
    view = svc.search(None, COFFEE)
    sid, card = view["session_id"], first_with(view)
    out = svc.photos(sid, card["id"])
    assert out["fresh"] is True and out["message"] == "" and out["lookups_used"] == 1
    assert len(out["images"]) == 6 and all(u.startswith("https://") for u in out["images"])
    again = svc.photos(sid, card["id"])
    assert (
        again["fresh"] is False and again["lookups_used"] == 1 and again["images"] == out["images"]
    )
    saved = svc.store.load(sid)
    assert saved.lookups_used == 1 and saved.lookup_usd == pytest.approx(0.025)
    assert svc.session_view(sid)["photos_cached"] == [card["id"]]
    assert out["rating"]["rating"] in (card["rating"], 4.6)


def test_a_gallery_after_open_costs_nothing_and_open_after_a_gallery_does_not_look_up_twice(
    tmp_path: Path,
) -> None:
    svc = gallery_service(tmp_path)
    view = svc.search(None, COFFEE)
    sid = view["session_id"]
    a, b = view["search"]["results"][0], view["search"]["results"][1]
    svc.resolve(sid, a["id"])  # Open first
    free = svc.photos(sid, a["id"])
    assert free["fresh"] is False and free["lookups_used"] == 1 and len(free["images"]) == 6
    svc.photos(sid, b["id"])  # the gallery first
    opened = svc.resolve(sid, b["id"])
    assert opened["lookups_used"] == 2 and opened["resolved"]["link"]
    session = svc.store.load(sid)
    assert session.lookups_used == 2 and session.lookup_usd == pytest.approx(0.05)


def test_the_cap_is_shared_and_the_refusal_says_why(tmp_path: Path) -> None:
    cfg = Config(tracing_disabled=True, shop_lookups_per_session=1, shop_gallery=True)
    from app.shop.providers import build_shop_providers

    search, lookup = build_shop_providers(cfg)
    svc = ShopService(cfg, SessionStore(tmp_path / "shop-data"), search, lookup)
    view = svc.search(None, COFFEE)
    sid = view["session_id"]
    a, b = view["search"]["results"][0], view["search"]["results"][1]
    svc.photos(sid, a["id"])
    with pytest.raises(ApiError) as err:
        svc.photos(sid, b["id"])
    assert (
        err.value.status == 429
        and "Lookup limit for this session reached (1 of 1)" in err.value.message
    )
    assert svc.photos(sid, a["id"])["fresh"] is False  # the one already looked up stays free
    assert svc.resolve(sid, b["id"])["resolved"]["status"] == "cap_reached"  # Open obeys it too


def test_a_product_with_nothing_more_says_so_and_is_counted_once(tmp_path: Path) -> None:
    svc = gallery_service(tmp_path)
    view = svc.search(None, COFFEE)
    sid = view["session_id"]
    odd = None
    for r in view["search"]["results"]:
        if r["product_id"].endswith("9"):
            odd = r
            break
    if odd is None:  # make one: the mock lookup has nothing for ids ending in 9
        session = svc.store.load(sid)
        first = next(iter(session.results.values()))
        session.results[first.id] = first.model_copy(update={"product_id": "p99999"})
        svc.store.save(session)
        odd = {"id": first.id}
    out = svc.photos(sid, odd["id"])
    assert out["images"] == [] and out["message"] == "No more photos were found for this product."
    assert svc.photos(sid, odd["id"])["lookups_used"] == 1


class FailingLookup:
    name = "serpapi"

    def __init__(self, status: int | None = 500) -> None:
        self.status = status
        self.calls = 0

    def lookup(self, product_id: str, gl: str = "us", hl: str = "en") -> ProductDetails:
        self.calls += 1
        raise ProviderError("serpapi", "boom", status=self.status)


def test_a_failed_lookup_shows_a_short_message_and_does_not_count_twice(tmp_path: Path) -> None:
    use = FailingLookup()
    svc = make_service(
        tmp_path,
        FakeSearch([[cand("a1", 40.0, title="Wood Table")]]),
        use,
        config(shop_gallery=True),
    )
    view = svc.search(None, "tables")
    sid, rid = view["session_id"], view["search"]["results"][0]["id"]
    out = svc.photos(sid, rid)
    assert (
        out["images"] == [] and out["message"] == "The photos could not be loaded for this product."
    )
    assert svc.photos(sid, rid)["lookups_used"] == 1 and use.calls == 1
    fallback = svc.resolve(sid, rid)  # Open does not try again either
    assert use.calls == 1 and fallback["resolved"]["status"] == "fallback"
    assert svc.store.load(sid).lookups_used == 1


def test_a_bad_key_is_an_error_and_is_not_cached(tmp_path: Path) -> None:
    use = FailingLookup(status=401)
    svc = make_service(
        tmp_path,
        FakeSearch([[cand("a1", 40.0, title="Wood Table")]]),
        use,
        config(shop_gallery=True),
    )
    view = svc.search(None, "tables")
    sid, rid = view["session_id"], view["search"]["results"][0]["id"]
    with pytest.raises(ApiError) as err:
        svc.photos(sid, rid)
    assert err.value.status == 502 and "key" not in err.value.message.lower().replace("keys", "")
    assert svc.store.load(sid).lookups_used == 0 and not svc.store.load(sid).product_cache


def test_only_https_images_reach_the_page_and_a_card_without_an_id_has_no_gallery(
    tmp_path: Path,
) -> None:
    class Mixed:
        name = "serpapi"

        def lookup(self, product_id: str, gl: str = "us", hl: str = "en") -> ProductDetails:
            return ProductDetails(
                images=[
                    "http://insecure.test/a.jpg",
                    "https://ok.test/1.jpg",
                    "javascript:alert(1)",
                    *[f"https://ok.test/{i}.jpg" for i in range(2, 12)],
                ]
            )

    svc = make_service(
        tmp_path,
        FakeSearch([[cand("a1", 40.0, title="Wood Table")]]),
        Mixed(),
        config(shop_gallery=True),
    )
    view = svc.search(None, "tables")
    sid, rid = view["session_id"], view["search"]["results"][0]["id"]
    out = svc.photos(sid, rid)
    assert out["images"] == [f"https://ok.test/{i}.jpg" for i in range(1, 7)]
    session = svc.store.load(sid)
    session.results[rid] = session.results[rid].model_copy(update={"product_id": ""})
    svc.store.save(session)
    with pytest.raises(ApiError) as err:
        svc.photos(sid, rid)
    assert err.value.status == 400
    with pytest.raises(ApiError) as err:
        svc.photos(sid, "nope")
    assert err.value.status == 404
    with pytest.raises(ApiError) as err:
        svc.photos(sid, 5)
    assert err.value.status == 400


def test_the_page_has_an_accessible_gallery_and_the_csp_needed_no_change() -> None:
    js = (STATIC / "app.js").read_text()
    css = (STATIC / "style.css").read_text()
    assert "More photos" in js and "'aria-expanded'" in js and "Previous photo" in js
    assert "ArrowLeft" in js and "ArrowRight" in js and "indexOf('https://') === 0" in js
    assert "referrerPolicy = 'no-referrer'" in js and "aria-live" in js
    assert "no more photos can be loaded" in js and "/api/photos" in js
    assert "if (r.product_id && state.galleryOn)" in js  # no id, or the gallery off: no button
    assert ".gallery" in css and "innerHTML" not in js
    from app.shop.server import API_PATHS, CSP

    assert "/api/photos" in API_PATHS
    assert "img-src https:" in CSP and "default-src 'none'" in CSP and "unsafe-inline" not in CSP
