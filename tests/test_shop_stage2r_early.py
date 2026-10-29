"""Stage 2r, phase 2: a Shop Again stops its optional searches when the unseen pool is plentiful
and the quotas, focus and guards can be met from it; the personalising searches still run."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.shop.models import ShopQuery
from app.shop.providers import ShopFixtureLookup
from app.shop.search import AgainContext, supply_is_plentiful
from app.shop.service import ShopService
from app.shop.store import SessionStore
from app.shop.thresholds import PLENTIFUL_UNSEEN
from app.shop.typed import typed_attributes
from app.shopping import SearchCandidate
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)

REQUEST = "wood dining table"


def cand(i: int, title: str = "", seller: str = "") -> SearchCandidate:
    return SearchCandidate(
        f"c{i}", title or f"Wood Dining Table {i}", seller or f"Seller {i % 20}", 100.0 + i, "USD",
        None, None, "https://www.google.com/search?q=x",
    )  # fmt: skip


def pool_of(n: int, **kw: str) -> list[SearchCandidate]:
    return [cand(i, **kw) for i in range(n)]


# ---- the test itself ----------------------------------------------------------------------


def test_fewer_than_sixty_unseen_candidates_is_never_plentiful() -> None:
    ok, why = supply_is_plentiful(pool_of(PLENTIFUL_UNSEEN - 1), AgainContext(), [])
    assert not ok and "under 60" in why


def test_sixty_unseen_candidates_with_nothing_to_meet_is_plentiful() -> None:
    ok, why = supply_is_plentiful(pool_of(PLENTIFUL_UNSEEN), AgainContext(), [])
    assert ok and "60 unseen candidates" in why


def test_a_typed_quota_that_cannot_be_met_yet_keeps_the_search_going() -> None:
    query = ShopQuery(request="navy velvet accent chairs", search_text="navy velvet accent chair",
                      category="accent chair", color_terms=["navy_dark_blue"])  # fmt: skip
    typed = typed_attributes(query)
    ok, why = supply_is_plentiful(pool_of(80, title="Velvet Accent Chair"), AgainContext(), typed)
    assert not ok and "color navy" in why  # no title names a colour: nothing can be shown to match
    titles = [cand(i, "Navy Velvet Accent Chair") for i in range(80)]
    assert supply_is_plentiful(titles, AgainContext(), typed)[0]  # 17 are needed; all 80 say navy


def test_a_focus_share_that_cannot_be_met_keeps_the_search_going() -> None:
    from app.shop.refine import RefineSet

    rs = RefineSet()
    rs.focus_values = {("shape", "round"): "chip1"}
    again = AgainContext(refine=rs)
    ok, why = supply_is_plentiful(pool_of(80, title="Wood Dining Table"), again, [])
    assert not ok and "focus round" in why
    round_titles = [cand(i, "Round Wood Dining Table") for i in range(80)]
    assert supply_is_plentiful(round_titles, again, [])[0]


def test_a_guard_with_no_alternative_value_keeps_the_search_going() -> None:
    again = AgainContext(guard_caps={"shape": 0.6})
    rect = pool_of(80, title="Rectangular Wood Dining Table")
    ok, why = supply_is_plentiful(rect, again, [])
    assert not ok and "shape" in why
    mixed = rect[:40] + [cand(100 + i, "Round Wood Dining Table") for i in range(40)]
    assert supply_is_plentiful(mixed, again, [])[0]


# ---- through the service -------------------------------------------------------------------------


class Plenty:
    """Every query gives 40 unseen candidates on page 1 and nothing after."""

    name = "serper"
    thread_safe = True

    def __init__(self, per_query: int = 40, titles: str = "Wood Dining Table") -> None:
        self.per_query = per_query
        self.titles = titles
        self.calls: list[str] = []

    def search(
        self, query: str, gl: str = "us", hl: str = "en", page: int = 1
    ) -> list[SearchCandidate]:
        key = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
        self.calls.append(key)
        if page > 1:
            return []
        tag = re.sub(r"[^a-z0-9]", "", key)[:10]
        return [
            SearchCandidate(f"{tag}-{i}", f"{self.titles} {tag} {i}", f"Seller {i % 23}",
                            100.0 + i, "USD", None, None, "https://www.google.com/search?q=x")
            for i in range(self.per_query)
        ]  # fmt: skip


def shop_again(tmp_path: Path, search: Plenty, early: bool, **kw: Any) -> dict[str, Any]:
    cfg = Config(tracing_disabled=True, shop_early_stop=early, shop_thin_variants=True, **kw)
    svc = ShopService(cfg, SessionStore(tmp_path / f"d{early}"), search, ShopFixtureLookup())
    view = svc.search(None, REQUEST)
    sid = view["session_id"]
    for r in view["search"]["results"][:6]:
        svc.rate(sid, r["id"], "love")
    return svc.shop_again(sid)["search"]


def mixed_again(tmp_path: Path, early: bool) -> dict[str, Any]:
    """The owner's mixed scenario (a liked premium seller, an open shape) on the mock catalogue."""
    import test_shop_learn as learn
    from app.shop.providers import build_shop_providers

    cfg = Config(tracing_disabled=True, shop_early_stop=early)
    search, lookup = build_shop_providers(cfg)
    svc = ShopService(cfg, SessionStore(tmp_path / f"mixed{early}"), search, lookup)
    sid, _ = learn.seller_scenario(svc)
    return svc.shop_again(sid)["search"]


def test_a_plentiful_pool_stops_before_the_cap_and_keeps_the_personalising_searches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.shop.search.PLENTIFUL_UNSEEN", 10)  # the mock catalogue is small
    off = mixed_again(tmp_path, False)
    on = mixed_again(tmp_path, True)
    assert off["serper_calls"] == 5 and on["serper_calls"] == 3 and len(on["results"]) == 24
    assert any("hartwell" in q.lower() for q in on["queries"])  # the liked seller is still searched
    assert not any(q.startswith(("rectangular wood", "round wood")) for q in on["queries"])
    stops = on["trace"]["batch"]["early_stops"]
    assert any(s["stopped"] for s in stops) and all(s["why"] for s in stops)
    assert "early_stops" not in off["trace"]["batch"]


def test_the_typed_quota_that_cannot_be_met_yet_does_not_stop_the_optional_searches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.shop.search.PLENTIFUL_UNSEEN", 10)
    stops = mixed_again(tmp_path, True)["trace"]["batch"]["early_stops"]
    first = stops[0]
    assert not first["stopped"] and "material wood or metal" in first["why"]


def test_a_short_pool_still_uses_the_full_set_of_searches(tmp_path: Path) -> None:
    off = mixed_again(tmp_path, False)
    on = mixed_again(tmp_path, True)  # 16 to 41 unseen candidates: under the real 60
    assert on["serper_calls"] == off["serper_calls"] == 5
    assert not any(s["stopped"] for s in on["trace"]["batch"]["early_stops"])


def test_the_flag_off_changes_nothing(tmp_path: Path) -> None:
    search = Plenty(per_query=5)
    off = shop_again(tmp_path, search, early=False)
    assert "early_stops" not in off["trace"]["batch"]
