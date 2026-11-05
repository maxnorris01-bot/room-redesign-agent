"""The clarify harness: for each request, the quick first fetch, the question writer, the picture matcher,
the scripted persona's answers, and the normal pipeline twice (baseline and with the answers applied),
both scored on the top 24 by the EXISTING judges.

Per case (see evals/shop_clarify/README.md for the metrics):

    parse (Haiku, as the pipeline does) -> ONE Serper call for the base query (through the Shop eval's
    record/replay cache) -> analyze_facets (Haiku, one forced tool call) -> pick_option_images (code)
    -> quality_flags (code) -> persona answers (code) -> run_query twice -> persona and attribute
    judges on 24 results each.

Time to questions is parse + fetch + model, one after the other, as a visitor would wait for them if
the parse were not overlapped with the fetch; `overlapped_s` is the same with the parse and the fetch
at the same time (the fetch needs the parse's search text, so this is a lower bound, not a plan). A
cached fetch reports the latency recorded when the call was live (a sidecar file next to the results),
so a re-run does not understate the wait.

Hard stops: $0.30 Anthropic and 150 Serper credits for the whole run. A case that raises is recorded
as failed and the run goes on; at a stop the case is recorded as partial and the rest as not run.
"""

from __future__ import annotations

import dataclasses
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import BudgetExceededError
from app.llm import Budget
from app.prompts import load_prompt
from app.shop.clarify import (
    Caller,
    ImagePicks,
    Questions,
    analyze_facets,
    apply_answers,
    map_answers,
    pick_option_images,
    quality_flags,
)
from app.shop.clock import Clock
from app.shop.models import ShopQuery, clean_request
from app.shop.parse import parse_request
from app.shop.search import SearchOutcome, run_query
from app.shopping import SearchCandidate
from evals.shop import scorers as sc
from evals.shop.cache import CacheStats, request_key
from evals.shop.judges import JudgeItem
from evals.shop.runner import Env, build_env
from evals.shop.spec import thresholds_sha256
from evals.shop_clarify.persona import answer_questions
from evals.shop_clarify.spec import RESULTS_DIR, ClarifyCase

ANTHROPIC_STOP_USD = 0.30
SERPER_STOP_CREDITS = 150
SHOWN = 24  # results judged per arm
GOOD_PERSONA = {"love", "like"}
LATENCY_FILE = "fetch-latency.json"


class HardStop(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Spend:
    """Running Anthropic dollars and Serper credits against the run's hard stops."""

    anthropic_usd: float = 0.0
    serper_credits: float = 0.0
    stopped: str = ""
    limit_usd: float = ANTHROPIC_STOP_USD
    limit_credits: float = SERPER_STOP_CREDITS

    def add(self, usd: float, credits: float = 0.0) -> None:
        self.anthropic_usd = round(self.anthropic_usd + usd, 6)
        self.serper_credits += credits
        self.check()

    def check(self) -> None:
        if self.anthropic_usd > self.limit_usd:
            self.stopped = (
                f"Anthropic spend ${self.anthropic_usd:.4f} passed the stop ${self.limit_usd:.2f}"
            )
        elif self.serper_credits > self.limit_credits:
            self.stopped = (
                f"Serper credits {self.serper_credits:.0f} passed the stop {self.limit_credits:.0f}"
            )
        if self.stopped:
            raise HardStop(self.stopped)


# ---- timed fetch ------------------------------------------------------------------------------


class LatencyStore:
    """Live Serper latencies by request key, kept beside the results (gitignored), so a cached call
    still reports how long the live one took."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, float] = {}
        if path is not None and path.is_file():
            try:
                loaded = json.loads(path.read_text())
                self._data = {str(k): float(v) for k, v in loaded.items()}
            except (OSError, ValueError):
                self._data = {}

    def get(self, key: str) -> float | None:
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, seconds: float) -> None:
        with self._lock:
            self._data[key] = round(seconds, 3)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._data, indent=1))


@dataclass
class FetchInfo:
    seconds: float = 0.0  # what to report (the recorded live latency for a cached call, if known)
    wall_seconds: float = 0.0  # what this call took here
    cached: bool = False
    live: bool = False  # a real network call
    latency_known: bool = True


class TimedSearch:
    """Wraps a search provider: times each call, and tells a live call from a cache hit."""

    def __init__(
        self, inner: Any, stats: CacheStats | None, store: LatencyStore, mock: bool
    ) -> None:
        self._inner = inner
        self._stats = stats
        self._store = store
        self._mock = mock
        self.last = FetchInfo()
        self.live_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def search(
        self, query: str, gl: str = "us", hl: str = "en", page: int = 1
    ) -> list[SearchCandidate]:
        hits = self._stats.hits if self._stats is not None else 0
        started = time.monotonic()
        out: list[SearchCandidate] = self._inner.search(query, gl, hl, page)
        wall = time.monotonic() - started
        body: dict[str, Any] = {"q": query, "gl": gl, "hl": hl}
        if page > 1:
            body["page"] = page
        key = request_key(body)
        cached = self._stats is not None and self._stats.hits > hits
        if self._mock:
            self.last = FetchInfo(seconds=wall, wall_seconds=wall)
        elif cached:
            known = self._store.get(key)
            self.last = FetchInfo(
                seconds=known if known is not None else wall, wall_seconds=wall, cached=True,
                latency_known=known is not None,
            )  # fmt: skip
        else:
            self.live_calls += 1
            self._store.put(key, wall)
            self.last = FetchInfo(seconds=wall, wall_seconds=wall, live=True)
        return out


# ---- scoring one arm --------------------------------------------------------------------------


def judge_arm(
    case: ClarifyCase, outcome: SearchOutcome, env: Env, answered: list[str], spend: Spend
) -> dict[str, Any]:
    """The top 24 of an outcome, scored by the existing persona and attribute/category judges. The
    attribute judge checks the persona's answered traits in BOTH arms (the same list), so the two
    arms are scored against the same wish."""
    results = outcome.results[:SHOWN]
    items = [
        JudgeItem(n, r.title, r.price, r.seller, r.thumbnail_url)
        for n, r in enumerate(results, start=1)
    ]
    judge_case = case.to_case(answered)
    before = env.judge.stats.cost_usd
    cat = env.judge.category(judge_case, items) if items else {}
    spend.add(env.judge.stats.cost_usd - before)
    before = env.judge.stats.cost_usd
    per = env.judge.persona(case.to_case(), items) if items else {}
    spend.add(env.judge.stats.cost_usd - before)
    persona = sc.rate([v.answer if v else None for v in per.values()], GOOD_PERSONA)
    attributes = sc.rate([v.attributes if v else None for v in cat.values()], {"yes"})
    category = sc.rate([v.category if v else None for v in cat.values()], {"yes"})
    return {
        "count": len(results),
        "persona": persona,
        "attributes": attributes,
        "category": category,
        "search_text": outcome.query.search_text,
        "seconds": outcome.seconds.get("total"),
        "titles": [
            {"title": r.title, "price": r.price, "seller": r.seller, "persona": per[n].answer if per.get(n) else None}  # type: ignore[union-attr]
            for n, r in enumerate(results, start=1)
        ],
    }  # fmt: skip


def _points(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else round((b - a) * 100, 2)


def picks_json(questions: Questions, picks: ImagePicks) -> list[dict[str, Any]]:
    out = []
    for q in questions.questions:
        options = []
        for i, o in enumerate(q.options):
            p = picks.for_option(q.id, i)
            options.append(
                {
                    "label": o.label,
                    "value": o.value,
                    "pick": None
                    if p is None
                    else {
                        "product_id": p.product_id,
                        "title": p.title,
                        "thumbnail_url": p.thumbnail_url,
                        "how": p.how,
                    },
                }  # fmt: skip
            )
        out.append(
            {
                "id": q.id,
                "text": q.text,
                "kind": q.kind,
                "dimension": q.dimension,
                "options": options,
            }
        )
    return out


# ---- one case ---------------------------------------------------------------------------------


def run_case(
    case: ClarifyCase,
    env: Env,
    spend: Spend,
    caller: Caller,
    store: LatencyStore,
    skip_pipeline: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": case.id, "request": case.request, "vagueness": case.vagueness, "status": "ok", "error": None,
        "timing": {}, "questions": [], "notes": [], "flags": [], "images": {}, "persona": [],
        "arms": {}, "lift": {}, "cost": {}, "applied": [],
    }  # fmt: skip
    started = time.monotonic()
    spent0, credits0 = spend.anthropic_usd, spend.serper_credits
    config = env.config
    mock = env.mode == "mock"
    try:
        search, _lookup = env.make_providers(case.to_case())
        timed = TimedSearch(search, env.cache, store, mock)
        budget = Budget(config)
        t = time.monotonic()
        query, _completion, warning = parse_request(clean_request(case.request), budget)
        parse_s = time.monotonic() - t
        spend.add(budget.cost_usd)
        result["parse_warning"] = warning
        result["search_text"] = query.search_text

        misses0 = env.cache.misses if env.cache is not None else 0
        candidates = timed.search(query.search_text)
        fetch = timed.last
        live_credits = (
            (env.cache.misses - misses0) * config.serper_credits_per_call
            if env.cache is not None
            else 0
        )
        spend.add(0.0, 0 if mock else live_credits)

        clarify_budget = Budget(config)
        questions = analyze_facets(
            case.request, query, candidates, budget=clarify_budget, caller=caller
        )
        spend.add(questions.cost_usd)
        model_s = questions.seconds
        total = parse_s + fetch.seconds + model_s
        result["timing"] = {
            "parse_s": round(parse_s, 3), "fetch_s": round(fetch.seconds, 3), "model_s": round(model_s, 3),
            "total_s": round(total, 3), "overlapped_s": round(max(parse_s, fetch.seconds) + model_s, 3),
            "fetch_cached": fetch.cached, "fetch_latency_known": fetch.latency_known,
            "candidates": len(candidates), "shown_to_model": questions.shown,
        }  # fmt: skip
        result["notes"] = list(questions.notes)

        picks = pick_option_images(questions, candidates, category=query.category)
        flags = quality_flags(case.request, query, questions)
        answers, persona_picks = answer_questions(questions.questions, case.answers)
        options = sum(len(q.options) for q in questions.questions)
        result["questions"] = picks_json(questions, picks)
        result["images"] = {
            "options": options, "with_image": picks.with_image,
            "coverage": round(picks.coverage, 3) if options else None,
        }  # fmt: skip
        result["flags"] = [dataclasses.asdict(f) for f in flags]
        result["persona"] = [
            {"question": p.question_id, "trait": p.trait, "wanted": p.wanted, "how": p.how,
             "answer": p.option.label if p.option else None}
            for p in persona_picks
        ]  # fmt: skip
        result["cost"] = {
            "parse_usd": round(parse_s and budget.cost_usd, 6), "model_usd": questions.cost_usd,
            "fetch_credits": 0 if mock else live_credits,
        }  # fmt: skip

        if not skip_pipeline:
            applied = map_answers(query, answers)
            result["applied"] = applied.mapped
            answered_words = [p.wanted for p in persona_picks if p.option is not None]
            clock = Clock()
            for arm, arm_query in (("baseline", query), ("answers", applied.query)):
                if arm == "answers" and not answers:
                    result["arms"]["answers"] = None
                    continue
                m0 = env.cache.misses if env.cache is not None else 0
                arm_budget = Budget(config)
                outcome = run_query(
                    arm_query, timed, config, arm_budget, [], clock.now(), None, clock
                )
                credits = (
                    (env.cache.misses - m0) * config.serper_credits_per_call
                    if env.cache is not None
                    else 0
                )
                spend.add(outcome.anthropic_usd, 0 if mock else credits)
                scored = judge_arm(case, outcome, env, answered_words, spend)
                scored["serper_calls"] = outcome.serper_calls
                scored["live_credits"] = 0 if mock else credits
                scored["anthropic_usd"] = outcome.anthropic_usd
                result["arms"][arm] = scored
            base, ans = result["arms"]["baseline"], result["arms"].get("answers")
            if ans is not None:
                result["lift"] = {
                    "persona_points": _points(base["persona"]["rate"], ans["persona"]["rate"]),
                    "attribute_points": _points(
                        base["attributes"]["rate"], ans["attributes"]["rate"]
                    ),
                }
    except HardStop:
        result["status"], result["error"] = "partial", spend.stopped
    except BudgetExceededError as exc:
        result["status"], result["error"] = "failed", f"budget: {exc}"[:200]
    except Exception as exc:  # one case failing never stops the run
        result["status"], result["error"] = "failed", f"{type(exc).__name__}: {exc}"[:200]
    result["cost"] = {
        **result["cost"],
        "anthropic_usd": round(spend.anthropic_usd - spent0, 6),
        "serper_credits": round(spend.serper_credits - credits0, 2),
    }
    result["wall_seconds"] = round(time.monotonic() - started, 3)
    return result


# ---- the whole run ----------------------------------------------------------------------------


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def run_all(
    cases: list[ClarifyCase],
    env: Env,
    caller: Caller,
    progress: Callable[[str], None] = lambda _m: None,
    store: LatencyStore | None = None,
    spend: Spend | None = None,
    skip_pipeline: bool = False,
) -> dict[str, Any]:
    spend = spend or Spend()
    store = store or LatencyStore(None)
    run_id, started = new_run_id(), datetime.now(UTC).isoformat(timespec="seconds")
    results: list[dict[str, Any]] = []
    stopped = ""
    for case in cases:
        progress(f"{case.id}: {case.request}")
        results.append(run_case(case, env, spend, caller, store, skip_pipeline))
        if spend.stopped:
            stopped = spend.stopped
            break
    for case in cases[len(results) :]:
        results.append(
            {
                "id": case.id,
                "request": case.request,
                "vagueness": case.vagueness,
                "status": "not_run",
                "error": stopped,
            }
        )
    return {
        "run_id": run_id, "mode": env.mode, "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "prompt_version": load_prompt("shop_clarify").version,
        "models": {"clarify": env.config.model_retrieval, "judge": env.config.model_retrieval},
        "limits": {"anthropic_usd": spend.limit_usd, "serper_credits": spend.limit_credits},
        "totals": {"anthropic_usd": spend.anthropic_usd, "serper_credits": spend.serper_credits},
        "stopped": stopped,
        "thresholds_sha256": thresholds_sha256(),
        "serper_cache": env.cache.as_dict() if env.cache is not None else {"mode": "off"},
        "cases": results,
    }  # fmt: skip


def write_results(payload: dict[str, Any], directory: Path = RESULTS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"clarify-{payload['mode']}-{payload['run_id']}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


def make_env(mode: str, directory: Path = RESULTS_DIR) -> tuple[Env, LatencyStore]:
    """The Shop eval's environment (config, judges, providers with the Serper record/replay cache). A
    live run defaults to cache mode `use` (a saved response under 14 days old is free; a miss goes
    live and is saved), so reruns cost nothing."""
    cache = os.environ.get("EVAL_SHOP_SERPER_CACHE") or ("use" if mode == "live" else "off")
    env = build_env(mode, serper_cache=cache)
    return env, LatencyStore(directory / LATENCY_FILE)


# for the tests and the plan: names a caller can import
__all__ = [
    "ANTHROPIC_STOP_USD", "SERPER_STOP_CREDITS", "HardStop", "LatencyStore", "Spend", "TimedSearch",
    "apply_answers", "judge_arm", "make_env", "run_all", "run_case", "write_results",
    "ShopQuery", "field",
]  # fmt: skip
