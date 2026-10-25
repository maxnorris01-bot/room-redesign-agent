"""Shop Mode behaviour without HTTP: search, rate, resolve, Shop Again, preferences and views.

`ApiError` carries the HTTP status and a short, safe message for the caller. Messages never include
a URL or a key (provider errors name the provider and the status only).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.config import BudgetExceededError, Config
from app.shop import store as store_mod
from app.shop.again import run_shop_again
from app.shop.clock import Clock
from app.shop.jobs import JobError, JobManager
from app.shop.models import (
    MAX_COMMENT_CHARS,
    MAX_REQUEST_CHARS,
    RATINGS,
    StoredProfile,
    clean_comment,
    clean_request,
)
from app.shop.prefs import pin_seller, profile_summary, reapply_removed, remove_entry, with_pins
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
    now_iso_ms,
    saved_ids,
)
from app.shop.textutil import normalize_phrase
from app.shopping import ProductLookup, ProviderError, SearchProvider
from app.thumbnails import Thumbnailer, build_thumbnailer

UNSET = object()


class _Default:
    """Marks "build the thumbnailer from the config" (an explicit None means no thumbnails)."""


DEFAULT = _Default()


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def headline(record: SearchRecord) -> str:
    """The one short sentence above the grid for a Shop Again (everything else stays in the trace):
    how many results, why fewer than 24 if so, and the tagging failure reason only when it failed.
    Empty for a typed search (its own short messages are shown as they are)."""
    if record.kind != "shop_again":
        return ""
    n = len(record.result_ids)
    batch = record.trace.get("batch", {}) if isinstance(record.trace, dict) else {}
    why = str(batch.get("shortfall", {}).get("explanation", ""))
    text = (
        f"{n} new results." if n >= 24 else f"Only {n} new results" + (f" ({why})." if why else ".")
    )
    reason = str(record.tagging.get("tag_reason", ""))
    if reason:
        short = reason if len(reason) <= 80 else reason[:77].rstrip() + "..."
        text += f" Candidate tagging failed: {short}"
        text += "" if short.endswith(".") else "."
    stopped = str(batch.get("timings", {}).get("stopped_before", ""))
    if stopped:
        text += f" Stopped early before {stopped}."
    return text


class ShopService:
    def __init__(
        self,
        config: Config,
        store: SessionStore,
        search: SearchProvider,
        lookup: ProductLookup,
        thumbnailer: Thumbnailer | None | _Default = DEFAULT,
        clock: Clock | None = None,
        jobs: JobManager | None = None,
    ) -> None:
        self.clock = clock or Clock()
        self.jobs = jobs or JobManager(self.clock)
        self.thumbnailer: Thumbnailer | None = (
            build_thumbnailer(config) if isinstance(thumbnailer, _Default) else thumbnailer
        )
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
            "profile": None,
            "profile_summary": "",
            "can_shop_again": False,
            "shop_agains_used": 0,
            "shop_agains_cap": self.config.shop_agains_per_session,
            "saved": [],
            "session_usd": 0.0,
        }
        if session is None:
            return base
        base.update(
            session_id=session.session_id,
            ratings={
                rid: {"rating": rec.rating, "comment": rec.comment, "rated_at": rec.rated_at}
                for rid, rec in session.ratings.items()
            },
            can_shop_again=any(rec.rating is not None for rec in session.ratings.values())
            and any(r.kind == "search" for r in session.searches),
            profile=session.profile.model_dump(mode="json") if session.profile else None,
            profile_summary=profile_summary(session.profile),
            shop_agains_used=session.shop_agains_used,
            saved=[session.results[rid].model_dump(mode="json") for rid in saved_ids(session)],
            session_usd=round(sum(r.total_usd for r in session.searches) + session.lookup_usd, 6),
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
                "kind": last.kind,
                "note": last.note,
                "used_query": last.used_query.model_dump(mode="json") if last.used_query else None,
                "diversity": {d: {"value": v[0], "share": v[1]} for d, v in last.diversity.items()},
                "seller_share": (
                    {"seller": last.seller_share[0], "share": last.seller_share[1]}
                    if last.seller_share
                    else None
                ),
                "dropped_phrasings": last.dropped_phrasings,
                "trace": last.trace,
                "headline": headline(last),
                "tagging": last.tagging,
                "queries": last.queries,
                "alignment": (
                    {
                        "matches": last.alignment_matches,
                        "total": last.alignment_total,
                        "signals": last.alignment_signals,
                    }
                    if last.alignment_matches is not None
                    else None
                ),
                "serper_calls": last.serper_calls,
                "serper_credits": (
                    last.serper_credits
                    if last.serper_credits is not None
                    else last.serper_calls * self.config.serper_credits_per_call
                ),
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

    def _prepare_search(self, session_id: Any, request: Any) -> tuple[str, str | None]:
        """Validate a search request; returns (the cleaned request, the session id or None)."""
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
            return cleaned, None  # created after the search succeeds, so a failure leaves no file
        session = self._load(session_id)
        if len(session.searches) >= MAX_SEARCHES_PER_SESSION:
            raise ApiError(
                429, f"search limit for this session reached ({MAX_SEARCHES_PER_SESSION})"
            )
        assert isinstance(session_id, str)
        return cleaned, session_id

    def _do_search(
        self, cleaned: str, session_id: str | None, progress: Callable[[str], None] | None = None
    ) -> dict[str, Any]:
        try:
            outcome = run_search(
                cleaned, self.search_provider, self.config, progress=progress, clock=self.clock
            )
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

    def search(self, session_id: Any, request: Any) -> dict[str, Any]:
        cleaned, sid = self._prepare_search(session_id, request)
        return self._do_search(cleaned, sid)

    def start_search(self, session_id: Any, request: Any) -> dict[str, Any]:
        """Start a search as a background job: `{"job_id": ...}` (poll `job_status`)."""
        cleaned, sid = self._prepare_search(session_id, request)
        return {
            "job_id": self._start("search", sid, lambda say: self._do_search(cleaned, sid, say))
        }

    def _start(
        self,
        kind: str,
        session_id: str | None,
        work: Callable[[Callable[[str], None]], dict[str, Any]],
    ) -> str:
        try:
            return self.jobs.start(session_id, kind, work, self._job_failure)
        except JobError as exc:
            raise ApiError(exc.status, exc.message) from None

    @staticmethod
    def _job_failure(exc: Exception) -> tuple[int, str]:
        if isinstance(exc, ApiError):
            return exc.status, exc.message
        return 500, "Something went wrong; try again"

    def job_status(self, job_id: Any, session_id: Any) -> dict[str, Any]:
        """The state of a background job (see `app.shop.jobs`): 404 for an unknown job id or a
        job that belongs to another session."""
        if not isinstance(job_id, str):
            raise ApiError(404, "unknown job")
        sid = session_id if isinstance(session_id, str) and session_id else None
        try:
            return self.jobs.status(job_id, sid)
        except JobError as exc:
            raise ApiError(exc.status, exc.message) from None

    @staticmethod
    def _record(session: Session, request: str, outcome: SearchOutcome, note: str = "") -> None:
        index = len(session.searches) + 1
        for result in outcome.results:
            session.results[result.id] = result
            session.first_seen.setdefault(result.id, index)
        session.searches.append(
            SearchRecord(
                index=index,
                request=request,
                query=outcome.display_query or outcome.query,
                used_query=outcome.query if outcome.display_query else None,
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
                kind=outcome.kind,
                serper_credits=outcome.serper_credits,
                note=note,
                queries=outcome.queries,
                dropped_phrasings=outcome.dropped_phrasings,
                trace=outcome.trace,
                tagging=outcome.tagging,
                diversity={d: [v[0], v[1]] for d, v in outcome.diversity.items()},
                seller_share=list(outcome.seller_share) if outcome.seller_share else None,
                pages=outcome.pages,
                alignment_matches=outcome.alignment[0] if outcome.alignment else None,
                alignment_total=outcome.alignment[1] if outcome.alignment else None,
                alignment_signals=outcome.alignment_signals if outcome.alignment else None,
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
            now = now_iso_ms()
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

    # ---- shop again and preferences -------------------------------------------------------

    def _prepare_again(self, session_id: Any) -> Session:
        """Validate a Shop Again request and return the session it will run on."""
        session = self._load(session_id)
        if not any(rec.rating is not None for rec in session.ratings.values()):
            raise ApiError(400, "Rate a few results first")
        if not any(r.kind == "search" for r in session.searches):
            raise ApiError(400, "Search first")
        if session.shop_agains_used >= self.config.shop_agains_per_session:
            raise ApiError(
                429,
                f"Shop Again limit reached ({self.config.shop_agains_per_session} per session)",
            )
        return session

    def _do_again(
        self, session_id: str, session: Session, progress: Callable[[str], None] | None = None
    ) -> dict[str, Any]:
        try:
            outcome, profile, query = run_shop_again(
                session, self.search_provider, self.config, self.thumbnailer, self.clock, progress
            )
        except ProviderError as exc:
            raise ApiError(502, str(exc)) from None
        except BudgetExceededError as exc:
            raise ApiError(429, str(exc)) from None
        with self.store.transaction(session_id) as live:
            if live.shop_agains_used >= self.config.shop_agains_per_session:
                raise ApiError(429, "Shop Again limit for this session reached")
            # ratings made while the search ran are kept; the profile replaces the old one except
            # that entries deleted meanwhile stay deleted
            if live.profile is not None:
                profile = with_pins(
                    reapply_removed(profile, live.profile.removed), live.profile.pinned_sellers
                )
            live.profile = profile
            live.shop_agains_used += 1
            live.tag_cache.update(outcome.new_tags)  # a result is tagged once, ever
            for peer in outcome.peer_log:  # rotate: what was searched is remembered
                row = live.searched_peers.setdefault(
                    peer["key"], {"name": peer["peer"], "searches": 0, "new": 0, "empty": True}
                )
                row["searches"] += 1
                row["new"] += peer["new_candidates"]
                row["empty"] = peer["new_candidates"] == 0
            self._record(live, live.searches[-1].request if live.searches else "", outcome,
                         note=profile_summary(profile))  # fmt: skip
            return self._view(live)

    def shop_again(self, session_id: Any) -> dict[str, Any]:
        """A fresh set of results shaped by the ratings (see `app.shop.again`)."""
        session = self._prepare_again(session_id)
        assert isinstance(session_id, str)
        return self._do_again(session_id, session)

    def start_shop_again(self, session_id: Any) -> dict[str, Any]:
        """Start a Shop Again as a background job: `{"job_id": ...}` (poll `job_status`)."""
        session = self._prepare_again(session_id)
        assert isinstance(session_id, str)
        return {
            "job_id": self._start(
                "shop_again", session_id, lambda say: self._do_again(session_id, session, say)
            )
        }

    def remove_preference(self, session_id: Any, kind: Any, phrase: Any) -> dict[str, Any]:
        """Delete one preference chip; its phrase is never added back by a later merge."""
        if kind not in ("like", "dislike", "inferred", "guess"):
            raise ApiError(400, "kind must be like, dislike, inferred or guess")
        if not isinstance(phrase, str) or not phrase.strip() or len(phrase) > 80:
            raise ApiError(400, "phrase must be short text")
        self._load(session_id)
        assert isinstance(session_id, str)
        with self.store.transaction(session_id) as session:
            updated: StoredProfile | None = (
                remove_entry(session.profile, kind, phrase) if session.profile else None
            )
            if updated is None:
                raise ApiError(404, "no such preference")
            session.profile = updated
            return {
                "profile": updated.model_dump(mode="json"),
                "profile_summary": profile_summary(updated),
                "removed": normalize_phrase(phrase),
            }

    def pin_preference(self, session_id: Any, phrase: Any) -> dict[str, Any]:
        """Pin or unpin the seller chip: a pinned seller may fill up to 80% of a Shop Again batch
        (otherwise 50%). Stored in the session's profile."""
        if not isinstance(phrase, str) or not phrase.strip() or len(phrase) > 80:
            raise ApiError(400, "phrase must be short text")
        self._load(session_id)
        assert isinstance(session_id, str)
        with self.store.transaction(session_id) as session:
            updated = pin_seller(session.profile, phrase) if session.profile else None
            if updated is None:
                raise ApiError(404, "no such seller chip")
            session.profile = updated
            return {
                "profile": updated.model_dump(mode="json"),
                "profile_summary": profile_summary(updated),
                "pinned": bool(updated.pinned_sellers),
            }
