"""Run the deployment-mode server locally and exercise it over real HTTP (mock providers, a
temporary SQLite database, no network beyond 127.0.0.1, no docker).

    make deploy-smoke

It prints what it sees: /healthz, a 401 without a login, the login, an authenticated call, a
search through the job model, a cap_reached answer and the maintenance answer. Exit status 1 when
something is not as expected.
"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.config import Config
from app.shop.auth import SqlAuth
from app.shop.caps import CapGuard, Meter
from app.shop.dbstore import SqlSessionStore, migrate, open_database
from app.shop.deploy import DeploySettings
from app.shop.providers import ShopFixtureLookup, build_shop_providers
from app.shop.server import ShopApp, make_server
from app.shop.service import ShopService

SECRET = "smoke-origin-secret"
ORIGIN = "https://portfolio.example"
HOST = "shop.test"
FAILS: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        FAILS.append(what)


class Client:
    def __init__(self, port: int, secret: bool = True) -> None:
        self.port = port
        self.cookie = ""
        self.secret = secret

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, dict[str, str], Any]:
        headers = {
            "Host": HOST,
            "Content-Type": "application/json",
            "Origin": ORIGIN,
            "X-Forwarded-For": "203.0.113.7",
        }
        if self.secret:
            headers["X-Origin-Secret"] = SECRET
        if self.cookie:
            headers["Cookie"] = self.cookie
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        raw = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=raw, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        out = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        try:
            return resp.status, out, json.loads(data) if data else None
        except ValueError:
            return resp.status, out, data


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="shop-smoke-"))
    db = open_database(f"sqlite:///{tmp / 'smoke.db'}")
    migrate(db)
    settings = DeploySettings(
        deploy=True, api_prefix="/api/shop", allowed_hosts=frozenset({HOST}),
        allowed_origins=frozenset({ORIGIN}), origin_secret=SECRET, public_url="http://127.0.0.1",
        contact_email="owner@example.org", database_url="sqlite:///smoke", cap_searches=1,
    )  # fmt: skip
    store = SqlSessionStore(db)
    store.after_save = Meter()
    cfg = Config(tracing_disabled=True)
    search, _ = build_shop_providers(cfg)
    service = ShopService(cfg, store, search, ShopFixtureLookup())
    service.guard = CapGuard(db, settings)
    auth = SqlAuth(db, settings)
    app = ShopApp(service, 0, settings, auth, store)
    server = make_server(app, 0, "127.0.0.1")
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api = "/api/shop"
    try:
        c = Client(port)
        status, _, body = c.call("GET", "/healthz")
        check(status == 200 and body == {"ok": True}, f"/healthz answers {body}")
        status, _, body = Client(port, secret=False).call("GET", f"{api}/session")
        check(status == 403 and body == {}, "without the origin secret: 403 and an empty body")
        status, _, body = c.call("GET", "/")
        check(
            status == 404 and body["error"]["code"] == "not_found",
            "the root serves nothing: 404 JSON",
        )
        status, _, body = c.call("GET", f"{api}/session")
        check(
            status == 401 and body["error"]["code"] == "login_required",
            f"unauthenticated: {status} {body['error']['code']}",
        )
        status, _, body = c.call("GET", f"{api}/auth/session")
        check(
            status == 200
            and body == {"authenticated": False, "contact_email": "owner@example.org"},
            f"auth/session before login: {body}",
        )
        status, _, body = c.call("POST", f"{api}/auth/login", {"code": "wrong"})
        check(status == 401, "a wrong code: 401")
        _, code = auth.create_invite("Smoke test")
        status, headers, body = c.call("POST", f"{api}/auth/login", {"code": code})
        check(
            status == 200 and body == {"ok": True} and "httponly" in headers["set-cookie"].lower(),
            "login sets an HttpOnly cookie",
        )
        c.cookie = headers["set-cookie"].split(";")[0]
        status, _, body = c.call("GET", f"{api}/auth/session")
        check(
            body["authenticated"] and body["label"] == "Smoke test" and "usage" in body,
            f"auth/session after login: label {body.get('label')}, usage keys {sorted(body.get('usage', {}))}",
        )
        status, headers, body = c.call("GET", f"{api}/session")
        check(
            status == 200
            and headers["cache-control"] == "no-store"
            and "Cookie" in headers["vary"],
            "an authenticated call: 200, no-store, Vary: Cookie",
        )
        status, _, body = c.call("POST", f"{api}/search", {"request": "oak coffee tables"})
        job = body["job_id"]
        sid = ""
        for _ in range(600):
            status, _, state = c.call("GET", f"{api}/job/{job}")
            if state["state"] != "running":
                sid = state["result"]["session_id"]
                break
            service.jobs.wait(job, 0.2)
        check(
            status == 200 and bool(sid),
            f"a search ran as a job and finished (session {sid[:4]}...)",
        )
        status, _, body = c.call(
            "POST", f"{api}/search", {"session_id": sid, "request": "more tables"}
        )
        e = body["error"]
        check(
            status == 429 and e["code"] == "cap_reached" and e["limit"] == "searches",
            f"cap_reached: {e['limit']}, resets_at {e['resets_at']}",
        )
        print(f"       message: {e['message']}")
        status, _, body = c.call("GET", f"{api}/session?id={sid}")
        check(
            status == 200 and body["search"]["results"],
            "shown results are still readable after the cap",
        )
        app.settings = replace(settings, disabled=True)
        status, _, body = c.call("GET", f"{api}/session")
        check(
            status == 503 and body["error"]["code"] == "maintenance",
            f"kill switch: {status} {body['error']['code']}",
        )
        status, _, body = c.call("GET", f"{api}/auth/session")
        check(body.get("maintenance") is True, "auth/session still answers, with maintenance: true")
        status, _, body = c.call("GET", "/healthz")
        check(status == 200, "/healthz still answers under the kill switch")
    finally:
        server.shutdown()
        server.server_close()
    print("all checks passed" if not FAILS else f"{len(FAILS)} check(s) failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
