"""Shop Mode stage 2j: topics. A new search for a different product starts a new topic: what was
learned stops shaping it, saved items stay, and a refinement of the same product keeps everything.

Offline like the other Shop Mode tests (same loopback-only socket guard).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.shop.evidence import MARKETPLACE_TEXT
from app.shop.providers import ReplayPoolSearch, build_shop_providers
from app.shop.replay import REPLAY_FILE_EVIDENCE, seed_session
from app.shop.service import ApiError, ShopService
from app.shop.store import (
    REFINEMENT_NOTICE,
    SessionStore,
    advance_topic,
    current_topic,
    in_topic,
    plural,
    rated_count,
    rated_results,
    sellers_to_deprioritize,
    topic_key,
)
from test_shop import (  # noqa: F401  (the autouse guard must be imported to apply here)
    FakeLookup,
    RouterClient,
    _loopback_only,
    query,
    use_client,
)
from test_shop_learn import COFFEE, mock_service, pick

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "app" / "shop" / "static"
COUCHES = "modern couches under $1000"


def coffee_session(svc: ShopService) -> tuple[str, list[dict[str, Any]]]:
    view = svc.search(None, COFFEE)
    sid, first = view["session_id"], view["search"]["results"]
    svc.rate(sid, pick(first, "Round Walnut Drum")["id"], "love")
    svc.rate(sid, pick(first, "Round Brushed Brass Drum")["id"], "love")
    svc.rate(sid, pick(first, "Rectangular ", premium=False)["id"], "like")
    svc.rate(sid, pick(first, "Oval ", premium=False)["id"], "dislike")
    return sid, first


# ---- the topic of a search ----------------------------------------------------------------------


def test_the_first_search_starts_topic_one_without_a_notice(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    view = svc.search(None, COFFEE)
    assert view["topic"]["id"] == 1 and view["topic"]["label"] == "coffee table"
    assert view["search"]["topic"] == 1 and view["search"]["topic_notice"] == ""


def test_the_topic_is_the_canonical_category_not_the_raw_text() -> None:
    assert topic_key(query(category="couch")) == topic_key(query(category="Sofa")) == "sofa"
    assert topic_key(query(category="coffee tables")) == "coffee table"
    assert (
        topic_key(query(category="curtains")) == topic_key(query(category="curtain")) == "curtain"
    )
    assert topic_key(query(category="chandeliers")) == "chandelier"  # unknown: its singular text
    assert topic_key(query(category="  ")) == ""
    assert plural("couch") == "couches" and plural("coffee table") == "coffee tables"
    assert plural("curtains") == "curtains" and plural("") == "this"


def test_a_refinement_of_the_same_product_keeps_what_was_learned(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    sid, _ = coffee_session(svc)
    again = svc.shop_again(sid)
    assert again["profile"]["inferred"]
    refined = svc.search(sid, "walnut coffee table with storage")
    assert refined["search"]["topic_notice"] == REFINEMENT_NOTICE
    assert refined["topic"]["id"] == 1 and refined["topic"]["counted"] == 4
    assert refined["profile"] is not None and refined["profile"]["inferred"]
    assert refined["can_shop_again"] is True


def test_a_different_product_starts_fresh_and_says_so(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    sid, _ = coffee_session(svc)
    svc.shop_again(sid)
    view = svc.search(sid, COUCHES)
    assert view["search"]["topic_notice"] == (
        "Started fresh for couches. Your saved coffee tables are still in Saved and no longer "
        "shape this search."
    )
    assert view["topic"]["id"] == 2 and view["topic"]["label"] == "couch"
    assert view["profile"] is None and view["can_shop_again"] is False
    assert view["topic"]["counted"] == 0 and view["topic"]["excluded"] == 4
    session = svc.store.load(sid)
    assert session.searched_peers == {} and len(session.topics) == 2


def test_the_notice_does_not_mention_saved_items_when_there_are_none(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    sid = svc.search(None, COFFEE)["session_id"]
    view = svc.search(sid, COUCHES)
    assert view["search"]["topic_notice"] == "Started fresh for couches."


def test_a_missing_category_resets_and_says_that_is_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = mock_service(tmp_path)
    sid, _ = coffee_session(svc)
    blank = {
        "search_text": "something nice", "category": "", "price_min": None, "price_max": None,
        "style_terms": [], "material_terms": [], "must_have": [], "avoid": [],
    }  # fmt: skip
    use_client(monkeypatch, RouterClient(parse=blank))
    view = svc.search(sid, "something nice")
    notice = view["search"]["topic_notice"]
    assert notice.startswith("Started fresh: I could not tell what kind of product this is")
    assert "still in Saved" in notice and view["topic"]["id"] == 2 and view["profile"] is None


# ---- what counts in a topic ----------------------------------------------------------------------------


def test_ratings_from_an_earlier_topic_do_not_count_but_stay_in_saved(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    sid, first = coffee_session(svc)
    view = svc.search(sid, COUCHES)
    session = svc.store.load(sid)
    assert rated_results(session) == [] and rated_count(session) == 0
    assert sellers_to_deprioritize(session) == set()
    saved = view["saved"]
    assert len(saved) == 3 and all(row["topic_label"] == COFFEE[:80] for row in saved)
    # the ratings are still there, stamped with topic 1
    assert {rec.topic for rec in session.ratings.values()} == {1}
    assert not any(in_topic(rec, session) for rec in session.ratings.values())
    with pytest.raises(ApiError) as err:
        svc.shop_again(sid)
    assert err.value.status == 400  # nothing rated in this topic yet


def test_rating_in_the_new_topic_counts_and_re_rating_an_old_item_wins(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    sid, first = coffee_session(svc)
    couches = svc.search(sid, COUCHES)["search"]["results"]
    svc.rate(sid, couches[0]["id"], "love")
    old_id = pick(first, "Round Walnut Drum")["id"]
    svc.rate(sid, old_id, "like")  # from the Saved panel: it now counts toward couches
    session = svc.store.load(sid)
    assert session.ratings[couches[0]["id"]].topic == 2 and session.ratings[old_id].topic == 2
    assert rated_count(session) == 2
    view = svc.session_view(sid)
    assert view["topic"]["counted"] == 2 and view["topic"]["excluded"] == 3
    assert not [row for row in view["saved"] if row["id"] == old_id and "topic_label" in row]
    assert svc.shop_again(sid)["search"]["kind"] == "shop_again"


def test_the_new_topic_learns_only_from_its_own_ratings(tmp_path: Path) -> None:
    cfg = Config(tracing_disabled=True)
    store = SessionStore(tmp_path / "shop-data")
    svc = ShopService(cfg, store, ReplayPoolSearch(REPLAY_FILE_EVIDENCE), FakeLookup())
    sid = seed_session(store, replay=REPLAY_FILE_EVIDENCE).session_id
    first = svc.shop_again(sid)
    texts = {e["text"] for e in first["profile"]["inferred"]}
    assert MARKETPLACE_TEXT in texts and "seller: West Elm" in texts  # 48 ratings of evidence
    svc.search_provider = build_shop_providers(cfg)[0]  # the mock catalogue has couches
    fresh = svc.search(sid, COUCHES)
    assert fresh["profile"] is None and fresh["topic"]["excluded"] == 48
    new = fresh["search"]["results"]
    svc.rate(sid, new[0]["id"], "love")
    svc.rate(sid, new[1]["id"], "dislike")
    again = svc.shop_again(sid)
    inferred = {e["text"] for e in again["profile"]["inferred"]}
    assert not inferred & {MARKETPLACE_TEXT, "seller: West Elm", "seller: Article"}
    trace = again["profile"]["trace"]
    assert trace["rated"] == {"love": 1, "like": 0, "dislike": 1}  # two ratings, not fifty
    assert trace["evidence"]["baseline"] == 0.5  # 1 liked of 2: the old session is not in it
    assert again["profile"]["rated_count"] == 2


def test_tags_and_the_seen_memory_survive_a_new_topic(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    sid, first = coffee_session(svc)
    svc.shop_again(sid)
    before = svc.store.load(sid)
    svc.search(sid, COUCHES)
    after = svc.store.load(sid)
    assert after.tag_cache.keys() >= before.tag_cache.keys() and before.tag_cache
    assert set(before.results) <= set(after.results)  # everything shown stays excluded later
    assert [s.topic for s in after.searches] == [1, 1, 2]
    assert after.pool_snapshots and {s["topic"] for s in after.pool_snapshots} == {1, 2}


def test_a_dismissed_marketplace_chip_does_not_carry_into_the_next_topic(tmp_path: Path) -> None:
    cfg = Config(tracing_disabled=True)
    store = SessionStore(tmp_path / "shop-data")
    svc = ShopService(cfg, store, ReplayPoolSearch(REPLAY_FILE_EVIDENCE), FakeLookup())
    sid = seed_session(store, replay=REPLAY_FILE_EVIDENCE).session_id
    svc.shop_again(sid)
    svc.remove_preference(sid, "inferred", MARKETPLACE_TEXT)
    assert svc.store.load(sid).profile.dismissed  # type: ignore[union-attr]
    svc.search(sid, COUCHES)
    assert svc.store.load(sid).profile is None  # the dismissal state went with the profile


# ---- migration -----------------------------------------------------------------------------------------


def test_a_session_file_from_before_topics_is_migrated_on_load_without_losing_anything(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path / "shop-data")
    seeded = seed_session(store, replay=REPLAY_FILE_EVIDENCE)
    path = tmp_path / "shop-data" / f"{seeded.session_id}.json"
    raw = json.loads(path.read_text())
    for key in ("topics", "current_topic"):
        raw.pop(key, None)
    for rating in raw["ratings"].values():
        rating.pop("topic", None)
    for record in raw["searches"]:
        record.pop("topic", None)
        record.pop("topic_notice", None)
    path.write_text(json.dumps(raw))
    session = store.load(seeded.session_id)
    assert len(session.topics) == 1 and session.current_topic == 1
    assert current_topic(session) is not None and current_topic(session).label == "coffee table"  # type: ignore[union-attr]
    assert len(session.ratings) == 48 and {r.topic for r in session.ratings.values()} == {1}
    assert [s.topic for s in session.searches] == [1] and len(session.results) == 48
    store.save(session)  # the next save writes the migrated file
    assert json.loads(path.read_text())["current_topic"] == 1


def test_an_empty_session_has_no_topic_yet(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "shop-data")
    session = store.create()
    assert store.load(session.session_id).topics == [] and current_topic(session) is None
    note = advance_topic(session, query(category="sofa"), "sofas")
    assert note == "" and session.current_topic == 1


# ---- jobs that outlive their topic ----------------------------------------------------------------------


def test_a_shop_again_that_finishes_after_a_new_search_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.shop.service as service_mod

    svc = mock_service(tmp_path)
    sid, _ = coffee_session(svc)
    real = service_mod.run_shop_again

    def slow(*args: Any, **kwargs: Any) -> Any:
        out = real(*args, **kwargs)
        svc.search(sid, COUCHES)  # the visitor starts a new topic while the job was running
        return out

    monkeypatch.setattr(service_mod, "run_shop_again", slow)
    with pytest.raises(ApiError) as err:
        svc.shop_again(sid)
    assert err.value.status == 409
    session = svc.store.load(sid)
    assert session.profile is None and session.current_topic == 2
    assert [s.kind for s in session.searches] == ["search", "search"]  # no Shop Again recorded
    assert session.shop_agains_used == 0


def test_a_comment_read_that_finishes_after_a_new_topic_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.shop.prep as prep_mod

    cfg = Config(tracing_disabled=True, shop_prep_enabled=True)
    search, lookup = build_shop_providers(cfg)
    svc = ShopService(cfg, SessionStore(tmp_path / "shop-data"), search, lookup)
    sid, first = coffee_session(svc)
    svc.prep.wait(sid, 20)
    rid = pick(first, "Round Walnut Drum")["id"]
    svc.rate(sid, rid, comment="lovely finish")
    svc.prep.wait(sid, 20)
    svc.rate(sid, rid, comment="lovely finish and shape")
    real = prep_mod.read_rated_items

    def late(*args: Any, **kwargs: Any) -> Any:
        out = real(*args, **kwargs)
        svc.search(sid, COUCHES)
        return out

    monkeypatch.setattr(prep_mod, "read_rated_items", late)
    svc.prep.wait(sid, 20)
    session = svc.store.load(sid)
    assert not [k for k in session.comment_cache if "and shape" in k]
    assert session.current_topic == 2


# ---- the page ---------------------------------------------------------------------------------------------


def test_the_page_shows_the_notice_the_saved_label_and_ignores_late_answers() -> None:
    js = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    css = (STATIC / "style.css").read_text()
    assert 'id="topic-notice"' in html and 'aria-live="polite"' in html
    assert "renderTopicNotice" in js and "topic_notice" in js
    assert "data.topic.id < state.topic.id" in js  # a late answer for an old topic is ignored
    assert "From an earlier search: " in js and "topic-tag" in css
    assert "Ratings counted: " in js and "an earlier topic" in js
    assert "innerHTML" not in js
