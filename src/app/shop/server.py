"""The Shop Mode web server: standard library only.

`ShopApp.handle` is pure (method, path, headers, body in; status, headers, body out) so it is
tested without sockets; `make_server` wraps it in `ThreadingHTTPServer`.

Two modes (ADR 0013):

* Local (the default, no deployment settings): bound to 127.0.0.1, routes `GET /`,
  `GET /static/<file>`, `GET /api/session`, `GET /api/job/<id>`, `POST /api/search` and
  `/api/shop-again` (each starts a background job and answers 202 with its id), `/api/rate`,
  `/api/resolve` and the rest of `app.shop.routes.ROUTES`, no login. Defences (the server only
  listens locally, but any web page in the visitor's browser could still try to reach it): the
  Host header must name this server (DNS rebinding), a present Origin must be this server's,
  POSTs must be application/json (a cross-site form cannot send that without a CORS preflight,
  which is never answered), bodies are capped, and every response carries a strict CSP.
* Deployment (`APP_DEPLOY_MODE=1`): API only. `GET /healthz` and the JSON API under
  `APP_API_PREFIX`; no HTML, no static files. A request must carry the origin secret the portfolio
  site's rewrite adds, the Host and (for writes) the Origin must be the configured ones, there are
  no CORS headers, every API route but the login routes needs a login cookie, a session belongs to
  the invite that made it, and every response is `no-store`.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from app.shop.context import current_invite
from app.shop.deploy import CODES, LOCAL_HOST, DeploySettings
from app.shop.redact import log_failure
from app.shop.routes import ROUTES, STATE_CHANGING, Route, match
from app.shop.service import UNSET, ApiError, ShopService

HOST = LOCAL_HOST
MAX_BODY_BYTES = 8 * 1024
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
LOCAL_PREFIX = "/api"
API_PATHS = {
    f"{LOCAL_PREFIX}{r.path}" for r in ROUTES if not r.deploy_only and r.path != "/job/{id}"
}
POST_PATHS = {f"{LOCAL_PREFIX}{r.path}" for r in ROUTES if r.method == "POST" and not r.deploy_only}
JOB_PREFIX = "/api/job/"  # GET /api/job/<id>?session=<session id>
COOKIE_NAME = "shop_login"
COOKIE_MAX_AGE_S = 14 * 24 * 3600
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src https:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
}
MAINTENANCE_MESSAGE = "Shop Mode is paused for maintenance. Please try again later."


@dataclass(frozen=True)
class Identity:
    """Who is calling (deployment mode): the invite, its label and the login session."""

    invite_id: str
    label: str
    login_id: str = ""


class AuthBackend(Protocol):
    def login(self, code: str, ip: str) -> tuple[str | None, int]:
        """(a new login cookie value, 0) or (None, seconds to wait; 0 for a plain refusal)."""
        ...

    def logout(self, cookie: str | None) -> None: ...

    def identify(self, cookie: str | None) -> Identity | None: ...

    def usage(self, identity: Identity) -> dict[str, Any]: ...


class SessionAccess(Protocol):
    def owner_of(self, session_id: str) -> str | None: ...

    def claim(self, session_id: str, invite_id: str) -> None: ...


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)

    def all_headers(self) -> dict[str, str]:
        return {"Content-Type": self.content_type, **SECURITY_HEADERS, **self.headers}


def json_response(
    status: int, payload: dict[str, Any], headers: dict[str, str] | None = None
) -> Response:
    return Response(
        status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers or {}
    )


def error_response(status: int, message: str) -> Response:
    return json_response(status, {"error": message})


def default_code(status: int) -> str:
    if status == 401:
        return "login_required"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status == 503:
        return "maintenance"
    if status >= 500:
        return "server_error"
    return "bad_request"


class ShopApp:
    def __init__(
        self,
        service: ShopService,
        port: int = 0,
        settings: DeploySettings | None = None,
        auth: AuthBackend | None = None,
        access: SessionAccess | None = None,
    ) -> None:
        self.service = service
        self.settings = settings or DeploySettings()
        self.auth = auth
        self.access = access
        self.allowed_hosts: set[str] = set()
        self._job_owner: dict[str, str] = {}
        self.set_port(port)

    # ---- local checks ---------------------------------------------------------------------

    def set_port(self, port: int) -> None:
        self.allowed_hosts = {f"{HOST}:{port}", f"localhost:{port}"}

    def _origin_ok(self, headers: Mapping[str, str]) -> bool:
        origin = headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in self.allowed_hosts}

    # ---- errors ---------------------------------------------------------------------------

    def _error(
        self,
        status: int,
        message: str,
        code: str | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        if not self.settings.deploy:
            return json_response(status, {"error": message}, headers)
        code = code or default_code(status)
        if status >= 500 and code != "maintenance":
            message = "Something went wrong on our side. Please try again."
        body = {"error": {"code": code, "message": message, **(extra or {})}}
        return json_response(status, body, headers)

    def _api_error(self, exc: ApiError) -> Response:
        return self._error(exc.status, exc.message, exc.code, exc.extra)

    # ---- entry ----------------------------------------------------------------------------

    def handle(
        self, method: str, target: str, headers: Mapping[str, str], body: bytes = b""
    ) -> Response:
        headers = {k.title(): v for k, v in headers.items()}
        if self.settings.deploy:
            resp = self._handle_deploy(method, target, headers, body)
            resp.headers.setdefault("Vary", "Cookie")  # nothing here may be shared between visitors
            return resp
        if headers.get("Host", "") not in self.allowed_hosts:
            return error_response(403, "unexpected host")
        if not self._origin_ok(headers):
            return error_response(403, "unexpected origin")
        parts = urlsplit(target)
        path = parts.path
        if method == "GET" and path in STATIC_FILES:
            name, ctype = STATIC_FILES[path]
            return Response(200, (STATIC_DIR / name).read_bytes(), ctype)
        if path.startswith(LOCAL_PREFIX + "/"):
            return self._api(method, path[len(LOCAL_PREFIX) :], parts.query, headers, body, None)
        if path in STATIC_FILES:
            return Response(405, b'{"error": "method not allowed"}', headers={"Allow": "GET, POST"})
        return error_response(404, "not found")

    # ---- deployment mode ------------------------------------------------------------------

    def _trusted(self, headers: Mapping[str, str]) -> bool:
        """Whether the origin secret was presented (or none is configured, for local testing)."""
        secret = self.settings.origin_secret
        if not secret:
            return False  # nothing proves the caller: forwarded headers are never trusted
        given = headers.get("X-Origin-Secret", "")
        return hmac.compare_digest(given.encode("utf-8"), secret.encode("utf-8"))

    def client_ip(self, headers: Mapping[str, str]) -> str:
        """The visitor's address from the trusted x-forwarded-for (first address), else
        "unknown": an untrusted request cannot choose its own rate-limit bucket."""
        if not self._trusted(headers):
            return "unknown"
        forwarded = headers.get("X-Forwarded-For", "").split(",")[0].strip()
        return forwarded or headers.get("X-Real-Ip", "").strip() or "unknown"

    def _host_ok(self, headers: Mapping[str, str]) -> bool:
        s = self.settings
        host = headers.get("Host", "").lower()
        if host in s.allowed_hosts:
            return True
        forwarded = headers.get("X-Forwarded-Host", "").split(",")[0].strip().lower()
        return bool(forwarded) and forwarded in s.forwarded_hosts

    def _origin_allowed(self, headers: Mapping[str, str]) -> bool:
        origin = headers.get("Origin")
        if origin is None:
            referer = headers.get("Referer", "")
            parts = urlsplit(referer)
            origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
        return origin.rstrip("/").lower() in self.settings.allowed_origins

    def _handle_deploy(
        self, method: str, target: str, headers: Mapping[str, str], body: bytes
    ) -> Response:
        parts = urlsplit(target)
        path = parts.path
        if path == "/healthz":  # no login, no secret, no database, no provider
            if method == "GET":
                return json_response(200, {"ok": True}, {"Vary": "Cookie"})
            return self._error(405, "Method not allowed.", "bad_request", None, {"Allow": "GET"})
        if self.settings.origin_secret and not self._trusted(headers):
            return Response(403, b"{}")  # nothing else: no detail for a caller without the secret
        if not self._host_ok(headers):
            return self._error(403, "This host is not allowed.", "forbidden")
        prefix = self.settings.api_prefix
        if not path.startswith(prefix + "/"):
            return self._error(404, "Not found.", "not_found")
        return self._api(method, path[len(prefix) :], parts.query, headers, body, self.settings)

    # ---- the API, both modes --------------------------------------------------------------

    def _api(
        self,
        method: str,
        rel: str,
        query: str,
        headers: Mapping[str, str],
        body: bytes,
        deploy: DeploySettings | None,
    ) -> Response:
        route, other_method, job_id = match(method, rel, deploy is not None)
        if route is None:
            if other_method:
                allow = ", ".join(sorted({r.method for r in ROUTES if r.path == rel}))
                msg = "method not allowed"
                if deploy is None:
                    return Response(
                        405, b'{"error": "method not allowed"}', headers={"Allow": allow}
                    )
                return self._error(405, msg, "bad_request", None, {"Allow": allow})
            return self._error(404, "not found" if deploy is None else "Not found.", "not_found")
        extra_headers: dict[str, str] = {}
        identity: Identity | None = None
        if deploy is not None:
            if method in STATE_CHANGING and not self._origin_allowed(headers):
                return self._error(403, "This origin is not allowed.", "forbidden")
            if route.body:
                ctype = headers.get("Content-Type", "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    return self._error(400, "Send application/json.", "bad_request")
            if deploy.disabled and route.name != "auth_session":
                return self._error(503, MAINTENANCE_MESSAGE, "maintenance")
            if route.auth == "login":
                identity = self._identify(headers)
                if identity is None:
                    return self._error(
                        401, "Please log in with your invite code.", "login_required"
                    )
        data: dict[str, Any] = {}
        if route.body and deploy is None:
            ctype = headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return error_response(415, "send application/json")
        if route.body:
            checked = self._json_body(body, deploy is not None)
            if isinstance(checked, Response):
                return checked
            data = checked
        q = parse_qs(query)
        try:
            if route.name == "auth_session":
                return self._auth_session(headers, deploy)
            if route.name == "auth_login":
                return self._auth_login(data, headers)
            if route.name == "auth_logout":
                return self._auth_logout(headers)
            if identity is not None:
                denied = self._ownership(route, data, q, job_id, identity)
                if denied is not None:
                    return denied
            token = current_invite.set(identity.invite_id if identity else None)
            try:
                result = self._call(route, data, q, job_id, identity)
            finally:
                current_invite.reset(token)
        except ApiError as exc:
            return self._api_error(exc)
        except Exception as exc:
            if deploy is None:
                raise
            log_failure(route.name, exc)  # one redacted line; the client gets the generic sentence
            return self._error(500, "unexpected error")
        if deploy is not None and route.name == "job" and result.get("state") == "error":
            if int(result.get("status", 500)) >= 500:  # no provider detail leaves the service
                result = {**result, "error": "Something went wrong on our side. Please try again."}
        status = 202 if route.name in ("search", "shop_again") else 200
        if identity is not None and route.name in ("search", "shop_again") and "job_id" in result:
            self._remember_job(result["job_id"], identity.invite_id)
        if identity is not None and route.name == "job":
            self._claim_new_session(result, identity)
        if deploy is not None:
            extra_headers["Vary"] = "Cookie"
        return json_response(status, result, extra_headers)

    def _json_body(self, body: bytes, deploy: bool) -> dict[str, Any] | Response:
        if len(body) > MAX_BODY_BYTES:
            return self._error(
                413,
                "request body too large" if not deploy else "The request is too large.",
                "bad_request",
            )
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._error(
                400,
                "body is not valid JSON" if not deploy else "The body is not valid JSON.",
                "bad_request",
            )
        if not isinstance(data, dict):
            return self._error(
                400,
                "body must be a JSON object" if not deploy else "The body must be a JSON object.",
                "bad_request",
            )
        return data

    def _call(
        self,
        route: Route,
        data: dict[str, Any],
        q: dict[str, list[str]],
        job_id: str,
        identity: Identity | None,
    ) -> dict[str, Any]:
        s = self.service
        name = route.name
        if name == "session":
            ids = q.get("id", [])
            return s.session_view(ids[0] if ids else None)
        if name == "job":
            return s.job_status(job_id, q.get("session", [""])[0])
        if name == "search":
            return s.start_search(data.get("session_id"), data.get("request"))
        if name == "rate":
            return s.rate(
                data.get("session_id"),
                data.get("result_id"),
                data["rating"] if "rating" in data else UNSET,
                data["comment"] if "comment" in data else UNSET,
            )
        if name == "shop_again":
            return s.start_shop_again(data.get("session_id"), data.get("refine_text"))
        if name == "photos":
            return s.photos(data.get("session_id"), data.get("result_id"))
        if name == "refine":
            return s.refine(data.get("session_id"), data.get("text"))
        if name == "refine_remove":
            return s.remove_refine(data.get("session_id"), data.get("id"))
        if name == "preference_remove":
            return s.remove_preference(data.get("session_id"), data.get("kind"), data.get("phrase"))
        if name == "context_remove":
            return s.remove_context(data.get("session_id"), data.get("kind"), data.get("value"))
        if name == "reset":
            return s.reset_session(data.get("session_id"))
        if name == "preference_pin":
            return s.pin_preference(data.get("session_id"), data.get("phrase"))
        return s.resolve(data.get("session_id"), data.get("result_id"))

    # ---- sessions belong to the invite that made them -------------------------------------

    def _ownership(
        self,
        route: Route,
        data: dict[str, Any],
        q: dict[str, list[str]],
        job_id: str,
        identity: Identity,
    ) -> Response | None:
        gone = self._error(404, "That was not found.", "not_found")
        if route.name == "job":
            return None if self._job_owner.get(job_id) == identity.invite_id else gone
        sid = q.get("id", [""])[0] if route.name == "session" else data.get("session_id")
        if not isinstance(sid, str) or not sid or self.access is None:
            return None
        return None if self.access.owner_of(sid) == identity.invite_id else gone

    def _remember_job(self, job_id: str, invite_id: str) -> None:
        if len(self._job_owner) > 2000:  # jobs are forgotten by the manager after ten minutes
            for key in list(self._job_owner)[:1000]:
                del self._job_owner[key]
        self._job_owner[job_id] = invite_id

    def _claim_new_session(self, status: dict[str, Any], identity: Identity) -> None:
        result = status.get("result")
        sid = result.get("session_id") if isinstance(result, dict) else None
        if isinstance(sid, str) and self.access is not None and self.access.owner_of(sid) is None:
            self.access.claim(sid, identity.invite_id)

    # ---- login ----------------------------------------------------------------------------

    def _cookie(self, headers: Mapping[str, str]) -> str | None:
        raw = headers.get("Cookie", "")
        if not raw:
            return None
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:  # a malformed Cookie header is no cookie
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _identify(self, headers: Mapping[str, str]) -> Identity | None:
        if self.auth is None:
            return None
        return self.auth.identify(self._cookie(headers))

    def set_cookie(self, value: str, max_age: int) -> str:
        s = self.settings
        parts = [
            f"{COOKIE_NAME}={value}",
            f"Max-Age={max_age}",
            f"Path={s.api_prefix}",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if s.secure_cookie:
            parts.append("Secure")
        return "; ".join(parts)

    def _auth_session(self, headers: Mapping[str, str], deploy: DeploySettings | None) -> Response:
        if deploy is None:
            return json_response(200, {"authenticated": True, "local": True})
        extra: dict[str, Any] = {"maintenance": True} if deploy.disabled else {}
        contact = deploy.contact_email or None
        identity = self._identify(headers)
        if identity is None or self.auth is None:
            return json_response(
                200, {"authenticated": False, "contact_email": contact, **extra}, {"Vary": "Cookie"}
            )
        return json_response(
            200,
            {
                "authenticated": True, "label": identity.label, "contact_email": contact,
                "usage": self.auth.usage(identity), **extra,
            },
            {"Vary": "Cookie"},
        )  # fmt: skip

    def _auth_login(self, data: dict[str, Any], headers: Mapping[str, str]) -> Response:
        code = data.get("code")
        if self.auth is None or not isinstance(code, str) or not code or len(code) > 200:
            return self._error(400, "Enter your invite code.", "bad_request")
        cookie, wait = self.auth.login(code, self.client_ip(headers))
        if cookie is None and wait > 0:
            return self._error(
                429, "Too many attempts. Please wait a little and try again.", "rate_limited",
                None, {"Retry-After": str(wait), "Vary": "Cookie"},
            )  # fmt: skip
        if cookie is None:
            return self._error(
                401, "That invite code did not work.", "login_required", None, {"Vary": "Cookie"}
            )
        return json_response(
            200,
            {"ok": True},
            {"Set-Cookie": self.set_cookie(cookie, COOKIE_MAX_AGE_S), "Vary": "Cookie"},
        )

    def _auth_logout(self, headers: Mapping[str, str]) -> Response:
        if self.auth is not None:
            self.auth.logout(self._cookie(headers))
        return json_response(
            200, {"ok": True}, {"Set-Cookie": self.set_cookie("", 0), "Vary": "Cookie"}
        )


def make_handler(app: ShopApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ShopMode"
        sys_version = ""

        def _send(self, response: Response) -> None:
            self.send_response(response.status)
            headers = response.all_headers()
            if app.settings.deploy:  # API responses: never cached, vary by login (ADR 0013)
                headers.setdefault("Vary", "Cookie")
                headers.update(
                    {
                        "Cache-Control": "no-store",
                        **{k: v for k, v in response.headers.items() if k == "Cache-Control"},
                    }
                )
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response.body)

        def _body(self) -> bytes | Response:
            raw = self.headers.get("Content-Length")
            if raw is None or not raw.isdigit():
                self.close_connection = True
                return app._error(411, "Content-Length required", "bad_request")
            length = int(raw)
            if length > MAX_BODY_BYTES:
                self.close_connection = True  # the body is never read
                return app._error(413, "request body too large", "bad_request")
            return self.rfile.read(length)

        def do_GET(self) -> None:
            self._send(app.handle("GET", self.path, dict(self.headers.items())))

        def do_POST(self) -> None:
            body = self._body()
            if isinstance(body, Response):
                self._send(body)
                return
            self._send(app.handle("POST", self.path, dict(self.headers.items()), body))

        def do_PUT(self) -> None:
            self._send(app.handle(self.command, self.path, dict(self.headers.items())))

        do_DELETE = do_PATCH = do_OPTIONS = do_PUT

        def log_message(self, format: str, *args: Any) -> None:
            pass  # no request logging: paths carry session ids

    return Handler


def make_server(app: ShopApp, port: int = 8765, host: str = HOST) -> ThreadingHTTPServer:
    """A server on 127.0.0.1 (`port=0` picks a free one); deployment mode may pass another host.
    The app learns the bound port."""
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    app.set_port(server.server_address[1])
    return server


__all__ = ["CODES", "ShopApp", "make_server"]
