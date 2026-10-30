"""Stage D1, phase 3: invite codes, login cookies, route protection, session isolation, the admin
commands. SQLite stands in for Postgres; no sockets."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.shop import admin
from app.shop.auth import LoginLimiter, SqlAuth, hash_code
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
        cfg = Config(tracing_disabled=True)
        search, _ = build_shop_providers(cfg)
        self.service = ShopService(cfg, self.store, search, ShopFixtureLookup())
        self.auth = SqlAuth(
            self.db, settings(), self.clock, LoginLimiter(lambda: self.clock.now.timestamp())
        )
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

    def login(self, code: str, ip: str = "203.0.113.5") -> str:
        resp = self.call("POST", "/auth/login", {"code": code}, ip=ip)
        assert resp.status == 200, resp.body
        return str(resp.headers["Set-Cookie"].split(";")[0].split("=", 1)[1])

    def invite(self, label: str = "Friend", days: int | None = None) -> tuple[str, str]:
        return self.auth.create_invite(label, days)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def error(resp: Any) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(resp.body)["error"]
    return out


def test_a_code_is_random_long_and_only_its_hash_is_stored(rig: Rig) -> None:
    invite_id, code = rig.invite("Ann")
    other = rig.invite("Bob")[1]
    assert code != other and len(code) >= 5 + 32  # 24 random bytes, urlsafe
    rows = rig.db.transaction(
        lambda tx: tx.execute("SELECT code_hash, label FROM invites WHERE id = ?", (invite_id,))
    )
    assert rows[0][0] == hash_code(code) and code not in repr(rows) and len(rows[0][0]) == 64


def test_login_sets_a_cookie_that_works_and_is_stored_only_as_a_hash(rig: Rig) -> None:
    _, code = rig.invite("Ann")
    resp = rig.call("POST", "/auth/login", {"code": code})
    cookie_header = resp.headers["Set-Cookie"]
    value = cookie_header.split(";")[0].split("=", 1)[1]
    for flag in ("HttpOnly", "SameSite=Lax", "Secure", "Path=/api/shop", "Max-Age=1209600"):
        assert flag in cookie_header
    assert rig.call("GET", "/session", cookie=value).status == 200
    stored = rig.db.transaction(
        lambda tx: tx.execute("SELECT id_hash, expires_at FROM login_sessions")
    )
    assert stored[0][0] == hash_code(value) and value not in repr(stored)
    assert stored[0][1] > rig.clock.now.isoformat()[:19]


def test_a_wrong_expired_and_revoked_code_all_look_the_same(rig: Rig) -> None:
    good_id, good = rig.invite("ok")
    _, old = rig.invite("old", days=1)
    revoked_id, revoked = rig.invite("gone")
    rig.auth.revoke_invite(revoked_id)
    rig.clock.now += timedelta(days=2)
    answers = []
    for code in ("nope", old, revoked, "x" * 190):
        resp = rig.call("POST", "/auth/login", {"code": code}, ip="198.51.100.7")
        answers.append((resp.status, resp.body, "Set-Cookie" in resp.headers))
    assert len({a for a in answers}) == 1 and answers[0][0] == 401 and answers[0][2] is False
    assert good_id and rig.login(good, ip="198.51.100.8")  # the good code still works


def test_login_attempts_are_rate_limited_per_address_and_globally(rig: Rig) -> None:
    last = None
    for _ in range(9):
        last = rig.call("POST", "/auth/login", {"code": "nope"}, ip="192.0.2.1")
    assert (
        last.status == 429
        and error(last)["code"] == "rate_limited"
        and int(last.headers["Retry-After"]) >= 1
    )
    assert (
        rig.call("POST", "/auth/login", {"code": "nope"}, ip="192.0.2.2").status == 401
    )  # another address
    rig.clock.now += timedelta(minutes=11)
    assert (
        rig.call("POST", "/auth/login", {"code": "nope"}, ip="192.0.2.1").status == 401
    )  # the window passed
    tight = LoginLimiter(lambda: rig.clock.now.timestamp(), per_ip=100, total=3)
    assert [tight.check(f"10.0.0.{i}") for i in range(4)][:3] == [0, 0, 0] and tight.check(
        "10.0.0.9"
    ) > 0


def test_forged_forwarded_addresses_cannot_dodge_the_limit(rig: Rig) -> None:
    for i in range(9):  # no secret: the forwarded address is ignored, everyone is "unknown"
        resp = rig.app.handle(
            "POST",
            "/api/shop/auth/login",
            {
                "Host": "shop.fly.dev",
                "Origin": ORIGIN,
                "Content-Type": "application/json",
                "X-Forwarded-For": f"1.1.1.{i}",
            },
            b'{"code":"x"}',
        )
    assert resp.status == 403 and resp.body == b"{}"


def test_every_route_in_the_table_is_protected_or_deliberately_open(rig: Rig) -> None:
    """A new route cannot be added unprotected: this walks the table."""
    open_routes = {r.name for r in ROUTES if r.auth == "none"}
    assert open_routes == {"auth_login", "auth_logout", "auth_session"}
    for route in ROUTES:
        rel = route.path.replace("{id}", "j" * 20)
        resp = rig.call(route.method, rel, {} if route.body else None)
        if route.auth == "login":
            assert resp.status == 401 and error(resp)["code"] == "login_required", route
        else:
            assert resp.status != 401 or route.name == "auth_login", route
    # and with a login none of them answers 401
    _, code = rig.invite()
    cookie = rig.login(code)
    for route in ROUTES:
        if route.auth == "login":
            rel = route.path.replace("{id}", "j" * 20)
            assert rig.call(route.method, rel, {} if route.body else None, cookie).status != 401, (
                route
            )


def test_a_revoked_invite_ends_its_sessions_at_once_and_so_does_expiry(rig: Rig) -> None:
    invite_id, code = rig.invite("Ann")
    cookie = rig.login(code)
    assert rig.call("GET", "/session", cookie=cookie).status == 200
    assert rig.auth.revoke_invite(invite_id) and not rig.auth.revoke_invite("missing")
    assert rig.call("GET", "/session", cookie=cookie).status == 401
    _, short = rig.invite("short", days=1)
    c2 = rig.login(short, ip="203.0.113.6")
    rig.clock.now += timedelta(days=2)
    assert rig.call("GET", "/session", cookie=c2).status == 401
    _, long_code = rig.invite("long")
    c3 = rig.login(long_code, ip="203.0.113.7")
    rig.clock.now += timedelta(days=15)  # past the 14 day login lifetime
    assert rig.call("GET", "/session", cookie=c3).status == 401


def test_logout_clears_the_cookie_and_the_login(rig: Rig) -> None:
    _, code = rig.invite()
    cookie = rig.login(code)
    out = rig.call("POST", "/auth/logout", {}, cookie)
    assert out.status == 200 and "Max-Age=0" in out.headers["Set-Cookie"]
    assert rig.call("GET", "/session", cookie=cookie).status == 401


def test_auth_session_shapes(rig: Rig) -> None:
    anon = json.loads(rig.call("GET", "/auth/session").body)
    assert anon == {"authenticated": False, "contact_email": "me@example.org"}
    _, code = rig.invite("Ann")
    cookie = rig.login(code)
    me = json.loads(rig.call("GET", "/auth/session", cookie=cookie).body)
    assert (
        me["authenticated"] is True
        and me["label"] == "Ann"
        and me["contact_email"] == "me@example.org"
    )
    assert set(me["usage"]) == {"invite_day", "session", "resets_at"}
    assert me["usage"]["invite_day"] == {"used_usd": 0.0, "cap_usd": 1.0}
    assert me["usage"]["session"] == {"cap_usd": 0.5, "max_searches": 8, "max_shop_agains": 25}
    assert "global" not in json.dumps(me)
    assert (
        json.loads(rig.call("GET", "/auth/session", cookie="garbage").body)["authenticated"]
        is False
    )


def test_one_invite_cannot_read_or_change_anothers_session(rig: Rig) -> None:
    c1 = rig.login(rig.invite("Ann")[1], ip="203.0.113.1")
    c2 = rig.login(rig.invite("Bob")[1], ip="203.0.113.2")
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


# ---- admin commands -------------------------------------------------------------------------------------


def admin_run(tmp_path: Path, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    code = admin.run(list(args), {"DATABASE_URL": f"sqlite:///{tmp_path / 'a.db'}"}, out)
    return code, out.getvalue()


def test_the_admin_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert admin_run(tmp_path, "migrate")[0] == 0 and admin_run(tmp_path, "migrate")[0] == 0
    code, text = admin_run(tmp_path, "invite", "create", "--label", "Ann", "--days", "30")
    assert code == 0 and "shop-" in text
    secret = [ln for ln in text.splitlines() if ln.startswith("shop-")][0]
    invite_id = text.split()[1]
    listed = admin_run(tmp_path, "invite", "list")[1]
    assert "Ann" in listed and "active" in listed and "never" in listed and secret not in listed
    assert "0.0000" in listed
    code, text = admin_run(tmp_path, "caps", "show")
    assert (
        code == 0
        and "$0.50" in text
        and "$1.00" in text
        and "$3.00" in text
        and "8 searches" in text
    )
    assert admin_run(tmp_path, "invite", "revoke", invite_id)[0] == 0
    assert "revoked" in admin_run(tmp_path, "invite", "list")[1]
    assert admin_run(tmp_path, "invite", "revoke", "nope")[0] == 2
    assert admin.run(["migrate"], {}, io.StringIO()) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_nothing_secret_reaches_the_output_or_the_logs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = {"DATABASE_URL": f"sqlite:///{tmp_path / 'a.db'}", "APP_ORIGIN_SECRET": SECRET}
    out = io.StringIO()
    admin.run(["migrate"], env, out)
    admin.run(["caps", "show"], env, out)
    admin.run(["invite", "list"], env, out)
    seen = out.getvalue() + capsys.readouterr().out + capsys.readouterr().err
    assert SECRET not in seen and str(tmp_path) not in seen
