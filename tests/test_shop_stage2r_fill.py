"""Stage 2r, phase 1: the fill loop. A batch short of 24 after every filter, cap and guard runs
on-request variant searches until it is full, a cap is reached, or two searches in a row add
nothing; each step and the stop reason are traced; "filtered out" is broken into causes."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import Config
from app.shop.models import FilterCounts
from app.shop.providers import ShopFixtureLookup
from app.shop.search import EXHAUSTED_MESSAGE, explain_breakdown, filter_breakdown
from app.shop.service import ShopService
from app.shop.store import SessionStore
from app.shopping import SearchCandidate
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)

REQUEST = "mid century dining chair"


class Script:
    """Serper stand-in: `base(page)` gives the typed words' candidates, `variant(n, page)` the nth
    other query's."""

    name = "serper"
    thread_safe = True

    def __init__(
        self,
        base: Callable[[int], list[tuple[str, str]]],
        variant: Callable[[int, int], list[tuple[str, str]]],
    ) -> None:
        self.base, self.variant = base, variant
        self.key = " ".join(re.findall(r"[a-z0-9]+", REQUEST.lower()))
        self.calls: list[tuple[str, int]] = []
        self.others: list[str] = []

    def search(
        self, query: str, gl: str = "us", hl: str = "en", page: int = 1
    ) -> list[SearchCandidate]:
        key = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
        self.calls.append((key, page))
        if key == self.key:
            rows = self.base(page)
            tag = "b"
        else:
            if key not in self.others:
                self.others.append(key)
            rows = self.variant(self.others.index(key), page)
            tag = f"v{self.others.index(key)}"
        return [
            SearchCandidate(
                f"{tag}-{page}-{n}", f"Mid Century Dining Chair {title}", seller, 60.0 + n,
                "USD", None, None, "https://www.google.com/search?q=x",
            )
            for n, (title, seller) in enumerate(rows)
        ]  # fmt: skip


def rows(n: int, sellers: int, tag: str = "x") -> list[tuple[str, str]]:
    return [(f"{tag}{i}", f"Seller {tag}{i % sellers}") for i in range(n)]


def service(tmp_path: Path, search: Script, **kw: Any) -> ShopService:
    cfg = Config(tracing_disabled=True, shop_thin_variants=True, **kw)
    return ShopService(cfg, SessionStore(tmp_path / "d"), search, ShopFixtureLookup())


def first(svc: ShopService) -> dict[str, Any]:
    return svc.search(None, REQUEST)["search"]


# ---- the trigger is the shown count --------------------------------------------------------------


def test_a_batch_short_after_the_seller_cap_runs_variants_until_it_is_full(tmp_path: Path) -> None:
    """24 candidates from three sellers: the raw pool is full, the shown batch is 6."""
    script = Script(lambda p: rows(24, 3) if p == 1 else [], lambda n, p: rows(12, 12, f"v{n}"))
    view = first(service(tmp_path, script))
    assert len(view["results"]) == 24
    fill = view["trace"]["fill"]
    assert (
        fill["shown_start"] < 24 and fill["shown_end"] == 24 and fill["stop"] == "the batch is full"
    )
    assert [s["shown_after"] for s in fill["steps"]] == sorted(
        s["shown_after"] for s in fill["steps"]
    )
    assert all(s["shown_after"] > s["shown_before"] for s in fill["steps"])
    assert view["serper_calls"] <= 4


def test_the_thin_trigger_reads_the_shown_count_not_the_pool_size(tmp_path: Path) -> None:
    """Case-06 style: a pool of 28 candidates, 18 shown after the cap, and one search call."""
    heavy = [(f"a{i}", "Big A") for i in range(14)] + [(f"b{i}", "Big B") for i in range(8)]
    heavy += rows(6, 6, "s")
    script = Script(lambda p: heavy if p == 1 else [], lambda n, p: rows(12, 12, f"w{n}"))
    view = first(service(tmp_path, script))
    thin = view["trace"]["thin"]
    assert thin["before"] < 24 and thin["after"] > thin["before"]  # the loop ran on 18 shown
    assert any(c[0] != script.key for c in script.calls)


def test_the_first_search_cap_holds(tmp_path: Path) -> None:
    script = Script(lambda p: rows(4, 4) if p == 1 else [], lambda n, p: rows(2, 2, f"v{n}{p}"))
    view = first(service(tmp_path, script))
    assert view["serper_calls"] <= 4 and len(script.calls) <= 4
    assert view["trace"]["fill"]["call_limit"] == 4
    assert "search cap (4 calls)" in view["trace"]["fill"]["stop"]


def test_a_full_batch_runs_no_fill(tmp_path: Path) -> None:
    script = Script(lambda p: rows(40, 14) if p == 1 else [], lambda n, p: rows(5, 5))
    view = first(service(tmp_path, script))
    assert len(view["results"]) == 24 and "fill" not in view["trace"]


# ---- a Shop Again --------------------------------------------------------------------------------


def again_view(tmp_path: Path, script: Script, **kw: Any) -> tuple[ShopService, dict[str, Any]]:
    svc = service(tmp_path, script, **kw)
    view = svc.search(None, REQUEST)
    sid = view["session_id"]
    for r in view["search"]["results"][:3]:
        svc.rate(sid, r["id"], "like")
    return svc, svc.shop_again(sid)["search"]


def test_the_loop_stops_after_two_searches_in_a_row_add_nothing(tmp_path: Path) -> None:
    script = Script(
        lambda p: rows(6, 6, f"p{p}") if p <= 3 else [],
        lambda n, p: rows(3, 3, f"v{n}") if n == 0 and p == 1 else [],
    )
    svc, view = again_view(tmp_path, script)
    fill = view["trace"]["batch"].get("fill", {})
    assert fill and len(view["results"]) < 24
    assert "2 searches in a row added nothing new" in fill["stop"], fill["stop"]
    empty = [s for s in fill["steps"] if not s["new_candidates"]]
    assert len(empty) >= 2 and view["serper_calls"] <= svc.config.shop_again_max_serper_calls_total


def test_the_ninecall_cap_of_a_shop_again_holds_in_the_fill_loop(tmp_path: Path) -> None:
    script = Script(
        lambda p: rows(6, 6, f"p{p}") if p <= 2 else [],
        lambda n, p: rows(2, 2, f"v{n}p{p}"),  # every variant finds two new ones: it never gives up
    )
    svc, view = again_view(tmp_path, script)
    assert view["serper_calls"] <= 9
    fill = view["trace"]["batch"]["fill"]
    assert fill["stop"] in {"the search cap (9 calls) was reached", "no more variants to try"}


def test_fill_steps_record_kind_query_new_candidates_and_the_shown_count(tmp_path: Path) -> None:
    script = Script(lambda p: rows(8, 8) if p == 1 else [], lambda n, p: rows(8, 8, f"v{n}"))
    _, view = again_view(tmp_path, script, shop_model_queries=True)
    fill = view["trace"]["batch"]["fill"]
    for step in fill["steps"]:
        assert {
            "kind",
            "query",
            "page",
            "found",
            "new_candidates",
            "shown_before",
            "shown_after",
        } <= set(step)
        assert step["kind"] in {"writer", "filler"}
    assert fill["shown_end"] == len(view["results"]) and fill["target"] == 24


def test_variants_in_the_fill_loop_keep_every_hard_word(tmp_path: Path) -> None:
    script = Script(lambda p: rows(6, 6) if p == 1 else [], lambda n, p: rows(10, 10, f"v{n}"))
    first(service(tmp_path, script))
    assert script.others, "the fill loop should have searched variants"
    for text in script.others:
        words = set(text.split())
        assert {"mid", "century", "dining", "chair"} <= words, text
        assert not words & {"modern", "classic", "contemporary", "upholstered", "wooden"}


def test_new_items_carry_their_source(tmp_path: Path) -> None:
    script = Script(lambda p: rows(24, 3) if p == 1 else [], lambda n, p: rows(12, 12, f"v{n}"))
    view = first(service(tmp_path, script))
    kinds = {r["source"]["kind"] for r in view["results"]}
    assert kinds == {"base", "thin_variant"}
    svc, again = again_view(tmp_path, script, shop_model_queries=True)
    assert {r["source"]["kind"] for r in again["results"]} <= {
        "base", "writer", "filler", "seller", "peer", "supply", "refill",
    }  # fmt: skip


# ---- exhaustion is unchanged ---------------------------------------------------------------------


def test_a_truly_exhausted_pool_still_says_so(tmp_path: Path) -> None:
    script = Script(lambda p: rows(12, 12) if p == 1 else [], lambda n, p: [])
    svc = service(tmp_path, script, shop_model_queries=True)
    view = svc.search(None, REQUEST)
    sid = view["session_id"]
    for r in view["search"]["results"][:3]:
        svc.rate(sid, r["id"], "like")
    for _ in range(3):
        again = svc.shop_again(sid)["search"]
        if not again["results"]:
            break
    assert not again["results"] and EXHAUSTED_MESSAGE in again["warnings"]
    assert again["trace"]["batch"]["exhausted"]["breakdown"]["returned"] >= 0


# ---- the breakdown -------------------------------------------------------------------------------


def counts(**kw: int) -> FilterCounts:
    return FilterCounts(**kw)


def test_the_breakdown_sums_to_what_the_searches_returned() -> None:
    c = counts(returned=200, no_price=10, out_of_range=40, wrong_category=60, price_outliers=2,
               duplicates=20, seen_excluded=30, kept=38)  # fmt: skip
    parts = filter_breakdown(c, shown=24, seller_capped=9, guarded=3)
    assert parts["returned"] == 200 and parts["shown"] == 24
    named = [k for k in parts if k not in ("returned", "shown")]
    assert sum(parts[k] for k in named) + parts["shown"] == 200
    assert parts["price"] == 50 and parts["category_or_size"] == 60 and parts["seller_cap"] == 9
    assert parts["other"] == 200 - 24 - 50 - 60 - 2 - 30 - 20 - 9 - 3


def test_the_breakdown_never_counts_more_than_was_returned() -> None:
    c = counts(returned=50, no_price=0, wrong_category=40, seen_excluded=5, duplicates=5, kept=0)
    parts = filter_breakdown(c, shown=0, seller_capped=7, guarded=7)
    assert parts["seller_cap"] == 0 and parts["diversity_guard"] == 0 and parts["other"] == 0


def test_the_sentence_keeps_the_old_numbers_and_names_the_causes() -> None:
    c = counts(returned=130, out_of_range=100, wrong_category=10, seen_excluded=5, kept=15)
    parts = filter_breakdown(c, shown=5, seller_capped=6, guarded=2)
    text = explain_breakdown(c, 110, parts)
    assert text.startswith("5 seen or rated, 0 duplicates, 110 filtered out (")
    assert "100 outside your price" in text and "10 the wrong category or size" in text
    assert "6 held back by the seller cap" in text and "2 by diversity guards" in text
    plain = explain_breakdown(
        counts(returned=5, kept=5), 0, filter_breakdown(counts(returned=5, kept=5), 5, 0, 0)
    )
    assert plain == "0 seen or rated, 0 duplicates, 0 filtered out"


def test_a_short_batch_message_carries_the_breakdown_and_the_fill_stop(tmp_path: Path) -> None:
    script = Script(lambda p: rows(10, 10) if p == 1 else [], lambda n, p: [])
    view = first(service(tmp_path, script))
    banner = [w for w in view["warnings"] if w.startswith("Only ")]
    assert len(banner) == 1 and "searched" in banner[0] and "stopped:" in banner[0]
    assert re.fullmatch(r"Only \d+ results matched.*; try broadening the request\.", banner[0])
