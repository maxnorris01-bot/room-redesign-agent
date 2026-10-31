"""Anonymous visitors and their login sessions for the deployed Shop Mode API (ADR 0014).

There are no accounts and no invite codes. `POST /auth/start` makes a visitor (a random id, no
personal data) and a login session: a random 128-bit value handed to the browser as an HttpOnly
cookie and stored only as its SHA-256, valid for 30 days and extended when used. Protection comes
from limits, not from a secret the visitor holds: the client address (taken only from the trusted
`X-Forwarded-For`) is stored as HMAC-SHA256 under a key derived from `APP_ORIGIN_SECRET`, never raw;
one address may start a few visitors a day, everyone a few hundred, and an address only a handful a
minute, so clearing cookies resets nothing (spend is counted per address, see `caps.py`). Rotating
the origin secret changes the key, which starts every address counter from zero. Nothing here prints
or logs a cookie, a hash or an address.

`bot_check_allowed` is the one place a later bot check (Cloudflare Turnstile) plugs in.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from app.shop.dbstore import Database, Tx
from app.shop.deploy import DeploySettings
from app.shop.redact import log_failure
from app.shop.server import Identity, StartRefused
from app.shop.thresholds import USAGE_RETENTION_DAYS

LOGIN_DAYS = 30
SEEN_EVERY_S = 60  # last_seen is written at most this often
PURGE_EVERY_S = 6 * 3600  # retention runs lazily, at most this often (the machine auto-stops)
MINUTE_S = 60


def bot_check_allowed(headers: Mapping[str, str]) -> bool:
    """Whether this request to `POST /auth/start` may create a visitor.

    The hook for a later bot check: Cloudflare Turnstile (the page would send the widget's token,
    this function would verify it with Cloudflare and return False on a failure). Today it allows
    everything and makes no third-party call (docs/todo.md)."""
    return True


def hash_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def utc_day(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d")


def seconds_to_midnight(moment: datetime) -> int:
    now = moment.astimezone(UTC)
    midnight = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
    return max(1, int((midnight - now).total_seconds()) + 1)


def address_key(origin_secret: str) -> bytes:
    """The HMAC key for client addresses, derived from the origin secret (so it is never stored)."""
    return hashlib.sha256(b"shop-address-v1:" + origin_secret.encode("utf-8")).digest()


def hmac_address(key: bytes, address: str) -> str:
    return hmac.new(key, address.encode("utf-8"), hashlib.sha256).hexdigest()


class StartLimiter:
    """A sliding window of start times per address key (in memory: one machine)."""

    def __init__(
        self, limit: int, window_s: int = MINUTE_S, now: Callable[[], float] = time.time
    ) -> None:
        self.limit, self.window_s, self._now = limit, window_s, now
        self._by_key: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> int:
        """Seconds to wait (0 when the start may go ahead); a permitted start is counted."""
        with self._lock:
            now = self._now()
            cutoff = now - self.window_s
            mine = self._by_key.setdefault(key, deque())
            while mine and mine[0] <= cutoff:
                mine.popleft()
            if len(self._by_key) > 5000:  # forget idle addresses
                for k in [k for k, v in self._by_key.items() if not v and k != key]:
                    del self._by_key[k]
            if len(mine) >= self.limit:
                return max(1, int(mine[0] + self.window_s - now) + 1)
            mine.append(now)
            return 0


def purge(db: Database, now: datetime, retention_days: int) -> dict[str, int]:
    """Delete visitors, login sessions and shop sessions unused for `retention_days`, the shop
    sessions of deleted visitors, their per-session counters, and usage rows older than 40 days.
    Returns how many rows went from each table."""
    cutoff = iso(now - timedelta(days=retention_days))
    usage_cutoff = utc_day(now - timedelta(days=USAGE_RETENTION_DAYS))
    old_visitors = "SELECT id FROM visitors WHERE last_seen < ?"
    sessions_where = f"updated_at < ? OR visitor_id IN ({old_visitors})"
    logins_where = f"last_seen < ? OR expires_at < ? OR visitor_id IN ({old_visitors})"

    def run(tx: Tx) -> dict[str, int]:
        def count(sql: str, params: tuple[Any, ...] = ()) -> int:
            return int(tx.execute(sql, params)[0][0])

        out: dict[str, int] = {}
        args: tuple[Any, ...] = (cutoff, cutoff)
        out["shop_sessions"] = count(
            f"SELECT COUNT(*) FROM shop_sessions WHERE {sessions_where}", args
        )
        tx.execute(f"DELETE FROM shop_sessions WHERE {sessions_where}", args)
        args = (cutoff, iso(now), cutoff)
        out["login_sessions"] = count(
            f"SELECT COUNT(*) FROM login_sessions WHERE {logins_where}", args
        )
        tx.execute(f"DELETE FROM login_sessions WHERE {logins_where}", args)
        out["visitors"] = count("SELECT COUNT(*) FROM visitors WHERE last_seen < ?", (cutoff,))
        tx.execute("DELETE FROM visitors WHERE last_seen < ?", (cutoff,))
        before = count("SELECT COUNT(*) FROM usage")
        tx.execute("DELETE FROM usage WHERE day <> '' AND day < ?", (usage_cutoff,))
        tx.execute(
            "DELETE FROM usage WHERE scope = 'session' AND key NOT IN "
            "(SELECT session_id FROM shop_sessions)"
        )
        out["usage"] = before - count("SELECT COUNT(*) FROM usage")
        return out

    return db.transaction(run)


class SqlAuth:
    """`AuthBackend` and the admin operations, over a `Database`."""

    def __init__(
        self,
        db: Database,
        settings: DeploySettings | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        limiter: StartLimiter | None = None,
    ) -> None:
        self.db = db
        self.settings = settings or DeploySettings()
        self._now = now
        self._key = address_key(self.settings.origin_secret)
        self.limiter = limiter or StartLimiter(
            max(1, self.settings.starts_per_ip_minute), MINUTE_S, lambda: self._now().timestamp()
        )
        self._seen: dict[str, float] = {}
        self._last_purge = 0.0
        self._purge_lock = threading.Lock()

    # ---- retention --------------------------------------------------------------------------

    def tick(self) -> None:
        """Run the retention purge when six hours have passed since the last one (a request
        triggers it: there is no timer thread). A failure is logged and never fails the request."""
        stamp = self._now().timestamp()
        if stamp - self._last_purge < PURGE_EVERY_S or not self._purge_lock.acquire(False):
            return
        try:
            if stamp - self._last_purge >= PURGE_EVERY_S:
                self._last_purge = stamp
                purge(self.db, self._now(), self.settings.retention_days)
        except Exception as exc:
            log_failure("purge", exc)
        finally:
            self._purge_lock.release()

    # ---- start ------------------------------------------------------------------------------

    def start(self, ip: str, headers: Mapping[str, str] | None = None) -> str:
        """A new visitor and login session for the address `ip`; returns the cookie value. Raises
        `StartRefused` (with Retry-After seconds) past a limit."""
        self.tick()
        if not bot_check_allowed(headers or {}):
            raise StartRefused(0, "That could not be verified. Please try again.", 403)
        now = self._now()
        key = hmac_address(self._key, ip)
        wait = self.limiter.check(key)
        if wait:
            raise StartRefused(
                wait, "Too many visits were started from this address. Please wait a minute."
            )
        s = self.settings
        day = utc_day(now)
        cookie = secrets.token_urlsafe(16)  # 128 bits
        visitor = secrets.token_hex(8)

        def run(tx: Tx) -> bool:
            counters = (("visitors_ip_day", key), ("visitors_day", "*"))
            for (scope, k), cap in zip(
                counters, (s.max_visitors_per_ip_day, s.max_visitors_day), strict=True
            ):
                rows = tx.execute(
                    "SELECT searches FROM usage WHERE scope = ? AND key = ? AND day = ?",
                    (scope, k, day),
                )
                if rows and int(rows[0][0]) >= cap:
                    return False
            for scope, k in counters:
                tx.execute(
                    "INSERT INTO usage (scope, key, day, usd, searches, agains) "
                    "VALUES (?, ?, ?, 0, 1, 0) ON CONFLICT (scope, key, day) "
                    "DO UPDATE SET searches = usage.searches + 1",
                    (scope, k, day),
                )
            tx.execute(
                "INSERT INTO visitors (id, created_at, last_seen, ip_hmac) VALUES (?, ?, ?, ?)",
                (visitor, iso(now), iso(now), key),
            )
            tx.execute(
                "INSERT INTO login_sessions (id_hash, visitor_id, created_at, last_seen, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    hash_value(cookie),
                    visitor,
                    iso(now),
                    iso(now),
                    iso(now + timedelta(days=LOGIN_DAYS)),
                ),
            )
            return True

        if not self.db.transaction(run):
            raise StartRefused(
                seconds_to_midnight(now),
                "Too many visitors have started today. Please try again after midnight UTC.",
            )
        return cookie

    def logout(self, cookie: str | None) -> None:
        if cookie:
            self.db.transaction(
                lambda tx: tx.execute(
                    "DELETE FROM login_sessions WHERE id_hash = ?", (hash_value(cookie),)
                )
            )

    def identify(self, cookie: str | None) -> Identity | None:
        if not cookie:
            return None
        self.tick()
        digest = hash_value(cookie)
        now = self._now()

        def run(tx: Tx) -> Identity | None:
            rows = tx.execute(
                "SELECT expires_at, visitor_id FROM login_sessions WHERE id_hash = ?", (digest,)
            )
            if not rows or rows[0][0] <= iso(now):
                return None
            visitor_id = str(rows[0][1])
            if not tx.execute("SELECT 1 FROM visitors WHERE id = ?", (visitor_id,)):
                return None
            stamp = now.timestamp()
            if stamp - self._seen.get(digest, 0.0) > SEEN_EVERY_S:
                self._seen[digest] = stamp
                tx.execute(
                    "UPDATE login_sessions SET last_seen = ?, expires_at = ? WHERE id_hash = ?",
                    (iso(now), iso(now + timedelta(days=LOGIN_DAYS)), digest),
                )
                tx.execute("UPDATE visitors SET last_seen = ? WHERE id = ?", (iso(now), visitor_id))
            return Identity(visitor_id, digest[:12])

        return self.db.transaction(run)

    def usage(self, identity: Identity) -> dict[str, Any]:
        s = self.settings
        now = self._now()
        today = utc_day(now)

        def run(tx: Tx) -> float:
            rows = tx.execute(
                "SELECT u.usd FROM usage u JOIN visitors v ON v.ip_hmac = u.key "
                "WHERE u.scope = 'ip_day' AND v.id = ? AND u.day = ?",
                (identity.visitor_id, today),
            )
            return float(rows[0][0]) if rows else 0.0

        used = self.db.transaction(run)
        tomorrow = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
        return {
            "ip_day": {"used_usd": round(used, 4), "cap_usd": s.cap_ip_day_usd},
            "session": {
                "cap_usd": s.cap_session_usd, "max_searches": s.cap_searches,
                "max_shop_agains": s.cap_shop_agains,
            },
            "resets_at": iso(tomorrow),
        }  # fmt: skip

    def visitors_today(self) -> int:
        day = utc_day(self._now())
        rows = self.db.transaction(
            lambda tx: tx.execute(
                "SELECT searches FROM usage WHERE scope = 'visitors_day' AND key = '*' AND day = ?",
                (day,),
            )
        )
        return int(rows[0][0]) if rows else 0
