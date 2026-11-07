"""Refine B1: Shop Again is always available, and typed refine filters shape the next search.

Fakes only (same doubles and loopback-only guard as the other Shop Mode tests)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from app.shop import search as shop_search
from app.shop.refine_filters import (
    MAX_ADDED_WORDS,
    FilterError,
    candidate_attrs,
    chip_of,
    filters_of,
    match_stats,
    parse_filters,
    satisfies_all,
    targeted_text,
    tier_key,
    tier_sorted,
    with_filters,
)
from app.shop.server import ShopApp
from app.shop.service import ApiError, ShopService
from app.shopping import SearchCandidate
from test_shop import (
    FakeLookup,
    RouterClient,
    _loopback_only,  # noqa: F401  (the autouse guard must apply here)
    cand,
    config,
    make_service,
    use_client,
)
from test_shop_again import app, hdr, post, searched  # noqa: F401  (the `app` fixture)

GRAY_BLUE = [{"dimension": "color", "values": ["gray", "blue"]}]
FULL = [*GRAY_BLUE, {"dimension": "style", "values": ["vintage"]}]


class ByText:
    """A search double that answers by phrasing: `rules` is a list of (substring, pages); the
    first rule whose substring is in the query serves `pages[page - 1]`. Records every call."""

    name = "serper"

    def __init__(self, rules: list[tuple[str, list[list[SearchCandidate]]]]) -> None:
        self.rules = rules
        self.calls: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def search(
        self, query: str, gl: str = "us", hl: str = "en", page: int = 1
    ) -> list[SearchCandidate]:
        with self._lock:
            self.calls.append((query, page))
        for needle, pages in self.rules:
            if needle in query:
                return pages[page - 1] if page <= len(pages) else []
        return []


def rugs(prefix: str, n: int, title: str) -> list[SearchCandidate]:
    base = 50.0 + 10 * sum(map(ord, prefix))  # distinct titles and prices: nothing dedupes
    return [cand(f"{prefix}{i}", base + i, title=f"{title} {prefix}{i}") for i in range(n)]


def service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, search: Any, **cfg: Any
) -> ShopService:
    use_client(monkeypatch, RouterClient())
    return make_service(tmp_path, search, FakeLookup(), config(**cfg))


def titles(view: dict[str, Any]) -> list[str]:
    return [r["title"] for r in view["search"]["results"]]


# ---- the pure pieces ------------------------------------------------------------------------


def test_parse_filters_accepts_vocabulary_keys_and_orders_by_priority() -> None:
    got = parse_filters(
        [
            {"dimension": "style", "values": ["vintage"]},
            {"dimension": "color", "values": ["Grey", "blue", "gray"]},
        ]
    )
    assert got == {"color": ["gray", "blue"], "style": ["vintage"]}
    assert parse_filters([]) == {}
    assert parse_filters([{"dimension": "color", "values": []}]) == {}


@pytest.mark.parametrize(
    "bad",
    [
        {"dimension": "color"},
        [{"dimension": "price", "values": ["x"]}],
        [{"dimension": "color", "values": ["not-a-colour"]}],
        [{"dimension": "color", "values": [3]}],
        [
            {
                "dimension": "color",
                "values": ["red", "blue", "gray", "green", "pink", "tan", "black"],
            }
        ],
        ["color"],
        "color",
    ],
)
def test_parse_filters_rejects_everything_else(bad: Any) -> None:
    with pytest.raises(FilterError):
        parse_filters(bad)


def test_chips_roundtrip_and_replace_as_a_set() -> None:
    chip = chip_of("color", ["gray", "blue"])
    assert chip.id == "filter:color" and chip.kind == "filter"
    assert chip.label == "Colour: gray or blue" and chip.target == "color:gray,blue"
    other = chip_of("style", ["vintage"])
    assert filters_of([other, chip]) == {"color": ["gray", "blue"], "style": ["vintage"]}
    first = with_filters([], {"color": ["gray"], "style": ["vintage"]})
    second = with_filters(first, {"color": ["blue"]})
    assert [c.id for c in second] == ["filter:color"] and second[0].target == "color:blue"
    assert with_filters(second, {}) == []


def test_targeted_text_adds_at_most_four_words_no_price_and_no_repeats() -> None:
    f = {"color": ["gray", "blue"], "style": ["vintage", "boho"], "material": ["wool"]}
    text = targeted_text("area rug", f)
    assert text == "area rug gray blue vintage boho"
    assert len(text.split()) - 2 == MAX_ADDED_WORDS
    assert not any(ch.isdigit() or ch == "$" for ch in text)
    assert targeted_text("gray area rug", {"color": ["gray"]}) == "gray area rug"
    assert targeted_text("rug", {"color": ["terracotta"], "style": ["mid-century"]}) == (
        "rug terracotta mid-century"
    )


def test_tiering_puts_a_gray_rug_above_a_red_persian_vintage_one() -> None:
    f = parse_filters(FULL)
    red = candidate_attrs("Red Persian Vintage Area Rug")
    gray = candidate_attrs("Gray Modern Area Rug")
    both = candidate_attrs("Gray Vintage Area Rug")
    plain = candidate_attrs("Area Rug")
    assert tier_key(both, f) < tier_key(gray, f) < tier_key(red, f)
    assert tier_key(gray, f) < tier_key(plain, f) < tier_key(red, f)  # unread beats known-wrong


def test_or_within_a_dimension_and_and_across() -> None:
    f = parse_filters(FULL)
    assert satisfies_all(candidate_attrs("Blue Vintage Rug"), f)  # blue is one of gray OR blue
    assert satisfies_all(candidate_attrs("Gray Vintage Rug"), f)
    assert not satisfies_all(candidate_attrs("Gray Modern Rug"), f)  # style fails: AND across
    assert not satisfies_all(candidate_attrs("Red Vintage Rug"), f)
    assert not satisfies_all({}, {})  # no filters: nothing to satisfy


def test_a_photo_colour_decides_before_the_title() -> None:
    f = parse_filters(GRAY_BLUE)
    wrong = candidate_attrs("Gray Vintage Rug", "red")  # the photo says red
    right = candidate_attrs("Vintage Rug", "gray")
    assert wrong["color"] == ["red"] and right["color"] == ["gray"]
    assert tier_key(right, f) < tier_key(wrong, f)
    assert candidate_attrs("Blue Rug", "metallic")["color"] == ["blue"]  # no key: title stays


def test_a_known_wrong_colour_never_outranks_a_fitting_one() -> None:
    f = parse_filters([*GRAY_BLUE, {"dimension": "style", "values": ["vintage"]}])
    wrong_but_more = {"color": ["red"], "style": ["vintage"], "material": ["wool"]}
    right_but_less = {"color": ["gray"], "style": ["modern"]}
    order = tier_sorted([wrong_but_more, right_but_less], lambda a: a, f)
    assert order == [right_but_less, wrong_but_more]


def test_tier_sort_is_stable_inside_a_tier() -> None:
    f = parse_filters(GRAY_BLUE)
    items = ["Gray Rug A", "Blue Rug B", "Gray Rug C"]
    assert tier_sorted(items, candidate_attrs, f) == items
    assert tier_sorted(items, candidate_attrs, {}) == items


def test_match_stats_counts_the_shown_results_that_satisfy_every_filter() -> None:
    f = parse_filters(FULL)
    rows = [
        candidate_attrs("Gray Vintage Rug"),
        candidate_attrs("Blue Vintage Rug"),
        candidate_attrs("Gray Modern Rug"),
        None,
    ]
    assert match_stats(rows, f) == {
        "satisfied_all": 2, "shown": 4, "filters": ["color", "style"],
    }  # fmt: skip


# ---- Shop Again without ratings ---------------------------------------------------------------


def first_and_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filters: Any = None, **cfg: Any
) -> tuple[ShopService, ByText, dict[str, Any], dict[str, Any]]:
    """A first search for rugs (red ones, nothing gray), then a Shop Again with nothing rated."""
    shown = rugs("a", 24, "Red Persian Vintage Area Rug")  # a full first page
    page2 = rugs("b", 10, "Green Shag Area Rug") + rugs("c", 4, "Gray Modern Area Rug")
    targeted = (
        rugs("g", 5, "Gray Vintage Area Rug")
        + rugs("u", 4, "Blue Vintage Area Rug")
        + rugs("m", 5, "Gray Modern Area Rug")
        + rugs("r", 5, "Red Persian Vintage Area Rug")
    )
    search = ByText([("gray", [targeted, targeted[:2]]), ("rug", [shown, page2])])
    svc = service(tmp_path, monkeypatch, search, **cfg)
    first = svc.search(None, "area rug")
    if filters is not None:
        svc.refine(first["session_id"], None, filters)
    again = svc.shop_again(first["session_id"])
    return svc, search, first, again


def test_can_shop_again_after_a_search_with_nothing_rated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service(tmp_path, monkeypatch, ByText([("rug", [rugs("a", 5, "Rug")])]))
    view = svc.search(None, "area rug")
    assert view["can_shop_again"] is True
    assert svc.session_view(view["session_id"])["can_shop_again"] is True
    assert svc.session_view(None)["can_shop_again"] is False  # nothing searched yet


def test_cannot_shop_again_at_the_cap_or_while_a_job_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service(
        tmp_path, monkeypatch, ByText([("rug", [rugs("a", 5, "Rug")])]), shop_agains_per_session=1
    )
    sid = svc.search(None, "area rug")["session_id"]
    gate = threading.Event()
    svc.jobs.start(sid, "search", lambda _p: (gate.wait(5), {})[1], lambda e: (500, str(e)))
    try:
        assert svc.session_view(sid)["can_shop_again"] is False  # a job is running
    finally:
        gate.set()
    for job in list(svc.jobs._jobs):
        svc.jobs.wait(job, 5)
    assert svc.session_view(sid)["can_shop_again"] is True
    svc.shop_again(sid)
    view = svc.session_view(sid)
    assert view["shop_agains_used"] == 1 and view["can_shop_again"] is False  # at the cap
    with pytest.raises(ApiError) as err:
        svc.shop_again(sid)
    assert err.value.status == 429


def test_an_unrated_shop_again_excludes_everything_shown_and_brings_new_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, search, first, again = first_and_again(tmp_path, monkeypatch)
    shown = {r["id"] for r in first["search"]["results"]}
    new = {r["id"] for r in again["search"]["results"]}
    assert again["search"]["kind"] == "shop_again" and new and not (new & shown)
    assert again["profile"] is not None and not again["profile"]["inferred"]  # nothing learned
    assert ("area rug", 2) in search.calls  # a fresh fetch: the next page
    assert again["search"]["filters_match"] is None  # no filters, no claim


def test_a_first_search_never_touches_the_filter_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the filter tiers ran for a first search")

    monkeypatch.setattr(shop_search, "tier_sorted", boom)
    monkeypatch.setattr(shop_search, "match_stats", boom)
    pool = rugs("a", 14, "Gray Vintage Area Rug") + rugs("b", 14, "Red Area Rug")
    search = ByText([("rug", [pool])])
    svc = service(tmp_path, monkeypatch, search)
    view = svc.search(None, "area rug")
    assert [r["title"] for r in view["search"]["results"]] and view["search"][
        "filters_match"
    ] is None
    assert [c[0] for c in search.calls] == ["area rug"]  # one fetch, as before
    assert len(view["search"]["results"]) == 24


# ---- the filters: stored, replaced, cleared, removed -----------------------------------------


def test_filters_are_stored_replaced_cleared_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, _, first, _ = first_and_again(tmp_path, monkeypatch)
    sid = first["session_id"]
    out = svc.refine(sid, None, FULL)
    assert [c["id"] for c in out["refine"]] == ["filter:color", "filter:style"]
    assert (
        out["refine"][0]["label"] == "Colour: gray or blue" and out["refine"][0]["kind"] == "filter"
    )
    assert [c["id"] for c in svc.session_view(sid)["refine"]] == ["filter:color", "filter:style"]
    replaced = svc.refine(sid, None, [{"dimension": "material", "values": ["wool"]}])
    assert [c["id"] for c in replaced["refine"]] == ["filter:material"]  # the set was replaced
    svc.refine(sid, None, FULL)
    removed = svc.remove_refine(sid, "filter:color")  # the existing remove endpoint
    assert [c["id"] for c in removed["refine"]] == ["filter:style"]
    assert svc.refine(sid, None, [])["refine"] == []  # an empty list clears them
    with pytest.raises(ApiError) as err:
        svc.refine(sid, None, [{"dimension": "color", "values": ["nope"]}])
    assert err.value.status == 400
    with pytest.raises(ApiError):
        svc.refine(sid, None)  # neither text nor filters: the old message


def test_filters_endpoint_and_a_new_category_clears_them(app: ShopApp) -> None:  # noqa: F811
    sid, _ = searched(app)
    out = post(app, "/api/refine", {"session_id": sid, "filters": GRAY_BLUE})
    assert out.status == 200 and json.loads(out.body)["refine"][0]["id"] == "filter:color"
    assert post(app, "/api/refine", {"session_id": sid, "filters": "gray"}).status == 400
    assert post(app, "/api/refine", {"session_id": sid}).status == 400
    view = json.loads(app.handle("GET", f"/api/session?id={sid}", hdr()).body)
    assert [c["id"] for c in view["refine"]] == ["filter:color"]
    done = json.loads(post(app, "/api/search", {"request": "round dining table"}).body)
    assert done["session_id"]  # a different session: nothing carried
    moved = json.loads(post(app, "/api/search", {"session_id": sid, "request": "sofa"}).body)
    assert moved["refine"] == []  # a new category starts a new topic: the filters are gone


# ---- the filtered Shop Again ------------------------------------------------------------------


def test_one_targeted_fetch_tiers_and_filters_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, search, first, again = first_and_again(tmp_path, monkeypatch, FULL)
    targeted = [c for c in search.calls if "gray" in c[0]]
    assert targeted == [("area rug gray blue vintage", 1)]  # ONE targeted fetch, page 1
    assert len(search.calls) <= 4
    out = titles(again)
    shown_titles = {r["title"] for r in first["search"]["results"]}
    assert not set(out) & shown_titles  # nothing shown before comes back
    assert len({r["id"] for r in again["search"]["results"]}) == len(out)  # no duplicates
    first_tier = [t for t in out if ("Gray" in t or "Blue" in t) and "Vintage" in t]
    assert out[: len(first_tier)] == first_tier and len(first_tier) == 9  # tier 1 leads
    colour_only = [t for t in out if ("Gray" in t or "Blue" in t) and "Vintage" not in t]
    assert out[len(first_tier) : len(first_tier) + len(colour_only)] == colour_only
    red = out.index(next(t for t in out if t.startswith("Red")))
    assert red > len(first_tier) + len(colour_only) - 1  # a red Persian never beats a gray one
    match = again["search"]["filters_match"]
    assert match == {"satisfied_all": 9, "shown": len(out), "filters": ["color", "style"]}
    assert len(out) > 9  # the page still fills past tier 1


def test_the_extra_fetch_is_counted_once_and_is_one_shop_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, search, _, plain = first_and_again(tmp_path / "plain", monkeypatch)
    svc, fsearch, _, filtered = first_and_again(tmp_path / "filtered", monkeypatch, FULL)
    extra = filtered["search"]["serper_calls"] - plain["search"]["serper_calls"]
    assert extra == 1 and filtered["shop_agains_used"] == 1  # one fetch, one Shop Again
    credits = svc.config.serper_credits_per_call
    assert filtered["search"]["serper_credits"] == filtered["search"]["serper_calls"] * credits
    assert filtered["search"]["total_usd"] >= plain["search"]["total_usd"]


def test_a_second_targeted_fetch_only_when_the_first_found_nothing_new_and_never_a_third(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = rugs("a", 6, "Red Area Rug")
    seen_again = [*shown]  # the targeted page 1 only repeats what the session showed
    fresh = rugs("g", 6, "Gray Vintage Area Rug")
    search = ByText([("gray", [seen_again, fresh, fresh]), ("rug", [shown, []])])
    svc = service(tmp_path, monkeypatch, search)
    sid = svc.search(None, "area rug")["session_id"]
    svc.refine(sid, None, FULL)
    again = svc.shop_again(sid)
    targeted = [c for c in search.calls if "gray" in c[0]]
    assert targeted == [("area rug gray blue vintage", 1), ("area rug gray blue vintage", 2)]
    assert {r["title"] for r in again["search"]["results"]} >= {"Gray Vintage Area Rug g0"}
    # the next Shop Again continues after the pages already fetched for those words (3, then 4
    # because 3 repeated what was shown), and never goes past two fetches
    svc.shop_again(sid)
    assert [c[1] for c in search.calls if "gray" in c[0]] == [1, 2, 3, 4]


def test_a_failed_targeted_fetch_is_a_warning_not_a_failed_shop_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.shopping import ProviderError

    class Failing(ByText):
        def search(self, query: str, gl: str = "us", hl: str = "en", page: int = 1) -> Any:
            if "gray" in query:
                with self._lock:
                    self.calls.append((query, page))
                raise ProviderError("serper", "search failed")
            return super().search(query, gl, hl, page)

    shown = rugs("a", 24, "Red Area Rug")
    search = Failing([("rug", [shown, rugs("b", 6, "Green Area Rug")])])
    svc = service(tmp_path, monkeypatch, search)
    sid = svc.search(None, "area rug")["session_id"]
    svc.refine(sid, None, GRAY_BLUE)
    again = svc.shop_again(sid)
    assert again["search"]["results"]
    assert any("search for your filters" in w for w in again["search"]["warnings"])
    assert len([c for c in search.calls if "gray" in c[0]]) == 1  # no retry after an error


def test_filters_apply_to_every_following_shop_again_until_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, search, first, _ = first_and_again(tmp_path, monkeypatch, GRAY_BLUE)
    sid = first["session_id"]
    svc.shop_again(sid)
    # page 1 for the first Shop Again; page 2 for the second (it repeated what was shown, so the
    # one allowed retry asked page 3)
    assert [c[1] for c in search.calls if "gray" in c[0]] == [1, 2, 3]
    svc.remove_refine(sid, "filter:color")
    before = len(search.calls)
    out = svc.shop_again(sid)
    assert not [c for c in search.calls[before:] if "gray" in c[0]]
    assert out["search"]["filters_match"] is None


def test_partial_fill_keeps_the_page_full_and_reports_how_many_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = rugs("a", 24, "Red Area Rug")
    other = rugs("b", 10, "Green Shag Area Rug") + rugs("c", 6, "Red Persian Vintage Area Rug")
    one_match = rugs("g", 2, "Gray Vintage Area Rug")
    search = ByText([("gray", [one_match]), ("rug", [shown, other])])
    svc = service(tmp_path, monkeypatch, search)
    sid = svc.search(None, "area rug")["session_id"]
    svc.refine(sid, None, FULL)
    again = svc.shop_again(sid)
    out = titles(again)
    assert out[:2] == ["Gray Vintage Area Rug g0", "Gray Vintage Area Rug g1"]
    assert len(out) == 18  # every unseen candidate: only 2 satisfy all, the rest fill the page
    assert again["search"]["filters_match"]["satisfied_all"] == 2
    assert again["search"]["filters_match"]["shown"] == 18


def test_filters_reset_when_the_category_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    search = ByText([("sofa", [rugs("s", 6, "Gray Sofa")]), ("rug", [rugs("a", 6, "Red Rug")])])
    svc = service(tmp_path, monkeypatch, search)
    sid = svc.search(None, "area rug")["session_id"]
    svc.refine(sid, None, FULL)
    assert svc.session_view(sid)["refine"]
    view = svc.search(sid, "sofa")
    assert view["refine"] == [] and view["topic"]["id"] == 2
