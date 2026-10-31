"""Spend caps for the deployed Shop Mode API (ADR 0013, 0014).

Spend is counted from the same numbers the page already shows (`session_usd`: Anthropic plus Serper
credits at their configured price, SerpApi lookups, background preparation, refine calls). Every time
the database store saves a session, `Meter` adds the growth of that session's cost to three
counters in the same transaction: per session, per visitor address per UTC day, and for everyone per UTC day,
so counts survive restarts and two sessions near a cap cannot both slip past it. `CapGuard` is asked
before a search, a Shop Again or a lookup starts; past a cap it raises a 429 `cap_reached` error that
names the limit and when it resets. Work already running finishes and is counted; results already
shown and Saved items stay readable (nothing else is guarded).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.shop.context import current_visitor
from app.shop.dbstore import Database, Tx
from app.shop.deploy import DeploySettings
from app.shop.service import ApiError
from app.shop.store import Session

LIMITS = ("session", "ip_day", "global_day", "searches", "shop_agains")


def session_cost(session: Session) -> float:
    return (
        sum(r.total_usd for r in session.searches)
        + session.lookup_usd
        + float(session.prep.get("usd", 0.0))
        + session.refine_usd
    )


def _day(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%d")


def next_reset(now: datetime) -> str:
    midnight = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
    return midnight.isoformat(timespec="seconds")


class Meter:
    """The `after_save` hook of `SqlSessionStore`: counts spend as sessions are saved."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now

    def __call__(self, tx: Tx, session: Session) -> None:
        cost = session_cost(session)
        sid = session.session_id
        rows = tx.execute(
            "SELECT usd FROM usage WHERE scope = 'session' AND key = ? AND day = ''", (sid,)
        )
        old = float(rows[0][0]) if rows else 0.0
        new = max(old, cost)
        searches = sum(1 for r in session.searches if r.kind == "search")
        tx.execute(
            "INSERT INTO usage (scope, key, day, usd, searches, agains) VALUES ('session', ?, '', ?, ?, ?) "
            "ON CONFLICT (scope, key, day) DO UPDATE SET usd = excluded.usd, "
            "searches = excluded.searches, agains = excluded.agains",
            (sid, new, searches, session.shop_agains_used),
        )
        delta = new - old
        if delta <= 0:
            return
        day = _day(self._now())
        owner = tx.execute(
            "SELECT v.ip_hmac FROM shop_sessions s JOIN visitors v ON v.id = s.visitor_id "
            "WHERE s.session_id = ?",
            (sid,),
        )
        targets = [("global_day", "*")]
        if owner:
            targets.append(("ip_day", str(owner[0][0])))
        for scope, key in targets:
            tx.execute(
                "INSERT INTO usage (scope, key, day, usd, searches, agains) VALUES (?, ?, ?, ?, 0, 0) "
                "ON CONFLICT (scope, key, day) DO UPDATE SET usd = usage.usd + excluded.usd",
                (scope, key, day, delta),
            )


class CapGuard:
    def __init__(
        self,
        db: Database,
        settings: DeploySettings,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = db
        self.settings = settings
        self._now = now

    def _read(self, session_id: str | None, visitor: str | None) -> dict[str, Any]:
        now = self._now()
        day = _day(now)

        def run(tx: Tx) -> dict[str, Any]:
            def one(scope: str, key: str, day_: str) -> tuple[float, int, int]:
                rows = tx.execute(
                    "SELECT usd, searches, agains FROM usage WHERE scope = ? AND key = ? AND day = ?",
                    (scope, key, day_),
                )
                return (
                    (float(rows[0][0]), int(rows[0][1]), int(rows[0][2])) if rows else (0.0, 0, 0)
                )

            owner = visitor
            if owner is None and session_id:
                rows = tx.execute(
                    "SELECT visitor_id FROM shop_sessions WHERE session_id = ?", (session_id,)
                )
                owner = str(rows[0][0]) if rows and rows[0][0] is not None else None
            address = None
            if owner:
                rows = tx.execute("SELECT ip_hmac FROM visitors WHERE id = ?", (owner,))
                address = str(rows[0][0]) if rows else None
            return {
                "global": one("global_day", "*", day),
                "ip": one("ip_day", address, day) if address else (0.0, 0, 0),
                "session": one("session", session_id, "") if session_id else (0.0, 0, 0),
            }

        return self.db.transaction(run)

    def _error(self, limit: str, message: str, resets: bool) -> ApiError:
        resets_at = next_reset(self._now()) if resets else None
        return ApiError(429, message, "cap_reached", {"limit": limit, "resets_at": resets_at})

    def check(self, kind: str, session_id: str | None = None) -> None:
        """Raise `cap_reached` when a `search`, `again` or `spend` (a lookup, photos, a refine) may
        not start. The visitor is the request's (`current_visitor`) or the session's owner; the daily allowance
        is counted per visitor address."""
        s = self.settings
        used = self._read(session_id, current_visitor.get())
        if used["global"][0] >= s.cap_global_day_usd:
            raise self._error(
                "global_day",
                "Shop Mode has reached its limit for today. It resets at midnight UTC.", True,
            )  # fmt: skip
        if used["ip"][0] >= s.cap_ip_day_usd:
            raise self._error(
                "ip_day",
                "This address has used today's allowance. It resets at midnight UTC.", True,
            )  # fmt: skip
        if used["session"][0] >= s.cap_session_usd:
            raise self._error(
                "session",
                "This session has used its spending allowance. Start over to keep going.",
                False,
            )
        if kind == "search" and used["session"][1] >= s.cap_searches:
            raise self._error(
                "searches",
                f"This session has used its {s.cap_searches} search{'' if s.cap_searches == 1 else 'es'}. Start over to keep going.",
                False,
            )
        if kind == "again" and used["session"][2] >= s.cap_shop_agains:
            raise self._error(
                "shop_agains",
                f"This session has used its {s.cap_shop_agains} Shop Again{'' if s.cap_shop_agains == 1 else 's'}. Start over to keep going.",
                False,
            )

    def background_ok(self, session_id: str) -> bool:
        """Whether background preparation may start: not past the daily or the session caps."""
        try:
            self.check("spend", session_id)
        except ApiError:
            return False
        return True
