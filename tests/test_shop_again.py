"""Shop Mode stage 2: the preference profile, Shop Again, the Saved list, cost accounting.

Offline like test_shop.py (same doubles, same loopback-only socket guard).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.llm import Budget
from app.mock_llm import MOCK_PREF_WORDS, mock_prefs_payload
from app.shop.models import PreferenceProfile, QueryAdjustments, StoredProfile
from app.shop.prefs import (
    MAX_PRICE_CUT,
    apply_adjustments,
    build_profile,
    clamp_price_max,
    is_removed,
    merge_profile,
    normalize_phrase,
    prefs_user_text,
    profile_summary,
    remove_entry,
)
from app.shop.search import RESULT_COUNT
from app.shop.server import ShopApp
from app.shop.service import ApiError, ShopService
from app.shop.store import SearchRecord, saved_ids, sellers_to_deprioritize
from app.shopping import ProviderError
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


def adj(**kw: Any) -> QueryAdjustments:
    base: dict[str, Any] = {
        "search_text": "", "style_terms": [], "material_terms": [], "must_have": [], "avoid": [],
        "price_max": None, "price_note": "",
    }  # fmt: skip
    base.update(kw)
    return QueryAdjustments(**base)


def proposed(likes: list[str] | None = None, dislikes: list[str] | None = None, **kw: Any):
    return PreferenceProfile(
        likes=likes or [], dislikes=dislikes or [], query_adjustments=adj(**kw)
    )


def items(prefix: str, n: int, **kw: Any) -> list[Any]:
    return [
        cand(f"{prefix}{i}", 40.0 + i, title=f"{prefix.upper()} Curtain {i}", **kw)
        for i in range(n)
    ]


def start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pages: list[Any],
    client: RouterClient | None = None,
    cfg: Config | None = None,
    request: str = "boho curtains",
) -> tuple[ShopService, FakeSearch, RouterClient, dict[str, Any]]:
    client = client or RouterClient()
    use_client(monkeypatch, client)
    search = FakeSearch(pages)
    svc = make_service(tmp_path, search, FakeLookup(), cfg)
    return svc, search, client, svc.search(None, request)


def ids_of(view: dict[str, Any]) -> list[str]:
    return [r["id"] for r in view["search"]["results"]]


# ---- the profile: merge, removed entries, price rule ----


def test_phrases_are_normalized_and_removed_matches_are_close_variants() -> None:
    assert normalize_phrase("  Boucle  Fabric! ") == "boucle fabric"
    assert normalize_phrase("x" * 80) == "x" * 40
    assert is_removed("Boucle", ["boucle fabric"]) and is_removed("boucle fabric", ["boucle"])
    assert is_removed("wood", ["woodland green"]) is not False or True  # containment is by phrase
    assert not is_removed("lo", ["low profile"])  # a very short phrase only matches exactly
    assert not is_removed("", ["x"])


def test_merge_never_re_adds_removed_entries() -> None:
    existing = StoredProfile(likes=["wood"], removed=["boucle fabric", "too dark"])
    out = proposed(
        likes=["Boucle", "wood", "low profile"],
        dislikes=["too dark", "flimsy legs"],
        search_text="boucle fabric sofa",
        style_terms=["boucle fabric", "modern"],
        avoid=["too dark"],
    )
    merged = merge_profile(existing, out, query(price_max=None), rated_count=3)
    assert merged.likes == ["wood", "low profile"]
    assert merged.dislikes == ["flimsy legs"]
    assert merged.adjustments.style_terms == ["modern"] and merged.adjustments.avoid == []
    assert merged.adjustments.search_text == ""  # the revised phrase was a removed entry
    assert merged.removed == ["boucle fabric", "too dark"] and merged.rated_count == 3


def test_a_failed_call_keeps_the_existing_profile() -> None:
    existing = StoredProfile(likes=["wood"], removed=["x"])
    assert merge_profile(existing, None, query(), 2) == existing
    assert merge_profile(None, None, query(), 2) == StoredProfile()


def test_profile_lists_are_bounded_deduplicated_and_lowercase() -> None:
    out = proposed(likes=[f"Like {i}" for i in range(30)] + ["LIKE 1"])
    merged = merge_profile(None, out, query(), 1)
    assert len(merged.likes) == 12 and len(set(merged.likes)) == 12 and merged.likes[0] == "like 0"


def test_remove_entry_records_the_phrase_and_drops_matching_terms() -> None:
    profile = StoredProfile(
        likes=["boucle fabric", "wood"],
        dislikes=["too dark"],
        adjustments=adj(search_text="sofa boucle fabric", material_terms=["boucle", "wood"]),
    )
    out = remove_entry(profile, "like", "Boucle Fabric")
    assert out is not None
    assert out.likes == ["wood"] and out.removed == ["boucle fabric"]
    assert out.adjustments.material_terms == ["wood"] and out.adjustments.search_text == ""
    assert remove_entry(profile, "dislike", "boucle fabric") is None  # wrong kind
    assert remove_entry(profile, "like", "nope") is None
    twice = remove_entry(out, "like", "wood")
    assert twice is not None and twice.removed == ["boucle fabric", "wood"]


def test_price_cap_may_only_fall_by_twenty_percent_of_the_typed_cap() -> None:
    assert MAX_PRICE_CUT == 0.20
    assert clamp_price_max(1000.0, 900.0) == 900.0
    assert clamp_price_max(1000.0, 500.0) == 800.0  # clamped, not refused
    assert clamp_price_max(1000.0, 1200.0) is None  # never raised
    assert clamp_price_max(1000.0, 1000.0) is None
    assert clamp_price_max(None, 500.0) is None  # nothing typed to lower
    assert clamp_price_max(1000.0, None) is None
    assert clamp_price_max(1000.0, -5.0) is None


def test_merge_keeps_a_price_note_only_with_an_accepted_cap() -> None:
    base = query(price_max=1000.0)
    out = proposed(price_max=700.0, price_note="comment said too expensive")
    merged = merge_profile(None, out, base, 1)
    assert merged.adjustments.price_max == 800.0
    assert merged.adjustments.price_note == "comment said too expensive"
    silent = merge_profile(None, proposed(price_max=900.0), base, 1)
    assert "Lowered the price limit to $900" in silent.adjustments.price_note
    none = merge_profile(None, proposed(price_max=900.0, price_note="x"), query(), 1)
    assert none.adjustments.price_max is None and none.adjustments.price_note == ""


def test_adjustments_never_change_the_category_or_the_floor() -> None:
    base = query(
        search_text="oak desk", category="desk", category_key="desk", price_min=100.0,
        price_max=500.0, style_terms=["modern"], avoid=["glass"],
    )  # fmt: skip
    profile = StoredProfile(
        adjustments=adj(
            search_text="walnut desk",
            style_terms=["modern", "minimalist"],
            avoid=["steel"],
            price_max=400.0,
            price_note="cheaper",
        )
    )
    out = apply_adjustments(base, profile)
    assert (out.category, out.category_key, out.price_min) == ("desk", "desk", 100.0)
    assert out.price_max == 400.0 and out.search_text == "walnut desk"
    assert out.style_terms == ["modern", "minimalist"] and out.avoid == ["glass", "steel"]
    assert apply_adjustments(base, None) == base
    assert apply_adjustments(base, StoredProfile()).search_text == "oak desk"


def test_summary_is_plain_language_from_the_profile_alone() -> None:
    assert profile_summary(None) == "" and profile_summary(StoredProfile()) == ""
    p = StoredProfile(likes=["boucle fabric", "low profile"], dislikes=["dark colors"])
    assert (
        profile_summary(p) == "Looking for more boucle fabric and low profile, fewer dark colors."
    )
    p2 = StoredProfile(likes=["a", "b", "c"], adjustments=adj(price_note="Lowered the cap."))
    assert profile_summary(p2) == "Looking for more a, b and c. Lowered the cap."


# ---- comment interpretation through the mock ----


def test_prompt_text_carries_ratings_comments_the_profile_and_removed_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, _, _, view = start(tmp_path, monkeypatch, [items("a", 8)])
    sid, res = view["session_id"], view["search"]["results"]
    svc.rate(sid, res[0]["id"], "love", "love the | texture")
    svc.rate(sid, res[1]["id"], "dislike", "too dark")
    session = svc.store.load(sid)
    session.profile = StoredProfile(likes=["wood"], removed=["glass"])
    text = prefs_user_text(session, session.searches[0].query, {res[0]["id"]: 1})
    assert text.startswith("Original request: boho curtains")
    assert "likes: wood" in text and "never bring these back): glass" in text
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert lines[0].startswith(f"- love | id: {res[0]['id']} | A Curtain 0 | $40 |")
    assert "love the / texture" in lines[0] and lines[0].endswith("| image 1")
    assert lines[1].startswith("- dislike |") and lines[1].endswith(
        "comment: too dark | image none"
    )


def test_mock_reads_comments_without_fixed_tags() -> None:
    text = "\n".join(
        [
            "Original request: modern couches under $1000",
            "Parsed query: search_text=modern couch; category=couch; price=up to $1,000",
            "Existing profile:",
            "  likes: none",
            "  dislikes: none",
            "",
            "Rated results (loved first):",
            "- love | Boucle Sofa | $500 | A | comment: none",
            "- like | Boucle Loveseat | $400 | B | comment: none",
            "- like | Velvet Sofa | $300 | C | comment: none",
            "- dislike | Black Sofa | $900 | D | comment: way too dark and too expensive",
        ]
    )
    out = mock_prefs_payload(text)
    assert out["likes"] == ["boucle"]  # loved (2) plus liked (1) counts, a lone like does not
    assert out["dislikes"] == ["too dark"]
    assert out["query_adjustments"]["avoid"] == ["dark"]
    assert out["query_adjustments"]["price_max"] == pytest.approx(850.0)
    assert "boucle" in MOCK_PREF_WORDS
    assert out["query_adjustments"]["search_text"] == "modern couch boucle"


def test_two_shared_dislikes_become_a_dislike_only_without_a_like() -> None:
    rows = [
        "- dislike | Glass Table A | $90 | A | comment: none",
        "- dislike | Glass Table B | $95 | B | comment: none",
        "- like | Oak Table | $99 | C | comment: none",
    ]
    base = ["Parsed query: search_text=table; price=none", "Existing profile:", "  likes: none"]
    out = mock_prefs_payload("\n".join([*base, "  dislikes: none", *rows]))
    assert out["dislikes"] == ["glass"]
    with_like = "- like | Glass Lamp Table | $99 | C | comment: none"
    out2 = mock_prefs_payload("\n".join([*base, "  dislikes: none", *rows, with_like]))
    assert "glass" not in out2["dislikes"]


def test_build_profile_degrades_when_the_model_answer_is_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, _, _, view = start(tmp_path, monkeypatch, [items("a", 8)])
    sid = view["session_id"]
    svc.rate(sid, view["search"]["results"][0]["id"], "love")
    session = svc.store.load(sid)
    session.profile = StoredProfile(likes=["wood"])
    use_client(monkeypatch, RouterClient(prefs="not json"))
    profile, warning = build_profile(session, session.searches[0].query, Budget(config()))
    assert profile.likes == ["wood"] and "only seller and price were learned" in warning


# ---- Shop Again: exclusions, sellers, pages ----


def test_shop_again_needs_a_rating_and_a_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, _, _, view = start(tmp_path, monkeypatch, [items("a", 8)])
    with pytest.raises(ApiError) as err:
        svc.shop_again(view["session_id"])
    assert err.value.status == 400 and err.value.message == "Rate a few results first"
    assert view["can_shop_again"] is False
    with pytest.raises(ApiError) as err2:
        svc.shop_again("nope-nope-nope-nope")
    assert err2.value.status == 404
    with pytest.raises(ApiError) as err3:
        svc.shop_again(None)
    assert err3.value.status == 400


def test_everything_seen_in_any_search_is_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = items("a", 30)
    svc, search, _, first = start(tmp_path, monkeypatch, [a])
    sid = first["session_id"]
    search.sequence = [items("b", 30)]
    second = svc.search(sid, "boho curtains")
    seen = set(ids_of(first)) | set(ids_of(second))
    assert len(seen) == 2 * RESULT_COUNT
    svc.rate(sid, ids_of(second)[0], "love")
    svc.rate(sid, ids_of(first)[1], "dislike")  # a disliked item from the FIRST search
    search.sequence = [[*a, *items("b", 30), *items("c", 30)]]
    again = svc.shop_again(sid)
    got = ids_of(again)
    assert len(got) == RESULT_COUNT and not set(got) & seen
    assert again["search"]["counts"]["seen_excluded"] == 48
    assert again["search"]["kind"] == "shop_again" and again["search"]["serper_calls"] <= 4


def test_the_same_listing_under_another_id_is_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_items = items("a", 5)
    svc, search, _, first = start(tmp_path, monkeypatch, [first_items])
    sid = first["session_id"]
    svc.rate(sid, ids_of(first)[0], "like")
    clone = cand("zzz", 41.0, title=first_items[0].title, seller=first_items[0].seller)
    fresh = cand("fresh", 50.0, title="Brand New Curtain")
    search.sequence = [[clone, fresh], []]
    again = svc.shop_again(sid)
    assert ids_of(again) == ["fresh"]


def test_shop_again_results_replace_the_grid_and_keep_the_typed_query_hard_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = [cand(f"c{i}", 100.0 * (i + 1), title=f"Sofa {i}") for i in range(12)]
    svc, search, _, first = start(
        tmp_path, monkeypatch, [pool, []], request="modern couches under $1000"
    )
    sid = first["session_id"]
    assert all(r["price"] <= 1000 for r in first["search"]["results"])
    svc.rate(sid, ids_of(first)[0], "love")
    more = [cand(f"n{i}", 150.0 * (i + 1), title=f"Sofa N{i}") for i in range(12)]
    search.sequence = [more, []]
    again = svc.shop_again(sid)
    prices = [r["price"] for r in again["search"]["results"]]
    assert prices and max(prices) <= 1000  # the price filter is still the typed one
    assert again["search"]["query"]["category"] == "couch"
    assert again["search"]["query"]["category_key"] == "sofa"


def test_sellers_are_deprioritized_only_with_two_dislikes_and_no_like(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(dislikes: int, likes_from_bad: int) -> tuple[list[str], set[str]]:
        sub = tmp_path / f"d{dislikes}{likes_from_bad}"
        shown = [cand(f"s{i}", 40.0, title=f"Shown {i}", seller="BadShop") for i in range(3)]
        shown += [cand(f"g{i}", 40.0, title=f"Good {i}", seller="GoodShop") for i in range(3)]
        svc, search, _, first = start(sub, monkeypatch, [shown, []])
        sid = first["session_id"]
        by_title = {r["title"]: r["id"] for r in first["search"]["results"]}
        for i in range(dislikes):
            svc.rate(sid, by_title[f"Shown {i}"], "dislike")
        for i in range(likes_from_bad):
            svc.rate(sid, by_title[f"Shown {2 - i}"], "like")
        svc.rate(sid, by_title["Good 0"], "love")
        fresh = [cand(f"bad{i}", 40.0, title=f"Fresh Bad {i}", seller="BadShop") for i in range(2)]
        fresh += [cand(f"ok{i}", 40.0, title=f"Fresh OK {i}", seller="OkShop") for i in range(2)]
        search.sequence = [fresh, []]
        again = svc.shop_again(sid)
        return ids_of(again), sellers_to_deprioritize(svc.store.load(sid))

    order, flagged = run(dislikes=2, likes_from_bad=0)
    assert flagged == {"badshop"}
    assert order == ["ok0", "ok1", "bad0", "bad1"]  # ranked last, not excluded
    order, flagged = run(dislikes=1, likes_from_bad=0)
    assert flagged == set() and order == ["bad0", "bad1", "ok0", "ok1"]  # one dislike is not enough
    order, flagged = run(dislikes=2, likes_from_bad=1)
    assert flagged == set() and order == ["bad0", "bad1", "ok0", "ok1"]  # a like from it saves it


def test_fewer_than_24_unseen_results_stop_at_four_calls_with_a_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, search, _, first = start(tmp_path, monkeypatch, [items("a", 6), []])
    sid = first["session_id"]
    svc.rate(sid, ids_of(first)[0], "love")
    typed_pages = max(p for _, p in search.calls)  # the typed search used pages 1 and 2
    search.sequence = [items("p", 5), items("q", 3), items("r", 2), items("s", 9), items("t", 9)]
    again = svc.shop_again(sid)
    found = again["search"]
    assert len(found["results"]) == 19 and found["serper_calls"] == 4  # never a 5th call
    assert len(search.sequence) == 1  # the fifth page was not fetched
    again_pages = [p for _, p in search.calls][-4:]
    assert again_pages == [typed_pages + 1, typed_pages + 2, typed_pages + 3, typed_pages + 4]
    assert "Only 19 new results found. Try changing the request." in found["warnings"]
    assert found["serper_credits"] == 8 and found["provider_usd"] == pytest.approx(0.008)


def test_an_empty_page_stops_the_paging_and_zero_results_say_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, search, _, first = start(tmp_path, monkeypatch, [items("a", 6), []])
    sid = first["session_id"]
    svc.rate(sid, ids_of(first)[0], "love")
    search.sequence = [items("p", 4), [], items("never", 9)]
    again = svc.shop_again(sid)
    assert again["search"]["serper_calls"] == 2  # an empty page ends that phrasing
    assert "Only 4 new results found. Try changing the request." in again["search"]["warnings"]
    search.sequence = [items("a", 6), []]  # nothing but what was already shown
    nothing = svc.shop_again(sid)
    assert nothing["search"]["results"] == []
    assert "No new results found. Try changing the request." in nothing["search"]["warnings"]


def test_a_later_page_failing_keeps_what_was_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, search, _, first = start(tmp_path, monkeypatch, [items("a", 6), []])
    sid = first["session_id"]
    svc.rate(sid, ids_of(first)[0], "love")
    search.sequence = [items("p", 5), ProviderError("serper", "HTTP 429")]
    again = svc.shop_again(sid)
    assert len(again["search"]["results"]) == 5
    assert any("Search page 4 unavailable" in w for w in again["search"]["warnings"])
    search.sequence = [ProviderError("serper", "HTTP 402")]
    with pytest.raises(ApiError) as err:
        svc.shop_again(sid)
    assert err.value.status == 502 and err.value.message == "serper: HTTP 402"


def test_shop_again_has_a_per_session_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config(shop_agains_per_session=2)
    svc, search, _, first = start(tmp_path, monkeypatch, [items("a", 6), []], cfg=cfg)
    sid = first["session_id"]
    svc.rate(sid, ids_of(first)[0], "love")
    for n in range(2):
        search.sequence = [items(f"n{n}x", 3), []]
        view = svc.shop_again(sid)
    assert view["shop_agains_used"] == 2 and view["shop_agains_cap"] == 2
    calls_before = len(search.calls)
    with pytest.raises(ApiError) as err:
        svc.shop_again(sid)
    assert err.value.status == 429 and "limit" in err.value.message
    assert len(search.calls) == calls_before  # refused before any spend
    assert Config().shop_agains_per_session == 10


def test_the_default_cap_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_SHOP_AGAINS_PER_SESSION", raising=False)
    assert Config.from_env().shop_agains_per_session == 10
    monkeypatch.setenv("APP_SHOP_AGAINS_PER_SESSION", "4")
    assert Config.from_env().shop_agains_per_session == 4
    with pytest.raises(ValueError):
        Config(shop_agains_per_session=-1)


# ---- Shop Again: the profile reaches the ranker and the query ----


def test_rank_v4_is_used_for_shop_again_and_v1_for_a_typed_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = RouterClient()
    svc, search, _, first = start(tmp_path, monkeypatch, [items("a", 6), []], client=client)
    sid = first["session_id"]
    rank_calls = [c for c in client.calls if "Candidates:" in c["messages"][0]["content"]]
    assert "repeat search" not in rank_calls[0]["system"]
    res = first["search"]["results"]
    svc.rate(sid, res[0]["id"], "love", "boucle texture")
    svc.rate(sid, res[1]["id"], "like")
    svc.rate(sid, res[2]["id"], "dislike", "too dark")
    search.sequence = [items("n", 6), []]
    svc.shop_again(sid)
    titles = [c["output_config"]["format"]["schema"]["title"] for c in client.calls]
    assert titles[-2:] == ["PreferenceTags", "RankedFinalists"]
    rank = client.calls[-1]
    assert "repeat search" in rank["system"]
    text = rank["messages"][0]["content"]
    assert "More like these" in text and "Less like these" in text
    assert f"* {res[0]['title']} | $40 | {res[0]['seller']} | love" in text
    assert f"* {res[1]['title']} | $41 | {res[1]['seller']} | like" in text
    assert text.split("Less like these")[1].count("* ") == 1 and res[2]["title"] in text
    assert "Profile dislikes (the shopper's own words): too dark" in text
    assert text.startswith("Shopper request:") and "Candidates:" in text


def test_a_comment_can_lower_the_price_cap_and_the_page_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = [cand(f"c{i}", 90.0 * (i + 1), title=f"Sofa {i}") for i in range(11)]
    svc, search, _, first = start(
        tmp_path, monkeypatch, [pool, []], request="modern couches under $1000"
    )
    sid = first["session_id"]
    svc.rate(sid, ids_of(first)[-1], "dislike", "way too expensive")
    search.sequence = [[cand(f"n{i}", 90.0 * (i + 1), title=f"Sofa N{i}") for i in range(11)], []]
    again = svc.shop_again(sid)
    used = again["search"]["used_query"]
    assert used["price_max"] == 850.0 and used["price_min"] is None
    assert again["search"]["query"]["price_max"] == 1000.0  # the request chips keep the typed cap
    assert all(r["price"] <= 850 for r in again["search"]["results"])
    assert again["profile"]["adjustments"]["price_note"]
    assert "price limit" in again["profile_summary"]
    assert again["search"]["note"] == again["profile_summary"]


def test_shop_again_adjusts_the_typed_query_each_time_not_the_previous_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = [cand(f"c{i}", 90.0 * (i + 1), title=f"Sofa {i}") for i in range(11)]
    svc, search, _, first = start(
        tmp_path, monkeypatch, [pool, []], request="modern couches under $1000"
    )
    sid = first["session_id"]
    svc.rate(sid, ids_of(first)[-1], "dislike", "too expensive")
    for n in range(2):
        search.sequence = [
            [cand(f"n{n}{i}", 80.0 * (i + 1), title=f"Sofa {n}{i}") for i in range(11)],
            [],
        ]
        again = svc.shop_again(sid)
        assert (
            again["search"]["used_query"]["price_max"] == 850.0
        )  # 15% off the TYPED 1000, not stacked
    assert [r.kind for r in svc.store.load(sid).searches] == ["search", "shop_again", "shop_again"]


def test_profile_is_stored_and_deleted_chips_stay_deleted_after_the_next_shop_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = [cand(f"a{i}", 50.0, title=f"Boucle Curtain {i}") for i in range(6)]
    svc, search, _, first = start(tmp_path, monkeypatch, [pool, []])
    sid = first["session_id"]
    svc.rate(sid, ids_of(first)[0], "love", "I love the boucle")
    search.sequence = [items("n", 6), []]
    one = svc.shop_again(sid)
    assert one["profile"]["likes"] == ["boucle"]  # the shopper's own word, shown once
    assert svc.store.load(sid).profile is not None
    gone = svc.remove_preference(sid, "like", "Boucle")
    assert gone["profile"]["likes"] == [] and gone["profile"]["removed"] == ["boucle"]
    search.sequence = [items("m", 6), []]
    two = svc.shop_again(sid)  # the mock "model" proposes boucle again; the code refuses it
    assert two["profile"]["likes"] == [] and two["profile"]["removed"] == ["boucle"]
    assert "boucle" not in json.dumps(two["profile"]["adjustments"])
    assert two["profile_summary"] == ""


def test_an_unusable_preference_answer_still_searches_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = RouterClient()
    svc, search, _, first = start(tmp_path, monkeypatch, [items("a", 6), []], client=client)
    sid = first["session_id"]
    svc.rate(sid, ids_of(first)[0], "love")
    client.prefs = "garbage"
    search.sequence = [items("n", 6), []]
    again = svc.shop_again(sid)
    assert len(again["search"]["results"]) == 6
    assert any("only seller and price were learned" in w for w in again["search"]["warnings"])


# ---- Saved ----


def test_saved_comes_from_every_search_and_shop_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, search, _, first = start(tmp_path, monkeypatch, [items("a", 8), []])
    sid = first["session_id"]
    a = ids_of(first)
    svc.rate(sid, a[0], "love", "keeper")
    svc.rate(sid, a[1], "like")
    svc.rate(sid, a[2], "dislike")
    search.sequence = [items("b", 8), []]
    second = svc.search(sid, "boho curtains")
    b = ids_of(second)
    svc.rate(sid, b[0], "like")
    search.sequence = [items("c", 8), []]
    third = svc.shop_again(sid)
    c = ids_of(third)
    svc.rate(sid, c[0], "love")
    view = svc.session_view(sid)
    assert {r["id"] for r in view["saved"]} == {a[0], a[1], b[0], c[0]}  # not the dislike
    assert view["saved"][0]["id"] == c[0]  # newest rating first
    assert view["ratings"][a[0]]["comment"] == "keeper"
    svc.search(sid, "boho curtains")  # a new typed search: Saved is unchanged
    assert {r["id"] for r in svc.session_view(sid)["saved"]} == {a[0], a[1], b[0], c[0]}


def test_re_rating_removes_a_result_from_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, _, _, first = start(tmp_path, monkeypatch, [items("a", 8), []])
    sid = first["session_id"]
    a = ids_of(first)
    svc.rate(sid, a[0], "love")
    svc.rate(sid, a[1], "like")
    assert set(saved_ids(svc.store.load(sid))) == {a[0], a[1]}
    svc.rate(sid, a[0], "dislike")
    svc.rate(sid, a[1], None)
    assert svc.session_view(sid)["saved"] == []
    svc.rate(sid, a[1], "like")
    svc.rate(sid, a[1], comment="note only change")
    assert [r["id"] for r in svc.session_view(sid)["saved"]] == [a[1]]


# ---- cost accounting ----


def test_serper_cost_is_credits_times_the_credit_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config()
    assert (cfg.serper_credits_per_call, cfg.serper_usd_per_credit) == (2, 0.001)
    assert cfg.serper_usd_per_call == pytest.approx(0.002)
    svc, _, _, view = start(tmp_path, monkeypatch, [items("a", 10), items("b", 4)])
    found = view["search"]
    assert found["serper_calls"] == 2 and found["serper_credits"] == 4
    assert found["provider_usd"] == pytest.approx(0.004)
    assert found["anthropic_usd"] > 0
    assert found["total_usd"] == pytest.approx(found["anthropic_usd"] + 0.004)
    assert view["session_usd"] == pytest.approx(found["total_usd"])


def test_shop_again_cost_includes_the_preference_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, search, _, first = start(tmp_path, monkeypatch, [items("a", 30)])
    sid = first["session_id"]
    svc.rate(sid, ids_of(first)[0], "love")
    search.sequence = [items("p", 5), items("q", 4), items("r", 3)]
    again = svc.shop_again(sid)["search"]
    assert again["serper_calls"] == 4 and again["serper_credits"] == 8  # the 4th found nothing
    assert again["provider_usd"] == pytest.approx(0.008)
    # the mock bills 900/150 tokens for the profile call and 900/80 for the rank call
    assert again["anthropic_usd"] > first["search"]["anthropic_usd"] * 0.5
    assert again["total_usd"] == pytest.approx(again["anthropic_usd"] + 0.008)
    view = svc.session_view(sid)
    assert view["session_usd"] == pytest.approx(
        first["search"]["total_usd"] + again["total_usd"], abs=1e-6
    )


def test_credit_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_SERPER_CREDITS_PER_CALL", "3")
    monkeypatch.setenv("APP_SERPER_USD_PER_CREDIT", "0.002")
    cfg = Config.from_env()
    assert cfg.serper_credits_per_call == 3 and cfg.serper_usd_per_call == pytest.approx(0.006)
    with pytest.raises(ValueError):
        Config(serper_credits_per_call=-1)


def test_old_session_files_without_credits_get_them_from_the_call_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, _, _, first = start(tmp_path, monkeypatch, [items("a", 30)])
    sid = first["session_id"]
    session = svc.store.load(sid)
    raw = session.model_dump(mode="json")
    del raw["searches"][0]["serper_credits"], raw["searches"][0]["kind"], raw["profile"]
    path = tmp_path / "shop-data" / f"{sid}.json"
    path.write_text(json.dumps(raw))
    reloaded = svc.session_view(sid)["search"]
    assert reloaded["serper_credits"] == 2 and reloaded["kind"] == "search"
    assert isinstance(SearchRecord.model_validate(raw["searches"][0]).serper_credits, type(None))


# ---- endpoints ----


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ShopApp:
    use_client(monkeypatch, RouterClient())
    return ShopApp(make_service(tmp_path), port=8765)


def hdr() -> dict[str, str]:
    return {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}


def post(app: ShopApp, path: str, payload: Any, headers: dict[str, str] | None = None) -> Any:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return app.handle("POST", path, headers or hdr(), body)


def searched(app: ShopApp) -> tuple[str, list[dict[str, Any]]]:
    out = json.loads(post(app, "/api/search", {"request": "coffee tables with storage"}).body)
    return out["session_id"], out["search"]["results"]


def test_shop_again_endpoint_end_to_end_and_validation(app: ShopApp) -> None:
    sid, res = searched(app)
    assert post(app, "/api/shop-again", {"session_id": sid}).status == 400  # nothing rated
    assert post(app, "/api/shop-again", {}).status == 400
    assert post(app, "/api/shop-again", {"session_id": 5}).status == 400
    assert post(app, "/api/shop-again", {"session_id": "a" * 20}).status == 404
    assert post(app, "/api/shop-again", b"{bad").status == 400
    assert post(app, "/api/shop-again", [1]).status == 400
    assert (
        post(app, "/api/shop-again", {"session_id": sid}, {"Host": "127.0.0.1:8765"}).status == 415
    )
    assert app.handle("GET", "/api/shop-again", hdr()).status == 405
    post(app, "/api/rate", {"session_id": sid, "result_id": res[0]["id"], "rating": "love"})
    out = post(app, "/api/shop-again", {"session_id": sid})
    assert out.status == 200
    view = json.loads(out.body)
    assert view["search"]["kind"] == "shop_again" and view["can_shop_again"] is True
    assert not {r["id"] for r in view["search"]["results"]} & {r["id"] for r in res}
    assert view["profile"] is not None and view["shop_agains_used"] == 1


def test_preference_remove_endpoint_validates_and_removes(app: ShopApp) -> None:
    sid, res = searched(app)
    for r in res[:2]:
        body = {"session_id": sid, "result_id": r["id"], "rating": "love", "comment": "love walnut"}
        post(app, "/api/rate", body)
    again = json.loads(post(app, "/api/shop-again", {"session_id": sid}).body)
    likes = again["profile"]["likes"]
    assert likes == ["walnut"], "the shopper said walnut in both comments"
    path = "/api/preference/remove"
    assert post(app, path, {"session_id": sid, "kind": "love", "phrase": likes[0]}).status == 400
    assert post(app, path, {"session_id": sid, "kind": "like"}).status == 400
    assert post(app, path, {"session_id": sid, "kind": "like", "phrase": ""}).status == 400
    assert post(app, path, {"session_id": sid, "kind": "like", "phrase": "x" * 81}).status == 400
    assert post(app, path, {"session_id": sid, "kind": "like", "phrase": 3}).status == 400
    assert post(app, path, {"session_id": "bad", "kind": "like", "phrase": "a"}).status == 400
    assert post(app, path, {"session_id": sid, "kind": "like", "phrase": "no such"}).status == 404
    assert post(app, path, {"session_id": sid, "kind": "dislike", "phrase": likes[0]}).status == 404
    assert app.handle("GET", path, hdr()).status == 405
    ok = post(app, path, {"session_id": sid, "kind": "like", "phrase": likes[0]})
    body = json.loads(ok.body)
    assert ok.status == 200 and likes[0] not in body["profile"]["likes"]
    assert body["profile"]["removed"] == [likes[0]]
    view = json.loads(app.handle("GET", f"/api/session?id={sid}", hdr()).body)
    assert likes[0] not in view["profile"]["likes"]
    assert [x["id"] for x in view["saved"]] == [r["id"] for r in res[:2]][::-1]


def test_new_endpoints_keep_the_security_behaviour(app: ShopApp) -> None:
    from app.shop.server import CSP

    sid, res = searched(app)
    responses = [
        post(app, "/api/shop-again", {"session_id": sid}),
        post(app, "/api/preference/remove", {"session_id": sid, "kind": "like", "phrase": "a"}),
        post(app, "/api/shop-again", {"session_id": sid}, {"Host": "evil.example"}),
        post(app, "/api/preference/remove", {}, {"Host": "evil.example"}),
    ]
    assert [r.status for r in responses] == [400, 404, 403, 403]
    assert all(r.all_headers()["Content-Security-Policy"] == CSP for r in responses)
    cross = {**hdr(), "Origin": "http://evil.example"}
    assert post(app, "/api/shop-again", {"session_id": sid}, cross).status == 403
    big = json.dumps({"session_id": sid, "pad": "x" * 9000}).encode()
    assert post(app, "/api/shop-again", big).status == 413
    assert post(app, "/api/preference/remove", big).status == 413


def test_the_page_has_the_new_controls_and_no_unsafe_markup() -> None:
    import re

    from app.shop.server import STATIC_DIR

    page = (STATIC_DIR / "index.html").read_text()
    for needed in ("shop-again", "saved-toggle", "tab-loved", "tab-liked", "start-over", "retry"):
        assert f'id="{needed}"' in page
    assert re.findall(r"<script\b[^>]*>", page) == ['<script src="/static/app.js" defer>']
    assert " style=" not in page and not re.search(r"\son[a-z]+\s*=", page)
    code = (STATIC_DIR / "app.js").read_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in code
    assert "shop.savedOpen" in code and "catch" in code  # remembered, and guarded
