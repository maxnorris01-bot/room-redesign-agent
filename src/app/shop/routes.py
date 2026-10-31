"""The route table of the Shop Mode JSON API: the server, the login check and
docs/api-contract.md are all held to it by tests.

Paths are relative to the API prefix (`/api` locally, `APP_API_PREFIX` in deployment mode).
`auth` is "none" (open: the page asks `/auth/session` whether it has a visit, and calls
`/auth/start` when it has not) or "login" (needs the visitor cookie).
`body` says whether the route takes a JSON body (and so needs Content-Type application/json).
`{id}` is the job id.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    auth: str  # "none" | "login"
    body: bool
    name: str
    deploy_only: bool = False  # absent locally (login routes)


ROUTES: tuple[Route, ...] = (
    Route("GET", "/session", "login", False, "session"),
    Route("GET", "/job/{id}", "login", False, "job"),
    Route("POST", "/search", "login", True, "search"),
    Route("POST", "/shop-again", "login", True, "shop_again"),
    Route("POST", "/rate", "login", True, "rate"),
    Route("POST", "/resolve", "login", True, "resolve"),
    Route("POST", "/photos", "login", True, "photos"),
    Route("POST", "/refine", "login", True, "refine"),
    Route("POST", "/refine/remove", "login", True, "refine_remove"),
    Route("POST", "/preference/remove", "login", True, "preference_remove"),
    Route("POST", "/preference/pin", "login", True, "preference_pin"),
    Route("POST", "/context/remove", "login", True, "context_remove"),
    Route("POST", "/reset", "login", True, "reset"),
    Route("POST", "/auth/start", "none", False, "auth_start", True),
    Route("POST", "/auth/logout", "none", True, "auth_logout", True),
    Route("GET", "/auth/session", "none", False, "auth_session"),
)
JOB_PREFIX = "/job/"
STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}


def match(method: str, rel_path: str, deploy: bool = True) -> tuple[Route | None, bool, str]:
    """(the route, whether another method would match this path, the job id if any)."""
    other = False
    job_id = ""
    for route in ROUTES:
        if route.deploy_only and not deploy:
            continue
        if route.path == "/job/{id}":
            if rel_path.startswith(JOB_PREFIX) and len(rel_path) > len(JOB_PREFIX):
                if "/" not in rel_path[len(JOB_PREFIX) :]:
                    if route.method == method:
                        return route, False, rel_path[len(JOB_PREFIX) :]
            continue
        if route.path == rel_path:
            if route.method == method:
                return route, False, job_id
            other = True
    return None, other, job_id
