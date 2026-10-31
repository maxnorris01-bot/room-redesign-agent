"""Stages D1 and D1.2: anonymous visitors (`POST /auth/start`), the cookie, route protection, session
isolation, the address limits, retention, the admin commands. SQLite stands in for Postgres; no
sockets."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.shop import admin
from app.shop.auth import SqlAuth, StartLimiter, address_key, hash_value, hmac_address, purge
from app.shop.caps import Meter
from app.shop.dbstore import SqliteDatabase, SqlSessionStore, migrate
from app.shop.deploy import DeploySettings
from app.shop.providers import ShopFixtureLookup, build_shop_providers
from app.shop.routes import ROUTES
from app.shop.server import COOKIE_NAME, ShopApp
from app.shop.service import ShopService
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)

SECRET = "origin-secret"
ORIGIN = "https://portfolio.example"
BASE = {"Host": "shop.fly.dev", "X-Origin-Secret": SECRET}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def settings() -> DeploySettings:
    return DeploySettings(
        deploy=True, host="0.0.0.0", api_prefix="/api/shop", allowed_hosts=frozenset({"shop.fly.dev"}),
        allowed_origins=frozenset({ORIGIN}), origin_secret=SECRET, public_url=ORIGIN,
        contact_email="me@example.org", database_url="sqlite:///x",
    )  # fmt: skip


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = Clock()
        self.db = SqliteDatabase(str(tmp_path / "a.db"))
        migrate(self.db)
        self.store = SqlSessionStore(self.db)
        self.store.after_save = Meter(self.clock)
        cfg = Config(tracing_disabled=True)
        search, _ = build_shop_providers(cfg)
        self.service = ShopService(cfg, self.store, search, ShopFixtureLookup())
        self.auth = SqlAuth(self.db, settings(), self.clock)
        self.app = ShopApp(self.service, 0, settings(), self.auth, self.store)

    def call(self, method: str, path: str, body: Any = None, cookie: str | None = None, ip: str = "203.0.113.5", extra: dict[str, str] | None = None) -> Any:  # fmt: skip
        h = {
            **BASE,
            "Origin": ORIGIN,
            "Content-Type": "application/json",
            "X-Forwarded-For": ip,
            **(extra or {}),
        }
        if cookie:
            h["Cookie"] = f"{COOKIE_NAME}={cookie}"
        raw = b"" if body is None else json.dumps(body).encode()
        return self.app.handle(method, f"/api/shop{path}", h, raw)

    def visit(self, ip: str = "203.0.113.5") -> str:
        """Start a visit (POST /auth/start) and return the cookie value."""
        resp = self.call("POST", "/auth/start", ip=ip)
        assert resp.status == 200, resp.body
        return str(resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1])

    def rows(self, sql: str) -> list[tuple[Any, ...]]:
        return self.db.transaction(lambda tx: tx.execute(sql))


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def error(resp: Any) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(resp.body)["error"]
    return out


def test_start_sets_a_cookie_that_works_and_is_stored_only_as_a_hash(rig: Rig) -> None:
    resp = rig.call("POST", "/auth/start")
    assert resp.status == 200 and json.loads(resp.body) == {"ok": True}
    header = resp.headers["Set-Cookie"]
    value = header.split(";")[0].split("=", 1)[1]
    for flag in ("HttpOnly", "SameSite=Lax", "Secure", "Path=/api/shop", "Max-Age=2592000"):
        assert flag in header
    assert len(value) >= 22  # 128 random bits, urlsafe
    assert rig.call("GET", "/session", cookie=value).status == 200
    stored = rig.rows("SELECT id_hash, expires_at FROM login_sessions")
    assert stored[0][0] == hash_value(value) and value not in repr(stored)
    assert stored[0][1] > rig.clock.now.isoformat()[:19]
    assert rig.call("POST", "/auth/start").headers["Set-Cookie"] != header  # a new visitor


def test_a_secure_flag_follows_the_public_url(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.app.settings = DeploySettings(**{**rig.app.settings.__dict__, "public_url": "http://x"})
    header = rig.call("POST", "/auth/start").headers["Set-Cookie"]
    assert "Secure" not in header and "HttpOnly" in header


def test_a_visit_is_extended_when_used_and_expires_when_not(rig: Rig) -> None:
    cookie = rig.visit()
    rig.clock.now += timedelta(days=20)
    assert rig.call("GET", "/session", cookie=cookie).status == 200  # used: extended
    rig.clock.now += timedelta(days=20)  # 40 days from the start, 20 from the last use
    assert rig.call("GET", "/session", cookie=cookie).status == 200
    rig.clock.now += timedelta(days=31)  # unused for longer than the lifetime
    assert rig.call("GET", "/session", cookie=cookie).status == 401


def test_start_is_limited_per_address_per_minute_with_retry_after(rig: Rig) -> None:
    last = None
    for _ in range(11):
        last = rig.call("POST", "/auth/start", ip="192.0.2.1")
    assert last.status == 429 and error(last)["code"] == "rate_limited"
    assert int(last.headers["Retry-After"]) >= 1
    assert rig.call("POST", "/auth/start", ip="192.0.2.2").status == 200  # another address
    rig.clock.now += timedelta(minutes=2)
    assert rig.call("POST", "/auth/start", ip="192.0.2.1").status == 200  # the window passed
    tight = StartLimiter(2, 60, lambda: rig.clock.now.timestamp())
    assert [tight.check("a"), tight.check("a"), tight.check("a") > 0] == [0, 0, True]


def test_one_address_may_start_only_so_many_visitors_a_day(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.app.settings = DeploySettings(**{**rig.app.settings.__dict__, "max_visitors_per_ip_day": 3})
    rig.auth.settings = rig.app.settings
    for _ in range(3):
        assert rig.call("POST", "/auth/start", ip="192.0.2.9").status == 200
        rig.clock.now += timedelta(minutes=2)  # keep under the per-minute limit
    blocked = rig.call("POST", "/auth/start", ip="192.0.2.9")
    assert blocked.status == 429 and error(blocked)["code"] == "rate_limited"
    wait = int(blocked.headers["Retry-After"])
    assert 1 <= wait <= 86400 and "midnight UTC" in error(blocked)["message"]
    assert rig.call("POST", "/auth/start", ip="192.0.2.10").status == 200  # another address
    rig.clock.now += timedelta(days=1)  # tomorrow
    assert rig.call("POST", "/auth/start", ip="192.0.2.9").status == 200


def test_everyone_may_start_only_so_many_visitors_a_day(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.app.settings = DeploySettings(**{**rig.app.settings.__dict__, "max_visitors_day": 2})
    rig.auth.settings = rig.app.settings
    assert rig.call("POST", "/auth/start", ip="192.0.2.1").status == 200
    assert rig.call("POST", "/auth/start", ip="192.0.2.2").status == 200
    blocked = rig.call("POST", "/auth/start", ip="192.0.2.3")
    assert blocked.status == 429 and int(blocked.headers["Retry-After"]) >= 1
    assert len(rig.rows("SELECT id FROM visitors")) == 2  # nothing was made for the refused one


def test_the_address_is_stored_only_as_a_keyed_hash(rig: Rig) -> None:
    rig.visit(ip="198.51.100.77")
    dump = repr(
        [
            rig.rows(f"SELECT * FROM {t}")
            for t in ("visitors", "usage", "login_sessions", "shop_sessions")
        ]
    )
    assert "198.51.100.77" not in dump
    key = address_key(SECRET)
    assert rig.rows("SELECT ip_hmac FROM visitors")[0][0] == hmac_address(key, "198.51.100.77")
    assert hmac_address(address_key("another secret"), "198.51.100.77") != hmac_address(
        key, "198.51.100.77"
    )  # rotating the origin secret starts every address from zero


def test_forged_forwarded_addresses_cannot_dodge_the_limit(rig: Rig) -> None:
    for i in range(12):  # no secret: the forwarded address is ignored, nothing is created
        resp = rig.app.handle(
            "POST",
            "/api/shop/auth/start",
            {
                "Host": "shop.fly.dev",
                "Origin": ORIGIN,
                "Content-Type": "application/json",
                "X-Forwarded-For": f"1.1.1.{i}",
            },
            b"",
        )
    assert resp.status == 403 and resp.body == b"{}" and rig.rows("SELECT id FROM visitors") == []


def test_start_needs_an_allowed_origin(rig: Rig) -> None:
    resp = rig.call("POST", "/auth/start", extra={"Origin": "https://evil.example"})
    assert resp.status == 403 and rig.rows("SELECT id FROM visitors") == []


def test_every_route_in_the_table_is_protected_or_deliberately_open(rig: Rig) -> None:
    """A new route cannot be added unprotected: this walks the table."""
    open_routes = {r.name for r in ROUTES if r.auth == "none"}
    assert open_routes == {"auth_start", "auth_logout", "auth_session"}
    for route in ROUTES:
        rel = route.path.replace("{id}", "j" * 20)
        resp = rig.call(route.method, rel, {} if route.body else None)
        if route.auth == "login":
            assert resp.status == 401 and error(resp)["code"] == "session_required", route
            assert "/auth/start" in error(resp)["message"]
        else:
            assert resp.status != 401, route
    cookie = rig.visit()
    for route in ROUTES:
        if route.auth == "login":
            rel = route.path.replace("{id}", "j" * 20)
            assert rig.call(route.method, rel, {} if route.body else None, cookie).status != 401, (
                route
            )


def test_logout_clears_the_cookie_and_the_next_visit_is_a_new_visitor(rig: Rig) -> None:
    cookie = rig.visit()
    out = rig.call("POST", "/auth/logout", {}, cookie)
    assert out.status == 200 and "Max-Age=0" in out.headers["Set-Cookie"]
    assert rig.call("GET", "/session", cookie=cookie).status == 401
    rig.visit()
    assert len(rig.rows("SELECT id FROM visitors")) == 2


def test_auth_session_shapes(rig: Rig) -> None:
    anon = json.loads(rig.call("GET", "/auth/session").body)
    assert anon == {"authenticated": False, "contact_email": "me@example.org"}
    cookie = rig.visit()
    me = json.loads(rig.call("GET", "/auth/session", cookie=cookie).body)
    assert me["authenticated"] is True and me["contact_email"] == "me@example.org"
    assert "label" not in me
    assert set(me["usage"]) == {"ip_day", "session", "resets_at"}
    assert me["usage"]["ip_day"] == {"used_usd": 0.0, "cap_usd": 1.0}
    assert me["usage"]["session"] == {"cap_usd": 0.5, "max_searches": 8, "max_shop_agains": 25}
    assert me["usage"]["resets_at"] == "2026-10-31T00:00:00+00:00"
    assert "global" not in json.dumps(me)
    assert (
        json.loads(rig.call("GET", "/auth/session", cookie="garbage").body)["authenticated"]
        is False
    )


def test_one_visitor_cannot_read_or_change_anothers_session(rig: Rig) -> None:
    c1 = rig.visit(ip="203.0.113.1")
    c2 = rig.visit(ip="203.0.113.2")
    started = rig.call("POST", "/search", {"request": "oak coffee tables"}, c1)
    assert started.status == 202
    job = json.loads(started.body)["job_id"]
    rig.service.jobs.wait(job)
    assert rig.call("GET", f"/job/{job}", cookie=c2).status == 404  # not even the job
    done = json.loads(rig.call("GET", f"/job/{job}", cookie=c1).body)
    sid = done["result"]["session_id"]
    assert rig.store.owner_of(sid) is not None
    mine = rig.call("GET", f"/session?id={sid}", cookie=c1)
    assert mine.status == 200 and json.loads(mine.body)["session_id"] == sid
    for resp in (
        rig.call("GET", f"/session?id={sid}", cookie=c2),
        rig.call("POST", "/rate", {"session_id": sid, "result_id": "x", "rating": "love"}, c2),
        rig.call("POST", "/search", {"session_id": sid, "request": "rugs"}, c2),
        rig.call("POST", "/reset", {"session_id": sid}, c2),
    ):
        assert resp.status == 404 and error(resp)["code"] == "not_found"
    assert rig.call("GET", f"/session?id={'a' * 16}", cookie=c1).status == 404  # unknown: the same


# ---- retention ------------------------------------------------------------------------------------------


def seed_old_visit(rig: Rig, days_ago: int) -> str:
    cookie = rig.visit(ip=f"203.0.113.{days_ago}")
    started = rig.call(
        "POST", "/search", {"request": "oak coffee tables"}, cookie, ip=f"203.0.113.{days_ago}"
    )
    rig.service.jobs.wait(json.loads(started.body)["job_id"])
    rig.call("GET", f"/job/{json.loads(started.body)['job_id']}", cookie=cookie)
    rig.service.prep.wait(rig.rows("SELECT session_id FROM shop_sessions")[-1][0], 5)
    rig.clock.now += timedelta(days=days_ago)
    return cookie


def test_retention_purges_unused_visitors_and_keeps_active_ones(rig: Rig) -> None:
    old = rig.visit(ip="203.0.113.50")
    rig.clock.now += timedelta(days=29)
    fresh = rig.visit(ip="203.0.113.51")
    rig.call("POST", "/search", {"request": "oak coffee tables"}, fresh, ip="203.0.113.51")
    rig.clock.now += timedelta(days=2)  # old is 31 days unused, fresh 2
    for job in list(rig.service.jobs._jobs):  # type: ignore[attr-defined]
        rig.service.jobs.wait(job)
    gone = purge(rig.db, rig.clock.now, 30)
    assert gone["visitors"] == 1 and gone["login_sessions"] >= 1
    assert [r[0] for r in rig.rows("SELECT ip_hmac FROM visitors")] == [
        hmac_address(address_key(SECRET), "203.0.113.51")
    ]
    assert rig.call("GET", "/session", cookie=old).status == 401
    assert rig.call("GET", "/session", cookie=fresh).status == 200


def test_retention_removes_shop_sessions_and_their_counters_and_old_usage(rig: Rig) -> None:
    cookie = rig.visit()
    started = rig.call("POST", "/search", {"request": "oak coffee tables"}, cookie)
    job = json.loads(started.body)["job_id"]
    rig.service.jobs.wait(job)
    sid = json.loads(rig.call("GET", f"/job/{job}", cookie=cookie).body)["result"]["session_id"]
    rig.service.prep.wait(sid, 5)
    assert rig.rows("SELECT session_id FROM shop_sessions") and rig.rows(
        "SELECT * FROM usage WHERE scope = 'session'"
    )
    rig.clock.now += timedelta(days=45)
    gone = purge(rig.db, rig.clock.now, 30)
    assert gone["shop_sessions"] == 1 and gone["visitors"] == 1
    assert rig.rows("SELECT * FROM shop_sessions") == []
    assert (
        rig.rows("SELECT * FROM usage") == []
    )  # session counter, address counter, daily rows (40 d)


def test_usage_rows_newer_than_forty_days_stay(rig: Rig) -> None:
    rig.visit()
    rig.clock.now += timedelta(days=35)
    purge(rig.db, rig.clock.now, 30)
    assert rig.rows("SELECT scope FROM usage WHERE scope = 'visitors_day'")  # 35 days old: kept
    rig.clock.now += timedelta(days=6)
    purge(rig.db, rig.clock.now, 30)
    assert rig.rows("SELECT scope FROM usage WHERE scope = 'visitors_day'") == []


def test_retention_runs_lazily_at_most_every_six_hours(rig: Rig) -> None:
    rig.visit()
    rig.auth.tick()  # the first call runs a purge (nothing is old yet)
    rig.db.transaction(
        lambda tx: tx.execute("UPDATE visitors SET last_seen = '2026-01-01T00:00:00+00:00'")
    )
    rig.clock.now += timedelta(hours=5)
    rig.auth.tick()  # under six hours since the last purge: nothing runs
    assert rig.rows("SELECT id FROM visitors")
    rig.clock.now += timedelta(hours=2)
    rig.call("GET", "/auth/session", cookie="x")  # any request may trigger it; identify() does
    assert rig.rows("SELECT id FROM visitors") == []


def test_a_purge_failure_never_fails_a_request(
    rig: Rig, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("purge failed")

    monkeypatch.setattr("app.shop.auth.purge", boom)
    assert rig.call("POST", "/auth/start").status == 200
    assert "shop error [purge] RuntimeError" in capsys.readouterr().err


def test_the_bot_check_hook_can_refuse(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.shop.auth.bot_check_allowed", lambda headers: False)
    resp = rig.call("POST", "/auth/start")
    assert resp.status == 403 and rig.rows("SELECT id FROM visitors") == []


# ---- admin commands -------------------------------------------------------------------------------------


def admin_run(tmp_path: Path, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    code = admin.run(list(args), {"DATABASE_URL": f"sqlite:///{tmp_path / 'a.db'}"}, out)
    return code, out.getvalue()


def test_the_admin_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert admin_run(tmp_path, "migrate")[0] == 0 and admin_run(tmp_path, "migrate")[0] == 0
    code, text = admin_run(tmp_path, "caps", "show")
    assert code == 0
    for needle in (
        "$0.50",
        "$1.00",
        "$2.00",
        "8 searches",
        "25 Shop Agains",
        "visitors started today: 0 of 200",
        "30 days",
    ):
        assert needle in text, needle
    code, text = admin_run(tmp_path, "purge")
    assert code == 0 and text.startswith("purged: ")
    assert admin.run(["migrate"], {}, io.StringIO()) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_the_invite_commands_are_gone(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        admin_run(tmp_path, "invite", "create", "--label", "x")


def test_caps_show_counts_visitors_started_today(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    live = SqlAuth(rig.db, settings())  # the admin command reads the real clock
    live.start("192.0.2.1")
    live.start("192.0.2.2")
    assert "visitors started today: 2 of 200" in admin_run(tmp_path, "caps", "show")[1]


def test_nothing_secret_or_raw_reaches_the_output_or_the_logs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rig = Rig(tmp_path)
    rig.visit(ip="198.51.100.200")
    env = {"DATABASE_URL": f"sqlite:///{tmp_path / 'a.db'}", "APP_ORIGIN_SECRET": SECRET}
    out = io.StringIO()
    for cmd in (["migrate"], ["caps", "show"], ["purge"]):
        admin.run(cmd, env, out)
    seen = out.getvalue() + capsys.readouterr().out + capsys.readouterr().err
    assert SECRET not in seen and str(tmp_path) not in seen and "198.51.100.200" not in seen


def test_a_visit_and_a_search_print_no_address_cookie_or_secret(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    cookie = rig.visit(ip="198.51.100.123")
    started = rig.call(
        "POST", "/search", {"request": "oak coffee tables"}, cookie, ip="198.51.100.123"
    )
    rig.service.jobs.wait(json.loads(started.body)["job_id"])
    rig.call("GET", "/auth/session", cookie=cookie, ip="198.51.100.123")
    seen = capsys.readouterr()
    text = seen.out + seen.err
    assert "198.51.100.123" not in text and cookie not in text and SECRET not in text
    body = rig.call("GET", "/auth/session", cookie=cookie, ip="198.51.100.123").body.decode()
    assert "198.51.100.123" not in body and hash_value("198.51.100.123") not in body
