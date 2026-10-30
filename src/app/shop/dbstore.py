"""A database store for Shop Mode sessions, invites, logins and spend (ADR 0013).

The service stores sessions in JSON files by default (`app.shop.store.SessionStore`). With
`DATABASE_URL` set, `open_store` returns a `SqlSessionStore` over Postgres (psycopg 3, hand-written
SQL, a migration command, retries on connection errors: Neon suspends a database after five minutes
idle, so the first connection can be slow or fail). The same SQL runs on SQLite (`sqlite:///path`),
which is how the tests and the local deployment check run without a Postgres server; the parts only
real Postgres can prove (the driver, TLS, a suspended-then-resumed database) are listed in
docs/deploy.md as a manual smoke test.

SQL is written once with `?` placeholders and plain types (TEXT, INTEGER, DOUBLE PRECISION);
`PostgresDatabase` turns `?` into `%s`. Session data is the same JSON the file store holds (no keys,
no raw provider responses). A read-modify-write of one session happens under that session's
in-process lock (one machine only; in-flight jobs also live in memory).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit

from app.shop.store import (
    Session,
    SessionError,
    SessionStore,
    ensure_topics,
    now_iso,
    valid_session_id,
)

T = TypeVar("T")
RETRIES = 3
RETRY_WAIT_S = 0.4

# ---- the schema ---------------------------------------------------------------------------------

MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            """CREATE TABLE IF NOT EXISTS shop_sessions (
                session_id TEXT PRIMARY KEY,
                invite_id TEXT,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS invites (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                code_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                revoked_at TEXT,
                last_seen TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS login_sessions (
                id_hash TEXT PRIMARY KEY,
                invite_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS usage (
                scope TEXT NOT NULL,
                key TEXT NOT NULL,
                day TEXT NOT NULL,
                usd DOUBLE PRECISION NOT NULL DEFAULT 0,
                searches INTEGER NOT NULL DEFAULT 0,
                agains INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (scope, key, day)
            )""",
            "CREATE INDEX IF NOT EXISTS login_sessions_invite ON login_sessions (invite_id)",
        ],
    ),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


class Tx(Protocol):
    def execute(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]: ...


class Database(Protocol):
    """Runs a function inside one transaction; retries when the connection fails."""

    kind: str

    def transaction(self, fn: Callable[[Tx], T]) -> T: ...


class DatabaseError(RuntimeError):
    """The database could not be reached or answered badly (never carries the URL)."""


# ---- SQLite (tests, local deployment check) -------------------------------------------------------


class _SqliteTx:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        cur = self._conn.execute(sql, tuple(params))
        return [tuple(r) for r in cur.fetchall()] if cur.description else []


class SqliteDatabase:
    kind = "sqlite"

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._memory = (
            sqlite3.connect(path, check_same_thread=False) if path == ":memory:" else None
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._memory is not None:
            yield self._memory
            return
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    def transaction(self, fn: Callable[[Tx], T]) -> T:
        with self._lock, self._connect() as conn:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                out = fn(_SqliteTx(conn))
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return out


# ---- Postgres (psycopg 3) --------------------------------------------------------------------------


class _PgTx:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        cur = self._conn.execute(sql.replace("?", "%s"), tuple(params))
        return [tuple(r) for r in cur.fetchall()] if cur.description else []


class PostgresDatabase:
    kind = "postgres"

    def __init__(
        self,
        url: str,
        wait: Callable[[float], None] | None = None,
        connect: Callable[[str], Any] | None = None,
    ) -> None:
        self._url = url
        self._wait = wait or (lambda s: threading.Event().wait(s))
        self._connect_fn = connect

    def _connect(self) -> Any:
        if self._connect_fn is not None:
            return self._connect_fn(self._url)
        import psycopg  # imported late: only a deployment with DATABASE_URL needs it

        return psycopg.connect(self._url, connect_timeout=10, autocommit=False)

    def transaction(self, fn: Callable[[Tx], T]) -> T:
        last: Exception | None = None
        for attempt in range(RETRIES):
            conn = None
            try:
                conn = self._connect()
            except Exception as exc:  # a suspended database can refuse the first connection
                last = exc
                self._wait(RETRY_WAIT_S * (attempt + 1))
                continue
            try:
                out = fn(_PgTx(conn))
                conn.commit()
                return out
            except Exception as exc:
                conn.rollback()
                if type(exc).__name__ in ("OperationalError", "InterfaceError"):
                    last = exc
                    self._wait(RETRY_WAIT_S * (attempt + 1))
                    continue
                raise
            finally:
                conn.close()
        raise DatabaseError(f"the database is not reachable ({type(last).__name__})")


def open_database(url: str) -> Database:
    """A database for `url`: `sqlite:///path` (tests, local) or `postgres://` / `postgresql://`."""
    parts = urlsplit(url)
    if parts.scheme == "sqlite":
        path = parts.path[1:] if parts.path.startswith("/") else parts.path
        return SqliteDatabase(path or ":memory:")
    if parts.scheme in ("postgres", "postgresql"):
        return PostgresDatabase(url)
    raise DatabaseError("DATABASE_URL must start with postgres://, postgresql:// or sqlite:///")


# ---- migrations ---------------------------------------------------------------------------------------


def migrate(db: Database) -> int:
    """Apply every schema step not applied yet; running it twice changes nothing. Returns the
    version the database is at."""

    def run(tx: Tx) -> int:
        tx.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        rows = tx.execute("SELECT MAX(version) FROM schema_version")
        have = int(rows[0][0]) if rows and rows[0][0] is not None else 0
        for version, statements in MIGRATIONS:
            if version > have:
                for sql in statements:
                    tx.execute(sql)
                tx.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
                have = version
        return have

    return db.transaction(run)


# ---- the session store ----------------------------------------------------------------------------------


class SqlSessionStore:
    """`SessionBackend` over a `Database`: the same interface as the JSON file store."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.after_save: Callable[[Tx, Session], None] | None = None  # the spend meter (phase 4)

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(session_id, threading.Lock())

    @staticmethod
    def _check(session_id: str) -> None:
        if not valid_session_id(session_id):
            raise SessionError("invalid session id")

    def create(self, owner: str | None = None) -> Session:
        session = SessionStore(Path("/nonexistent")).new_session()
        self.save(session, owner)
        return session

    def exists(self, session_id: str) -> bool:
        if not valid_session_id(session_id):
            return False
        rows = self.db.transaction(
            lambda tx: tx.execute("SELECT 1 FROM shop_sessions WHERE session_id = ?", (session_id,))
        )
        return bool(rows)

    def load(self, session_id: str) -> Session:
        self._check(session_id)
        rows = self.db.transaction(
            lambda tx: tx.execute(
                "SELECT data FROM shop_sessions WHERE session_id = ?", (session_id,)
            )
        )
        if not rows:
            raise SessionError("session not found")
        try:
            return ensure_topics(Session.model_validate(json.loads(rows[0][0])))
        except (TypeError, ValueError):  # bad JSON and failed validation are both ValueErrors
            raise SessionError("session row unreadable") from None

    def save(self, session: Session, owner: str | None = None) -> None:
        self._check(session.session_id)
        session.updated_at = now_iso()
        data = json.dumps(session.model_dump(mode="json"), ensure_ascii=False)

        def run(tx: Tx) -> None:
            tx.execute(
                "INSERT INTO shop_sessions (session_id, invite_id, data, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (session_id) DO UPDATE SET "
                "data = excluded.data, updated_at = excluded.updated_at",
                (session.session_id, owner, data, session.created_at, session.updated_at),
            )
            if self.after_save is not None:
                self.after_save(tx, session)

        self.db.transaction(run)

    @contextmanager
    def transaction(self, session_id: str) -> Iterator[Session]:
        with self._lock_for(session_id):
            session = self.load(session_id)
            yield session
            self.save(session)

    def delete(self, session_id: str) -> None:
        self._check(session_id)
        self.db.transaction(
            lambda tx: tx.execute("DELETE FROM shop_sessions WHERE session_id = ?", (session_id,))
        )

    def owner_of(self, session_id: str) -> str | None:
        if not valid_session_id(session_id):
            return None
        rows = self.db.transaction(
            lambda tx: tx.execute(
                "SELECT invite_id FROM shop_sessions WHERE session_id = ?", (session_id,)
            )
        )
        return str(rows[0][0]) if rows and rows[0][0] is not None else None

    def claim(self, session_id: str, invite_id: str) -> None:
        self.db.transaction(
            lambda tx: tx.execute(
                "UPDATE shop_sessions SET invite_id = ? WHERE session_id = ? AND invite_id IS NULL",
                (invite_id, session_id),
            )
        )


def open_store(database_url: str, data_dir: Path) -> Any:
    """The session store the settings ask for: the database when `database_url` is set, else the
    JSON files under `data_dir` (the default, local)."""
    if not database_url:
        return SessionStore(data_dir)
    return SqlSessionStore(open_database(database_url))
