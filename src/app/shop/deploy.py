"""Deployment settings for the Shop Mode JSON API (ADR 0013, 0014).

Everything comes from the environment; with `APP_DEPLOY_MODE` unset the service behaves exactly as
it did before: 127.0.0.1, today's routes, today's page, no login. In deployment mode it serves only
`/healthz` and a JSON API under `APP_API_PREFIX`, behind a secret header added by the portfolio
site's rewrite. Names only are documented here and in docs/deploy.md; values are never printed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.shop.thresholds import (
    CAP_GLOBAL_DAY_USD,
    CAP_IP_DAY_USD,
    CAP_SESSION_AGAINS,
    CAP_SESSION_SEARCHES,
    CAP_SESSION_USD,
    MAX_VISITORS_DAY,
    MAX_VISITORS_PER_IP_DAY,
    STARTS_PER_IP_MINUTE,
    VISITOR_RETENTION_DAYS,
)

LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 8765
LOCAL_PREFIX = "/api"
DEFAULT_PREFIX = "/api/shop"
PREFIX_RE = re.compile(r"^/[a-z0-9][a-z0-9/_-]*[a-z0-9]$")

# error codes of the deployment API (docs/api-contract.md)
CODES = {
    "session_required": 401, "forbidden": 403, "not_found": 404, "rate_limited": 429,
    "cap_reached": 429, "maintenance": 503, "bad_request": 400, "server_error": 500,
}  # fmt: skip


def _csv(value: str | None) -> frozenset[str]:
    return frozenset(x.strip() for x in (value or "").split(",") if x.strip())


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def _number(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class DeploySettings:
    deploy: bool = False
    host: str = LOCAL_HOST
    port: int = LOCAL_PORT
    api_prefix: str = LOCAL_PREFIX
    allowed_hosts: frozenset[str] = frozenset()
    allowed_origins: frozenset[str] = frozenset()
    origin_secret: str = field(default="", repr=False)
    allow_no_secret: bool = False
    public_url: str = ""
    contact_email: str = ""
    disabled: bool = False
    database_url: str = field(default="", repr=False)
    cap_session_usd: float = CAP_SESSION_USD
    cap_ip_day_usd: float = CAP_IP_DAY_USD
    cap_global_day_usd: float = CAP_GLOBAL_DAY_USD
    cap_searches: int = CAP_SESSION_SEARCHES
    cap_shop_agains: int = CAP_SESSION_AGAINS
    max_visitors_per_ip_day: int = MAX_VISITORS_PER_IP_DAY
    max_visitors_day: int = MAX_VISITORS_DAY
    starts_per_ip_minute: int = STARTS_PER_IP_MINUTE
    retention_days: int = VISITOR_RETENTION_DAYS

    @property
    def secure_cookie(self) -> bool:
        return self.public_url.lower().startswith("https://")

    @property
    def forwarded_hosts(self) -> frozenset[str]:
        """The portfolio's own host names, taken from the allowed origins."""
        return frozenset(urlsplit(o).netloc.lower() for o in self.allowed_origins if o)

    def problems(self) -> list[str]:
        """What stops deployment mode from starting (empty when it may)."""
        if not self.deploy:
            return []
        out: list[str] = []
        if not self.origin_secret and not self.allow_no_secret:
            out.append(
                "APP_ORIGIN_SECRET is not set; deployment mode refuses to start without it "
                "(APP_ALLOW_NO_SECRET=1 is for local testing only, never in production)"
            )
        if not PREFIX_RE.match(self.api_prefix):
            out.append("APP_API_PREFIX must look like /api/shop (no trailing slash)")
        if not self.allowed_hosts:
            out.append("APP_ALLOWED_HOSTS must list the host name the service answers to")
        if not self.allowed_origins:
            out.append("APP_ALLOWED_ORIGINS must list the portfolio origin (https://...)")
        if not self.database_url:
            out.append("DATABASE_URL is not set")
        return out


def from_env(environ: Mapping[str, str] | None = None) -> DeploySettings:
    """Settings from `environ` (default `os.environ`). Locally everything stays at today's values
    and the prefix is always `/api`."""
    env = os.environ if environ is None else environ
    deploy = env.get("APP_DEPLOY_MODE", "").strip() == "1"
    host = env.get("APP_HOST", "").strip() or ("0.0.0.0" if deploy else LOCAL_HOST)  # noqa: S104
    try:
        port = int(env.get("APP_PORT", "").strip() or LOCAL_PORT)
    except ValueError:
        port = LOCAL_PORT
    prefix = (env.get("APP_API_PREFIX", "").strip() or DEFAULT_PREFIX) if deploy else LOCAL_PREFIX
    return DeploySettings(
        deploy=deploy,
        host=host if deploy else LOCAL_HOST,
        port=port,
        api_prefix=prefix.rstrip("/") if deploy else LOCAL_PREFIX,
        allowed_hosts=frozenset(h.lower() for h in _csv(env.get("APP_ALLOWED_HOSTS"))),
        allowed_origins=frozenset(
            o.rstrip("/").lower() for o in _csv(env.get("APP_ALLOWED_ORIGINS"))
        ),
        origin_secret=env.get("APP_ORIGIN_SECRET", ""),
        allow_no_secret=_flag(env.get("APP_ALLOW_NO_SECRET")),
        public_url=env.get("APP_PUBLIC_URL", "").strip(),
        contact_email=env.get("APP_CONTACT_EMAIL", "").strip(),
        disabled=_flag(env.get("APP_SHOP_DISABLED")),
        database_url=env.get("DATABASE_URL", ""),
        cap_session_usd=_number(env, "APP_CAP_SESSION_USD", CAP_SESSION_USD),
        cap_ip_day_usd=_number(env, "APP_CAP_IP_DAY_USD", CAP_IP_DAY_USD),
        cap_global_day_usd=_number(env, "APP_CAP_GLOBAL_DAY_USD", CAP_GLOBAL_DAY_USD),
        cap_searches=int(_number(env, "APP_CAP_SEARCHES", CAP_SESSION_SEARCHES)),
        cap_shop_agains=int(_number(env, "APP_CAP_SHOP_AGAINS", CAP_SESSION_AGAINS)),
        max_visitors_per_ip_day=int(
            _number(env, "APP_MAX_VISITORS_PER_IP_DAY", MAX_VISITORS_PER_IP_DAY)
        ),
        max_visitors_day=int(_number(env, "APP_MAX_VISITORS_DAY", MAX_VISITORS_DAY)),
        starts_per_ip_minute=int(_number(env, "APP_STARTS_PER_IP_MINUTE", STARTS_PER_IP_MINUTE)),
        retention_days=int(_number(env, "APP_VISITOR_RETENTION_DAYS", VISITOR_RETENTION_DAYS)),
    )
