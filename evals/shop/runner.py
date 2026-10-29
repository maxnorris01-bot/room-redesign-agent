"""The Shop eval runner: for each case, a first search through the service layer (not the browser),
then Shop Again twice, judging every batch and submitting the simulated person's ratings.

Per case: search (round 1) -> category/attribute judge on every shown item -> persona judge on every
shown item (measurement) -> the first `RATED_PER_ROUND` in display order are rated by the persona's
answer, as in real use -> [the case's refine step] -> Shop Again (round 2), judged and rated the same
way -> Shop Again (round 3), judged. The pipeline receives the request, the ratings and the refine
text and nothing else: persona text goes to the persona judge only.

Automatic lookups and Open are off (no SerpApi calls) and the gallery flag stays off. After every
step the running Anthropic spend and Serper credits are checked against the hard stops; at a stop
the case is recorded as partial, the rest as not run, and partial results are written with the
reason. A case that raises is recorded as failed with its error and the run goes on.
"""

from __future__ import annotations

import dataclasses
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import BudgetExceededError, Config
from app.prompts import PROMPTS_DIR, load_prompt
from app.shop.providers import ShopFixtureLookup, build_shop_providers
from app.shop.service import ApiError, ShopService
from app.shop.store import SessionStore
from app.shopping import ProductLookup, SearchProvider
from app.thumbnails import Thumbnailer, build_thumbnailer
from evals.shop import scorers as sc
from evals.shop.coverage import category_findings
from evals.shop.evaluate import evaluate, verdict
from evals.shop.judges import Caller, JudgeItem, ModelJudge
from evals.shop.mockdata import RouterSearch
from evals.shop.spec import SHOP_DIR, Case, Thresholds, load_thresholds, thresholds_sha256

RATED_PER_ROUND = 12  # the persona rates this many, in display order (a person rates a few)
ROUNDS = 3
RESULTS_DIR = SHOP_DIR / "results"
GOOD_PERSONA = {"love", "like"}


class HardStop(Exception):
    """The run's spend or credit total passed a hard stop."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class RunState:
    thresholds: Thresholds
    anthropic_usd: float = 0.0
    serper_credits: float = 0.0
    stopped: str = ""

    def add(self, anthropic_usd: float, credits: float) -> None:
        self.anthropic_usd = round(self.anthropic_usd + anthropic_usd, 6)
        self.serper_credits += credits
        self.check()

    def check(self) -> None:
        t = self.thresholds
        if self.anthropic_usd > t.hard_stop_anthropic_usd:
            self.stopped = (
                f"Anthropic spend ${self.anthropic_usd:.4f} passed the hard stop "
                f"${t.hard_stop_anthropic_usd:.2f}"
            )
        elif self.serper_credits > t.hard_stop_serper_credits:
            self.stopped = (
                f"Serper credits {self.serper_credits:.0f} passed the hard stop "
                f"{t.hard_stop_serper_credits}"
            )
        if self.stopped:
            raise HardStop(self.stopped)


@dataclass
class Env:
    mode: str
    config: Config
    thumbnailer: Thumbnailer | None
    judge: ModelJudge
    state: RunState
    make_providers: Callable[[Case], tuple[SearchProvider, ProductLookup]]
    notes: list[str] = field(default_factory=list)


def eval_config(mode: str) -> Config:
    """The pipeline's configuration for the eval: from the environment, mode forced, automatic
    lookups and the gallery off."""
    base = Config.from_env()
    return dataclasses.replace(base, llm_mode=mode, shop_auto_lookups=False, shop_gallery=False)


def build_env(mode: str, caller: Caller | None = None, config: Config | None = None) -> Env:
    config = config or eval_config(mode)
    thumbnailer = build_thumbnailer(config)
    judge = ModelJudge(config, thumbnailer, **({"caller": caller} if caller else {}))
    thresholds = load_thresholds()

    def providers(case: Case) -> tuple[SearchProvider, ProductLookup]:
        if mode == "mock":
            return RouterSearch(fixture_case=case.fixture != "generic"), ShopFixtureLookup()
        return build_shop_providers(config)

    return Env(mode, config, thumbnailer, judge, RunState(thresholds), providers)


def product_color_row(
    listings: list[sc.Listing], color: str, verdicts: list[Any]
) -> dict[str, Any]:
    """The product-colour score of a batch. Since stage 2p the share is what the category judge
    (prompt v2) says of each thumbnail: yes over the items it could judge (cannot tell counts as not
    matching, a judge failure is left out); the title-keyword share the baseline used is kept as a
    second figure."""
    judged = sc.color_match_share([v.color_match if v is not None else None for v in verdicts])
    title = sc.product_color_share(listings, color)
    return {
        "share": judged["share"], "count": judged["count"], "measure": "judge",
        "judged": judged, "title_share": title["share"], "title_count": title["count"],
    }  # fmt: skip


# ---- one round ------------------------------------------------------------------------------


def footer_line(view: dict[str, Any]) -> str:
    """What the page's footer shows for a batch, built from the same numbers (no URLs)."""
    s = view["search"]
    parts = [
        f"{'Shop Again' if s['kind'] == 'shop_again' else 'Search'} took {s['seconds']['total']:.1f} s",
        f"{s['serper_calls']} Serper calls = {s['serper_credits']} credits (${s['provider_usd']:.4f})",
        f"Anthropic ${s['anthropic_usd']:.4f}",
        f"est. total ${s['total_usd']:.4f}",
    ]
    if s.get("alignment"):
        a = s["alignment"]
        if a.get("signals", 1) == 0:  # as the page says it: nothing decided yet, not "0 matched"
            parts.append("matches your preferences: none yet")
        else:
            parts.append(f"matches your preferences: {a['matches']} of {a['total']}")
    return " · ".join(parts)


def trace_summary(view: dict[str, Any]) -> dict[str, Any]:
    """The part of a batch's trace worth keeping in the results file: what the pipeline read from the
    request, what it had learned (chips, converged values, leanings, alignment), how the pool and
    the cap behaved. Compact, and no titles or links. (The first live run kept less of this, which
    left its diagnosis partly blind; see docs/evals/shop-eval-baseline-2026-10-26.md.)"""
    s = view["search"]
    trace = s.get("trace") or {}
    dups = trace.get("duplicates") or {}
    refine = (trace.get("refine") or {}).get("chips", [])
    batch = trace.get("batch") or {}
    profile = view.get("profile") or {}
    request = trace.get("request") or {}
    return {
        "duplicates": {k: dups.get(k) for k in ("before", "after", "hidden")},
        "refine_chips": [{"label": c.get("label"), "kind": c.get("kind")} for c in refine],
        "mode": trace.get("mode"),
        "warnings": list(s.get("warnings", []))[:5],
        "request": {
            k: request.get(k)
            for k in ("search_text", "product_colors", "suggested", "avoid", "from_suggested", "from_avoid", "results")
            if k in request
        },
        "learned": {
            "alignment": s.get("alignment"),
            "alignment_counted": trace.get("alignment_counted"),
            "chips": [
                {"kind": e.get("attr_kind"), "text": e.get("text"), "status": e.get("status"),
                 "dimension": e.get("dimension"), "values": e.get("values")}
                for e in profile.get("inferred", [])
            ],  # fmt: skip
            "stated": {"likes": len(profile.get("likes", [])), "dislikes": len(profile.get("dislikes", []))},
            "dimensions": {
                dim: {k: d.get(k) for k in ("decision", "top", "top_share", "known_coverage", "support")}
                for dim, d in (trace.get("dimensions") or {}).items()
            },
            "marketplace": (trace.get("marketplace") or {}).get("decision"),
        },
        "exhausted": batch.get("exhausted"),
        "thin": trace.get("thin"),
        "typed": trace.get("typed") or batch.get("typed"),
        "pool": {
            k: batch.get(k)
            for k in ("pool_before", "pool_after", "shortfall", "serper_calls", "marketplace_excluded", "refine_removed")
            if k in batch
        },
        "seller_cap": trace.get("seller_cap") or batch.get("seller_cap"),
        "price_outliers": trace.get("price_outliers") or batch.get("price_outliers"),
    }  # fmt: skip


def search_texts(view: dict[str, Any]) -> list[str]:
    s = view["search"]
    texts = [q for q in s.get("queries", []) if isinstance(q, str)]
    texts.append(s["query"]["search_text"])
    if s.get("used_query"):
        texts.append(s["used_query"]["search_text"])
    return [t for t in texts if t]


def judge_items(listings: list[sc.Listing]) -> list[JudgeItem]:
    return [
        JudgeItem(n, it.title, it.price, it.seller, it.thumbnail_url)
        for n, it in enumerate(listings, start=1)
    ]


def play_round(
    case: Case,
    env: Env,
    svc: ShopService,
    sid: str,
    view: dict[str, Any],
    round_no: int,
    seen_titles: list[str],
    submit: bool,
) -> tuple[dict[str, Any], sc.Batch]:
    """Judge a shown batch, score it, and (when `submit`) rate its first twelve as the persona."""
    s = view["search"]
    fingerprints = {k: v for k, v in svc.store.load(sid).fingerprints.items() if v is not None}
    listings = sc.listings_from_view(view, fingerprints)
    batch = sc.Batch(
        round=round_no, kind=s["kind"], listings=listings, search_texts=search_texts(view),
        parsed_min=s["query"].get("price_min"), parsed_max=s["query"].get("price_max"),
        seconds=float(s["seconds"]["total"]),
    )  # fmt: skip
    pipeline_anthropic = float(s["anthropic_usd"])
    env.state.add(pipeline_anthropic, float(s["serper_credits"]))

    spent_before = env.judge.stats.cost_usd
    items = judge_items(listings)
    cat = env.judge.category(case, items)
    env.state.add(env.judge.stats.cost_usd - spent_before, 0)
    spent_before = env.judge.stats.cost_usd
    per = env.judge.persona(case, items)
    env.state.add(env.judge.stats.cost_usd - spent_before, 0)

    cat_rate = sc.rate([v.category if v else None for v in cat.values()], {"yes"})
    attr_rate = sc.rate([v.attributes if v else None for v in cat.values()], {"yes"})
    persona_rate = sc.rate([v.answer if v else None for v in per.values()], GOOD_PERSONA)

    submitted = 0
    rated: dict[str, str] = {}
    if submit:
        for n, it in enumerate(listings[:RATED_PER_ROUND], start=1):
            verdict_ = per.get(n)
            if verdict_ is None:
                continue  # a failed judgement is never turned into a rating
            svc.rate(sid, it.id, verdict_.answer)
            rated[it.id] = verdict_.answer
            submitted += 1

    row: dict[str, Any] = {
        "round": round_no,
        "kind": s["kind"],
        "seconds": batch.seconds,
        "count": len(listings),
        "price": sc.price_compliance(listings, batch.parsed_min, batch.parsed_max),
        "category": cat_rate,
        "attributes": attr_rate,
        "persona": persona_rate,
        "ratings_submitted": submitted,
        "exhausted": s["kind"] == "shop_again" and not listings,  # nothing unseen was left
        "sellers": sc.seller_diversity(listings),
        "near_dups": sc.near_duplicate_groups(listings),
        "avoid": sc.avoid_color_share(listings, case.avoid_colors) if case.avoid_colors else None,
        "product_color": (
            product_color_row(listings, case.product_color, [cat.get(n) for n in range(1, len(listings) + 1)])
            if case.product_color
            else None
        ),
        "refine": None,
        "footer": footer_line(view),
        "trace": trace_summary(view),
        "cost": {
            "anthropic_usd": pipeline_anthropic,
            "provider_usd": float(s["provider_usd"]),
            "serper_credits": float(s["serper_credits"]),
        },
        "items": [
            {
                "id": it.id, "title": it.title, "price": it.price, "seller": it.seller,
                "thumbnail_url": it.thumbnail_url, "source": it.source,
                "category": cat[n].category if cat.get(n) else None,  # type: ignore[union-attr]
                "attributes": cat[n].attributes if cat.get(n) else None,  # type: ignore[union-attr]
                "persona": per[n].answer if per.get(n) else None,  # type: ignore[union-attr]
                "rated": rated.get(it.id),
            }
            for n, it in enumerate(listings, start=1)
        ],
    }  # fmt: skip
    if case.refine is not None and round_no >= 2:
        step = case.refine
        refine: dict[str, Any] = {"text": step.text}
        if step.effect == "exclude_seller":
            refine["exclusion"] = sc.exclusion_respected(listings, step.sellers)
        else:
            refine["focus"] = sc.focus_share(listings, step.title_term, seen_titles)
        row["refine"] = refine
    return row, batch


# ---- one case -------------------------------------------------------------------------------


def run_case(case: Case, env: Env) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": case.id, "request": case.request, "kind": case.kind, "status": "ok", "error": None,
        "rounds": [], "context": {}, "repeats": None, "lift": {}, "refine_step": None,
        "cost": {"pipeline_usd": 0.0, "judge_usd": 0.0, "serper_credits": 0.0},
        "wall_seconds": 0.0,
    }  # fmt: skip
    started = time.monotonic()
    judge_before = env.judge.stats.cost_usd
    folder = tempfile.mkdtemp(prefix="shop-eval-")
    svc: ShopService | None = None
    sid = ""
    try:
        search, lookup = env.make_providers(case)
        svc = ShopService(
            env.config, SessionStore(Path(folder)), search, lookup, thumbnailer=env.thumbnailer
        )
        batches: list[sc.Batch] = []
        seen_titles: list[str] = []
        view = svc.search(None, case.request)
        sid = view["session_id"]
        for round_no in range(1, ROUNDS + 1):
            row, batch = play_round(
                case, env, svc, sid, view, round_no, seen_titles, submit=round_no < ROUNDS
            )
            result["rounds"].append(row)
            batches.append(batch)
            seen_titles += [i.title for i in batch.listings]
            if round_no == ROUNDS:
                break
            if round_no == 1 and case.refine is not None:
                result["refine_step"] = apply_refine(svc, sid, case, env)
            view = svc.shop_again(sid)
        result["repeats"] = sc.repeats(batches)
        if case.context_colors:
            texts = [t for b in batches for t in b.search_texts]
            result["context"] = {"leakage": sc.context_leakage(texts, case.context_colors)}
        rates = [r["persona"]["rate"] for r in result["rounds"]]
        result["lift"] = {
            "r2_minus_r1": _points(rates, 1),
            "r3_minus_r1": _points(rates, 2),
            "rates": rates,
        }
    except HardStop:  # what the case had finished is kept; the run stops after it
        result["status"], result["error"] = "partial", env.state.stopped
    except BudgetExceededError as exc:  # a per-call cap inside the pipeline: this case failed
        result["status"], result["error"] = "failed", f"budget: {exc}"[:200]
    except Exception as exc:  # one case failing never stops the run
        result["status"], result["error"] = "failed", f"{type(exc).__name__}: {exc}"[:200]
    finally:
        if svc is not None and sid:
            svc.prep.cancel(sid)  # background preparation must stop before its folder goes
            svc.prep.wait(sid, 5)
        shutil.rmtree(folder, ignore_errors=True)
        rounds = result["rounds"]
        result["cost"] = {
            "pipeline_usd": round(
                sum(r["cost"]["anthropic_usd"] + r["cost"]["provider_usd"] for r in rounds)
                + float((result.get("refine_step") or {}).get("usd", 0.0)),
                6,
            ),
            "judge_usd": round(env.judge.stats.cost_usd - judge_before, 6),
            "serper_credits": sum(r["cost"]["serper_credits"] for r in rounds),
        }
        result["wall_seconds"] = round(time.monotonic() - started, 3)
    return result


def _points(rates: list[float | None], later: int) -> float | None:
    if len(rates) <= later or rates[0] is None or rates[later] is None:
        return None
    return round((rates[later] - rates[0]) * 100, 2)  # type: ignore[operator]


def apply_refine(svc: ShopService, sid: str, case: Case, env: Env) -> dict[str, Any]:
    """The case's refine step, as typed into the page's box. A refinement the pipeline cannot read
    is recorded (a finding), not an error."""
    assert case.refine is not None
    try:
        out = svc.refine(sid, case.refine.text)
    except ApiError as exc:
        return {
            "text": case.refine.text,
            "accepted": False,
            "message": exc.message[:120],
            "usd": 0.0,
        }
    usd = float(out.get("refine_usd", 0.0))
    env.state.add(usd, 0)
    return {
        "text": case.refine.text,
        "accepted": True,
        "chips": [{"kind": c["kind"], "label": c["label"], "approximate": c["approximate"]}
                  for c in out["refine"]],
        "usd": usd,
    }  # fmt: skip


# ---- the whole run ----------------------------------------------------------------------------


def prompt_versions() -> dict[str, str]:
    """Versions of every Shop prompt and both judge prompts (so a result is attributable)."""
    names = sorted(p.name[:-3] for p in PROMPTS_DIR.glob("*.md") if p.name.count(".") == 1)
    return {n: load_prompt(n).version for n in names if n.startswith(("shop_", "eval_shop"))}


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def run_all(
    cases: list[Case], env: Env, progress: Callable[[str], None] = lambda _m: None
) -> dict[str, Any]:
    run_id = new_run_id()
    started = datetime.now(UTC).isoformat(timespec="seconds")
    results: list[dict[str, Any]] = []
    stopped = ""
    for case in cases:
        progress(f"{case.id}: {case.request}")
        results.append(run_case(case, env))
        if env.state.stopped:
            stopped = env.state.stopped
            break
    for case in cases[len(results) :]:
        results.append({"id": case.id, "request": case.request, "kind": case.kind,
                        "status": "not_run", "error": stopped, "rounds": []})  # fmt: skip
    checks = evaluate(results, env.state.thresholds)
    return {
        "run_id": run_id,
        "mode": env.mode,
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "thresholds_sha256": thresholds_sha256(),
        "thresholds": env.state.thresholds.model_dump(),
        "case_index_version": 1,
        "prompt_versions": prompt_versions(),
        "judge_versions": env.judge.versions,
        "models": {
            "pipeline": env.config.model_retrieval,
            "rank": env.config.rank_model,
            "judge": env.config.model_retrieval,
        },
        "judge_stats": dataclasses.asdict(env.judge.stats),
        "totals": {
            "anthropic_usd": env.state.anthropic_usd,
            "serper_credits": env.state.serper_credits,
        },
        "stopped": stopped,
        "category_findings": category_findings(cases),
        "cases": results,
        "checks": checks,
        "verdict": verdict(results, checks, stopped),
        "notes": env.notes,
    }


def write_results(payload: dict[str, Any], directory: Path = RESULTS_DIR) -> Path:
    import json

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"shop-{payload['mode']}-{payload['run_id']}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path
