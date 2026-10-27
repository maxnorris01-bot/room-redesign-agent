"""Shop Mode behaviour without HTTP: search, rate, resolve, Shop Again, preferences and views.

`ApiError` carries the HTTP status and a short, safe message for the caller. Messages never include
a URL or a key (provider errors name the provider and the status only).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.config import BudgetExceededError, Config
from app.llm import Budget
from app.shop import store as store_mod
from app.shop.again import run_shop_again
from app.shop.ahead import LookAhead
from app.shop.clock import Clock
from app.shop.jobs import JobError, JobManager
from app.shop.models import (
    MAX_COMMENT_CHARS,
    MAX_REQUEST_CHARS,
    RATINGS,
    RefineChip,
    StoredProfile,
    clean_comment,
    clean_request,
    is_https_url,
)
from app.shop.nearby import build_fingerprinter
from app.shop.parse import COLOR_CONTEXT_KINDS
from app.shop.prefs import (
    pin_seller,
    profile_summary,
    reapply_removed,
    remove_entry,
    with_pins,
)
from app.shop.prep import PrepManager
from app.shop.progress import SHOP_AGAIN_REFINE_STEPS, Progress
from app.shop.refine import REJECTED, add_chips, clean_instruction, interpret
from app.shop.resolve import (
    UnknownResult,
    auto_lookups_used,
    fill_rating,
    product_lookup,
    resolve,
)
from app.shop.search import SearchOutcome, near_context, run_search
from app.shop.store import (
    MAX_SEARCHES_PER_SESSION,
    RatingRecord,
    SearchRecord,
    Session,
    SessionError,
    SessionStore,
    Topic,
    advance_topic,
    comment_key,
    current_topic,
    in_topic,
    now_iso,
    now_iso_ms,
    rated_count,
    saved_ids,
)
from app.shop.textutil import normalize_phrase
from app.shop.thresholds import OPEN_UI_TIMEOUT_S
from app.shop.vocab import normalize
from app.shopping import ProductLookup, ProviderError, SearchProvider
from app.thumbnails import Thumbnailer, build_thumbnailer

MAX_GALLERY_SHOWN = 6
MAX_HIDDEN = 400  # listings remembered as hidden duplicates (the oldest go first)
MIN_GALLERY = 2  # fewer usable images than this is "no more photos available"
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
        self.fingerprinter = build_fingerprinter(config)
        self.ahead = LookAhead(config, store, lookup)
        self.prep = PrepManager(config, store, self.thumbnailer, self.clock, self.fingerprinter)

    # ---- views ----------------------------------------------------------------------------

    def _view(self, session: Session | None) -> dict[str, Any]:
        base: dict[str, Any] = {
            "mode": self.config.llm_mode,
            "lookups_cap": self.config.shop_lookups_per_session,
            "gallery": self.config.shop_gallery,
            "open_timeout_s": OPEN_UI_TIMEOUT_S,
            "lookup_summary": {"auto": 0, "you": 0, "median_s": None},
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
            "prep_usd": 0.0,
            "photos_cached": [],
            "photos_none": [],
            "lookup_trace": [],
            "refine": [],
            "refine_usd": 0.0,
        }
        if session is None:
            return base
        base.update(
            session_id=session.session_id,
            ratings={
                rid: {
                    "rating": rec.rating,
                    "comment": rec.comment,
                    "rated_at": rec.rated_at,
                    "topic": rec.topic,
                }  # fmt: skip
                for rid, rec in session.ratings.items()
            },
            can_shop_again=rated_count(session) > 0
            and any(r.kind == "search" for r in session.searches),
            topic=self._topic_view(session),
            profile=session.profile.model_dump(mode="json") if session.profile else None,
            profile_summary=profile_summary(session.profile),
            shop_agains_used=session.shop_agains_used,
            saved=[self._saved_row(session, rid) for rid in saved_ids(session)],
            session_usd=round(
                sum(r.total_usd for r in session.searches)
                + session.lookup_usd
                + float(session.prep.get("usd", 0.0))
                + session.refine_usd,
                6,
            ),
            prep_usd=round(float(session.prep.get("usd", 0.0)), 6),
            photos_cached=sorted(
                rid for rid, row in session.product_cache.items() if self._gallery_ok(row)
            )
            if self.config.shop_gallery
            else [],
            photos_none=sorted(
                rid
                for rid, row in session.product_cache.items()
                if not row.get("error") and not self._gallery_ok(row)
            )
            if self.config.shop_gallery
            else [],
            lookup_trace=self._lookup_trace(session),
            lookup_summary=self._lookup_summary(session),
            refine=[c.model_dump(mode="json") for c in self._refinements(session)],
            refine_usd=round(session.refine_usd, 6),
            prep={
                "calls": int(session.prep.get("calls", 0)),
                "tagged": int(session.prep.get("tagged", 0)),
                "comments": int(session.prep.get("comments", 0)),
                "photo_tagged": sum(1 for v in session.tag_source.values() if v == "photo"),
                "errors": list(session.prep.get("errors", []))[-3:],
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
                "topic": last.topic,
                "topic_notice": last.topic_notice,
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

    @staticmethod
    def _gallery_ok(row: dict[str, Any]) -> bool:
        """A cached lookup with at least `MIN_GALLERY` usable images."""
        if row.get("error"):
            return False
        details = row.get("details") or {}
        return len([u for u in details.get("images", []) if is_https_url(u)]) >= MIN_GALLERY

    def _lookup_trace(self, session: Session) -> list[dict[str, Any]]:
        """Per product lookup: whether it was made ahead of time or by the shopper, how long it took
        (seconds), and, only when the gallery is on, which response fields held images and how many
        (names and counts, never a URL)."""
        out = []
        for n, row in enumerate(session.product_cache.values(), start=1):
            entry: dict[str, Any] = {
                "n": n,
                "ok": not row.get("error"),
                "by": row.get("by", "you"),
                "seconds": row.get("seconds"),
            }
            if self.config.shop_gallery:
                details = row.get("details") or {}
                entry["fields"] = dict(details.get("image_fields", {}))
                entry["usable"] = len(details.get("images", []))
            out.append(entry)
        return out

    @staticmethod
    def _lookup_summary(session: Session) -> dict[str, Any]:
        """Lookups made ahead (auto) and by the shopper (you), and the median duration."""
        rows = list(session.product_cache.values())
        times = sorted(float(r["seconds"]) for r in rows if r.get("seconds") is not None)
        median = None
        if times:
            mid = len(times) // 2
            median = times[mid] if len(times) % 2 else round((times[mid - 1] + times[mid]) / 2, 3)
        return {"auto": auto_lookups_used(session), "you": len(rows) - auto_lookups_used(session),
                "median_s": median}  # fmt: skip

    @staticmethod
    def _refinements(session: Session) -> list[RefineChip]:
        topic = current_topic(session)
        return list(topic.refinements) if topic is not None else []

    @staticmethod
    def _topic_view(session: Session) -> dict[str, Any] | None:
        """The current topic and how many ratings count toward it and how many do not (they were
        made in an earlier topic and only keep their place in Saved)."""
        topic = current_topic(session)
        if topic is None:
            return None
        rated = [rec for rec in session.ratings.values() if rec.rating is not None]
        counted = sum(1 for rec in rated if in_topic(rec, session))
        return {
            "id": topic.id, "label": topic.label, "request": topic.request,
            "counted": counted, "excluded": len(rated) - counted, "topics": len(session.topics),
        }  # fmt: skip

    @staticmethod
    def _saved_row(session: Session, rid: str) -> dict[str, Any]:
        """A saved result; one from an earlier topic carries the search it came from."""
        row = session.results[rid].model_dump(mode="json")
        rec = session.ratings.get(rid)
        if rec is not None and not in_topic(rec, session):
            old = next((t for t in session.topics if t.id == rec.topic), None)
            row["topic_label"] = (old.request if old else "")[:80]
        return row

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
        near = near_context(
            self.store.load(session_id) if session_id and self.store.exists(session_id) else None,
            self.fingerprinter,
            self.config,
        )
        try:
            outcome = run_search(
                cleaned, self.search_provider, self.config, progress=progress, clock=self.clock,
                near=near,
            )  # fmt: skip
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
            notice = advance_topic(session, outcome.display_query or outcome.query, cleaned)
            self._record(session, cleaned, outcome, topic_notice=notice)
            view = self._view(session)
        self.prep.submit_batch(session_id, outcome.results)  # tag it from photos while you browse
        return view

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
        steps: tuple[tuple[str, str], ...] | None = None,
    ) -> str:
        try:
            return self.jobs.start(session_id, kind, work, self._job_failure, steps)
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
    def _record(
        session: Session,
        request: str,
        outcome: SearchOutcome,
        note: str = "",
        topic_notice: str = "",
    ) -> None:
        index = len(session.searches) + 1
        for result in outcome.results:
            session.results[result.id] = result
            session.first_seen.setdefault(result.id, index)
        if outcome.pool_snapshot:
            session.pool_snapshots = [
                *session.pool_snapshots,
                {
                    "index": index,
                    "kind": outcome.kind,
                    "topic": session.current_topic,
                    "items": outcome.pool_snapshot[:100],
                },
            ][-10:]
        for row in outcome.hidden:  # the seen memory keeps what was collapsed
            session.hidden[row["key"]] = {k: v for k, v in row.items() if k != "key"}
        while len(session.hidden) > MAX_HIDDEN:
            session.hidden.pop(next(iter(session.hidden)))
        for result in outcome.results:
            if outcome.fingerprints.get(result.id) is not None:
                session.fingerprints[result.id] = outcome.fingerprints[result.id]
        rated_shown = sum(1 for r in outcome.results if r.rating is not None)
        reviews = {"with_rating": rated_shown, "total": len(outcome.results)}
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
                trace={
                    **outcome.trace,
                    **({"request": outcome.request_trace} if outcome.request_trace else {}),
                    "reviews": reviews,
                    **({"duplicates": outcome.duplicate_trace} if outcome.duplicate_trace else {}),
                },
                tagging=outcome.tagging,
                diversity={d: [v[0], v[1]] for d, v in outcome.diversity.items()},
                seller_share=list(outcome.seller_share) if outcome.seller_share else None,
                pages=outcome.pages,
                alignment_matches=outcome.alignment[0] if outcome.alignment else None,
                alignment_total=outcome.alignment[1] if outcome.alignment else None,
                alignment_signals=outcome.alignment_signals if outcome.alignment else None,
                topic=session.current_topic,
                topic_notice=topic_notice,
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
            if rating is not UNSET and rating is not None:
                record.topic = session.current_topic  # a rating made now counts toward this topic
            if comment is not UNSET:
                record.comment = clean_comment(comment)
            record.updated_at = now
            if record.rating is None and not record.comment:
                session.ratings.pop(result_id, None)
            else:
                session.ratings[result_id] = record
            # a changed or deleted comment makes the readings cached for it stale
            current = comment_key(result_id, record.comment) if record.comment.strip() else ""
            for key in [k for k in session.comment_cache if k.startswith(f"{result_id}|")]:
                if key != current:
                    del session.comment_cache[key]
            answer = {"result_id": result_id, "rating": record.rating, "comment": record.comment}
        if comment is not UNSET and record.comment.strip():
            self.prep.submit_comment(session_id, result_id, record.comment)
        if rating in ("love", "like"):  # look its store link up in the background (never a dislike)
            self.ahead.submit(session_id, result_id)
        return answer

    # ---- resolve --------------------------------------------------------------------------

    def resolve(self, session_id: Any, result_id: Any) -> dict[str, Any]:
        if not isinstance(result_id, str):
            raise ApiError(400, "result_id must be text")
        self._load(session_id)
        assert isinstance(session_id, str)
        try:
            with (
                self.ahead.key_lock(session_id, result_id),
                self.store.transaction(session_id) as session,
            ):
                link = resolve(session, result_id, self.lookup_provider, self.config)
                shown = session.results[result_id]
                return {
                    "rating": {"rating": shown.rating, "rating_count": shown.rating_count},
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

    # ---- more photos ----------------------------------------------------------------------

    def photos(self, session_id: Any, result_id: Any) -> dict[str, Any]:
        """Up to `MAX_GALLERY_SHOWN` product photos of one shown result, from the same SerpApi
        product lookup Open uses (cached per result in the session, counted once against the
        per-session cap). If Open or an earlier gallery already looked it up, this costs nothing."""
        if not isinstance(result_id, str):
            raise ApiError(400, "result_id must be text")
        self._load(session_id)
        assert isinstance(session_id, str)
        if not self.config.shop_gallery:
            raise ApiError(404, "not found")  # the gallery is off (APP_SHOP_GALLERY)
        try:
            with (
                self.ahead.key_lock(session_id, result_id),
                self.store.transaction(session_id) as session,
            ):
                result = session.results.get(result_id)
                if result is None:
                    raise ApiError(404, "unknown result")
                if not result.product_id:
                    raise ApiError(400, "This product has no photo gallery.")
                known = result_id in session.product_cache
                if not known and session.lookups_used >= self.config.shop_lookups_per_session:
                    raise ApiError(
                        429,
                        f"Lookup limit for this session reached ({session.lookups_used} of "
                        f"{self.config.shop_lookups_per_session}); no more photos can be loaded.",
                    )
                details, error, fresh = product_lookup(
                    session, result, self.lookup_provider, self.config
                )
                if fresh:
                    session.lookup_usd = round(
                        session.lookup_usd + self.config.serpapi_usd_per_search, 6
                    )
                fill_rating(session, result, details)
                images = [u for u in (details.images if details else []) if is_https_url(u)]
                message = ""
                unavailable = False
                if error is not None:
                    message = "The photos could not be loaded for this product."
                elif len(images) < MIN_GALLERY:
                    # the lookup worked but has almost nothing to show: cached, never asked twice
                    unavailable = True
                    message = "No more photos were found for this product."
                shown = session.results[result_id]
                return {
                    "images": images[:MAX_GALLERY_SHOWN] if not unavailable else [],
                    "message": message,
                    "unavailable": unavailable,
                    "image_fields": dict(details.image_fields) if details else {},
                    "fresh": fresh,
                    "lookups_used": session.lookups_used,
                    "lookups_cap": self.config.shop_lookups_per_session,
                    "rating": {"rating": shown.rating, "rating_count": shown.rating_count},
                }
        except ProviderError as exc:
            raise ApiError(502, str(exc)) from None

    # ---- shop again and preferences -------------------------------------------------------

    def _prepare_again(self, session_id: Any) -> Session:
        """Validate a Shop Again request and return the session it will run on."""
        session = self._load(session_id)
        if rated_count(session) == 0:
            raise ApiError(400, "Rate a few results first")
        if not any(r.kind == "search" for r in session.searches):
            raise ApiError(400, "Search first")
        if session.shop_agains_used >= self.config.shop_agains_per_session:
            raise ApiError(
                429,
                f"Shop Again limit reached ({self.config.shop_agains_per_session} per session)",
            )
        return session

    def _await_prep(
        self, session_id: str, session: Session, progress: Callable[[str], None] | None
    ) -> Session:
        """Give the background preparation up to `shop_prep_wait_s` to finish ("Finishing your
        last ratings (n of m)"), then carry on with whatever is cached: the live path covers
        anything missing. Returns the session as the preparation left it; never raises and never
        blocks longer than that."""
        done, total = self.prep.outstanding(session_id)
        if not self.prep.enabled or done >= total:
            return session
        steps = progress if isinstance(progress, Progress) else None
        if steps is not None:
            steps.begin("ratings", total=total)

        def report(finished: int, submitted: int) -> None:
            if progress is not None:
                progress(f"Finishing your last ratings ({finished} of {submitted})")
            if steps is not None:
                steps.set_total("ratings", submitted)
                steps.advance("ratings", max(0, finished - (steps.steps[0].done_count or 0)))

        self.prep.wait(session_id, self.config.shop_prep_wait_s, report)
        try:
            return self.store.load(session_id)
        except SessionError:
            return session

    def _do_again(
        self,
        session_id: str,
        session: Session,
        progress: Callable[[str], None] | None = None,
        refine_text: str = "",
    ) -> dict[str, Any]:
        refine_note = ""
        if refine_text:
            steps = progress if isinstance(progress, Progress) else None
            if steps is not None:
                steps.begin("refine")
            try:
                self.refine(session_id, refine_text)
            except ApiError as exc:
                refine_note = f"Your refinement was not applied: {exc.message}"
            if steps is not None:
                steps.finish("refine")
            session = self.store.load(session_id)
        started_topic = session.current_topic
        session = self._await_prep(session_id, session, progress)
        try:
            outcome, profile, query = run_shop_again(
                session, self.search_provider, self.config, self.thumbnailer, self.clock, progress,
                self.fingerprinter,
            )  # fmt: skip
        except ProviderError as exc:
            raise ApiError(502, str(exc)) from None
        except BudgetExceededError as exc:
            raise ApiError(429, str(exc)) from None
        with self.store.transaction(session_id) as live:
            if live.current_topic != started_topic:
                # a new topic began while this ran: nothing learned for the old one may land in it
                raise ApiError(
                    409, "You started a new search while this ran; its result was dropped"
                )
            if live.shop_agains_used >= self.config.shop_agains_per_session:
                raise ApiError(429, "Shop Again limit for this session reached")
            # ratings made while the search ran are kept; the profile replaces the old one except
            # that entries deleted meanwhile stay deleted
            if live.profile is not None:
                profile = with_pins(
                    reapply_removed(profile, live.profile.removed, live.profile.dismissed),
                    live.profile.pinned_sellers,
                )
            live.profile = profile
            live.shop_agains_used += 1
            if refine_note:
                outcome.warnings.insert(0, refine_note)
            for rid, tags in outcome.new_tags.items():  # a result is tagged once; a photo tag stays
                if live.tag_source.get(rid) != "photo":
                    live.tag_cache[rid] = tags
                    live.tag_source[rid] = "title"
            for peer in outcome.peer_log:  # rotate: what was searched is remembered
                row = live.searched_peers.setdefault(
                    peer["key"], {"name": peer["peer"], "searches": 0, "new": 0, "empty": True}
                )
                row["searches"] += 1
                row["new"] += peer["new_candidates"]
                row["empty"] = peer["new_candidates"] == 0
            self._record(live, live.searches[-1].request if live.searches else "", outcome,
                         note=profile_summary(profile))  # fmt: skip
            view = self._view(live)
        self.prep.submit_batch(
            session_id, outcome.results
        )  # the new batch is prepared as you browse
        return view

    def reset_session(self, session_id: Any) -> dict[str, Any]:
        """Start over: stop the background preparation for this session."""
        if not isinstance(session_id, str) or not self.store.exists(session_id):
            raise ApiError(404, "unknown session")
        self.prep.cancel(session_id)
        self.ahead.cancel(session_id)
        return {"ok": True}

    def shop_again(self, session_id: Any) -> dict[str, Any]:
        """A fresh set of results shaped by the ratings (see `app.shop.again`)."""
        session = self._prepare_again(session_id)
        assert isinstance(session_id, str)
        return self._do_again(session_id, session)

    def start_shop_again(self, session_id: Any, refine_text: Any = None) -> dict[str, Any]:
        """Start a Shop Again as a background job: `{"job_id": ...}` (poll `job_status`). Text left
        in the refine box is interpreted first, as an extra step ("Reading your refinement")."""
        session = self._prepare_again(session_id)
        assert isinstance(session_id, str)
        text = ""
        if refine_text is not None:
            if not isinstance(refine_text, str) or len(refine_text) > 1000:
                raise ApiError(400, "refinement must be short text")
            text = clean_instruction(refine_text)
        steps = SHOP_AGAIN_REFINE_STEPS if text else None
        return {
            "job_id": self._start(
                "shop_again",
                session_id,
                lambda say: self._do_again(session_id, session, say, text),
                steps,
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
                remove_entry(
                    session.profile,
                    kind,
                    phrase,
                    rated_count(session),
                )
                if session.profile
                else None
            )
            if updated is None:
                raise ApiError(404, "no such preference")
            session.profile = updated
            return {
                "profile": updated.model_dump(mode="json"),
                "profile_summary": profile_summary(updated),
                "removed": normalize_phrase(phrase),
            }

    def _topic_of(self, session: Session) -> Topic:
        topic = current_topic(session)
        if topic is None:
            raise ApiError(400, "Search first")
        return topic

    def refine(self, session_id: Any, text: Any) -> dict[str, Any]:
        """Interpret one plain instruction and add its chips to the current topic. Rejects empty or
        nonsense input with a short message. The instruction is untrusted text (see
        `app.shop.refine`)."""
        if not isinstance(text, str):
            raise ApiError(400, "type an instruction first")
        cleaned = clean_instruction(text)
        if not cleaned:
            raise ApiError(400, "type an instruction first")
        session = self._load(session_id)
        assert isinstance(session_id, str)
        self._topic_of(session)
        budget = Budget(self.config)
        try:
            result = interpret(cleaned, budget, self.config)
        except BudgetExceededError as exc:
            raise ApiError(429, str(exc)) from None
        with self.store.transaction(session_id) as live:
            live.refine_usd = round(live.refine_usd + result.usd, 6)
            if not result.chips:
                self.store.save(live)  # keep the cost of a call that found nothing
                raise ApiError(400, result.message or REJECTED)
            topic = self._topic_of(live)
            topic.refinements, notices = add_chips(topic.refinements, result.chips)
            chips = [c.model_dump(mode="json") for c in topic.refinements]
        return {"refine": chips, "notices": notices, "refine_usd": live.refine_usd}

    def remove_refine(self, session_id: Any, chip_id: Any) -> dict[str, Any]:
        """Delete one refine chip: its effect is gone from the next Shop Again."""
        if not isinstance(chip_id, str) or not chip_id or len(chip_id) > 40:
            raise ApiError(400, "chip id must be short text")
        self._load(session_id)
        assert isinstance(session_id, str)
        with self.store.transaction(session_id) as live:
            topic = self._topic_of(live)
            kept = [c for c in topic.refinements if c.id != chip_id]
            if len(kept) == len(topic.refinements):
                raise ApiError(404, "no such refinement")
            topic.refinements = kept
            return {"refine": [c.model_dump(mode="json") for c in kept]}

    def remove_context(self, session_id: Any, kind: Any, value: Any) -> dict[str, Any]:
        """Delete one chip of the request's setting (stage 2k): a context item (its colour effect
        goes with it when no colour-bearing item is left), a suggested colour (it leaves the search
        and the ranking) or a colour to avoid (it is no longer demoted). It applies to the current
        topic's typed request, so it holds for every later Shop Again."""
        if kind not in ("context", "suggested", "avoid"):
            raise ApiError(400, "kind must be context, suggested or avoid")
        if not isinstance(value, str) or not value.strip() or len(value) > 40:
            raise ApiError(400, "value must be short text")
        self._load(session_id)
        assert isinstance(session_id, str)
        with self.store.transaction(session_id) as session:
            typed = [
                s
                for s in session.searches
                if s.kind == "search" and s.topic == session.current_topic
            ]
            if not typed:
                raise ApiError(404, "no such chip")
            base = typed[-1].query
            text = " ".join(value.lower().split())
            if kind == "context":
                kept = [i for i in base.context if i.value != text]
                if len(kept) == len(base.context):
                    raise ApiError(404, "no such chip")
                update: dict[str, Any] = {"context": kept}
                if not any(i.kind in COLOR_CONTEXT_KINDS for i in kept):
                    update |= {"suggested_colors": [], "avoid_colors": [], "color_reason": ""}
            else:
                color = normalize("color", text)
                field = "suggested_colors" if kind == "suggested" else "avoid_colors"
                if color is None or color not in getattr(base, field):
                    raise ApiError(404, "no such chip")
                update = {field: [c for c in getattr(base, field) if c != color]}
            for record in session.searches:
                if record.topic == session.current_topic and record.query.request == base.request:
                    record.query = record.query.model_copy(update=update)
                    if record.used_query is not None:
                        record.used_query = record.used_query.model_copy(update=update)
            return {
                "query": base.model_copy(update=update).model_dump(mode="json"),
                "removed": text,
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
