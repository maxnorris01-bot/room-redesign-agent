"""Shop Mode sessions: one JSON file per session under `shop-data/` (gitignored).

A session keeps everything a later "Shop Again" needs: each search (the raw request, the parsed
`ShopQuery`, when, which result ids, what it cost), every result ever shown, each rating with its
optional comment, and every resolved retailer link. Files are written atomically (a temp file in
the same folder, then `os.replace`), readable only by the owner, and every read-modify-write of one
session happens under that session's lock.

The helpers at the bottom (`rated_results`, `seen_ids`, `disliked_sellers`) are the inputs Shop
Again will use. No Shop Again logic lives here.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.shop.models import (
    FilterCounts,
    LinkKind,
    Rating,
    ShopQuery,
    ShopResult,
    StoredProfile,
)
from app.shopping_retrieval import seller_key

SESSION_SCHEMA_VERSION = 1
DEFAULT_ROOT = Path("shop-data")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,32}$")
MAX_SEARCHES_PER_SESSION = 50


class SessionError(RuntimeError):
    """A session could not be found, read or updated."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def now_iso_ms() -> str:
    """For ratings, which are ordered by time: millisecond resolution, same format as the page's."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class SearchRecord(_Model):
    index: int  # 1-based, in order
    request: str
    query: ShopQuery
    created_at: str
    result_ids: list[str]
    serper_calls: int
    seconds: dict[str, float]
    anthropic_usd: float
    provider_usd: float
    total_usd: float
    counts: FilterCounts
    ranked: bool
    warnings: list[str] = []
    kind: str = "search"  # "search" (typed) or "shop_again"
    serper_credits: int | None = None  # None in files from before credits were tracked
    note: str = ""
    queries: list[str] = []  # Serper phrasings used (a Shop Again may use up to three)
    pages: dict[str, int] = {}  # phrasing key -> highest Serper page fetched
    alignment_matches: int | None = None  # Shop Again: results matching the inferred preferences
    alignment_total: int | None = None


class RatingRecord(_Model):
    rating: Rating | None = None
    comment: str = ""
    rated_at: str | None = None  # when the current rating was set
    updated_at: str = ""
    search_index: int | None = None  # the search whose results the visitor was looking at


class ResolvedLink(_Model):
    result_id: str
    status: str  # "resolved" | "fallback" (looked up, no retailer link)
    link: str
    link_kind: LinkKind
    seller: str = ""
    price: float | None = None
    shipping: float | None = None
    shipping_status: str = "unknown"
    total: float | None = None
    note: str = ""
    cost_usd: float = 0.0
    resolved_at: str = ""


class Session(_Model):
    schema_version: int = SESSION_SCHEMA_VERSION
    session_id: str
    created_at: str
    updated_at: str
    searches: list[SearchRecord] = []
    results: dict[str, ShopResult] = Field(default_factory=dict)  # every result shown, newest copy
    first_seen: dict[str, int] = Field(default_factory=dict)  # result id -> search index
    ratings: dict[str, RatingRecord] = Field(default_factory=dict)
    resolved: dict[str, ResolvedLink] = Field(default_factory=dict)
    lookups_used: int = 0
    lookup_usd: float = 0.0
    profile: StoredProfile | None = None  # Shop Again's preference profile
    shop_agains_used: int = 0


def valid_session_id(session_id: str) -> bool:
    return bool(SESSION_ID_RE.match(session_id))


class SessionStore:
    def __init__(self, root: Path = DEFAULT_ROOT) -> None:
        self.root = root
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _path(self, session_id: str) -> Path:
        if not valid_session_id(session_id):
            raise SessionError("invalid session id")
        return self.root / f"{session_id}.json"

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(session_id, threading.Lock())

    def create(self) -> Session:
        now = now_iso()
        session = Session(session_id=secrets.token_urlsafe(12), created_at=now, updated_at=now)
        self.save(session)
        return session

    def exists(self, session_id: str) -> bool:
        return valid_session_id(session_id) and self._path(session_id).is_file()

    def load(self, session_id: str) -> Session:
        path = self._path(session_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return Session.model_validate(raw)
        except FileNotFoundError:
            raise SessionError("session not found") from None
        except (OSError, ValueError) as exc:  # bad JSON and failed validation are both ValueErrors
            raise SessionError(f"session file unreadable ({type(exc).__name__})") from None

    def save(self, session: Session) -> None:
        """Write the session atomically: temp file in the same folder, fsync, then replace."""
        path = self._path(session.session_id)
        self.root.mkdir(parents=True, exist_ok=True)
        session.updated_at = now_iso()
        data = json.dumps(session.model_dump(mode="json"), indent=1, ensure_ascii=False)
        tmp = self.root / f".{session.session_id}.{uuid.uuid4().hex[:8]}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @contextmanager
    def transaction(self, session_id: str) -> Iterator[Session]:
        """Load the session under its lock, yield it, and save it when the block ends cleanly."""
        with self._lock_for(session_id):
            session = self.load(session_id)
            yield session
            self.save(session)


# ---- inputs for Shop Again (read-only helpers; no Shop Again logic) ---------------------


def rated_results(session: Session) -> list[dict[str, Any]]:
    """Every result the visitor rated, with the rating and comment, newest rating first."""
    rows = [
        {
            "result": session.results[rid],
            "rating": rec.rating,
            "comment": rec.comment,
            "rated_at": rec.rated_at,
        }
        for rid, rec in session.ratings.items()
        if rec.rating is not None and rid in session.results
    ]
    return sorted(rows, key=lambda r: r["rated_at"] or "", reverse=True)


def seen_ids(session: Session) -> set[str]:
    """Ids of every result ever shown in this session (Shop Again excludes these)."""
    return set(session.results)


def disliked_sellers(session: Session) -> list[str]:
    """Names of sellers with at least one disliked result, sorted, each once."""
    names = {
        session.results[rid].seller
        for rid, rec in session.ratings.items()
        if rec.rating == "dislike" and rid in session.results and session.results[rid].seller
    }
    return sorted(names)


def sellers_to_deprioritize(session: Session, min_dislikes: int = 2) -> set[str]:
    """Normalized names of sellers with at least `min_dislikes` disliked results and no loved or
    liked one. Shop Again ranks their results last; it never excludes them."""
    disliked: dict[str, int] = {}
    positive: set[str] = set()
    for rid, rec in session.ratings.items():
        shown = session.results.get(rid)
        if shown is None or rec.rating is None:
            continue
        key = seller_key(shown.seller)
        if rec.rating == "dislike":
            disliked[key] = disliked.get(key, 0) + 1
        else:
            positive.add(key)
    return {k for k, n in disliked.items() if n >= min_dislikes and k not in positive}


def saved_ids(session: Session) -> list[str]:
    """Ids of loved and liked results from every search, newest rating first."""
    rows = [
        (rec.rated_at or "", rid)
        for rid, rec in session.ratings.items()
        if rec.rating in ("love", "like") and rid in session.results
    ]
    return [rid for _, rid in sorted(rows, reverse=True)]
