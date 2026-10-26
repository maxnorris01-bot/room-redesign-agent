"""The Shop Mode web server: standard library only, bound to 127.0.0.1.

`ShopApp.handle` is pure (method, path, headers, body in; status, headers, body out) so it is
tested without sockets; `make_server` wraps it in `ThreadingHTTPServer`.

Routes: GET /, GET /static/<file>, GET /api/session, GET /api/job/<id>, POST /api/search and
/api/shop-again (each starts a background job and answers 202 with its id), /api/rate, /api/resolve.

Defences (the server only listens locally, but any web page in the visitor's browser could still
try to reach it): the Host header must name this server (DNS rebinding), a present Origin must be
this server's, POSTs must be application/json (a cross-site form cannot send that without a CORS
preflight, which is never answered), bodies are capped, and every response carries a strict CSP.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.shop.service import UNSET, ApiError, ShopService

HOST = "127.0.0.1"
MAX_BODY_BYTES = 8 * 1024
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
API_PATHS = {
    "/api/session", "/api/search", "/api/rate", "/api/resolve", "/api/shop-again",
    "/api/preference/remove", "/api/preference/pin", "/api/reset", "/api/context/remove",
    "/api/refine", "/api/refine/remove", "/api/photos",
}  # fmt: skip
POST_PATHS = API_PATHS - {"/api/session"}
JOB_PREFIX = "/api/job/"  # GET /api/job/<id>?session=<session id>
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


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)

    def all_headers(self) -> dict[str, str]:
        return {"Content-Type": self.content_type, **SECURITY_HEADERS, **self.headers}


def json_response(status: int, payload: dict[str, Any]) -> Response:
    return Response(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def error_response(status: int, message: str) -> Response:
    return json_response(status, {"error": message})


class ShopApp:
    def __init__(self, service: ShopService, port: int = 0) -> None:
        self.service = service
        self.allowed_hosts: set[str] = set()
        self.set_port(port)

    def set_port(self, port: int) -> None:
        self.allowed_hosts = {f"{HOST}:{port}", f"localhost:{port}"}

    def _origin_ok(self, headers: Mapping[str, str]) -> bool:
        origin = headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in self.allowed_hosts}

    def handle(
        self, method: str, target: str, headers: Mapping[str, str], body: bytes = b""
    ) -> Response:
        headers = {k.title(): v for k, v in headers.items()}
        if headers.get("Host", "") not in self.allowed_hosts:
            return error_response(403, "unexpected host")
        if not self._origin_ok(headers):
            return error_response(403, "unexpected origin")
        parts = urlsplit(target)
        path = parts.path
        if method == "GET":
            if path in STATIC_FILES:
                name, ctype = STATIC_FILES[path]
                return Response(200, (STATIC_DIR / name).read_bytes(), ctype)
            if path == "/api/session":
                ids = parse_qs(parts.query).get("id", [])
                return json_response(200, self.service.session_view(ids[0] if ids else None))
            if path.startswith(JOB_PREFIX):
                owner = parse_qs(parts.query).get("session", [""])[0]
                try:
                    status = self.service.job_status(path[len(JOB_PREFIX) :], owner)
                except ApiError as exc:
                    return error_response(exc.status, exc.message)
                return json_response(200, status)
            if path in POST_PATHS:
                return Response(405, b'{"error": "method not allowed"}', headers={"Allow": "POST"})
            return error_response(404, "not found")
        if method == "POST" and path in POST_PATHS:
            return self._post(path, headers, body)
        if path in STATIC_FILES or path in API_PATHS:
            return Response(405, b'{"error": "method not allowed"}', headers={"Allow": "GET, POST"})
        return error_response(404, "not found")

    def _post(self, path: str, headers: Mapping[str, str], body: bytes) -> Response:
        content_type = headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return error_response(415, "send application/json")
        if len(body) > MAX_BODY_BYTES:
            return error_response(413, "request body too large")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return error_response(400, "body is not valid JSON")
        if not isinstance(data, dict):
            return error_response(400, "body must be a JSON object")
        try:
            if path == "/api/search":
                result = self.service.start_search(data.get("session_id"), data.get("request"))
            elif path == "/api/rate":
                result = self.service.rate(
                    data.get("session_id"),
                    data.get("result_id"),
                    data["rating"] if "rating" in data else UNSET,
                    data["comment"] if "comment" in data else UNSET,
                )
            elif path == "/api/shop-again":
                result = self.service.start_shop_again(
                    data.get("session_id"), data.get("refine_text")
                )
            elif path == "/api/photos":
                result = self.service.photos(data.get("session_id"), data.get("result_id"))
            elif path == "/api/refine":
                result = self.service.refine(data.get("session_id"), data.get("text"))
            elif path == "/api/refine/remove":
                result = self.service.remove_refine(data.get("session_id"), data.get("id"))
            elif path == "/api/preference/remove":
                result = self.service.remove_preference(
                    data.get("session_id"), data.get("kind"), data.get("phrase")
                )
            elif path == "/api/context/remove":
                result = self.service.remove_context(
                    data.get("session_id"), data.get("kind"), data.get("value")
                )
            elif path == "/api/reset":
                result = self.service.reset_session(data.get("session_id"))
            elif path == "/api/preference/pin":
                result = self.service.pin_preference(data.get("session_id"), data.get("phrase"))
            else:
                result = self.service.resolve(data.get("session_id"), data.get("result_id"))
        except ApiError as exc:
            return error_response(exc.status, exc.message)
        if path in ("/api/search", "/api/shop-again"):
            return json_response(202, result)  # a job was started: poll /api/job/<id>
        return json_response(200, result)


def make_handler(app: ShopApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ShopMode"
        sys_version = ""

        def _send(self, response: Response) -> None:
            self.send_response(response.status)
            for key, value in response.all_headers().items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response.body)

        def _body(self) -> bytes | Response:
            raw = self.headers.get("Content-Length")
            if raw is None or not raw.isdigit():
                self.close_connection = True
                return error_response(411, "Content-Length required")
            length = int(raw)
            if length > MAX_BODY_BYTES:
                self.close_connection = True  # the body is never read
                return error_response(413, "request body too large")
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
            self._send(app.handle("PUT", self.path, dict(self.headers.items())))

        do_DELETE = do_PATCH = do_PUT

        def log_message(self, format: str, *args: Any) -> None:
            pass  # no request logging: paths carry session ids

    return Handler


def make_server(app: ShopApp, port: int = 8765) -> ThreadingHTTPServer:
    """A server on 127.0.0.1 (`port=0` picks a free one). The app learns the bound port."""
    server = ThreadingHTTPServer((HOST, port), make_handler(app))
    server.daemon_threads = True
    app.set_port(server.server_address[1])
    return server
