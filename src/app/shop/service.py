"""Shop Mode behaviour without HTTP: search, rate, resolve and the session view.

`ApiError` carries the HTTP status and a short, safe message for the caller. Messages never include
a URL or a key (provider errors name the provider and the status only).
"""

from __future__ import annotations

from typing import Any

from app.config import BudgetExceededError, Config
from app.shop import store as store_mod
from app.shop.models import (
    MAX_COMMENT_CHARS,
    MAX_REQUEST_CHARS,
    RATINGS,
    clean_comment,
    clean_request,
)
from app.shop.resolve import UnknownResult, resolve
from app.shop.search import SearchOutcome, run_search
from app.shop.store import (
    MAX_SEARCHES_PER_SESSION,
    RatingRecord,
    SearchRecord,
    Session,
    SessionError,
    SessionStore,
    now_iso,
)
from app.shopping import ProductLookup, ProviderError, SearchProvider

UNSET = object()


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ShopService:
    def __init__(
        self,
        config: Config,
        store: SessionStore,
        search: SearchProvider,
        lookup: ProductLookup,
    ) -> None:
        self.config = config
        self.store = store
        self.search_provider = search
        self.lookup_provider = lookup

    # ---- views ----------------------------------------------------------------------------

    def _view(self, session: Session | None) -> dict[str, Any]:
        base: dict[str, Any] = {
            "mode": self.config.llm_mode,
            "lookups_cap": self.config.shop_lookups_per_session,
            "session_id": None,
            "search": None,
            "ratings": {},
            "resolved": {},
            "lookups_used": 0,
            "search_count": 0,
        }
        if session is None:
            return base
        base.update(
            session_id=session.session_id,
            ratings={
                rid: {"rating": rec.rating, "comment": rec.comment}
                for rid, rec in session.ratings.items()
            },
            resolved={rid: r.model_dump(mode="json") for rid, r in session.resolved.items()},
            lookups_used=session.lookups_used,
            search_count=len(session.searches),
        )
        if session.searches:
            last = session.searches[-1]
            results = []
            for position, rid in enumerate(last.result_ids, start=1):
                shown = session.results.get(rid)
                if shown is not None:
                    results.append(
                        shown.model_copy(update={"rank": position}).model_dump(mode="json")
                    )
            base["search"] = {
                "index": last.index,
                "request": last.request,
                "query": last.query.model_dump(mode="json"),
                "created_at": last.created_at,
                "results": results,
                "seconds": last.seconds,
                "serper_calls": last.serper_calls,
                "anthropic_usd": last.anthropic_usd,
                "provider_usd": last.provider_usd,
                "total_usd": last.total_usd,
                "counts": last.counts.model_dump(mode="json"),
                "ranked": last.ranked,
                "warnings": last.warnings,
            }
        return base

    def _load(self, session_id: Any) -> Session:
        if not isinstance(session_id, str) or not store_mod.valid_session_id(session_id):
            raise ApiError(400, "invalid session id")
        try:
            return self.store.load(session_id)
        except SessionError:
            raise ApiError(404, "session not found") from None

    def session_view(self, session_id: str | None) -> dict[str, Any]:
        """The page's state. An absent, malformed or unknown id gives an empty view."""
        if session_id and self.store.exists(session_id):
            try:
                return self._view(self.store.load(session_id))
            except SessionError:
                pass
        return self._view(None)

    # ---- search ---------------------------------------------------------------------------

    def search(self, session_id: Any, request: Any) -> dict[str, Any]:
        if not isinstance(request, str):
            raise ApiError(400, "request must be text")
        text = request.strip()
        if not text:
            raise ApiError(400, "type what you are looking for")
        if len(text) > MAX_REQUEST_CHARS:
            raise ApiError(400, f"request is too long (at most {MAX_REQUEST_CHARS} characters)")
        cleaned = clean_request(text)
        if not cleaned:
            raise ApiError(400, "type what you are looking for")
        if session_id in (None, ""):
            session_id = None  # created after the search succeeds, so a failure leaves no file
        else:
            session = self._load(session_id)
            if len(session.searches) >= MAX_SEARCHES_PER_SESSION:
                raise ApiError(
                    429, f"search limit for this session reached ({MAX_SEARCHES_PER_SESSION})"
                )
        try:
            outcome = run_search(cleaned, self.search_provider, self.config)
        except ProviderError as exc:
            raise ApiError(502, str(exc)) from None
        except BudgetExceededError as exc:
            raise ApiError(429, str(exc)) from None
        if session_id is None:
            try:
                session_id = self.store.create().session_id
            except OSError:
                raise ApiError(500, "could not create a session") from None
        with self.store.transaction(session_id) as session:
            self._record(session, cleaned, outcome)
            return self._view(session)

    @staticmethod
    def _record(session: Session, request: str, outcome: SearchOutcome) -> None:
        index = len(session.searches) + 1
        for result in outcome.results:
            session.results[result.id] = result
            session.first_seen.setdefault(result.id, index)
        session.searches.append(
            SearchRecord(
                index=index,
                request=request,
                query=outcome.query,
                created_at=now_iso(),
                result_ids=[r.id for r in outcome.results],
                serper_calls=outcome.serper_calls,
                seconds=outcome.seconds,
                anthropic_usd=outcome.anthropic_usd,
                provider_usd=outcome.provider_usd,
                total_usd=outcome.total_usd,
                counts=outcome.counts,
                ranked=outcome.ranked,
                warnings=outcome.warnings,
            )
        )

    # ---- rate -----------------------------------------------------------------------------

    def rate(
        self, session_id: Any, result_id: Any, rating: Any = UNSET, comment: Any = UNSET
    ) -> dict[str, Any]:
        """Set the rating and/or the comment of one shown result. A missing field is unchanged;
        `rating=None` clears the rating. A record with neither is removed."""
        if rating is UNSET and comment is UNSET:
            raise ApiError(400, "send a rating or a comment")
        if rating is not UNSET and rating is not None and rating not in RATINGS:
            raise ApiError(400, f"rating must be one of {', '.join(RATINGS)} or null")
        if comment is not UNSET:
            if not isinstance(comment, str):
                raise ApiError(400, "comment must be text")
            if len(comment) > MAX_COMMENT_CHARS:
                raise ApiError(400, f"comment is too long (at most {MAX_COMMENT_CHARS} characters)")
        if not isinstance(result_id, str):
            raise ApiError(400, "result_id must be text")
        self._load(session_id)
        assert isinstance(session_id, str)
        with self.store.transaction(session_id) as session:
            if result_id not in session.results:
                raise ApiError(404, "unknown result")
            record = session.ratings.get(result_id, RatingRecord())
            now = now_iso()
            if rating is not UNSET and rating != record.rating:
                record.rating = rating
                record.rated_at = now if rating is not None else None
                if rating is not None:
                    record.search_index = len(session.searches)
            if comment is not UNSET:
                record.comment = clean_comment(comment)
            record.updated_at = now
            if record.rating is None and not record.comment:
                session.ratings.pop(result_id, None)
            else:
                session.ratings[result_id] = record
            return {"result_id": result_id, "rating": record.rating, "comment": record.comment}

    # ---- resolve --------------------------------------------------------------------------

    def resolve(self, session_id: Any, result_id: Any) -> dict[str, Any]:
        if not isinstance(result_id, str):
            raise ApiError(400, "result_id must be text")
        self._load(session_id)
        assert isinstance(session_id, str)
        try:
            with self.store.transaction(session_id) as session:
                link = resolve(session, result_id, self.lookup_provider, self.config)
                return {
                    "resolved": link.model_dump(mode="json"),
                    "lookups_used": session.lookups_used,
                    "lookups_cap": self.config.shop_lookups_per_session,
                }
        except UnknownResult:
            raise ApiError(404, "unknown result") from None
        except ProviderError as exc:
            raise ApiError(502, str(exc)) from None
        except BudgetExceededError as exc:
            raise ApiError(429, str(exc)) from None
