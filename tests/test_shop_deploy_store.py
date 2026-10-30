"""Stage D1, phase 2: the session store behind one interface, JSON files and a database (SQLite
stands in for Postgres here: the same SQL, no server). Real Postgres is a manual smoke test
(docs/deploy.md)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from app.shop import dbstore
from app.shop.dbstore import (
    DatabaseError,
    PostgresDatabase,
    SqliteDatabase,
    SqlSessionStore,
    migrate,
    open_database,
    open_store,
)
from app.shop.store import Session, SessionError, SessionStore
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)


@pytest.fixture(params=["json", "sqlite-file", "sqlite-memory"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "json":
        return SessionStore(tmp_path / "files")
    db = SqliteDatabase(str(tmp_path / "t.db") if request.param == "sqlite-file" else ":memory:")
    migrate(db)
    return SqlSessionStore(db)


def test_create_load_update_delete(store: Any) -> None:
    session = store.create()
    assert (
        store.exists(session.session_id)
        and store.load(session.session_id).session_id == session.session_id
    )
    with store.transaction(session.session_id) as live:
        live.shop_agains_used = 3
        live.lookup_usd = 0.25
    again = store.load(session.session_id)
    assert again.shop_agains_used == 3 and again.lookup_usd == 0.25
    assert again.updated_at >= again.created_at
    store.delete(session.session_id)
    assert not store.exists(session.session_id)
    with pytest.raises(SessionError):
        store.load(session.session_id)
    store.delete(session.session_id)  # deleting what is gone is fine


def test_a_failed_update_saves_nothing_new_and_bad_ids_are_refused(store: Any) -> None:
    session = store.create()
    with pytest.raises(RuntimeError), store.transaction(session.session_id) as live:
        live.shop_agains_used = 9
        raise RuntimeError("boom")
    assert store.load(session.session_id).shop_agains_used == 0
    for bad in ("", "x", "../etc/passwd", "a" * 40):
        assert not store.exists(bad)
        with pytest.raises(SessionError):
            store.load(bad)


def test_concurrent_updates_to_one_session_are_not_lost(store: Any) -> None:
    session = store.create()

    def bump() -> None:
        for _ in range(10):
            with store.transaction(session.session_id) as live:
                live.shop_agains_used += 1

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.load(session.session_id).shop_agains_used == 40


def test_the_stored_data_is_the_session_json_and_holds_no_key() -> None:
    db = SqliteDatabase(":memory:")
    migrate(db)
    sql = SqlSessionStore(db)
    session = sql.create()
    row = db.transaction(lambda tx: tx.execute("SELECT data FROM shop_sessions"))[0][0]
    assert json.loads(row)["session_id"] == session.session_id
    assert "sk-" not in row and "API_KEY" not in row and "api_key" not in row.lower()
    assert set(json.loads(row)) == set(Session.model_validate_json(row).model_dump(mode="json"))


def test_a_corrupt_or_wrong_row_is_a_session_error_not_a_crash() -> None:
    db = SqliteDatabase(":memory:")
    migrate(db)
    sql = SqlSessionStore(db)
    session = sql.create()
    for bad in ("not json", "[1, 2]", '{"session_id": 5}'):
        db.transaction(
            lambda tx, b=bad: tx.execute(  # type: ignore[misc]
                "UPDATE shop_sessions SET data = ? WHERE session_id = ?", (b, session.session_id)
            )
        )
        with pytest.raises(SessionError, match="unreadable"):
            sql.load(session.session_id)


def test_owner_and_claim() -> None:
    db = SqliteDatabase(":memory:")
    migrate(db)
    sql = SqlSessionStore(db)
    mine = sql.create(owner="inv1")
    loose = sql.create()
    assert sql.owner_of(mine.session_id) == "inv1" and sql.owner_of(loose.session_id) is None
    sql.claim(loose.session_id, "inv2")
    sql.claim(mine.session_id, "inv3")  # an owned session is never taken over
    assert sql.owner_of(loose.session_id) == "inv2" and sql.owner_of(mine.session_id) == "inv1"
    with sql.transaction(mine.session_id) as live:
        live.shop_agains_used = 1
    assert sql.owner_of(mine.session_id) == "inv1"  # saving keeps the owner
    assert sql.owner_of("nope") is None


# ---- migrations, selection ----------------------------------------------------------------------------


def test_the_migration_is_idempotent_and_creates_every_table(tmp_path: Path) -> None:
    db = SqliteDatabase(str(tmp_path / "m.db"))
    assert migrate(db) == dbstore.SCHEMA_VERSION == migrate(db)
    tables = {
        r[0]
        for r in db.transaction(
            lambda tx: tx.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        )
    }
    assert {"shop_sessions", "invites", "login_sessions", "usage", "schema_version"} <= tables
    versions = db.transaction(lambda tx: tx.execute("SELECT version FROM schema_version"))
    assert len(versions) == 1  # applied once, not twice


def test_the_store_comes_from_the_settings(tmp_path: Path) -> None:
    assert isinstance(open_store("", tmp_path), SessionStore)
    assert isinstance(open_store(f"sqlite:///{tmp_path / 'x.db'}", tmp_path), SqlSessionStore)
    assert isinstance(open_database("sqlite:///:memory:"), SqliteDatabase)
    assert isinstance(open_database("postgresql://u:p@h/db"), PostgresDatabase)
    with pytest.raises(DatabaseError, match="DATABASE_URL"):
        open_database("mysql://x")


# ---- the Postgres wrapper, with a fake connection -----------------------------------------------------


class OperationalError(Exception):
    pass


class FakeCursor:
    description = None

    def fetchall(self) -> list[Any]:
        return []


class FakeConn:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def execute(self, sql: str, params: Any = ()) -> FakeCursor:
        self.log.append(sql)
        return FakeCursor()

    def commit(self) -> None:
        self.log.append("COMMIT")

    def rollback(self) -> None:
        self.log.append("ROLLBACK")

    def close(self) -> None:
        self.log.append("CLOSE")


def test_the_first_connection_may_fail_and_is_retried() -> None:
    log: list[str] = []
    attempts = {"n": 0}
    waits: list[float] = []

    def connect(url: str) -> FakeConn:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OperationalError("connection timed out")  # a suspended Neon database
        return FakeConn(log)

    pg = PostgresDatabase("postgresql://u:secret@h/db", wait=waits.append, connect=connect)
    pg.transaction(lambda tx: tx.execute("SELECT ? , ?", (1, 2)))
    assert attempts["n"] == 3 and len(waits) == 2 and waits == sorted(waits)
    assert log == ["SELECT %s , %s", "COMMIT", "CLOSE"]  # ? became %s


def test_giving_up_raises_a_database_error_without_the_url() -> None:
    def connect(url: str) -> FakeConn:
        raise OperationalError("could not connect to postgresql://u:secret@h/db")

    pg = PostgresDatabase("postgresql://u:secret@h/db", wait=lambda s: None, connect=connect)
    with pytest.raises(DatabaseError) as info:
        pg.transaction(lambda tx: tx.execute("SELECT 1"))
    assert "secret" not in str(info.value) and "postgresql" not in str(info.value)


def test_a_query_error_rolls_back_and_is_not_retried() -> None:
    log: list[str] = []
    pg = PostgresDatabase("postgresql://x", wait=lambda s: None, connect=lambda u: FakeConn(log))

    def bad(tx: Any) -> None:
        raise ValueError("syntax")

    with pytest.raises(ValueError):
        pg.transaction(bad)
    assert log == ["ROLLBACK", "CLOSE"]


def test_the_service_runs_on_the_database_store(tmp_path: Path) -> None:
    from app.config import Config
    from app.shop.providers import ShopFixtureLookup, build_shop_providers
    from app.shop.service import ShopService

    db = SqliteDatabase(str(tmp_path / "s.db"))
    migrate(db)
    cfg = Config(tracing_disabled=True)
    search, _ = build_shop_providers(cfg)
    svc = ShopService(cfg, SqlSessionStore(db), search, ShopFixtureLookup())
    view = svc.search(None, "oak coffee tables")
    sid = view["session_id"]
    assert view["search"]["results"] and SqlSessionStore(db).exists(sid)
    svc.rate(sid, view["search"]["results"][0]["id"], "love")
    assert svc.session_view(sid)["saved"]
