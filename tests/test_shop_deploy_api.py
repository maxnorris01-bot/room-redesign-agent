"""Stage D1, phase 1: deployment mode (API only behind an origin secret), settings, proxy trust,
Host and Origin checks, error shapes, cache headers. No sockets, no database."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.shop.deploy import DeploySettings, from_env
from app.shop.providers import ShopFixtureLookup
from app.shop.routes import ROUTES, match
from app.shop.server import STATIC_FILES, Identity, ShopApp
from app.shop.service import ShopService
from app.shop.store import SessionStore
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)

SECRET = "test-origin-secret"
ORIGIN = "https://portfolio.example"
HOSTS = "shop.fly.dev"
GOOD = {"Host": HOSTS, "X-Origin-Secret": SECRET}


class FakeAuth:
    """Accepts the cookie value "ok" for invite i1."""

    def __init__(self) -> None:
        self.logins: list[tuple[str, str]] = []

    def login(self, code: str, ip: str) -> tuple[str | None, int]:
        self.logins.append((code, ip))
        return ("ok", 0) if code == "right" else (None, 0)

    def logout(self, cookie: str | None) -> None:
        return None

    def identify(self, cookie: str | None) -> Identity | None:
        return Identity("i1", "Friend", "l1") if cookie == "ok" else None

    def usage(self, identity: Identity) -> dict[str, Any]:
        return {"session_usd_cap": 0.5}


def settings(**kw: Any) -> DeploySettings:
    base: dict[str, Any] = {
        "deploy": True, "host": "0.0.0.0", "api_prefix": "/api/shop",
        "allowed_hosts": frozenset({HOSTS}), "allowed_origins": frozenset({ORIGIN}),
        "origin_secret": SECRET, "public_url": ORIGIN, "contact_email": "me@example.org",
        "database_url": "sqlite:///x",
    }  # fmt: skip
    return DeploySettings(**{**base, **kw})


def make_app(tmp_path: Path, auth: FakeAuth | None = None, **kw: Any) -> ShopApp:
    cfg = Config(tracing_disabled=True)
    from app.shop.providers import build_shop_providers

    search, _ = build_shop_providers(cfg)
    svc = ShopService(cfg, SessionStore(tmp_path / "d"), search, ShopFixtureLookup())
    return ShopApp(svc, 0, settings(**kw), auth or FakeAuth())


def call(app: ShopApp, method: str, path: str, headers: dict[str, str] | None = None,
         body: Any = None, login: bool = True) -> Any:  # fmt: skip
    h = {**GOOD, "Origin": ORIGIN, "Content-Type": "application/json", **(headers or {})}
    if login:
        h.setdefault("Cookie", "shop_login=ok")
    raw = b"" if body is None else json.dumps(body).encode()
    return app.handle(method, path, h, raw)


def err(resp: Any) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(resp.body)["error"]
    return out


# ---- settings ---------------------------------------------------------------------------------------


def test_every_setting_is_read_from_the_environment() -> None:
    s = from_env({
        "APP_DEPLOY_MODE": "1", "APP_PORT": "8080", "APP_API_PREFIX": "/api/shop/",
        "APP_ALLOWED_HOSTS": "A.fly.dev, b.fly.dev", "APP_ALLOWED_ORIGINS": "https://P.example/",
        "APP_ORIGIN_SECRET": "s", "APP_PUBLIC_URL": "https://p.example", "APP_CONTACT_EMAIL": "me@x.org",
        "APP_SHOP_DISABLED": "1", "DATABASE_URL": "postgres://x", "APP_ALLOW_NO_SECRET": "1",
    })  # fmt: skip
    assert s.deploy and s.host == "0.0.0.0" and s.port == 8080 and s.api_prefix == "/api/shop"
    assert s.allowed_hosts == {"a.fly.dev", "b.fly.dev"} and s.allowed_origins == {
        "https://p.example"
    }
    assert s.origin_secret == "s" and s.secure_cookie and s.contact_email == "me@x.org"
    assert s.disabled and s.database_url == "postgres://x" and s.allow_no_secret
    assert s.forwarded_hosts == {"p.example"} and s.problems() == []


def test_locally_nothing_changes_and_the_prefix_is_ignored() -> None:
    s = from_env({"APP_API_PREFIX": "/custom", "APP_HOST": "0.0.0.0", "APP_ORIGIN_SECRET": "x"})
    assert not s.deploy and s.host == "127.0.0.1" and s.port == 8765 and s.api_prefix == "/api"
    assert s.problems() == []
    assert from_env({"APP_DEPLOY_MODE": "1"}).api_prefix == "/api/shop"


def test_deployment_mode_refuses_to_start_without_the_secret_or_other_settings() -> None:
    bare = from_env({"APP_DEPLOY_MODE": "1"})
    text = " ".join(bare.problems())
    for needle in ("APP_ORIGIN_SECRET", "APP_ALLOWED_HOSTS", "APP_ALLOWED_ORIGINS", "DATABASE_URL"):
        assert needle in text
    assert "never in production" in text
    full = {"APP_DEPLOY_MODE": "1", "APP_ALLOWED_HOSTS": "a", "APP_ALLOWED_ORIGINS": "https://p",
            "DATABASE_URL": "x"}  # fmt: skip
    assert (
        from_env(full).problems() and not from_env({**full, "APP_ALLOW_NO_SECRET": "1"}).problems()
    )
    assert not from_env({**full, "APP_ORIGIN_SECRET": "s"}).problems()
    assert from_env({**full, "APP_ORIGIN_SECRET": "s", "APP_API_PREFIX": "api/"}).problems()


def test_the_secret_and_the_database_url_never_show_in_a_repr() -> None:
    assert SECRET not in repr(settings()) and "sqlite" not in repr(settings())


# ---- what deployment mode serves --------------------------------------------------------------------


def test_healthz_needs_no_login_no_secret_and_says_only_ok(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    resp = app.handle("GET", "/healthz", {})
    assert resp.status == 200 and json.loads(resp.body) == {"ok": True}
    assert app.handle("POST", "/healthz", {}).status == 405


def test_deployment_mode_serves_no_html_no_static_files_and_404s_the_rest(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    for path in [*STATIC_FILES, "/favicon.ico", "/api/session", "/anything", "/api/shopx/session"]:
        resp = call(app, "GET", path)
        assert resp.status == 404 and err(resp)["code"] == "not_found", path
        assert "html" not in resp.content_type
    assert call(app, "GET", "/api/shop/nothing").status == 404


def test_locally_the_page_and_the_static_files_are_still_served(tmp_path: Path) -> None:
    cfg = Config(tracing_disabled=True)
    svc = ShopService(cfg, SessionStore(tmp_path / "d"), ShopFixturesSearch(), ShopFixtureLookup())
    app = ShopApp(svc, 8765)
    hdr = {"Host": "127.0.0.1:8765"}
    for path, (_name, ctype) in STATIC_FILES.items():
        resp = app.handle("GET", path, hdr)
        assert resp.status == 200 and resp.content_type == ctype and resp.body
    assert app.handle("GET", "/api/session", hdr).status == 200
    assert json.loads(app.handle("GET", "/api/auth/session", hdr).body) == {
        "authenticated": True, "local": True,
    }  # fmt: skip
    assert (
        app.handle(
            "POST", "/api/auth/login", {**hdr, "Content-Type": "application/json"}, b"{}"
        ).status
        == 404
    )


class ShopFixturesSearch:
    name = "serper"
    thread_safe = True

    def search(self, query: str, gl: str = "us", hl: str = "en", page: int = 1) -> list[Any]:
        return []


def test_every_route_is_reachable_under_the_prefix(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    for route in ROUTES:
        rel = route.path.replace("{id}", "x" * 20)
        resp = call(app, route.method, f"/api/shop{rel}", body={} if route.body else None)
        assert b"Not found." not in resp.body, route  # reached a route (it may still say no)
        assert match(route.method, rel)[0] == route


# ---- the origin secret and the forwarded headers -----------------------------------------------------


def test_a_request_without_the_secret_gets_403_and_an_empty_body(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    for hdrs in (
        {"X-Origin-Secret": ""},
        {"X-Origin-Secret": "wrong"},
        {"X-Origin-Secret": SECRET + "x"},
    ):
        resp = call(app, "GET", "/api/shop/session", hdrs)
        assert resp.status == 403 and resp.body == b"{}"
    bare = app.handle("GET", "/api/shop/session", {"Host": HOSTS})
    assert bare.status == 403 and bare.body == b"{}"
    assert app.handle("GET", "/", {"Host": HOSTS}).status == 403  # not even a 404 without it


def test_the_comparison_is_timing_safe() -> None:
    import inspect

    from app.shop import server

    assert "hmac.compare_digest" in inspect.getsource(server.ShopApp._trusted)


def test_forwarded_headers_are_trusted_only_with_the_secret(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    fwd = {"X-Forwarded-For": "203.0.113.9, 10.0.0.1", "X-Real-Ip": "198.51.100.2"}
    assert app.client_ip({**GOOD, **fwd}) == "203.0.113.9"
    assert app.client_ip({"Host": HOSTS, **{k.title(): v for k, v in fwd.items()}}) == "unknown"
    assert app.client_ip({**GOOD, "X-Real-Ip": "198.51.100.2"}) == "198.51.100.2"
    no_secret = make_app(tmp_path, origin_secret="", allow_no_secret=True)
    assert no_secret.client_ip({"X-Forwarded-For": "203.0.113.9"}) == "unknown"


def test_the_host_must_be_configured_or_the_forwarded_host_the_portfolio(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    assert call(app, "GET", "/api/shop/auth/session", {"Host": "evil.example"}).status == 403
    ok = call(
        app,
        "GET",
        "/api/shop/auth/session",
        {"Host": "other.fly.dev", "X-Forwarded-Host": "portfolio.example"},
    )
    assert ok.status == 200
    bad = call(
        app,
        "GET",
        "/api/shop/auth/session",
        {"Host": "other.fly.dev", "X-Forwarded-Host": "evil.example"},
    )
    assert bad.status == 403 and err(bad)["code"] == "forbidden"


# ---- Origin and content type, no CORS ------------------------------------------------------------------


def test_writes_need_an_allowed_origin_or_referer(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    body = {"session_id": None}
    assert (
        call(app, "POST", "/api/shop/reset", {"Origin": "https://evil.example"}, body).status == 403
    )
    h = {
        k: v
        for k, v in {**GOOD, "Content-Type": "application/json", "Cookie": "shop_login=ok"}.items()
    }
    raw = b"{}"
    assert app.handle("POST", "/api/shop/reset", h, raw).status == 403  # neither Origin nor Referer
    assert (
        app.handle("POST", "/api/shop/reset", {**h, "Referer": ORIGIN + "/shop"}, raw).status != 403
    )
    assert app.handle("POST", "/api/shop/reset", {**h, "Origin": ORIGIN}, raw).status != 403
    assert call(app, "GET", "/api/shop/session", {"Origin": "https://evil.example"}).status != 403


def test_a_body_route_needs_application_json(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    resp = call(app, "POST", "/api/shop/search", {"Content-Type": "text/plain"}, {"request": "x"})
    assert resp.status == 400 and err(resp)["code"] == "bad_request"
    form = call(
        app, "POST", "/api/shop/search", {"Content-Type": "application/x-www-form-urlencoded"}, {}
    )
    assert form.status == 400


def test_no_cors_headers_are_ever_sent(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    for resp in (
        call(app, "GET", "/api/shop/auth/session"),
        call(app, "OPTIONS", "/api/shop/search", {"Access-Control-Request-Method": "POST"}),
        call(app, "POST", "/api/shop/search", {"Origin": "https://evil.example"}, {}),
        app.handle("GET", "/healthz", {}),
    ):
        assert not [h for h in resp.all_headers() if h.lower().startswith("access-control")]
    assert call(app, "OPTIONS", "/api/shop/search").status == 405


# ---- headers and error shapes --------------------------------------------------------------------------


def test_every_api_response_is_no_store_and_varies_by_cookie(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    for resp in (
        call(app, "GET", "/api/shop/session"),
        call(app, "GET", "/api/shop/auth/session"),
        call(app, "GET", "/api/shop/session", login=False),
        call(app, "POST", "/api/shop/reset", body={}),
    ):
        h = resp.all_headers()
        assert h["Cache-Control"] == "no-store" and "Cookie" in resp.headers.get("Vary", "")
        assert "Content-Security-Policy" in h and h["X-Content-Type-Options"] == "nosniff"


def test_errors_have_one_shape_and_no_internals(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    cases = [
        (call(app, "GET", "/api/shop/session", login=False), 401, "login_required"),
        (call(app, "GET", "/api/shop/nothing"), 404, "not_found"),
        (call(app, "GET", "/api/shop/auth/session", {"Host": "evil.example"}), 403, "forbidden"),
        (call(app, "POST", "/api/shop/search", body={"request": ""}), 400, "bad_request"),
        (call(app, "POST", "/api/shop/rate", body={}), 400, "bad_request"),
    ]
    for resp, status, code in cases:
        e = err(resp)
        assert (
            resp.status == status
            and e["code"] == code
            and isinstance(e["message"], str)
            and e["message"]
        )
        assert set(e) == {"code", "message"} and "Traceback" not in e["message"]


def test_server_errors_hide_their_detail(tmp_path: Path) -> None:
    from app.shop.service import ApiError

    app = make_app(tmp_path)
    resp = app._api_error(ApiError(502, "serper said https://google.serper.dev/x key=abc"))
    assert resp.status == 502 and err(resp)["code"] == "server_error"
    assert "serper" not in err(resp)["message"] and "abc" not in err(resp)["message"]
    capped = app._api_error(
        ApiError(429, "Too much", "cap_reached", {"limit": "session", "resets_at": None})
    )
    assert err(capped) == {
        "code": "cap_reached",
        "message": "Too much",
        "limit": "session",
        "resets_at": None,
    }


def test_the_cookie_is_httponly_lax_secure_and_scoped_to_the_prefix(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    resp = call(app, "POST", "/api/shop/auth/login", body={"code": "right"}, login=False)
    cookie = resp.headers["Set-Cookie"]
    assert resp.status == 200 and json.loads(resp.body) == {"ok": True}
    for flag in ("HttpOnly", "SameSite=Lax", "Secure", "Path=/api/shop", "Max-Age=1209600"):
        assert flag in cookie, flag
    plain = make_app(tmp_path, public_url="http://localhost:3000")
    assert (
        "Secure"
        not in call(
            plain, "POST", "/api/shop/auth/login", body={"code": "right"}, login=False
        ).headers["Set-Cookie"]
    )
    wrong = call(app, "POST", "/api/shop/auth/login", body={"code": "nope"}, login=False)
    assert wrong.status == 401 and "Set-Cookie" not in wrong.headers


def test_the_request_size_limit_still_holds(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    big = app.handle(
        "POST",
        "/api/shop/search",
        {**GOOD, "Origin": ORIGIN, "Content-Type": "application/json", "Cookie": "shop_login=ok"},
        b"x" * 9000,
    )
    assert big.status == 400 or big.status == 413
    assert (
        app.handle(
            "POST",
            "/api/shop/search",
            {
                **GOOD,
                "Origin": ORIGIN,
                "Content-Type": "application/json",
                "Cookie": "shop_login=ok",
            },
            b"[1]",
        ).status
        == 400
    )


def test_the_session_and_job_routes_answer_to_a_login(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    ok = call(app, "GET", "/api/shop/session")
    assert ok.status == 200 and json.loads(ok.body)["session_id"] is None
    started = call(app, "POST", "/api/shop/search", body={"request": "oak coffee tables"})
    assert started.status == 202
    job = json.loads(started.body)["job_id"]
    app.service.jobs.wait(job)
    done = call(app, "GET", f"/api/shop/job/{job}")
    assert done.status == 200 and json.loads(done.body)["state"] == "done"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_login_routes_are_open_to_the_unauthenticated(tmp_path: Path, method: str) -> None:
    app = make_app(tmp_path)
    path = "/api/shop/auth/session" if method == "GET" else "/api/shop/auth/logout"
    resp = call(app, method, path, login=False, body={} if method == "POST" else None)
    assert resp.status == 200
    if method == "GET":
        assert json.loads(resp.body) == {"authenticated": False, "contact_email": "me@example.org"}


def test_a_failed_job_hides_provider_detail_in_deployment_mode(tmp_path: Path) -> None:
    from app.shop.service import ApiError

    app = make_app(tmp_path)

    def boom(say: Any) -> dict[str, Any]:
        raise ApiError(502, "serper said HTTP 401 for key abc123")

    job = app.service._start("search", None, boom)
    app.service.jobs.wait(job)
    assert call(app, "GET", f"/api/shop/job/{job}").status == 404  # not this invite's job
    app._job_owner[job] = "i1"
    body = json.loads(call(app, "GET", f"/api/shop/job/{job}").body)
    text = json.dumps(body)
    assert body["state"] == "error" and "abc123" not in text and "serper" not in text
    assert body["error"].startswith("Something went wrong")
