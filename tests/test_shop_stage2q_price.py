"""Stage 2q, phase 4: the price outlier guard. An implausibly cheap furniture listing (the $2.23
"Evo Mesh Backrest Ergonomic Office Chair" of the post-2p re-run) is dropped, conservatively."""

from __future__ import annotations

import re
from pathlib import Path

from evals.shop import scorers as sc

from app.config import Config
from app.shop import thresholds as th
from app.shop.models import ShopQuery
from app.shop.pricecheck import guarded_category, split_price_outliers
from app.shop.providers import ShopFixtureLookup
from app.shop.search import apply_filters
from app.shop.service import ShopService
from app.shop.store import SessionStore
from app.shopping import SearchCandidate
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)


def chair(i: int, price: float | None, title: str = "") -> SearchCandidate:
    return SearchCandidate(
        f"c{i}", title or f"Ergonomic Office Chair model {i}", f"Shop {i % 5}", price, "USD", None,
        None, "https://www.google.com/search?q=x",
    )  # fmt: skip


def pool(prices: list[float | None]) -> list[SearchCandidate]:
    return [chair(i, p) for i, p in enumerate(prices)]


MESH_POOL = [129.0, 149.0, 189.0, 219.0, 249.0, 159.0, 179.0, 89.0, 56.99, 39.0, 99.0, 139.0]


def test_the_constants_are_named_with_their_reasons() -> None:
    assert th.PRICE_OUTLIER_MEDIAN_SHARE == 0.10 and th.PRICE_OUTLIER_FLOOR_USD == 15.0
    assert th.PRICE_OUTLIER_MIN_POOL == 8 and "desk chair" in th.PRICE_GUARD_CATEGORIES
    names = {"PRICE_OUTLIER_MEDIAN_SHARE", "PRICE_OUTLIER_FLOOR_USD", "PRICE_OUTLIER_MIN_POOL"}
    assert names <= set(th.all_thresholds())


def test_the_two_dollar_chair_is_dropped_with_its_reason() -> None:
    kept, dropped = split_price_outliers(pool([*MESH_POOL, 2.23]), "desk chair")
    assert len(dropped) == 1 and dropped[0]["price"] == 2.23
    assert (
        "under 10% of the pool's median" in dropped[0]["reason"] and "$15" in dropped[0]["reason"]
    )
    assert 2.23 not in [c.price for c in kept] and len(kept) == len(MESH_POOL)


def test_a_39_dollar_chair_and_a_56_dollar_chair_stay() -> None:
    kept, dropped = split_price_outliers(pool(MESH_POOL), "desk chair")
    assert dropped == [] and {39.0, 56.99} <= {c.price for c in kept}


def test_a_low_priced_item_in_a_low_priced_pool_stays() -> None:
    cheap = [30.0, 35.0, 40.0, 42.0, 45.0, 38.0, 36.0, 12.0, 44.0, 41.0]  # median 39.5
    kept, dropped = split_price_outliers(pool(cheap), "desk chair")
    assert dropped == [] and 12.0 in [c.price for c in kept]  # 10% of the median is $3.95


def test_the_thresholds_need_both_tests_to_hold() -> None:
    dear = [900.0, 1100.0, 1250.0, 1400.0, 1500.0, 1000.0, 1300.0, 1200.0]
    _, dropped = split_price_outliers(pool([*dear, 40.0]), "sofa")  # under 10% of $1,200, over $15
    assert dropped == []
    _, dropped = split_price_outliers(pool([*dear, 14.0]), "sofa")
    assert [d["price"] for d in dropped] == [14.0]


def test_unpriced_candidates_keep_todays_handling() -> None:
    kept, dropped = split_price_outliers(pool([*MESH_POOL, None]), "desk chair")
    assert dropped == [] and None in [c.price for c in kept]
    q = ShopQuery(request="mesh desk chairs", search_text="mesh desk chair", category="desk chair",
                  category_key="desk chair")  # fmt: skip
    pool_all, counts = apply_filters(pool([*MESH_POOL, None]), q)
    assert counts.no_price == 0 and None in [c.price for c in pool_all]  # no price filter: kept
    priced = q.model_copy(update={"price_max": 300.0})
    pool_priced, counts = apply_filters(pool([*MESH_POOL, None]), priced)
    assert counts.no_price == 1 and counts.price_outliers == 0


def test_only_furniture_categories_are_checked_and_small_pools_are_left_alone() -> None:
    assert guarded_category("desk chair") and guarded_category("sofa")
    assert not guarded_category("rug") and not guarded_category("lamp")
    assert not guarded_category(None) and not guarded_category("curtain")
    kept, dropped = split_price_outliers(pool([*MESH_POOL, 2.23]), "rug")
    assert dropped == [] and len(kept) == len(MESH_POOL) + 1
    kept, dropped = split_price_outliers(pool([120.0, 130.0, 2.23]), "desk chair")  # 3 priced
    assert dropped == []


def test_apply_filters_counts_and_lists_the_dropped_listing() -> None:
    q = ShopQuery(request="mesh desk chairs", search_text="mesh desk chair", category="desk chair",
                  category_key="desk chair")  # fmt: skip
    out, counts = apply_filters(pool([*MESH_POOL, 2.23]), q)
    assert counts.price_outliers == 1 and counts.kept == len(out) == len(MESH_POOL)
    assert counts.price_outlier_rows[0]["price"] == 2.23
    assert "http" not in str(counts.price_outlier_rows)


def test_a_search_drops_the_outlier_and_says_so_in_the_trace(tmp_path: Path) -> None:
    class Chairs:
        name = "serper"
        thread_safe = True

        def search(
            self, query: str, gl: str = "us", hl: str = "en", page: int = 1
        ) -> list[SearchCandidate]:
            if page > 1:
                return []
            prices = [*MESH_POOL, 2.23, *[100.0 + 7 * i for i in range(16)]]
            return [
                chair(i, p, f"Ergonomic Mesh Desk Chair {i}" if p != 2.23 else
                      "Evo Mesh Backrest Ergonomic Office Chair")
                for i, p in enumerate(prices)
            ]  # fmt: skip

    store = SessionStore(tmp_path / "d")
    svc = ShopService(Config(tracing_disabled=True), store, Chairs(), ShopFixtureLookup())
    view = svc.search(None, "ergonomic mesh desk chairs")["search"]
    assert 2.23 not in [r["price"] for r in view["results"]]
    info = view["trace"]["price_outliers"]
    assert info["count"] == 1 and info["dropped"][0]["price"] == 2.23
    assert re.search(r"Left out 1 listing\(s\) priced implausibly low", info["message"])


def test_the_price_compliance_scorer_is_unchanged() -> None:
    items = [sc.Listing("a", "x", 40.0, "s"), sc.Listing("b", "y", 2.23, "s"),
             sc.Listing("c", "z", None, "s")]  # fmt: skip
    got = sc.price_compliance(items, None, 150.0)
    assert got["checked"] == 3 and got["within"] == 2 and got["unpriced"] == 1
    assert got["share"] == 2 / 3
