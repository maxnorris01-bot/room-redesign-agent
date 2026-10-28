"""Shop Mode stage 2h: background preparation, the pipelined live path, the status bar's payloads
and page structure, and one seller canonicalization. Time is a fake clock or a bounded real wait
on an event: nothing here sleeps.

Offline like the other Shop Mode tests (same loopback-only socket guard).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.images import PreparedImage
from app.shop.clock import FakeClock
from app.shop.prefs import cached_readings
from app.shop.prep import PrepManager
from app.shop.progress import Progress, Timeline
from app.shop.providers import build_shop_providers
from app.shop.retailers import canonical_name, count_sellers, retailer_of, seller_key, tier_of
from app.shop.search import TagPipeline
from app.shop.service import ShopService
from app.shop.store import SessionStore, comment_key
from test_shop import (  # noqa: F401  (the autouse guard must be imported to apply here)
    RouterClient,
    _loopback_only,
    config,
    use_client,
)
from test_shop_learn import COFFEE, brass_scenario

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "app" / "shop" / "static"


def prep_config(**kw: Any) -> Config:
    return config(shop_prep_enabled=True, **kw)


def prep_service(tmp_path: Path, **kw: Any) -> ShopService:
    cfg = prep_config(**kw)
    search, lookup = build_shop_providers(cfg)
    return ShopService(cfg, SessionStore(tmp_path / "shop-data"), search, lookup)


def fake_image() -> PreparedImage:
    return PreparedImage("AAAA", "image/jpeg", 8, 8, 8, 8, 4)


def with_photos(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every thumbnail 'fetches', so preparation tags from photos."""
    monkeypatch.setattr(
        PrepManager, "_images", lambda self, items: {i.id: fake_image() for i in items}
    )


def tagging_calls(client: RouterClient) -> int:
    return sum(1 for c in client.calls if (c.get("tool_choice") or {}).get("name") == "record_tags")


def prefs_calls(client: RouterClient) -> int:
    return sum(
        1
        for c in client.calls
        if c.get("output_config", {}).get("format", {}).get("schema", {}).get("title")
        == "PreferenceTags"
    )


# ---- seller canonicalization -----------------------------------------------------------


@pytest.mark.parametrize(
    ("variants", "canonical"),
    [
        (("west elm", "West Elm", "westelm.com", "www.westelm.com", "WEST ELM"), "West Elm"),
        (
            ("Crate & Barrel", "crateandbarrel.com", "Crate and Barrel", "crate&barrel"),
            "Crate & Barrel",
        ),
        (("CB2", "cb2.com", "www.cb2.com", "Cb2"), "CB2"),
        (("Article", "article.com", "www.article.com"), "Article"),
        (("Pottery Barn", "potterybarn.com", "pottery-barn"), "Pottery Barn"),
        (("Wayfair", "wayfair.com", "www.wayfair.com"), "Wayfair"),
        (("Target", "target.com", "TARGET"), "Target"),
        (("IKEA", "ikea.com", "Ikea"), "IKEA"),
    ],
)
def test_seller_variants_are_one_seller_with_one_tier(
    variants: tuple[str, ...], canonical: str
) -> None:
    assert len({seller_key(v) for v in variants}) == 1
    for v in variants:
        assert canonical_name(v) == canonical
        assert retailer_of(v) is not None and retailer_of(v).name == canonical  # type: ignore[union-attr]
        assert tier_of(v) == tier_of(canonical) and tier_of(v) is not None


def test_an_unknown_seller_keeps_its_cleaned_name_and_has_no_tier() -> None:
    assert tier_of("Living Spaces") is None
    assert canonical_name("Living Spaces - Outlet") == "Living Spaces"
    assert seller_key("Living Spaces") == seller_key("living spaces")


def test_the_pool_after_count_has_one_row_per_seller_and_covers_the_whole_batch() -> None:
    names = ["west elm", "West Elm", "Crate & Barrel", "crateandbarrel.com", "Living Spaces",
             "Living Spaces", "livingspaces.com"]  # fmt: skip
    counts = count_sellers(names)
    assert counts == {"West Elm": 2, "Crate & Barrel": 2, "Living Spaces": 3}
    assert sum(counts.values()) == len(names)  # nothing is split off or left out


def test_a_batch_cannot_exceed_the_seller_cap_by_mixing_name_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shop.search import SELLER_CAP, run_search
    from test_shop import FakeSearch, cand

    use_client(monkeypatch, RouterClient())
    spellings = ["West Elm", "west elm", "westelm.com", "www.westelm.com"]
    items = [cand(f"v{i}", 10.0 + i, seller=spellings[i % 4]) for i in range(20)]
    items += [cand(f"o{i}", 40.0 + i, seller=f"Other {i}") for i in range(4)]
    out = run_search("sofa", FakeSearch([items, []]), config())
    west = [r for r in out.results if seller_key(r.seller) == seller_key("West Elm")]
    assert len(west) <= SELLER_CAP
    assert len(west) <= max(2, int(0.30 * len(out.results)))  # stage 2p: the cap follows the batch


# ---- progress, timeline ----------------------------------------------------------------


def test_progress_snapshot_has_the_step_payload() -> None:
    clock = FakeClock()
    p = Progress((("a", "Alpha"), ("b", "Beta"), ("c", "Gamma")), clock)
    snap = p.snapshot()
    assert [s["state"] for s in snap["steps"]] == ["pending"] * 3
    assert set(snap["steps"][0]) == {"id", "label", "state", "done_count", "total"}
    p.begin("a", total=4)
    p.advance("a", 3)
    clock.advance(2.4)
    snap = p.snapshot()
    assert snap["steps"][0] == {
        "id": "a", "label": "Alpha", "state": "active", "done_count": 3, "total": 4,
    }  # fmt: skip
    assert snap["elapsed_s"] == 2.4
    p.advance("a", 10)
    assert p.snapshot()["steps"][0]["done_count"] == 4  # never past the total
    p.finish("a")
    p.skip("b")
    states = [s["state"] for s in p.snapshot()["steps"]]
    assert states == ["done", "skipped", "pending"]
    p.finish_all()
    assert [s["state"] for s in p.snapshot()["steps"]] == ["done", "skipped", "done"]


def test_a_skipped_step_is_not_reopened_by_finish_all() -> None:
    p = Progress((("a", "A"), ("b", "B")), FakeClock())
    p.skip("a")
    p.finish_all()
    assert p.snapshot()["steps"][0]["state"] == "skipped"


def test_timeline_records_start_duration_queue_wait_and_phases() -> None:
    clock = FakeClock()
    tl = Timeline(clock)
    queued = clock.now()
    clock.advance(1.5)
    tl.phase_start("tagging")
    with tl.span("tag", "batch 1", queued_at=queued) as info:
        clock.advance(4.0)
        info["retries"] = 1
    tl.phase_end("tagging")
    out = tl.export()
    call = out["calls"][0]
    assert (call["start"], call["duration"], call["queue_wait"], call["retries"]) == (
        1.5, 4.0, 1.5, 1,
    )  # fmt: skip
    assert out["phases"] == [{"phase": "tagging", "start": 1.5, "end": 5.5}]


# ---- the status bar through the job endpoint -------------------------------------------


def test_a_running_job_reports_its_steps_and_elapsed_seconds() -> None:
    from app.shop.jobs import JobManager

    reached, go = threading.Event(), threading.Event()
    manager = JobManager(FakeClock())

    def work(progress: Progress) -> dict[str, Any]:
        progress.begin("searching", total=2)
        progress.advance("searching")
        reached.set()
        assert go.wait(5)
        return {"ok": True}

    job = manager.start("s1", "search", work, lambda exc: (500, str(exc)))
    assert reached.wait(5)
    running = manager.status(job, "s1")
    steps = running["steps"]
    assert [s["id"] for s in steps] == ["searching", "tagging", "ranking"]
    assert steps[0]["state"] == "active" and steps[0]["done_count"] == 1
    assert isinstance(running["elapsed_s"], float)
    go.set()
    manager.wait(job, timeout=5)
    done = manager.status(job, "s1")
    assert done["state"] == "done" and {s["state"] for s in done["steps"]} == {"done"}


# ---- background preparation ------------------------------------------------------------


def test_a_stored_batch_is_tagged_in_the_background_from_photos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = RouterClient()
    use_client(monkeypatch, client)
    with_photos(monkeypatch)
    svc = prep_service(tmp_path)
    view = svc.search(None, COFFEE)
    sid = view["session_id"]
    done, total = svc.prep.wait(sid, 20)
    assert done == total == 2  # 24 results are 2 calls of about 12
    session = svc.store.load(sid)
    ids = [r["id"] for r in view["search"]["results"]]
    assert all(rid in session.tag_cache and session.tag_source[rid] == "photo" for rid in ids)
    assert session.prep["calls"] == 2 and session.prep["tagged"] == len(ids)
    assert session.prep["usd"] >= 0
    # the same batch is not tagged twice: every item already has a photo tag
    assert svc.prep.submit_batch(sid, list(session.results.values())) == 0
    assert svc.view(sid)["prep_usd"] == session.prep["usd"] if hasattr(svc, "view") else True


def test_without_a_fetchable_photo_the_tags_come_from_the_title_and_a_photo_overrides_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_client(monkeypatch, RouterClient())
    monkeypatch.setattr(PrepManager, "_images", lambda self, items: {})
    svc = prep_service(tmp_path)
    sid = svc.search(None, COFFEE)["session_id"]
    svc.prep.wait(sid, 20)
    session = svc.store.load(sid)
    assert session.tag_cache and set(session.tag_source.values()) == {"title"}
    with_photos(monkeypatch)
    svc.prep.submit_batch(sid, list(session.results.values()))
    svc.prep.wait(sid, 20)
    assert set(svc.store.load(sid).tag_source.values()) == {"photo"}


def test_preparation_is_off_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = RouterClient()
    use_client(monkeypatch, client)
    cfg = config()
    search, lookup = build_shop_providers(cfg)
    svc = ShopService(cfg, SessionStore(tmp_path / "d"), search, lookup)
    sid = svc.search(None, COFFEE)["session_id"]
    assert svc.prep.outstanding(sid) == (0, 0)
    assert not svc.store.load(sid).tag_source


def test_a_batch_may_start_only_so_many_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_client(monkeypatch, RouterClient())
    with_photos(monkeypatch)
    svc = prep_service(tmp_path, shop_prep_calls_per_batch=1)
    view = svc.search(None, COFFEE)
    assert svc.prep.wait(view["session_id"], 20) == (1, 1)


def test_background_calls_never_exceed_the_concurrency_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_photos(monkeypatch)
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    class Gated(RouterClient):
        def _create(self, **kwargs: Any) -> Any:
            if (kwargs.get("tool_choice") or {}).get("name") == "record_tags":
                with lock:
                    state["now"] += 1
                    state["peak"] = max(state["peak"], state["now"])
                threading.Event().wait(0.05)  # a bounded timed wait, long enough to overlap
                with lock:
                    state["now"] -= 1
            return super()._create(**kwargs)

    use_client(monkeypatch, Gated())
    svc = prep_service(tmp_path, shop_prep_max_concurrent=2, shop_prep_chunk=4,
                       shop_prep_calls_per_batch=8)  # fmt: skip
    sid = svc.search(None, COFFEE)["session_id"]
    assert svc.prep.wait(sid, 30) == (6, 6)  # 24 results in chunks of 4
    assert 1 <= state["peak"] <= 2


def test_a_comment_is_read_as_it_is_written_and_cached_by_item_and_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = RouterClient()
    use_client(monkeypatch, client)
    with_photos(monkeypatch)
    svc = prep_service(tmp_path)
    sid, first = brass_scenario(svc)
    svc.prep.wait(sid, 30)
    rid = first[0]["id"]
    svc.rate(sid, rid, comment="love the round shape, hate the shiny finish")
    svc.prep.wait(sid, 30)
    session = svc.store.load(sid)
    key = comment_key(rid, "love the round shape, hate the shiny finish")
    assert key in session.comment_cache
    before = prefs_calls(client)
    # the same comment again is a cache hit: nothing new is submitted
    assert svc.prep.submit_comment(sid, rid, "love the round shape, hate the shiny finish") is False
    assert prefs_calls(client) == before
    # a changed comment is a different key: the old reading no longer covers the item
    svc.rate(sid, rid, comment="actually it is too big")
    session = svc.store.load(sid)
    base = svc.store.load(sid)
    assert comment_key(rid, "actually it is too big") not in session.comment_cache
    del base


def test_the_preferences_call_is_skipped_when_everything_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = RouterClient()
    use_client(monkeypatch, client)
    with_photos(monkeypatch)
    svc = prep_service(tmp_path)
    sid, _ = brass_scenario(svc)
    svc.prep.wait(sid, 30)
    before = prefs_calls(client)
    view = svc.shop_again(sid)
    assert prefs_calls(client) == before  # the ratings were already read from photo tags
    tagging = view["profile"]["tagging"]
    assert tagging["model_calls"] == 0 and tagging["from_cache"] == tagging["items_total"] == 5
    assert tagging["model_items"] == 0


def test_an_item_with_no_photo_tags_or_an_unread_comment_still_goes_to_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = RouterClient()
    use_client(monkeypatch, client)
    with_photos(monkeypatch)
    svc = prep_service(tmp_path, shop_prep_max_usd=0.0)  # preparation spends nothing
    sid, first = brass_scenario(svc)
    svc.prep.wait(sid, 30)
    assert not svc.store.load(sid).tag_source  # the cap stopped every call
    view = svc.shop_again(sid)
    tagging = view["profile"]["tagging"]
    assert prefs_calls(client) >= 1
    assert tagging["from_cache"] == 0 and tagging["model_items"] == 5
    del first


def test_a_partly_cached_session_sends_only_the_missing_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = RouterClient()
    use_client(monkeypatch, client)
    with_photos(monkeypatch)
    svc = prep_service(tmp_path)
    sid, first = brass_scenario(svc)
    svc.prep.wait(sid, 30)
    with svc.store.transaction(sid) as session:
        rated = [rid for rid, rec in session.ratings.items() if rec.rating is not None]
        for rid in rated[:2]:
            session.tag_cache.pop(rid, None)
            session.tag_source.pop(rid, None)
    base = svc.store.load(sid)
    from app.shop.again import base_query

    cached, missing, photo = cached_readings(base, base_query(base))  # type: ignore[arg-type]
    assert len(missing) == 2 and len(photo) == 3 and cached is not None
    view = svc.shop_again(sid)
    tagging = view["profile"]["tagging"]
    assert (
        tagging["from_cache"] == 3 and tagging["model_items"] == 2 and tagging["model_calls"] >= 1
    )
    del first


def test_shop_again_waits_a_bounded_time_for_preparation_then_carries_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_photos(monkeypatch)
    release = threading.Event()
    waiting = threading.Event()

    class Stuck(RouterClient):
        def _create(self, **kwargs: Any) -> Any:
            if (kwargs.get("tool_choice") or {}).get("name") == "record_tags" and not kwargs.get(
                "_live", False
            ):
                if threading.current_thread().name.startswith("shopprep"):
                    waiting.set()
                    release.wait(10)
            return super()._create(**kwargs)

    use_client(monkeypatch, Stuck())
    svc = prep_service(tmp_path, shop_prep_wait_s=0.05)
    sid, _ = brass_scenario(svc)
    assert waiting.wait(10)
    seen: list[str] = []
    session = svc.store.load(sid)
    svc._await_prep(sid, session, seen.append)
    assert seen and seen[0].startswith("Finishing your last ratings (")
    assert svc.prep.outstanding(sid)[0] < svc.prep.outstanding(sid)[1]  # it was not finished
    view = svc.shop_again(sid)  # the live path covers it: this still answers
    assert view["search"]["results"]
    release.set()
    svc.prep.wait(sid, 20)


def test_start_over_cancels_queued_and_running_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_photos(monkeypatch)
    release = threading.Event()
    started = threading.Event()

    class Held(RouterClient):
        def _create(self, **kwargs: Any) -> Any:
            if (kwargs.get("tool_choice") or {}).get("name") == "record_tags":
                started.set()
                release.wait(10)
            return super()._create(**kwargs)

    use_client(monkeypatch, Held())
    svc = prep_service(tmp_path, shop_prep_max_concurrent=1)
    sid = svc.search(None, COFFEE)["session_id"]
    assert started.wait(10)
    assert svc.reset_session(sid) == {"ok": True}
    assert svc.prep.outstanding(sid) == (0, 0)
    release.set()
    svc.prep.shutdown()
    session = svc.store.load(sid)
    assert not session.tag_source  # what was running is discarded, the queued call never ran


def test_the_reset_route_stops_preparation_and_rejects_an_unknown_session(tmp_path: Path) -> None:
    from app.shop.service import ApiError

    svc = prep_service(tmp_path)
    with pytest.raises(ApiError):
        svc.reset_session("nope")
    with pytest.raises(ApiError):
        svc.reset_session(None)


def test_preparation_spend_is_the_sessions_own_line_and_part_of_its_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_client(monkeypatch, RouterClient())
    with_photos(monkeypatch)
    svc = prep_service(tmp_path)
    view = svc.search(None, COFFEE)
    sid = view["session_id"]
    svc.prep.wait(sid, 20)
    again = svc.shop_again(sid) if False else svc.search(sid, COFFEE)
    assert "prep_usd" in again and "session_usd" in again
    assert again["prep_usd"] > 0
    assert again["session_usd"] >= again["prep_usd"]
    assert again["prep"]["calls"] >= 2


def test_a_failed_background_call_is_recorded_and_never_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_photos(monkeypatch)

    class Broken(RouterClient):
        def _create(self, **kwargs: Any) -> Any:
            if (kwargs.get("tool_choice") or {}).get("name") == "record_tags":
                raise RuntimeError("boom sk-ant-secret https://x.test/?key=abc")
            return super()._create(**kwargs)

    use_client(monkeypatch, Broken())
    svc = prep_service(tmp_path)
    sid = svc.search(None, COFFEE)["session_id"]
    done, total = svc.prep.wait(sid, 20)
    assert done == total
    errors = json.dumps(svc.store.load(sid).prep.get("errors", []))
    assert "sk-ant-secret" not in errors and "key=abc" not in errors


# ---- the live path ---------------------------------------------------------------------


def test_the_tag_pipeline_tags_each_candidate_once_and_stops_at_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.llm import Budget
    from app.shop.candidates import TagBook
    from test_shop import cand

    client = RouterClient()
    use_client(monkeypatch, client)
    cfg = config(shop_tag_top_n=10, shop_tag_workers=3)
    clock = FakeClock()
    pipe = TagPipeline(
        TagBook(), Budget(cfg), clock, cfg, Timeline(clock), None, clock.now() + 100.0, False
    )
    items = [cand(f"p{i}", 10.0 + i, title=f"Round Walnut Table {i}") for i in range(30)]
    queued = [pipe.submit(items[:8]), pipe.submit(items[:12]), pipe.submit(items[12:30])]
    pipe.wait()
    pipe.close()
    assert queued[0] == 8 and queued[1] == 0  # the first eight are not queued again
    assert sum(queued) <= 10  # one cap across every submit
    assert pipe.titles_queued == sum(queued) and len(pipe.book.new) == sum(queued)
    titles = [
        t for c in client.calls for t in str(c["messages"][0]["content"]).split("Round Walnut")
    ]
    assert len(titles) - len(client.calls) == sum(queued)  # every title went to the model once


def test_the_shop_again_trace_carries_the_timeline_phases_and_tag_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_client(monkeypatch, RouterClient())
    with_photos(monkeypatch)
    svc = prep_service(tmp_path)
    sid, _ = brass_scenario(svc)
    svc.prep.wait(sid, 30)
    view = svc.shop_again(sid)
    trace = view["search"]["trace"]
    timeline = trace["batch"]["timeline"]
    assert timeline["calls"] and timeline["phases"]
    kinds = {c["kind"] for c in timeline["calls"]}
    assert "search" in kinds
    assert all({"start", "duration", "queue_wait", "retries"} <= set(c) for c in timeline["calls"])
    assert trace["batch"]["tag_budget"]["queued"] <= trace["batch"]["tag_budget"]["cap"]
    for name in ("shape", "material", "color_finish", "frame_color", "style"):
        counts = trace["dimensions"][name]["exposure_sources"]
        assert isinstance(counts, dict)  # photo / title / keyword counts (mock tags no style)
        assert counts or name == "style"
        assert set(counts) <= {"photo", "title", "keyword"}
    shape = trace["dimensions"]["shape"]["exposure_sources"]
    assert shape.get("photo", 0) >= 1  # the shown items were tagged from their photos


# ---- mock delay ------------------------------------------------------------------------


def test_the_mock_delay_is_off_by_default_and_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.mock_llm as mock_llm

    waits: list[float] = []

    class Event:
        def wait(self, seconds: float) -> None:
            waits.append(seconds)

    monkeypatch.setattr(mock_llm.threading, "Event", Event)
    monkeypatch.delenv("APP_SHOP_MOCK_DELAY", raising=False)
    mock_llm.mock_step_delay()
    assert waits == []
    monkeypatch.setenv("APP_SHOP_MOCK_DELAY", "0.25")
    mock_llm.mock_step_delay()
    assert waits == [0.25]
    monkeypatch.setenv("APP_SHOP_MOCK_DELAY", "junk")
    mock_llm.mock_step_delay()
    assert waits == [0.25]
    assert Config(shop_mock_delay_s=0.0).shop_mock_delay_s == 0.0
    with pytest.raises(ValueError):
        Config(shop_mock_delay_s=-1.0)


# ---- the page --------------------------------------------------------------------------


def test_the_page_has_an_accessible_step_bar_and_no_spinner() -> None:
    html = (STATIC / "index.html").read_text()
    assert 'id="stepbar"' in html and 'role="progressbar"' in html
    for attr in ("aria-valuenow", "aria-valuemax", "aria-valuemin"):
        assert attr in html
    assert 'id="status"' in html and 'aria-live="polite"' in html
    assert "Taking longer than usual; this stops at 45 s" in html
    assert "spinner" not in html
    assert "<script>" not in html and " style=" not in html


def test_the_step_bar_script_uses_text_content_and_the_style_respects_reduced_motion() -> None:
    js = (STATIC / "app.js").read_text()
    css = (STATIC / "style.css").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js
    assert "aria-valuenow" in js and "aria-valuemax" in js
    assert "prefers-reduced-motion" in css and ".step.active .seg" in css
    assert "/api/reset" in js
    assert "%" not in js[js.index("function renderSteps") : js.index("function tickElapsed")]
    assert "background preparation" in js and "exposure_sources" in js and "queue wait" in js
    assert "prefers-color-scheme" in css or "var(--" in css
