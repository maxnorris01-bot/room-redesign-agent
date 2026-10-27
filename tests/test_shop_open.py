"""Shop Mode stage 2o: the gallery is dormant behind a flag, Open never leaves a blank tab, and
loved and liked items are resolved ahead of time. Offline like the other Shop Mode tests; nothing
sleeps (workers are gated on events with a timeout, never a delay).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from app.shop.providers import ShopFixtureLookup, ShopFixtureSearch
from app.shop.service import ApiError, ShopService
from app.shop.store import SessionStore
from app.shop.thresholds import OPEN_UI_TIMEOUT_S
from app.shopping import ProductDetails, ProviderError
from test_shop import _loopback_only, config  # noqa: F401  (the autouse guard must apply here)

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "app" / "shop" / "static"
COFFEE = "coffee tables with a mix of wood and metal with storage"
JS = (STATIC / "app.js").read_text()


class CountingLookup(ShopFixtureLookup):
    """The fixture lookup that records its calls, can be gated on an event and can fail."""

    def __init__(self, fail: int | None = None, gated: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.calls: list[str] = []
        self.gate = threading.Event()
        self.gated = gated
        self.guard = threading.Lock()
        self.active = 0
        self.peak = 0
        self.two_running = threading.Event()
        self.first_started = threading.Event()
        if not gated:
            self.gate.set()

    def lookup(self, product_id: str, gl: str = "us", hl: str = "en") -> ProductDetails:
        with self.guard:
            self.calls.append(product_id)
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.first_started.set()
            if self.active >= 2:
                self.two_running.set()
        try:
            self.gate.wait(timeout=10)
            if self.fail is not None:
                raise ProviderError("serpapi", "request failed", status=self.fail)
            return super().lookup(product_id)
        finally:
            with self.guard:
                self.active -= 1


def make(tmp_path: Path, lookup: CountingLookup | None = None, **kw: Any) -> ShopService:
    cfg = config(shop_auto_lookups=True, **kw)
    return ShopService(
        cfg, SessionStore(tmp_path / "shop-data"), ShopFixtureSearch(), lookup or CountingLookup()
    )


def first_results(svc: ShopService, n: int) -> tuple[str, list[dict[str, Any]]]:
    view = svc.search(None, COFFEE)
    rows = [
        r
        for r in view["search"]["results"]
        if r["product_id"] and not r["product_id"].endswith("9")
    ]
    return view["session_id"], rows[:n]


# ---- part 1: the gallery is dormant ----------------------------------------------------------


def test_with_the_flag_off_there_is_no_gallery_in_responses_and_the_endpoint_refuses(
    tmp_path: Path,
) -> None:
    svc = make(tmp_path)
    sid, rows = first_results(svc, 1)
    view = svc.session_view(sid)
    assert view["gallery"] is False and view["photos_cached"] == [] and view["photos_none"] == []
    with pytest.raises(ApiError) as err:
        svc.photos(sid, rows[0]["id"])
    assert err.value.status == 404


def test_with_the_flag_off_a_lookup_still_gives_the_link_and_rating_but_no_images(
    tmp_path: Path,
) -> None:
    svc = make(tmp_path)
    sid, rows = first_results(svc, 1)
    out = svc.resolve(sid, rows[0]["id"])
    assert out["resolved"]["status"] == "resolved" and out["resolved"]["link"].startswith(
        "https://"
    )
    assert out["rating"]["rating"] is not None  # the rating and review count still come through
    row = svc.store.load(sid).product_cache[rows[0]["id"]]
    assert row["details"]["images"] == [] and row["details"]["image_fields"] == {}
    trace = svc.session_view(sid)["lookup_trace"]
    assert len(trace) == 1 and "fields" not in trace[0] and "usable" not in trace[0]
    assert "thumbnails" not in json.dumps(svc.session_view(sid))


def test_with_the_flag_on_the_gallery_and_its_trace_come_back(tmp_path: Path) -> None:
    svc = make(tmp_path, shop_gallery=True)
    sid, rows = first_results(svc, 1)
    assert svc.session_view(sid)["gallery"] is True
    out = svc.photos(sid, rows[0]["id"])
    assert len(out["images"]) == 6
    trace = svc.session_view(sid)["lookup_trace"]
    assert trace[0]["fields"] == {"thumbnails": 6, "reviews_images": 3} and trace[0]["usable"] == 6


def test_the_page_builds_the_photos_button_only_when_the_flag_is_on() -> None:
    assert "if (r.product_id && state.galleryOn)" in JS
    assert "state.galleryOn = !!data.gallery" in JS
    html = (STATIC / "index.html").read_text()
    assert "photos" not in html.lower() and "gallery" not in html.lower()  # nothing static
    # the images-found wording appears only inside the flag check
    assert re.search(r"if \(state\.galleryOn && l\.ok\)", JS)
    assert "if (lookups && state.galleryOn && state.lookupTrace.length)" in JS


def test_the_flag_defaults() -> None:
    from app.config import Config

    assert Config().shop_gallery is False and Config().shop_auto_lookups_max == 8
    assert Config.from_env().shop_gallery is False  # unset: off
    assert Config.from_env().shop_auto_lookups is True


def test_the_env_flags_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Config

    monkeypatch.setenv("APP_SHOP_GALLERY", "1")
    monkeypatch.setenv("APP_SHOP_AUTO_LOOKUPS", "0")
    monkeypatch.setenv("APP_SHOP_AUTO_LOOKUPS_MAX", "5")
    cfg = Config.from_env()
    assert cfg.shop_gallery is True and cfg.shop_auto_lookups is False
    assert cfg.shop_auto_lookups_max == 5
    example = (ROOT / ".env.example").read_text()
    for name in ("APP_SHOP_GALLERY", "APP_SHOP_AUTO_LOOKUPS", "APP_SHOP_AUTO_LOOKUPS_MAX"):
        assert name in example


# ---- part 2: Open never leaves a blank tab ---------------------------------------------------


def test_the_open_constants_and_the_view_agree() -> None:
    assert OPEN_UI_TIMEOUT_S == 12
    svc_view = ShopService.__dict__["_view"]
    assert svc_view is not None
    from app.shop.thresholds import all_thresholds

    assert all_thresholds()["OPEN_UI_TIMEOUT_S"] == 12
    assert "state.openTimeoutS * 1000" in JS


def test_open_opens_the_tab_in_the_click_and_writes_a_loading_message() -> None:
    """The blank-tab cause (stage 2o): the old handler opened about:blank, wrote nothing into it,
    and on a failed lookup closed it; with a slow lookup the tab sat empty for seconds. The tab now
    says what it is doing and goes to the store, or to Google's page."""
    body = JS[JS.index("  function openResult(") : JS.index("  function renderCard(")]
    assert body.count("window.open") == 0  # it is only in newTab
    new_tab = JS[JS.index("  function newTab(") : JS.index("  function goTo(")]
    assert new_tab.count("window.open('about:blank', '_blank')") == 1
    assert "tab.opener = null" in new_tab
    assert "Finding the store page…" in body and "createTextNode" in new_tab
    assert body.index("newTab(") < body.index("resolve(id, true)")  # the tab first, then the lookup
    assert "Finding store…" in body and "button.disabled = true" in body
    assert "Could not find the store page; showing Google's product page." in JS
    assert "No link is available for this product." in body
    assert "tab.close()" in body  # only when neither link is usable
    assert body.count("tab.close()") == 1
    assert "innerHTML" not in JS[JS.index("/* ---- Open:") : JS.index("  function renderCard(")]


def test_a_blocked_tab_gets_one_inline_link_with_rel_noopener() -> None:
    body = JS[JS.index("  function setOpenNote(") : JS.index("  function newTab(")]
    assert "rel: 'noopener noreferrer'" in body and "target: '_blank'" in body
    assert "Your browser blocked the new tab." in JS and "'Go to store'" in JS


def test_one_lookup_in_flight_per_product_and_cached_opens_have_no_loading_page() -> None:
    body = JS[JS.index("  function openResult(") : JS.index("  function renderCard(")]
    assert body.index("if (openFlight[id]) return;") < body.index("newTab(")
    assert "var instant = store ||" in body  # a cached store link, or no product id: no lookup
    assert "newTab('')" in body  # an instant open has no loading message


def run_node(code: str) -> str:
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_link_validation_accepts_only_https_and_google_for_the_fallback() -> None:
    start = JS.index("  function httpsUrl(")
    end = JS.index("  function setOpenNote(")
    code = (
        JS[start:end]
        + """
    var cases = {
      https: ['https://shop.example.test/p?x=1', 'http://shop.example.test/p',
        'javascript:alert(1)', 'data:text/html,hi', 'ftp://a.test/x', 'file:///etc/passwd',
        'https://u:pw@a.test/', '', 'not a url', '//a.test/x'].map(httpsUrl),
      google: ['https://www.google.com/search?q=a', 'https://google.com/x',
        'http://www.google.com/x', 'https://google.com.evil.test/x', 'https://evilgoogle.com/x',
        'https://shop.test/'].map(googleUrl)
    };
    console.log(JSON.stringify(cases));
    """
    )
    got = json.loads(run_node(code))
    assert got["https"][0] == "https://shop.example.test/p?x=1" and got["https"][1:] == [None] * 9
    assert got["google"][:2] == ["https://www.google.com/search?q=a", "https://google.com/x"]
    assert got["google"][2:] == [None] * 4


def test_a_failed_or_missing_store_link_resolves_to_the_google_link_and_counts_once(
    tmp_path: Path,
) -> None:
    lookup = CountingLookup(fail=500)
    svc = make(tmp_path, lookup)
    sid, rows = first_results(svc, 1)
    out = svc.resolve(sid, rows[0]["id"])
    assert out["resolved"]["status"] == "fallback"
    assert out["resolved"]["link"] == rows[0]["link"]  # the Google product link, never empty
    again = svc.resolve(sid, rows[0]["id"])
    assert again["lookups_used"] == 1 and len(lookup.calls) == 1  # cached: free and instant
    no_store = make(tmp_path / "b")
    sid2 = no_store.search(None, COFFEE)["session_id"]
    nine = next(
        r for r in no_store.session_view(sid2)["search"]["results"] if r["product_id"].endswith("9")
    )
    assert no_store.resolve(sid2, nine["id"])["resolved"]["status"] == "fallback"


# ---- part 3: resolve ahead -------------------------------------------------------------------


def test_love_and_like_queue_a_lookup_and_dislike_does_not(tmp_path: Path) -> None:
    lookup = CountingLookup()
    svc = make(tmp_path, lookup)
    sid, rows = first_results(svc, 3)
    svc.rate(sid, rows[0]["id"], "love")
    svc.rate(sid, rows[1]["id"], "like")
    svc.rate(sid, rows[2]["id"], "dislike")
    svc.ahead.wait(sid)
    session = svc.store.load(sid)
    assert set(session.product_cache) == {rows[0]["id"], rows[1]["id"]}
    assert all(row["by"] == "auto" for row in session.product_cache.values())
    assert session.resolved[rows[0]["id"]].status == "resolved"  # Open will be instant
    assert session.lookups_used == 2 and len(lookup.calls) == 2


def test_each_product_is_looked_up_once_and_the_cache_is_shared_with_open(tmp_path: Path) -> None:
    lookup = CountingLookup()
    svc = make(tmp_path, lookup)
    sid, rows = first_results(svc, 1)
    rid = rows[0]["id"]
    svc.rate(sid, rid, "love")
    svc.rate(sid, rid, "like")
    svc.rate(sid, rid, "love")
    svc.ahead.wait(sid)
    out = svc.resolve(sid, rid)  # Open: no second lookup
    assert out["resolved"]["status"] == "resolved" and out["lookups_used"] == 1
    assert len(lookup.calls) == 1
    assert svc.session_view(sid)["lookup_summary"]["auto"] == 1


def test_a_failed_automatic_lookup_is_cached_counted_once_and_never_retried(
    tmp_path: Path,
) -> None:
    lookup = CountingLookup(fail=500)
    svc = make(tmp_path, lookup)
    sid, rows = first_results(svc, 1)
    rid = rows[0]["id"]
    svc.rate(sid, rid, "love")
    svc.ahead.wait(sid)
    svc.rate(sid, rid, None)
    svc.rate(sid, rid, "love")
    svc.ahead.wait(sid)
    session = svc.store.load(sid)
    assert session.product_cache[rid]["error"] is True and session.lookups_used == 1
    assert len(lookup.calls) == 1
    assert session.resolved[rid].status == "fallback"


def test_automatic_lookups_stop_at_eight_and_open_keeps_four(tmp_path: Path) -> None:
    lookup = CountingLookup()
    svc = make(tmp_path, lookup)
    sid, rows = first_results(svc, 12)
    assert len(rows) == 12
    for r in rows[:10]:
        svc.rate(sid, r["id"], "love")
    svc.ahead.wait(sid)
    session = svc.store.load(sid)
    assert session.lookups_used == 8 and len(lookup.calls) == 8
    for r in rows[8:12]:  # Open still has its four
        assert svc.resolve(sid, r["id"])["resolved"]["status"] == "resolved"
    assert svc.store.load(sid).lookups_used == 12
    last = svc.resolve(sid, rows[-1]["id"])
    assert last["resolved"]["status"] == "resolved"  # looked up already: still free
    summary = svc.session_view(sid)["lookup_summary"]
    assert summary["auto"] == 8 and summary["you"] == 4


def test_the_automatic_maximum_is_configurable(tmp_path: Path) -> None:
    lookup = CountingLookup()
    svc = make(tmp_path, lookup, shop_auto_lookups_max=2)
    sid, rows = first_results(svc, 4)
    for r in rows:
        svc.rate(sid, r["id"], "love")
    svc.ahead.wait(sid)
    assert len(lookup.calls) == 2


def test_automatic_lookups_can_be_turned_off(tmp_path: Path) -> None:
    lookup = CountingLookup()
    cfg = config(shop_auto_lookups=False)
    svc = ShopService(cfg, SessionStore(tmp_path / "d"), ShopFixtureSearch(), lookup)
    sid, rows = first_results(svc, 2)
    svc.rate(sid, rows[0]["id"], "love")
    svc.ahead.wait(sid)
    assert lookup.calls == [] and svc.store.load(sid).product_cache == {}


def test_at_most_two_run_at_once_and_ratings_never_wait_for_them(tmp_path: Path) -> None:
    lookup = CountingLookup(gated=True)
    svc = make(tmp_path, lookup)
    sid, rows = first_results(svc, 6)
    for r in rows[:5]:
        svc.rate(sid, r["id"], "love")  # returns at once although every lookup is held
    assert lookup.two_running.wait(timeout=10)
    svc.rate(sid, rows[5]["id"], "dislike")  # the session is not locked by the lookups
    assert svc.store.load(sid).ratings[rows[5]["id"]].rating == "dislike"
    lookup.gate.set()
    svc.ahead.wait(sid)
    assert lookup.peak == 2 and len(lookup.calls) == 5


def test_start_over_stops_the_queue_and_a_finished_lookup_writes_no_learned_state(
    tmp_path: Path,
) -> None:
    lookup = CountingLookup(gated=True)
    svc = make(tmp_path, lookup)
    sid, rows = first_results(svc, 4)
    for r in rows[:4]:
        svc.rate(sid, r["id"], "love")
    assert lookup.two_running.wait(timeout=10)  # two in flight, two queued
    before = svc.store.load(sid)
    svc.reset_session(sid)  # Start over
    lookup.gate.set()
    svc.ahead.wait(sid)
    session = svc.store.load(sid)
    assert len(lookup.calls) == 2  # the queued two never ran
    assert len(session.product_cache) == 2  # the two in flight may fill the cache
    assert session.profile == before.profile and session.ratings == before.ratings
    assert session.topics == before.topics


def test_a_lookup_finishing_after_a_topic_change_fills_the_cache_but_learns_nothing(
    tmp_path: Path,
) -> None:
    lookup = CountingLookup(gated=True)
    svc = make(tmp_path, lookup)
    sid, rows = first_results(svc, 1)
    rid = rows[0]["id"]
    svc.rate(sid, rid, "love")
    assert lookup.first_started.wait(timeout=10)
    svc.search(sid, "modern couches under $1000")  # a new topic
    before = svc.store.load(sid)
    lookup.gate.set()
    svc.ahead.wait(sid)
    after = svc.store.load(sid)
    assert rid in after.product_cache and after.profile == before.profile
    assert after.current_topic == before.current_topic and after.topics == before.topics
    assert {k: v.rating for k, v in after.ratings.items()} == {
        k: v.rating for k, v in before.ratings.items()
    }


def test_the_footer_and_trace_show_auto_and_manual_lookups_with_durations(tmp_path: Path) -> None:
    svc = make(tmp_path)
    sid, rows = first_results(svc, 2)
    svc.rate(sid, rows[0]["id"], "love")
    svc.ahead.wait(sid)
    svc.resolve(sid, rows[1]["id"])
    view = svc.session_view(sid)
    assert view["lookup_summary"]["auto"] == 1 and view["lookup_summary"]["you"] == 1
    assert isinstance(view["lookup_summary"]["median_s"], float)
    trace = view["lookup_trace"]
    assert [t["by"] for t in trace] == ["auto", "you"]
    assert all(isinstance(t["seconds"], float) for t in trace)
    dump = json.dumps(trace) + json.dumps(view["lookup_summary"])
    assert "http" not in dump and "key" not in dump.lower()
    assert "'link lookups ' + state.used + ' of ' + state.cap" in JS
    assert "'auto ' +" in JS and "', by you ' +" in JS and "median" in JS
    assert "made ahead" in JS and "by you" in JS


def test_the_median_is_the_middle_duration() -> None:
    from app.shop.store import Session

    session = Session(session_id="s", created_at="2026-10-26T00:00:00+00:00", updated_at="")
    for i, secs in enumerate((1.0, 3.0, 2.0)):
        session.product_cache[f"r{i}"] = {"error": False, "seconds": secs, "by": "you"}
    assert ShopService._lookup_summary(session)["median_s"] == 2.0
    session.product_cache["r3"] = {"error": False, "seconds": 10.0, "by": "auto"}
    assert ShopService._lookup_summary(session)["median_s"] == 2.5
