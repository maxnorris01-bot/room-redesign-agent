"""Stage D1, phase 7: docs/api-contract.md lists every route of the real route table, and only those."""

from __future__ import annotations

import re
from pathlib import Path

from app.shop.routes import ROUTES
from app.shop.server import API_PATHS, JOB_PREFIX, POST_PATHS

DOC = (Path(__file__).resolve().parents[1] / "docs" / "api-contract.md").read_text()


def documented() -> set[tuple[str, str]]:
    return {
        (m.group(1), m.group(2))
        for m in re.finditer(r"^### (GET|POST) (/\S*)$", DOC, flags=re.MULTILINE)
    }


def test_every_route_in_the_table_is_in_the_contract_and_vice_versa() -> None:
    table = {(r.method, r.path) for r in ROUTES}
    assert documented() == table


def test_the_server_constants_come_from_the_table() -> None:
    assert POST_PATHS == {
        f"/api{r.path}" for r in ROUTES if r.method == "POST" and not r.deploy_only
    }
    assert (
        "/api/session" in API_PATHS
        and "/api/job/{id}" not in API_PATHS
        and JOB_PREFIX == "/api/job/"
    )
    assert not any("/auth/login" in p for p in API_PATHS)


def test_the_contract_names_the_conventions_a_react_page_needs() -> None:
    for needle in (
        "login_required", "cap_reached", "maintenance", "rate_limited", "resets_at", "Retry-After", "no-store",
        "Vary: Cookie", "shop_login", "/healthz", "poll", "202", "Origin", "no thumbnail route", "usage",
    ):  # fmt: skip
        assert needle in DOC, needle


def test_every_documented_route_shows_a_request_or_response() -> None:
    sections = re.split(r"^### ", DOC, flags=re.MULTILINE)[1:]
    for sec in sections:
        title = sec.splitlines()[0]
        if title.startswith(("GET", "POST")):
            assert "Response" in sec or "response" in sec, title
