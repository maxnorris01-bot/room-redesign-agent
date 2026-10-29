"""Stage 2q, phase 3: the per-seller cap relaxes before the batch shrinks. The first walk keeps
clamp(floor(0.30 x batch size), 2, 4); a short batch from a pool that could fill one raises the cap
one step at a time (5, 6, 7) while no seller holds more than 0.30 of the batch actually shown."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from evals.shop import scorers as sc

from app.config import Config
from app.shop.models import ShopQuery
from app.shop.providers import ShopFixtureLookup
from app.shop.search import build_results, seller_cap_info, top_seller_share
from app.shop.service import ShopService
from app.shop.store import SessionStore
from app.shop.thresholds import RESULT_COUNT, SELLER_CAP_SHARE, seller_cap, seller_cap_ceiling
from app.shopping import SearchCandidate
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)


def pool_of(counts: dict[str, int]) -> list[tuple[SearchCandidate, str]]:
    """Candidates in rank order, interleaved by seller (round robin), as a live ranking does."""
    ordered: list[tuple[SearchCandidate, str]] = []
    n = 0
    for rank in range(max(counts.values())):
        for seller, k in counts.items():
            if rank < k:
                n += 1
                ordered.append((
                    SearchCandidate(f"p{n}", f"Rug {n} 8x10 {seller}", seller, 100.0 + n, "USD",
                                    None, None, "https://www.google.com/search?q=x"),
                    "",
                ))  # fmt: skip
    return ordered


def walk(counts: dict[str, int], **kw: Any) -> tuple[list[Any], list[dict[str, Any]]]:
    ordered = pool_of(counts)
    index = {c.product_id: i + 1 for i, (c, _) in enumerate(ordered)}
    q = ShopQuery(request="8x10 rug", search_text="8x10 rug", category="rug")
    log: list[dict[str, Any]] = []
    results, _, _ = build_results(ordered, q, index, relax_log=log, **kw)
    return results, log


def by_seller(results: list[Any]) -> Counter[str]:
    return Counter(r.seller for r in results)


RUG_LIKE = {
    "Lowe's": 14,
    "Walmart": 7,
    "Home Depot": 4,
    "Rugs USA": 4,
    "Wayfair": 3,
    "Kohl's": 2,
    "Rugs.com": 2,
    "Target": 2,
    "garvee": 2,
    "Boutique Rugs": 1,
    "Chewy.com": 1,
    "IKEA": 1,
    "Rugs Direct": 1,
}  # 44 items, 13 sellers, the case-06 pool of the post-2p re-run


def test_the_rug_like_pool_fills_a_batch_inside_the_first_cap() -> None:
    assert sum(RUG_LIKE.values()) == 44 and len(RUG_LIKE) == 13
    results, log = walk(RUG_LIKE)
    assert len(results) == RESULT_COUNT and log == []  # no relaxation needed
    assert max(by_seller(results).values()) <= seller_cap(RESULT_COUNT) == 4
    assert top_seller_share(results) <= SELLER_CAP_SHARE


# a pool where the first cap runs short: two big sellers, eleven with one rug each
SHORT_AT_FOUR = {"Lowe's": 20, "Walmart": 13, **{f"Shop {i}": 1 for i in range(11)}}


def test_a_short_batch_raises_the_cap_one_step_at_a_time_until_it_is_full() -> None:
    results, log = walk(SHORT_AT_FOUR)
    assert len(results) == RESULT_COUNT
    assert [(x["cap"], x["size"], x["kept"]) for x in log] == [
        (5, 21, True), (6, 23, True), (7, 24, True),
    ]  # fmt: skip
    assert max(by_seller(results).values()) == 7 and top_seller_share(results) <= SELLER_CAP_SHARE
    assert seller_cap_ceiling(RESULT_COUNT) == 7


def test_the_relaxation_stops_where_the_batch_would_break_the_share() -> None:
    """Ten sellers: cap 6 gives 20 (a share of exactly 0.30), cap 7 gives 22 and 7/22 is over it,
    so the batch stays at the largest one that holds the share."""
    pool = {"Lowe's": 20, "Walmart": 13, **{f"Shop {i}": 1 for i in range(8)}}
    results, log = walk(pool)
    assert len(results) == 20 and top_seller_share(results) <= SELLER_CAP_SHARE
    assert [(x["cap"], x["kept"]) for x in log] == [(5, True), (6, True), (7, False)]
    assert log[-1]["top_share"] > SELLER_CAP_SHARE


def test_three_sellers_stay_short_and_nothing_is_relaxed_into_one_seller() -> None:
    three = {"Lowe's": 12, "Walmart": 10, "Target": 8}
    results, log = walk(three)
    assert len(results) == 6 and by_seller(results) == Counter(
        {"Lowe's": 2, "Walmart": 2, "Target": 2}
    )
    assert log and not any(x["kept"] for x in log)  # every step would give a seller a third or more


def test_a_first_batch_pool_is_unchanged_and_logs_nothing() -> None:
    first = {f"Seller {i}": 3 for i in range(14)}  # 42 items, 14 sellers: case-04's first search
    results, log = walk(first)
    assert len(results) == RESULT_COUNT and log == []
    assert max(by_seller(results).values()) <= 4


def test_a_small_pool_is_not_relaxed() -> None:
    small = {"Lowe's": 6, "Walmart": 5, "Target": 4, "IKEA": 3}  # 18 items: it cannot reach 24
    results, log = walk(small)
    assert log == [] and len(results) <= 18


def test_affinity_sellers_keep_their_own_cap() -> None:
    results, _ = walk(RUG_LIKE, affinity_sellers={"lowe's"})
    assert len(results) == RESULT_COUNT


def test_the_trace_names_the_cap_it_settled_on() -> None:
    _, log = walk(SHORT_AT_FOUR)
    info = seller_cap_info(log, 44)
    assert info["base"] == 4 and info["final"] == 7 and len(info["steps"]) == 3
    assert info["message"] == (
        "A few sellers dominate this category; allowed up to 7 per seller to fill the batch."
    )
    assert seller_cap_info([], 44) == {}
    _, none = walk({"Lowe's": 12, "Walmart": 10, "Target": 8})
    assert "message" not in seller_cap_info(none, 30) and seller_cap_info(none, 30)["final"] is None


def test_the_eval_seller_scorer_reads_a_relaxed_batch_unchanged() -> None:
    results, _ = walk(SHORT_AT_FOUR)
    listings = [sc.Listing(r.id, r.title, r.price, r.seller) for r in results]
    got = sc.seller_diversity(listings)
    assert got["top"] == 7 and got["distinct"] == 13
    assert got["top_share"] == pytest.approx(7 / 24) and got["top_share"] <= 0.30


def test_a_search_through_the_service_records_the_steps(tmp_path: Path) -> None:
    class Many:
        name = "serper"
        thread_safe = True

        def search(
            self, query: str, gl: str = "us", hl: str = "en", page: int = 1
        ) -> list[SearchCandidate]:
            if page > 1:
                return []
            items = pool_of(SHORT_AT_FOUR)
            return [
                SearchCandidate(c.product_id, re.sub("Rug", "Wool Area Rug", c.title), c.seller,
                                c.price, "USD", None, None, c.google_panel_url)
                for c, _ in items
            ]  # fmt: skip

    store = SessionStore(tmp_path / "d")
    svc = ShopService(Config(tracing_disabled=True), store, Many(), ShopFixtureLookup())
    view = svc.search(None, "wool area rug")["search"]
    assert len(view["results"]) == RESULT_COUNT
    cap = view["trace"]["seller_cap"]
    assert cap["final"] == 7 and "allowed up to 7 per seller" in cap["message"]
    shares = Counter(r["seller"] for r in view["results"])
    assert max(shares.values()) / RESULT_COUNT <= 0.30
