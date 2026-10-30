"""Stage D1, phase 4: spend caps per session, per invite per day and for everyone per day, counted
in the database; the kill switch; background work. SQLite stands in for Postgres."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.shop.auth import LoginLimiter, SqlAuth
from app.shop.caps import CapGuard, Meter, session_cost
from app.shop.context import current_invite
from app.shop.dbstore import SqliteDatabase, SqlSessionStore, migrate
from app.shop.deploy import DeploySettings
from app.shop.providers import ShopFixtureLookup, build_shop_providers
from app.shop.server import COOKIE_NAME, ShopApp
from app.shop.service import ApiError, ShopService
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)

SECRET = "s"
ORIGIN = "https://portfolio.example"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


class Rig:
    def __init__(self, tmp_path: Path, **caps: Any) -> None:
        self.path = tmp_path / "c.db"
        self.settings = DeploySettings(
            deploy=True, api_prefix="/api/shop", allowed_hosts=frozenset({"h"}),
            allowed_origins=frozenset({ORIGIN}), origin_secret=SECRET, public_url=ORIGIN,
            contact_email="me@example.org", database_url="sqlite:///x", **caps,
        )  # fmt: skip
        self.open()

    def open(self) -> None:
        """(Re)open everything over the same database file: a restart."""
        self.db = SqliteDatabase(str(self.path))
        migrate(self.db)
        self.store = SqlSessionStore(self.db)
        self.store.after_save = Meter(lambda: NOW)
        cfg = Config(tracing_disabled=True, shop_prep_enabled=True)
        search, _ = build_shop_providers(cfg)
        self.service = ShopService(cfg, self.store, search, ShopFixtureLookup())
        self.guard = CapGuard(self.db, self.settings, lambda: NOW)
        self.service.guard = self.guard
        self.service.prep.guard = self.guard
        self.auth = SqlAuth(
            self.db, self.settings, lambda: NOW, LoginLimiter(lambda: NOW.timestamp())
        )
        self.app = ShopApp(self.service, 0, self.settings, self.auth, self.store)

    def call(self, method: str, path: str, body: Any = None, cookie: str | None = None) -> Any:
        h = {
            "Host": "h",
            "X-Origin-Secret": SECRET,
            "Origin": ORIGIN,
            "Content-Type": "application/json",
        }
        if cookie:
            h["Cookie"] = f"{COOKIE_NAME}={cookie}"
        raw = b"" if body is None else json.dumps(body).encode()
        return self.app.handle(method, f"/api/shop{path}", h, raw)

    def user(self, label: str = "Ann") -> str:
        _, code = self.auth.create_invite(label)
        resp = self.call("POST", "/auth/login", {"code": code})
        return str(resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1])

    def search(
        self, cookie: str, sid: str | None = None, request: str = "oak coffee tables"
    ) -> Any:
        started = self.call("POST", "/search", {"session_id": sid, "request": request}, cookie)
        if started.status != 202:
            return started
        job = json.loads(started.body)["job_id"]
        self.service.jobs.wait(job)
        return self.call("GET", f"/job/{job}", cookie=cookie)

    def usage(self, scope: str, key: str, day: str) -> float:
        rows = self.db.transaction(
            lambda tx: tx.execute(
                "SELECT usd FROM usage WHERE scope = ? AND key = ? AND day = ?", (scope, key, day)
            )
        )
        return float(rows[0][0]) if rows else 0.0


def sid_of(done: Any) -> str:
    return str(json.loads(done.body)["result"]["session_id"])


def err(resp: Any) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(resp.body)["error"]
    return out


def test_the_defaults_are_the_agreed_caps() -> None:
    s = DeploySettings()
    assert (s.cap_session_usd, s.cap_invite_day_usd, s.cap_global_day_usd) == (0.50, 1.00, 3.00)
    assert (s.cap_searches, s.cap_shop_agains) == (8, 25)


def test_spend_is_counted_per_session_invite_day_and_everyone(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    cookie = rig.user()
    done = rig.search(cookie)
    sid = sid_of(done)
    cost = session_cost(rig.store.load(sid))
    assert cost > 0
    invite = rig.store.owner_of(sid)
    assert invite and rig.usage("session", sid, "") == pytest.approx(cost)
    assert rig.usage("invite_day", invite, "2026-10-30") == pytest.approx(cost)
    assert rig.usage("global_day", "*", "2026-10-30") == pytest.approx(cost)
    usage = json.loads(rig.call("GET", "/auth/session", cookie=cookie).body)["usage"]
    assert usage["invite_day"]["used_usd"] == pytest.approx(cost, abs=1e-4)


def test_the_session_cap_stops_new_work_and_keeps_results_readable(tmp_path: Path) -> None:
    rig = Rig(tmp_path, cap_session_usd=0.0001)
    cookie = rig.user()
    sid = sid_of(rig.search(cookie))
    blocked = rig.call("POST", "/search", {"session_id": sid, "request": "rugs"}, cookie)
    e = err(blocked)
    assert blocked.status == 429 and e["code"] == "cap_reached" and e["limit"] == "session"
    assert e["resets_at"] is None and "session" in e["message"].lower()
    view = json.loads(rig.call("GET", f"/session?id={sid}", cookie=cookie).body)
    assert view["search"]["results"]  # what was shown stays readable
    first = view["search"]["results"][0]["id"]
    assert (
        rig.call(
            "POST", "/rate", {"session_id": sid, "result_id": first, "rating": "love"}, cookie
        ).status
        == 200
    )
    assert view["saved"] is not None
    # a brand-new session of the same invite is its own allowance
    assert rig.call("POST", "/search", {"request": "lamps"}, cookie).status == 202


def test_the_search_and_shop_again_counts(tmp_path: Path) -> None:
    rig = Rig(tmp_path, cap_searches=1, cap_shop_agains=1)
    cookie = rig.user()
    sid = sid_of(rig.search(cookie))
    e = err(rig.call("POST", "/search", {"session_id": sid, "request": "rugs"}, cookie))
    assert e["code"] == "cap_reached" and e["limit"] == "searches" and e["resets_at"] is None
    results = json.loads(rig.call("GET", f"/session?id={sid}", cookie=cookie).body)["search"][
        "results"
    ]
    for r in results[:4]:
        rig.call(
            "POST", "/rate", {"session_id": sid, "result_id": r["id"], "rating": "love"}, cookie
        )
    started = rig.call("POST", "/shop-again", {"session_id": sid}, cookie)
    assert started.status == 202
    rig.service.jobs.wait(json.loads(started.body)["job_id"])
    second = rig.call("POST", "/shop-again", {"session_id": sid}, cookie)
    assert second.status == 429 and err(second)["limit"] == "shop_agains"


def test_the_invite_day_cap_spans_sessions_and_resets_at_midnight_utc(tmp_path: Path) -> None:
    rig = Rig(tmp_path, cap_invite_day_usd=0.0001)
    ann, bob = rig.user("Ann"), rig.user("Bob")
    rig.search(ann)
    blocked = rig.call("POST", "/search", {"request": "other"}, ann)  # a new session, same invite
    e = err(blocked)
    assert blocked.status == 429 and e["limit"] == "invite_day"
    assert e["resets_at"] == "2026-10-31T00:00:00+00:00" and "midnight UTC" in e["message"]
    assert rig.call("POST", "/search", {"request": "bob"}, bob).status == 202  # another invite


def test_the_global_cap_stops_everyone(tmp_path: Path) -> None:
    rig = Rig(tmp_path, cap_global_day_usd=0.0001)
    ann, bob = rig.user("Ann"), rig.user("Bob")
    rig.search(ann)
    for cookie in (ann, bob):
        e = err(rig.call("POST", "/search", {"request": "x"}, cookie))
        assert e["limit"] == "global_day" and e["resets_at"] == "2026-10-31T00:00:00+00:00"


def test_counts_survive_a_restart(tmp_path: Path) -> None:
    rig = Rig(tmp_path, cap_searches=1)
    cookie = rig.user()
    sid = sid_of(rig.search(cookie))
    rig.open()  # a restart: new store, new guard, same database
    assert (
        err(rig.call("POST", "/search", {"session_id": sid, "request": "again"}, cookie))["limit"]
        == "searches"
    )
    assert rig.usage("global_day", "*", "2026-10-30") > 0


def test_two_sessions_near_a_cap_cannot_both_slip_past(tmp_path: Path) -> None:
    probe = Rig(tmp_path / "probe" if (tmp_path / "probe").mkdir() is None else tmp_path)
    one = session_cost(probe.store.load(sid_of(probe.search(probe.user()))))
    rig = Rig(
        tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path,
        cap_global_day_usd=one * 1.5,
    )
    ann, bob = rig.user("Ann"), rig.user("Bob")
    results: list[int] = []

    def go(cookie: str) -> None:
        results.append(rig.search(cookie).status)

    threads = [threading.Thread(target=go, args=(c,)) for c in (ann, bob)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # both were allowed to start (neither had spent yet); in-flight work finishes and is counted,
    # and from then on everyone is stopped
    spent = rig.usage("global_day", "*", "2026-10-30")
    assert spent == pytest.approx(one * 2, rel=0.2) and len(results) == 2
    assert err(rig.call("POST", "/search", {"request": "third"}, ann))["limit"] == "global_day"


def test_the_kill_switch_answers_503_everywhere_but_healthz_and_auth_session(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    cookie = rig.user()
    rig.app.settings = replace(rig.settings, disabled=True)
    for method, path, body in (
        ("GET", "/session", None),
        ("POST", "/search", {"request": "x"}),
        ("POST", "/auth/login", {"code": "x"}),
        ("POST", "/auth/logout", {}),
    ):
        resp = rig.call(method, path, body, cookie)
        assert resp.status == 503 and err(resp)["code"] == "maintenance", path
        assert (
            "maintenance" in err(resp)["message"].lower()
            or "paused" in err(resp)["message"].lower()
        )
    anon = json.loads(rig.call("GET", "/auth/session").body)
    assert anon["maintenance"] is True and anon["authenticated"] is False
    assert json.loads(rig.call("GET", "/auth/session", cookie=cookie).body)["maintenance"] is True
    assert rig.app.handle("GET", "/healthz", {}).status == 200


def test_background_preparation_respects_the_caps(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    cookie = rig.user()
    sid = sid_of(rig.search(cookie))
    session = rig.store.load(sid)
    batch = list(session.results.values())[:6]
    assert rig.guard.background_ok(sid)
    capped = CapGuard(rig.db, replace(rig.settings, cap_global_day_usd=0.0), lambda: NOW)
    assert not capped.background_ok(sid)
    rig.service.prep.guard = capped
    assert rig.service.prep.submit_batch(sid, batch) == 0  # nothing starts past the daily cap
    rig.service.prep.guard = rig.guard


def test_the_guard_asks_the_sessions_owner_when_no_request_context_exists(tmp_path: Path) -> None:
    rig = Rig(tmp_path, cap_invite_day_usd=0.0001)
    cookie = rig.user()
    sid = sid_of(rig.search(cookie))
    assert current_invite.get() is None
    with pytest.raises(ApiError) as info:
        rig.guard.check("spend", sid)
    assert info.value.code == "cap_reached" and info.value.extra["limit"] == "invite_day"
    assert rig.guard.check("spend", None) is None  # nobody to charge, nothing spent yet


def test_local_mode_has_no_guard_and_no_caps(tmp_path: Path) -> None:
    from app.shop.store import SessionStore

    cfg = Config(tracing_disabled=True)
    search, _ = build_shop_providers(cfg)
    svc = ShopService(cfg, SessionStore(tmp_path / "f"), search, ShopFixtureLookup())
    assert svc.guard is None and svc.prep.guard is None
    for _ in range(10):  # more than the deployed per-session search cap
        svc.search(None, "oak coffee tables")


def test_the_meter_never_lowers_a_total_and_counts_only_growth(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    cookie = rig.user()
    sid = sid_of(rig.search(cookie))
    rig.service.prep.wait(sid, 5)  # background preparation spends too, and is counted
    before = rig.usage("global_day", "*", "2026-10-30")
    with rig.store.transaction(sid) as live:
        live.shop_agains_used = 2  # a save with no new spend
    assert rig.usage("global_day", "*", "2026-10-30") == before
    with rig.store.transaction(sid) as live:
        live.lookup_usd += 0.01
    assert rig.usage("global_day", "*", "2026-10-30") == pytest.approx(before + 0.01)
