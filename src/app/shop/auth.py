"""Invite codes and login sessions for the deployed Shop Mode API (ADR 0013).

An invite is a random code (192 bits) shown once when it is made and stored only as a SHA-256 hash
(enough for a high-entropy code). Logging in with a code makes a login session: a random id handed
to the browser as an HttpOnly cookie and stored only as its hash, valid for 14 days. A revoked or
expired invite ends its login sessions at once (every request re-checks the invite). Login attempts
are limited per client address and globally; a wrong, expired and revoked code take the same path and
give the same answer. Nothing here prints or logs a code, a cookie or a hash.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.shop.dbstore import Database, Tx
from app.shop.deploy import DeploySettings
from app.shop.server import Identity

LOGIN_DAYS = 14
CODE_BYTES = 24  # 192 bits
CODE_PREFIX = "shop-"
SEEN_EVERY_S = 60  # last_seen is written at most this often
WINDOW_S = 600
PER_IP_ATTEMPTS = 8  # attempts per client address per window
GLOBAL_ATTEMPTS = 60  # attempts from everyone per window


def hash_code(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def utc_day(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d")


class LoginLimiter:
    """Sliding windows of attempt times per client address and in all."""

    def __init__(
        self,
        now: Callable[[], float] = time.time,
        per_ip: int = PER_IP_ATTEMPTS,
        total: int = GLOBAL_ATTEMPTS,
        window_s: int = WINDOW_S,
    ) -> None:
        self._now = now
        self.per_ip, self.total, self.window_s = per_ip, total, window_s
        self._ip: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()
        self._lock = threading.Lock()

    def check(self, ip: str) -> int:
        """Seconds to wait (0 when the attempt may go ahead); a permitted attempt is counted."""
        with self._lock:
            now = self._now()
            cutoff = now - self.window_s
            while self._all and self._all[0] <= cutoff:
                self._all.popleft()
            mine = self._ip.setdefault(ip, deque())
            while mine and mine[0] <= cutoff:
                mine.popleft()
            if len(self._ip) > 5000:  # forget idle addresses
                for key in [k for k, v in self._ip.items() if not v]:
                    del self._ip[key]
            for bucket, limit in ((mine, self.per_ip), (self._all, self.total)):
                if len(bucket) >= limit:
                    return max(1, int(bucket[0] + self.window_s - now) + 1)
            mine.append(now)
            self._all.append(now)
            return 0


class SqlAuth:
    """`AuthBackend` and the admin operations, over a `Database`."""

    def __init__(
        self,
        db: Database,
        settings: DeploySettings | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        limiter: LoginLimiter | None = None,
    ) -> None:
        self.db = db
        self.settings = settings or DeploySettings()
        self._now = now
        self.limiter = limiter or LoginLimiter(lambda: self._now().timestamp())
        self._seen: dict[str, float] = {}

    # ---- admin ------------------------------------------------------------------------------

    def create_invite(self, label: str, days: int | None = None) -> tuple[str, str]:
        """(invite id, the code): the code is returned once and never stored."""
        code = CODE_PREFIX + secrets.token_urlsafe(CODE_BYTES)
        invite_id = secrets.token_hex(6)
        now = self._now()
        expires = iso(now + timedelta(days=days)) if days else None
        self.db.transaction(
            lambda tx: tx.execute(
                "INSERT INTO invites (id, label, code_hash, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (invite_id, label[:80], hash_code(code), iso(now), expires),
            )
        )
        return invite_id, code

    def revoke_invite(self, invite_id: str) -> bool:
        def run(tx: Tx) -> bool:
            rows = tx.execute("SELECT 1 FROM invites WHERE id = ?", (invite_id,))
            if not rows:
                return False
            tx.execute(
                "UPDATE invites SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (iso(self._now()), invite_id),
            )
            tx.execute("DELETE FROM login_sessions WHERE invite_id = ?", (invite_id,))
            return True

        return self.db.transaction(run)

    def list_invites(self) -> list[dict[str, Any]]:
        """Label, created, last seen, spend today, revoked: never the code."""
        today = utc_day(self._now())

        def run(tx: Tx) -> list[dict[str, Any]]:
            rows = tx.execute(
                "SELECT i.id, i.label, i.created_at, i.expires_at, i.revoked_at, i.last_seen, "
                "COALESCE(u.usd, 0) FROM invites i LEFT JOIN usage u "
                "ON u.scope = 'invite_day' AND u.key = i.id AND u.day = ? ORDER BY i.created_at",
                (today,),
            )
            return [
                {
                    "id": r[0], "label": r[1], "created": r[2], "expires": r[3],
                    "revoked": r[4] is not None, "last_seen": r[5], "spend_today_usd": round(float(r[6]), 4),
                }
                for r in rows
            ]  # fmt: skip

        return self.db.transaction(run)

    # ---- login ------------------------------------------------------------------------------

    def login(self, code: str, ip: str) -> tuple[str | None, int]:
        wait = self.limiter.check(ip)
        if wait:
            return None, wait
        now = self._now()
        digest = hash_code(code)  # the same work whatever the code is

        def run(tx: Tx) -> str | None:
            rows = tx.execute(
                "SELECT id, expires_at, revoked_at FROM invites WHERE code_hash = ?", (digest,)
            )
            ok = bool(rows) and rows[0][2] is None and (rows[0][1] is None or rows[0][1] > iso(now))
            cookie = secrets.token_urlsafe(32)
            invite = rows[0][0] if rows else ""
            # a refused attempt still hashes a cookie and writes nothing: one path, one answer
            if not ok:
                hash_code(cookie)
                return None
            tx.execute(
                "INSERT INTO login_sessions (id_hash, invite_id, created_at, last_seen, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    hash_code(cookie),
                    invite,
                    iso(now),
                    iso(now),
                    iso(now + timedelta(days=LOGIN_DAYS)),
                ),
            )
            tx.execute("UPDATE invites SET last_seen = ? WHERE id = ?", (iso(now), invite))
            return cookie

        return self.db.transaction(run), 0

    def logout(self, cookie: str | None) -> None:
        if cookie:
            self.db.transaction(
                lambda tx: tx.execute(
                    "DELETE FROM login_sessions WHERE id_hash = ?", (hash_code(cookie),)
                )
            )

    def identify(self, cookie: str | None) -> Identity | None:
        if not cookie:
            return None
        digest = hash_code(cookie)
        now = self._now()

        def run(tx: Tx) -> Identity | None:
            rows = tx.execute(
                "SELECT l.expires_at, i.id, i.label, i.expires_at, i.revoked_at FROM login_sessions l "
                "JOIN invites i ON i.id = l.invite_id WHERE l.id_hash = ?",
                (digest,),
            )
            if not rows:
                return None
            l_exp, invite_id, label, i_exp, revoked = rows[0]
            if (
                revoked is not None
                or l_exp <= iso(now)
                or (i_exp is not None and i_exp <= iso(now))
            ):
                return None
            stamp = now.timestamp()
            if stamp - self._seen.get(digest, 0.0) > SEEN_EVERY_S:
                self._seen[digest] = stamp
                tx.execute(
                    "UPDATE login_sessions SET last_seen = ? WHERE id_hash = ?", (iso(now), digest)
                )
                tx.execute("UPDATE invites SET last_seen = ? WHERE id = ?", (iso(now), invite_id))
            return Identity(str(invite_id), str(label), digest[:12])

        return self.db.transaction(run)

    def usage(self, identity: Identity) -> dict[str, Any]:
        s = self.settings
        now = self._now()
        today = utc_day(now)

        def run(tx: Tx) -> float:
            rows = tx.execute(
                "SELECT usd FROM usage WHERE scope = 'invite_day' AND key = ? AND day = ?",
                (identity.invite_id, today),
            )
            return float(rows[0][0]) if rows else 0.0

        used = self.db.transaction(run)
        tomorrow = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
        return {
            "invite_day": {"used_usd": round(used, 4), "cap_usd": s.cap_invite_day_usd},
            "session": {
                "cap_usd": s.cap_session_usd, "max_searches": s.cap_searches,
                "max_shop_agains": s.cap_shop_agains,
            },
            "resets_at": iso(tomorrow),
        }  # fmt: skip
