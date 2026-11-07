"""Clarifying questions inside a first typed search (stage Q1, ADR 0017).

The spike modules (`clarify`, `clarify_verify`) turn listings into questions and answers into a
`ShopQuery`. This module puts them into the search job, behind `Config.shop_clarify`:

1. While the request is parsed, page 1 of the raw request is fetched (`Prefetch`). If the parsed
   search text is materially the same, the pipeline reuses that page as its own page 1; if not, the
   page still feeds the question call and is billed.
2. A side thread reads the first listings and asks for the questions (one model call). The job's
   status then shows them (`state: ready`, text only); the same thread checks the option pictures
   (vision calls) and shows the verified picture URLs on a later poll (`pictures_state: done`).
3. The pipeline goes on in the job's own thread. At "ready to rank" (`ClarifyRun.wait_at_gate`,
   called from `run_query` right before the pool is cut) it waits by the policy below, applies the
   answers (`apply_answers`) and ranks. That is the apply point; after it the answers are final.

Stage B2 (question 1 fast): question 1 comes from the request text alone, in parallel with the
parse and the fetch (`clarify_first`: a category template, else one tiny model call), and is shown
at once. Questions 2 to 5 are written from the listings once they arrive and are appended while the
shopper answers question 1 (`more_coming`). Pictures never hold a question back: an option with an
icon or swatch (`clarify_visual`) gets no picture search, and the few that need a real thumbnail
get it later through the strict title match and vision check.

Wait policy (`Config.clarify_idle_grace_s`, `clarify_max_wait_s`): no wait when the questions
failed or were skipped; questions still being written wait at most the grace; the clock runs from
the moment question 1 was shown, not from ready-to-rank: with no interaction the pipeline holds
until the grace has passed since then, and once the shopper has answered or touched the page
(`touched`) until the submit, but never longer than `clarify_max_wait_s` since then. It also waits
up to `MORE_COMING_WAIT_S` for the remaining questions to be appended. A failure ends the wait.

Nothing here raises into the search: a failure marks the run `failed` (or `skipped`), is logged
without detail the visitor could see, and the search proceeds exactly as it does without the flag.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.config import BudgetExceededError, Config
from app.llm import Budget, complete
from app.shop.clarify import (
    MAX_QUESTIONS,
    Answer,
    Question,
    Questions,
    analyze_facets,
    map_answers,
    pick_option_images,
    words,
)
from app.shop.clarify_first import first_question, stated_dimensions
from app.shop.clarify_mock import mock_caller, mock_first_caller, mock_verify_caller, priced
from app.shop.clarify_verify import verify_and_prune
from app.shop.clarify_visual import is_feature_label, needs_picture, option_visual
from app.shop.clock import Clock
from app.shop.models import ShopQuery
from app.shop.parse import parse_request
from app.shop.progress import Progress
from app.shop.search import SearchOutcome, query_key, run_query
from app.shop.store import ClarifyAnswer, ClarifyRecord, topic_key
from app.shopping import DEFAULT_GL, DEFAULT_HL, ProviderError, SearchCandidate, SearchProvider
from app.shopping_retrieval import _record_provider_call
from app.thumbnails import Thumbnailer

log = logging.getLogger("app.shop.clarify")

NO_PREFERENCE = "no_preference"
MAX_ANSWER_POSTS = 60  # answer updates one job takes (a page posts a few; this stops a loop)
SIDE_THREAD_JOIN_S = 15.0  # how long the end of a search waits for the picture checks to stop
SAME_SEARCH_OVERLAP = 0.75  # word overlap above which the parsed search text is "the same"
MORE_COMING_WAIT_S = 10.0  # how long the gate waits for questions 2..5 to be appended
STAGE_WAIT_S = 20.0  # how long a side stage waits for the parse or the first question

PENDING, READY, SKIPPED, FAILED = "pending", "ready", "skipped", "failed"


class ClarifyInvalid(ValueError):
    """The answers do not fit the stored questions (HTTP 400)."""


class ClarifyConflict(RuntimeError):
    """The apply point has passed, or there are no questions to answer (HTTP 409)."""


class ClarifyRateLimited(RuntimeError):
    """Too many answer updates for one job (HTTP 429)."""


@dataclass
class Gate:
    """What `ClarifyRun.wait_at_gate` hands the pipeline."""

    query: ShopQuery | None = None  # the parse with the answers applied (None: no change)
    waited: float = 0.0
    reason: str = ""  # submit | idle_grace | max_wait | failed | skipped | too_slow
    mapped: list[dict[str, str]] = field(default_factory=list)


class ClarifyRun:
    """The clarify state of one search job: the questions, the pictures, the answers and the wait.

    Thread safe. The side thread fills it (`set_ready`, `set_pictures`, `fail`, `skip`), the page's
    requests change it (`answer`), the pipeline reads it (`wait_at_gate`), the job status shows it
    (`snapshot`)."""

    def __init__(self, config: Config, now: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self._now = now
        self._cond = threading.Condition()
        self.state = PENDING
        self.reason = ""
        self.questions: list[Question] = []
        self.pictures: dict[tuple[str, str], str] = {}
        self.pictures_state = "pending"
        self.answers: dict[str, str] = {}
        self.submitted = False
        self.applied = False
        self.applied_answers: dict[str, str] = {}
        self.posts = 0
        self.touched = False
        self.more_coming = False
        self.source = ""  # where question 1 came from: template | model
        self.job_id = ""
        self.created = now()
        self.more_done_at: float | None = None
        self.ready_at: float | None = None
        self.pictures_at: float | None = None
        self.gate_at: float | None = None
        self.applied_at: float | None = None
        self.cancelled = threading.Event()  # the apply point passed: stop the picture checks
        self.trace: dict[str, Any] = {}

    # ---- the side thread --------------------------------------------------------------------

    def set_ready(
        self, questions: list[Question], *, more_coming: bool = False, source: str = ""
    ) -> bool:
        """Show the first questions (question 1, or the whole set when question 1 failed)."""
        with self._cond:
            if self.applied or self.state != PENDING:
                return False
            self.questions, self.state = questions, READY
            self.more_coming, self.source = more_coming, source
            self.ready_at = self._now()
            if not more_coming:
                self.more_done_at = self.ready_at
            self._cond.notify_all()
            return True

    def append_questions(self, questions: list[Question]) -> bool:
        """Add questions 2..5 to the ones shown. Ignored once the apply point has passed."""
        with self._cond:
            if self.applied or self.state != READY:
                return False
            room = MAX_QUESTIONS - len(self.questions)
            self.questions = [*self.questions, *questions[: max(0, room)]]
            self._cond.notify_all()
            return True

    def finish_more(self) -> None:
        """No more questions are coming (appended, none to add, or the call failed)."""
        with self._cond:
            if self.more_coming:
                self.more_coming = False
                self.more_done_at = self._now()
            self._cond.notify_all()

    def asked_dimensions(self) -> list[str]:
        with self._cond:
            return [q.dimension for q in self.questions]

    def questions_copy(self) -> list[Question]:
        with self._cond:
            return list(self.questions)

    def question_count(self) -> int:
        with self._cond:
            return len(self.questions)

    def set_pictures(self, pictures: dict[tuple[str, str], str]) -> None:
        with self._cond:
            if not self.applied:  # after the apply point nobody can use them
                self.pictures = pictures
                if pictures and self.pictures_at is None:
                    self.pictures_at = self._now()
            self.pictures_state = "done"

    def fail(self, reason: str) -> None:
        with self._cond:
            if self.state in (PENDING, READY) and not self.applied:
                self.state, self.reason = FAILED, reason
            self.pictures_state = "done"
            self._cond.notify_all()

    def skip(self, reason: str) -> None:
        with self._cond:
            if self.state == PENDING:
                self.state, self.reason = SKIPPED, reason
            self.pictures_state = "done"
            self._cond.notify_all()

    # ---- the page's answers -----------------------------------------------------------------

    def answer(self, items: Any, submit: Any, touched: Any = False) -> dict[str, Any]:
        """Record answers (`[{question_id, option_value | "no_preference"}]`), maybe the submit, and
        maybe `touched` (the shopper is interacting: the wait becomes the long one even with no
        answer yet).
        A later answer to the same question replaces the earlier; ids that are not a question of
        this job are ignored; an option value that is not one of the question's is refused. Posting
        the same thing again changes nothing. Past the apply point it is a conflict, except for a
        repeat of what was applied."""
        if not isinstance(items, list) or len(items) > 20:
            raise ClarifyInvalid("answers must be a list of at most 20")
        if not isinstance(submit, bool):
            raise ClarifyInvalid("submit must be true or false")
        if not isinstance(touched, bool):
            raise ClarifyInvalid("touched must be true or false")
        parsed: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                raise ClarifyInvalid("each answer must be an object")
            qid, value = item.get("question_id"), item.get("option_value")
            if not isinstance(qid, str) or not isinstance(value, str) or len(qid) > 60:
                raise ClarifyInvalid("each answer needs question_id and option_value as text")
            parsed[qid] = value
        with self._cond:
            if self.applied:
                known = {q.id for q in self.questions}
                same = all(
                    self.applied_answers.get(k) == v for k, v in parsed.items() if k in known
                )
                if same and (not submit or self.submitted):
                    return self._snapshot()
                raise ClarifyConflict("The questions are already applied")
            if self.state != READY:
                raise ClarifyConflict("There are no questions to answer")
            self.posts += 1
            if self.posts > MAX_ANSWER_POSTS:
                raise ClarifyRateLimited("Too many answer updates for this search")
            by_id = {q.id: q for q in self.questions}
            clean: dict[str, str] = {}
            for qid, value in parsed.items():
                q = by_id.get(qid)
                if q is None:
                    continue  # not one of this job's questions: ignored
                if value != NO_PREFERENCE and value not in {o.value for o in q.options}:
                    raise ClarifyInvalid(f"{value!r} is not an option of question {qid}")
                clean[qid] = value
            self.answers.update(clean)
            if touched:
                self.touched = True
            if submit:
                self.submitted = True
            self._cond.notify_all()
            return self._snapshot()

    def close(self) -> None:
        """The apply point has passed (or the search ended): stop the picture checks, take no more
        answers."""
        self.cancelled.set()
        with self._cond:
            self.applied = True
            self._cond.notify_all()

    # ---- the apply point --------------------------------------------------------------------

    def _deadline(self, start: float) -> float | None:
        """When the wait ends, for the state now (None: no wait)."""
        if self.state == PENDING:
            return start + self.config.clarify_idle_grace_s
        if self.state != READY or self.submitted:
            return None
        assert self.ready_at is not None
        if self.answers or self.touched:  # the clock runs from question 1, not from `start`
            return self.ready_at + self.config.clarify_max_wait_s
        return self.ready_at + self.config.clarify_idle_grace_s

    def wait_at_gate(self, parsed: ShopQuery) -> Gate:
        """Called by the pipeline when it is ready to rank: wait by the policy, then freeze the
        answers and return the query with them applied."""
        start = self._now()
        with self._cond:
            self.gate_at = start
            reason = ""
            while True:
                deadline = self._deadline(start)
                if deadline is None:
                    reason = "submit" if self.state == READY else self.state
                    break
                left = deadline - self._now()
                if left <= 0:
                    more_left = start + MORE_COMING_WAIT_S - self._now()
                    if self.state == READY and self.more_coming and more_left > 0:
                        self._cond.wait(more_left)  # the rest of the questions are on their way
                        continue
                    if self.state == PENDING:
                        self.state, self.reason = SKIPPED, "the questions were not ready in time"
                        reason = "too_slow"
                    else:
                        reason = "max_wait" if self.answers or self.touched else "idle_grace"
                    break
                self._cond.wait(left)
            self.applied = True
            self.applied_at = self._now()
            self.applied_answers = dict(self.answers)
            questions = list(self.questions)
            self._cond.notify_all()
        self.cancelled.set()
        waited = round(max(0.0, self.applied_at - start), 3)
        chosen = [
            Answer(q, next((o for o in q.options if o.value == self.applied_answers[q.id]), None))
            for q in questions
            if q.id in self.applied_answers
        ]
        mapped: list[dict[str, str]] = []
        query: ShopQuery | None = None
        if chosen:
            applied = map_answers(parsed, chosen)
            mapped = applied.mapped
            if mapped:
                query = applied.query
        return Gate(query=query, waited=waited, reason=reason, mapped=mapped)

    # ---- what the page sees -----------------------------------------------------------------

    def _snapshot(self) -> dict[str, Any]:
        now = self._now()
        deadline = None
        waiting = self.gate_at is not None and not self.applied
        if waiting and self.gate_at is not None:
            deadline = self._deadline(self.gate_at)
        out: dict[str, Any] = {
            "state": self.state,
            "questions": [self._question_view(q) for q in self.questions]
            if self.state == READY
            else [],
            "more_coming": self.more_coming and not self.applied,
            "pictures_state": self.pictures_state,
            "no_preference_value": NO_PREFERENCE,
            "answers": [{"question_id": k, "option_value": v} for k, v in self.answers.items()],
            "submitted": self.submitted,
            "applied": self.applied,
            "deadline": {
                "idle_grace_s": self.config.clarify_idle_grace_s,
                "max_wait_s": self.config.clarify_max_wait_s,
                "waiting": waiting,
                "seconds_left": round(max(0.0, deadline - now), 1)
                if deadline is not None
                else None,
                "shown_s_ago": round(now - self.ready_at, 1) if self.ready_at is not None else None,
            },
            "timing": self.timing(),
        }
        return out

    def _question_view(self, q: Question) -> dict[str, Any]:
        options = []
        for o in q.options:
            visual = option_visual(q.dimension, o.label)
            drawn = visual["icon"] is not None or visual["swatches"] is not None
            options.append(
                {
                    "label": o.label,
                    "value": o.value,
                    "icon": visual["icon"],
                    "swatches": visual["swatches"],
                    "picture_url": None if drawn else self.pictures.get((q.id, o.value)),
                }
            )
        return {
            "id": q.id,
            "text": q.text,
            "kind": q.kind,
            "dimension": q.dimension,
            "picture_eligible": any(needs_picture(q.dimension, o.label) for o in q.options),
            "options": options,
        }

    def timing(self) -> dict[str, Any]:
        """Milliseconds from the job's start, for Max's own searches (`clarify_timing` log line)."""

        def ms(at: float | None) -> int | None:
            return None if at is None else round((at - self.created) * 1000)

        return {
            "q1_ms": ms(self.ready_at),
            "rest_ms": ms(self.more_done_at),
            "first_pictures_ms": ms(self.pictures_at),
            "source": self.source or "none",
        }

    def snapshot(self) -> dict[str, Any]:
        with self._cond:
            return self._snapshot()


# ---- the first page, fetched while the request is parsed -------------------------------------


def _words(text: str) -> set[str]:
    return set(words(text))


def materially_same(raw: str, parsed_text: str) -> bool:
    """Whether the parsed search text would fetch about the page the raw request did: the same
    words, or all but a few (the parse drops "under $500" and adds nothing the shopper did not
    say)."""
    a, b = _words(raw), _words(parsed_text)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= SAME_SEARCH_OVERLAP


class Prefetch:
    """Page 1 of the raw request, fetched in a thread; the pipeline takes it once."""

    search_timeout = 20.0

    def __init__(self, search: SearchProvider, text: str, clock: Clock) -> None:
        self.search, self.text, self.clock = search, text, clock
        self.found: list[SearchCandidate] | None = None
        self.error: Exception | None = None
        self.seconds = 0.0
        self.consumed = False
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="shop-clarify-fetch")
        self._thread.start()

    def _run(self) -> None:
        started = self.clock.now()
        try:
            self.found = self.search.search(self.text, page=1)
        except ProviderError as exc:
            self.error = exc
        except Exception as exc:  # a provider bug must not become an unhandled thread error
            self.error = ProviderError(self.search.name, type(exc).__name__)
        finally:
            self.seconds = round(self.clock.now() - started, 3)
            self._done.set()

    def result(self, timeout: float | None = None) -> list[SearchCandidate] | None:
        self._done.wait(timeout)
        return self.found if self._done.is_set() else None

    def settle(self, budget: Budget) -> int:
        """Bill the fetch when the pipeline did not use it (a used one is billed as its own page 1);
        returns the Serper calls to add to the search's count."""
        self._done.wait(self.search_timeout)
        if self.consumed:
            return 0
        config = budget.config
        usd = (
            config.serper_usd_per_call
            if self.search.name == "serper"
            else (config.serpapi_usd_per_search)
        )
        _record_provider_call(
            budget, "search", "shop p1 (clarify)", self.search.name, usd, self.seconds,
            ok=self.error is None,
        )  # fmt: skip
        return 1


class PrefetchedSearch:
    """The provider the pipeline sees: page 1 of `text` is the prefetched page (once); every
    other call goes to the real provider."""

    def __init__(self, inner: SearchProvider, prefetch: Prefetch, text: str) -> None:
        self.inner, self.prefetch, self.text = inner, prefetch, text
        self.name = inner.name
        self.thread_safe = getattr(inner, "thread_safe", False)
        self._lock = threading.Lock()

    def search(
        self, query: str, gl: str = DEFAULT_GL, hl: str = DEFAULT_HL, page: int = 1
    ) -> list[SearchCandidate]:
        with self._lock:
            take = (
                page == 1
                and not self.prefetch.consumed
                and query_key(query) == query_key(self.text)
            )
            if take:
                self.prefetch.consumed = True
        if take:
            got = self.prefetch.result(self.prefetch.search_timeout)
            if got is not None:
                return got
            if self.prefetch.error is not None:
                raise self.prefetch.error
            self.prefetch.consumed = False  # too slow: fetch it again; the early call is billed
        return self.inner.search(query, gl=gl, hl=hl, page=page)


# ---- the question stage (the side thread) ----------------------------------------------------


@dataclass
class QuestionStageResult:
    options_total: int = 0
    dropped_by_title: int = 0
    dropped_questions: int = 0
    pictures: int = 0
    dropped_by_picture: int = 0  # options a picture check contradicted: they stay as text only
    question_usd: float = 0.0
    verify_usd: float = 0.0
    question_seconds: float = 0.0
    verify_seconds: float = 0.0
    verify_calls: int = 0


def prune_by_title(
    questions: Questions, candidates: Sequence[SearchCandidate], category: str
) -> tuple[list[Question], int, int]:
    """Instant, free text-phase pruning: an option no listing's title shows is dropped, and a
    question left with fewer than two options goes. Returns (questions, options dropped,
    questions dropped)."""
    picks = pick_option_images(questions, candidates, category=category)
    kept: list[Question] = []
    dropped_options = dropped_questions = 0
    for q in questions.questions:
        keep = [o for i, o in enumerate(q.options) if picks.title_evidence.get((q.id, i), 0) > 0]
        dropped_options += len(q.options) - len(keep)
        if len(keep) < 2:
            dropped_questions += 1
            dropped_options += len(keep)
            continue
        kept.append(q.model_copy(update={"options": keep}))
    return kept, dropped_options, dropped_questions


def stated_by(request: str, parsed: ShopQuery) -> set[str]:
    """The dimensions the request already decides: from the parse and from the raw text."""
    out = stated_dimensions(request)
    for dim, terms in (
        ("color", parsed.color_terms),
        ("material", parsed.material_terms),
        ("shape", parsed.shape_terms),
        ("style", parsed.style_terms),
    ):
        if terms:
            out.add(dim)
    return out


def finish_rest(
    questions: Sequence[Question], skip: set[str], room: int, first_number: int
) -> list[Question]:
    """The listing-grounded questions as appended: no dimension already asked or stated, one
    question per dimension, no feature word inside another dimension's question (the option goes;
    a question left with fewer than two options goes), at most `room`, ids `q<first_number>`..."""
    seen = set(skip)
    kept: list[Question] = []
    for q in questions:
        if q.dimension in seen:
            continue
        options = q.options
        if q.dimension != "feature":
            options = [o for o in options if not is_feature_label(o.label)]
        if len(options) < 2:
            continue
        seen.add(q.dimension)
        kept.append(q.model_copy(update={"options": options}))
    return [q.model_copy(update={"id": f"q{first_number + i}"}) for i, q in enumerate(kept[:room])]


def run_first_stage(
    run: ClarifyRun,
    request: str,
    budget: Budget,
    *,
    caller: Any,
    parse_done: threading.Event | None,
) -> None:
    """Question 1 from the request text alone, shown at once. Never raises. With `parse_done` the
    question waits for the parse first (a repeat topic that may already be answered)."""
    try:
        got = first_question(request, budget, caller=caller)
        if got.question is None:
            return
        if parse_done is not None:
            parse_done.wait(STAGE_WAIT_S)
        if run.cancelled.is_set():
            return
        run.set_ready([got.question], more_coming=True, source=got.source)
    except BudgetExceededError:
        log.warning("clarify: budget stopped the first question")
    except Exception as exc:
        log.warning("clarify: first question failed (%s)", type(exc).__name__)


def run_question_stage(
    run: ClarifyRun,
    request: str,
    parsed: ShopQuery,
    candidates: Sequence[SearchCandidate],
    budget: Budget,
    *,
    config: Config,
    thumbnailer: Thumbnailer | None,
    question_caller: Any,
    verify_caller: Any,
) -> QuestionStageResult:
    """Questions 2..5 from the listings (appended to question 1), then the pictures only the few
    options with no icon or swatch need. Never raises, never fails a run that has question 1."""
    out = QuestionStageResult()
    try:
        asked = run.asked_dimensions()
        room = MAX_QUESTIONS - len(asked)
        if room <= 0:
            run.finish_more()
            return out
        facets = analyze_facets(
            request, parsed, candidates, budget=budget, caller=question_caller,
            limit=config.clarify_candidates, asked=asked, max_questions=room,
        )  # fmt: skip
        out.question_usd, out.question_seconds = facets.cost_usd, facets.seconds
        skip = {*asked, *stated_by(request, parsed)}
        cleaned = finish_rest(facets.questions, skip, room, len(asked) + 1)
        shown, out.dropped_by_title, out.dropped_questions = prune_by_title(
            facets.model_copy(update={"questions": cleaned}), candidates, parsed.category
        )
        out.options_total = sum(len(q.options) for q in shown)
        if run.cancelled.is_set():
            run.skip("the apply point passed first")
            run.finish_more()
            return out
        if shown:
            if not run.append_questions(shown):
                run.set_ready(shown)  # question 1 never came: these are the first
        elif run.question_count() == 0:
            run.skip("; ".join(facets.notes) or "the model asked nothing")
        run.finish_more()
    except BudgetExceededError as exc:
        log.warning("clarify: budget stopped the question call (%s)", type(exc).__name__)
        run.finish_more()
        if run.question_count() == 0:
            run.fail("budget")
        return out
    except Exception as exc:
        log.warning("clarify: question stage failed (%s)", type(exc).__name__)
        run.finish_more()
        if run.question_count() == 0:
            run.fail("question stage error")
        return out
    run_picture_stage(run, parsed, candidates, budget, thumbnailer, verify_caller, out)
    return out


def run_picture_stage(
    run: ClarifyRun,
    parsed: ShopQuery,
    candidates: Sequence[SearchCandidate],
    budget: Budget,
    thumbnailer: Thumbnailer | None,
    verify_caller: Any,
    out: QuestionStageResult,
) -> None:
    """Real thumbnails for the options that have neither an icon nor a swatch, through the strict
    title match and the vision check. Runs after the questions are shown; never blocks them."""
    try:
        offered = [
            q.model_copy(
                update={"options": [o for o in q.options if needs_picture(q.dimension, o.label)]}
            )
            for q in run.questions_copy()
        ]
        offered = [q for q in offered if len(q.options) >= 2]
        if not offered or run.cancelled.is_set():
            run.set_pictures({})
            return
        verified = verify_and_prune(
            Questions(questions=offered), candidates, category=parsed.category,
            thumbnailer=thumbnailer, budget=budget, caller=verify_caller,
        )  # fmt: skip
        out.verify_usd, out.verify_seconds = verified.cost_usd, verified.seconds
        out.verify_calls = verified.calls
        pictures: dict[tuple[str, str], str] = {}
        for q in verified.questions.questions:
            for i, option in enumerate(q.options):
                pick = verified.picks.by_option.get((q.id, i))
                if pick is not None and pick.thumbnail_url.startswith("https://"):
                    pictures[(q.id, option.value)] = pick.thumbnail_url
        out.pictures = len(pictures)
        out.dropped_by_picture = sum(1 for r in verified.options if r.status == "dropped")
        run.set_pictures(pictures)
    except BudgetExceededError:
        log.warning("clarify: budget stopped the picture checks")
        run.set_pictures({})
    except Exception as exc:  # the text questions stay usable without pictures
        log.warning("clarify: picture checks failed (%s)", type(exc).__name__)
        run.set_pictures({})


# ---- the search ------------------------------------------------------------------------------


def stored_answers(record: ClarifyRecord | None) -> list[Answer]:
    """The stored answers as `Answer`s (a stored answer whose option is gone is skipped)."""
    if record is None:
        return []
    out: list[Answer] = []
    questions: dict[str, Question] = {}
    for raw in record.questions:
        try:
            q = Question.model_validate(raw)
        except ValueError:
            continue
        questions[q.id] = q
    for a in record.answers:
        stored_q = questions.get(a.question_id)
        if stored_q is None:
            continue
        q = stored_q
        if a.option_value == NO_PREFERENCE:
            out.append(Answer(q, None))
            continue
        option = next((o for o in q.options if o.value == a.option_value), None)
        if option is not None:
            out.append(Answer(q, option))
    return out


def apply_stored(query: ShopQuery, record: ClarifyRecord | None) -> ShopQuery:
    """`query` with the stored answers applied (Shop Again, a repeat search in the same topic)."""
    answers = stored_answers(record)
    return map_answers(query, answers).query if answers else query


def clarify_record(run: ClarifyRun, parsed: ShopQuery) -> ClarifyRecord | None:
    """What the session keeps of a finished run: the questions as shown and the applied answers
    (None when there were no questions or none was answered)."""
    if run.state != READY or not run.applied_answers:
        return None
    return ClarifyRecord(
        topic_key=topic_key(parsed),
        questions=[q.model_dump(mode="json") for q in run.questions],
        answers=[
            ClarifyAnswer(question_id=k, option_value=v) for k, v in run.applied_answers.items()
        ],
    )


def run_search_clarify(
    request: str,
    search: SearchProvider,
    config: Config,
    run: ClarifyRun,
    *,
    stored: Callable[[ShopQuery], ClarifyRecord | None],
    progress: Callable[[str], None] | None = None,
    clock: Clock | None = None,
    near: Any = None,
    photos: Any = None,
    thumbnailer: Thumbnailer | None = None,
    question_caller: Any = None,
    verify_caller: Any = None,
    first_caller: Any = None,
    may_have_stored: bool = True,
) -> tuple[SearchOutcome, ShopQuery]:
    """`run_search` with clarifying questions: parse and the first fetch in parallel, the question
    stage beside the pipeline, the apply point before ranking. Returns the outcome and the parse
    without answers (what the session records as the request). Raises what `run_search` raises."""
    clock = clock or Clock()
    t0 = clock.now()
    budget = Budget(config)
    warnings: list[str] = []
    mock = config.llm_mode == "mock"
    question_caller = question_caller or (priced(mock_caller) if mock else complete)
    verify_caller = verify_caller or (priced(mock_verify_caller) if mock else complete)
    first_caller = first_caller or (priced(mock_first_caller) if mock else complete)
    if isinstance(progress, Progress):
        progress.clarify = run
        run.job_id = progress.job_id
    if progress is not None:
        progress("Reading your request")
    cleaned = request
    prefetch = Prefetch(search, cleaned, clock)
    parse_done = threading.Event()
    first_done = threading.Event()

    def first_work() -> None:
        try:
            run_first_stage(
                run, cleaned, budget, caller=first_caller,
                parse_done=parse_done if may_have_stored else None,
            )  # fmt: skip
        finally:
            first_done.set()

    # question 1 starts now, beside the parse and the fetch: it needs only the request text
    first = threading.Thread(target=first_work, daemon=True, name="shop-clarify-first")
    first.start()
    try:
        query, _, parse_warning = parse_request(cleaned, budget)
    finally:
        parse_done.set()
    if parse_warning:
        warnings.append(parse_warning)
    record = stored(query)
    side: threading.Thread | None = None
    stage = QuestionStageResult()
    if record is not None and record.answers:
        run.skip("this topic was already asked; its answers are applied")
    else:

        def side_work() -> None:
            nonlocal stage
            found = prefetch.result(prefetch.search_timeout)
            first_done.wait(STAGE_WAIT_S)  # the dimensions question 1 asked are not asked again
            if not found:
                run.finish_more()
                run.skip("no listings to read")
                return
            stage = run_question_stage(
                run, cleaned, query, found, budget, config=config, thumbnailer=thumbnailer,
                question_caller=question_caller, verify_caller=verify_caller,
            )  # fmt: skip

        side = threading.Thread(target=side_work, daemon=True, name="shop-clarify")
        side.start()
    inner = search
    reused = materially_same(cleaned, query.search_text)
    if reused:
        inner = PrefetchedSearch(search, prefetch, query.search_text)
    gate_info: dict[str, Any] = {}

    def gate() -> Gate:
        if record is not None and record.answers:
            applied = apply_stored(query, record)
            gate_info.update(reason="stored", mapped=[])
            return Gate(query=applied if applied is not query else None, reason="stored")
        got = run.wait_at_gate(query)
        gate_info.update(reason=got.reason, waited=got.waited, mapped=got.mapped)
        return got

    try:
        outcome = run_query(
            query, inner, config, budget, warnings, t0, None, clock, progress, near, photos,
            rank_gate=gate,
        )  # fmt: skip
    finally:
        run.close()  # the apply point also passes when the pipeline fails first
    if side is not None:
        side.join(SIDE_THREAD_JOIN_S)
    first.join(SIDE_THREAD_JOIN_S)
    run.finish_more()
    timing = run.timing()
    print(
        f"clarify_timing job={run.job_id or '-'} q1_ms={timing['q1_ms']} "
        f"rest_ms={timing['rest_ms']} source={timing['source']} "
        f"questions={run.question_count()}",
        file=sys.stderr, flush=True,
    )  # fmt: skip
    extra_calls = prefetch.settle(budget) if not prefetch.consumed else 0
    outcome.serper_calls += extra_calls
    if search.name == "serper":
        outcome.serper_credits += extra_calls * config.serper_credits_per_call
    outcome.anthropic_usd = round(budget.cost_usd, 6)  # the side thread's calls are in the budget
    outcome.provider_usd = round(budget.provider_cost_usd, 6)
    if outcome.display_query is None and outcome.query is not query:
        outcome.display_query = query  # the request chips and the record keep the plain parse
    outcome.trace["clarify"] = {
        "state": run.state,
        "reason": run.reason,
        "seconds_to_text": _since(run.ready_at, run.created),
        "seconds_to_pictures": _since(run.pictures_at, run.created),
        "timing": timing,
        "options_shown": stage.options_total,
        "options_dropped_by_title": stage.dropped_by_title,
        "questions_dropped": stage.dropped_questions,
        "options_dropped_by_picture": stage.dropped_by_picture,
        "pictures": stage.pictures,
        "question_usd": stage.question_usd,
        "verification_usd": stage.verify_usd,
        "verification_calls": stage.verify_calls,
        "prefetch_reused": reused and prefetch.consumed,
        "prefetch_seconds": prefetch.seconds,
        "applied_reason": gate_info.get("reason", ""),
        "waited_s": gate_info.get("waited", 0.0),
        "answers_applied": len(run.applied_answers),
        "answers": gate_info.get("mapped", []),
    }
    return outcome, query


def _since(at: float | None, origin: float) -> float | None:
    return round(at - origin, 3) if at is not None else None
