"""The cost-and-latency spike pipeline (SPEC.md "Session 1").

Stages: prepare_image -> perception -> retrieval.

`run_spike` is the testable core; `scripts/spike.py` is a thin CLI around it. Nothing here enforces
the proposed cost/latency targets: they are printed as informational comparisons only (they are
not locked until an ADR says so - see SPEC.md and docs/working-notes-and-decisions.md).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from dataclasses import replace as dc_replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import tracing
from app.config import Config
from app.images import PreparedImage, prepare_image
from app.llm import Budget, Completion
from app.perception import PerceptionResult, perceive
from app.preferences import resolve_preferences
from app.pricing import is_priced
from app.proposal import propose_and_check, seller_concentration
from app.retrieval import RetrievalResult, item_count_message, retrieve
from app.tracing import span

# Freeplay cap scaling (UNTUNED GUESSES), see run_spike.
FREEPLAY_STEP_OVERHEAD = 6  # perception, proposal, style, revision, style, and one spare
FREEPLAY_PROVIDER_USD_PER_SLOT = (
    0.06  # a search and two lookups at the assumed list prices, rounded up
)

# PROPOSED targets from SPEC.md, not locked. Informational only: never enforced, never in
# evals/thresholds.yaml until an ADR locks them.
PROPOSED_TARGETS = {
    "perception_plus_retrieval_cost_usd": 0.08,
    "perception_plus_retrieval_seconds": 30.0,
    "full_run_mean_cost_usd": 0.20,
    "full_run_p95_seconds": 60.0,
}


@dataclass
class StageRow:
    stage: str
    model: str
    input_tokens: int
    output_tokens: int
    web_searches: int
    cost_usd: float
    seconds: float
    # Streamed calls only; first content_block_delta, see llm._create_message. None = not measured.
    time_to_first_token_seconds: float | None = None
    # Retrieval rows only: which slot, and what kind of call (search|rank|lookup|web_search).
    slot: str | None = None
    kind: str | None = None
    # True when the dollars are an estimate from the APP_*_USD settings (provider calls).
    cost_estimated: bool = False
    # Rows sharing a non-None group ran in parallel (self-check); only the slowest counts on the
    # critical path.
    group: str | None = None


@dataclass
class SpikeResult:
    run_id: str
    llm_mode: str
    started_at: str
    photo_name: str
    budget_usd: float | None  # None = freeplay (no budget)
    style: str
    prompt_versions: dict[str, str]
    models: dict[str, str]
    image: dict[str, Any]
    rows: list[StageRow] = field(default_factory=list)
    perception: dict[str, Any] = field(default_factory=dict)
    retrieval: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    unpriced_models: list[str] = field(default_factory=list)
    perception_only: bool = False
    retrieval_mode: str = "shopping"
    search_provider: str | None = None
    perception_output_stats: dict[str, int] = field(default_factory=dict)
    # The model's JSON exactly as returned (before pydantic), so a odd token can be traced to
    # the model or to our code. It is the same data as `perception`, not extra personal data.
    perception_raw_text: str = ""
    wall_clock_seconds: float = 0.0
    mode: str = "budget"  # "budget" (--budget given) or "freeplay" (no budget)
    price_tier: str | None = None  # freeplay only
    items: int = 0  # pieces (slots) asked for
    caps: dict[str, float] = field(default_factory=dict)  # the dollar caps this run ran under
    preferences: dict[str, Any] = field(default_factory=dict)  # keep / replace and what resolved
    kept_items: list[dict[str, str]] = field(default_factory=list)  # existing items that stay
    slot_plan: dict[str, Any] = field(default_factory=dict)  # slots chosen, dropped by cap, skipped
    warnings: list[str] = field(default_factory=list)  # run-level, shown after the table
    item_count_report: dict[str, Any] = field(default_factory=dict)  # set when fewer than asked
    seller_concentration: dict[str, Any] = field(default_factory=dict)
    proposal: dict[str, Any] = field(default_factory=dict)
    proposal_dropped: list[dict[str, str]] = field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.rows)

    @property
    def anthropic_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.rows if not r.cost_estimated)

    @property
    def provider_cost_usd(self) -> float:
        """Estimated provider dollars (search and lookup calls at the APP_*_USD settings)."""
        return sum(r.cost_usd for r in self.rows if r.cost_estimated)

    @property
    def total_seconds(self) -> float:
        return sum(r.seconds for r in self.rows)

    def _slowest_slot_chain(self) -> float:
        """The slowest retrieval slot: search, rank, then the slowest of its overlapping lookups
        (in web_search mode, the single search call)."""
        chains: dict[str, float] = {}
        lookups: dict[str, float] = {}
        for r in self.rows:
            if r.slot is None:
                continue
            if r.kind == "lookup":
                lookups[r.slot] = max(lookups.get(r.slot, 0.0), r.seconds)
            else:
                chains[r.slot] = chains.get(r.slot, 0.0) + r.seconds
        return max(
            (chains.get(s, 0.0) + lookups.get(s, 0.0) for s in {*chains, *lookups}), default=0.0
        )

    @property
    def perception_retrieval_seconds(self) -> float:
        """Perception plus the slowest retrieval slot (what the 30 s proposal talks about)."""
        perception = sum(r.seconds for r in self.rows if r.stage == "perception")
        return perception + self._slowest_slot_chain()

    @property
    def critical_path_seconds(self) -> float:
        """Wall time of the pipeline: prepare_image, perception, the slowest retrieval slot chain,
        the proposal, the self-check (its style call and code checks run in parallel: the slower
        one counts), then, if a revision ran, the revision and its re-check.

        `total_seconds` (the plain sum) double-counts all the parallel work and is kept alongside.
        """
        sequential = sum(r.seconds for r in self.rows if r.slot is None and r.group is None)
        grouped: dict[str, float] = {}
        for r in self.rows:
            if r.group is not None:
                grouped[r.group] = max(grouped.get(r.group, 0.0), r.seconds)
        return sequential + sum(grouped.values()) + self._slowest_slot_chain()

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["totals"] = {
            "cost_usd": self.total_cost_usd,
            "anthropic_cost_usd": self.anthropic_cost_usd,
            "provider_cost_usd_estimated": self.provider_cost_usd,
            "seconds": self.total_seconds,  # plain sum of stage seconds
            "critical_path_seconds": self.critical_path_seconds,
            "wall_clock_seconds": self.wall_clock_seconds,  # measured end to end
        }
        data["proposed_targets_informational"] = PROPOSED_TARGETS
        return data


def _row(stage: str, c: Completion, seconds: float) -> StageRow:
    return StageRow(
        stage,
        c.model,
        c.input_tokens,
        c.output_tokens,
        c.web_searches,
        c.cost_usd,
        seconds,
        c.time_to_first_token_s,
    )


def run_spike(
    photo: Path | bytes,
    *,
    budget_usd: float | None = None,
    style: str = "",
    slots: int | None = None,
    config: Config | None = None,
    run_id: str | None = None,
    perception_only: bool = False,
    with_proposal: bool = True,
    keep: list[str] | None = None,
    replace: list[str] | None = None,
    replace_matching: bool = False,
) -> SpikeResult:
    """Run the stages under one shared, thread-safe `Budget` (step + cost caps cover the run).

    Retrieval slots run concurrently. With `perception_only`, retrieval is skipped; with
    `with_proposal=False` the run stops after retrieval (no proposal, self-check or revision).
    `keep` / `replace` name existing furniture (ids or categories); an unknown or contradictory
    name raises `PreferenceError` once perception has run (the ids come from perception).
    """
    config = config or Config.from_env()
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    freeplay = budget_usd is None
    n_slots = slots if slots is not None else config.items_for(freeplay)
    if freeplay:
        # Caps scale with the number of pieces (never below the configured values): a rank call
        # per slot plus proposal, style and a revision; a search and up to two lookups per slot.
        # Untuned guesses; budget mode keeps the configured caps unchanged.
        config = dc_replace(
            config,
            max_steps=max(config.max_steps, n_slots + FREEPLAY_STEP_OVERHEAD),
            max_provider_cost_usd=max(
                config.max_provider_cost_usd, round(n_slots * FREEPLAY_PROVIDER_USD_PER_SLOT, 2)
            ),
        )
    budget = Budget(config)

    result = SpikeResult(
        run_id=run_id,
        llm_mode=config.llm_mode,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        photo_name=photo.name if isinstance(photo, Path) else "<bytes>",
        budget_usd=budget_usd,
        style=style,
        prompt_versions={},
        models={
            "perception": config.model_perception,
            "retrieval": config.rank_model
            if config.retrieval_mode == "shopping"
            else config.model_retrieval,
        },
        image={},
        perception_only=perception_only,
        retrieval_mode=config.retrieval_mode,
        search_provider=config.search_provider if config.retrieval_mode == "shopping" else None,
        mode="freeplay" if freeplay else "budget",
        price_tier=config.price_tier if freeplay else None,
        items=n_slots,
        caps={
            "max_cost_usd": config.max_cost_usd,
            "max_provider_cost_usd": config.max_provider_cost_usd,
        },
    )
    if perception_only:
        result.models.pop("retrieval")
    elif with_proposal:
        result.models["proposal"] = config.model_proposal
        result.models["style_check"] = config.model_stylecheck
    result.unpriced_models = [m for m in set(result.models.values()) if not is_priced(m)]

    with span("spike.run", config=config, run_id=run_id, llm_mode=config.llm_mode) as run_rec:
        run_started = t0 = time.perf_counter()
        with span("stage.prepare_image", config=config, run_id=run_id) as img_rec:
            prepared: PreparedImage = prepare_image(photo, max_edge=config.image_max_edge)
            img_rec.update(prepared.summary())
        result.image = prepared.summary()
        result.rows.append(StageRow("prepare_image", "-", 0, 0, 0, 0.0, time.perf_counter() - t0))

        t0 = time.perf_counter()
        perceived: PerceptionResult = perceive(prepared, budget=budget, run_id=run_id)
        result.rows.append(_row("perception", perceived.completion, time.perf_counter() - t0))
        result.prompt_versions["perception"] = perceived.prompt_version
        result.perception = perceived.perception.model_dump()
        result.perception_output_stats = perceived.output_stats
        result.perception_raw_text = perceived.completion.text

        if perception_only:
            result.steps = budget.steps
            result.wall_clock_seconds = time.perf_counter() - run_started
            run_rec.update(
                total_cost_usd=round(result.total_cost_usd, 5),
                critical_path_seconds=round(result.critical_path_seconds, 3),
                steps=budget.steps,
                perception_only=True,
            )
            return result

        prefs = resolve_preferences(perceived.perception, keep, replace, replace_matching)
        result.preferences = prefs.to_json()
        set_notes = [str(w["text"]) for w in prefs.set_warnings]
        result.warnings.extend(set_notes)
        result.kept_items = [{"id": r.id, "category": r.category} for r in prefs.decided("keep")]

        retrieved: RetrievalResult = retrieve(
            perceived.perception,
            budget_usd=budget_usd,
            style=style,
            max_slots=n_slots,
            budget=budget,
            run_id=run_id,
            preferences=prefs,
        )
        if retrieved.plan is not None:
            result.slot_plan = retrieved.plan.to_json()
        if result.slot_plan:
            result.slot_plan["price_stats"] = {
                sr.slot.category: sr.summary.get("price_stats") for sr in retrieved.slots
            }
            if freeplay:
                result.slot_plan["tier_bands"] = {
                    sr.slot.category: sr.summary.get("tier_band") for sr in retrieved.slots
                }
        for sr in retrieved.slots:
            for issue in sr.summary.get("lookup_issues", []):
                if not issue["why"].startswith("http error"):  # those are counted just below
                    result.warnings.append(
                        f"lookup returned no store for {issue['title']} ({issue['why']})"
                    )
        failures = [e for sr in retrieved.slots for e in sr.summary.get("lookup_errors", [])]
        if failures:
            kinds = ", ".join(sorted(set(failures)))
            result.warnings.append(
                f"{len(failures)} product lookup(s) failed ({kinds}); those candidates keep "
                "their Google page link instead of a retailer link"
            )
        result.prompt_versions["rank" if retrieved.mode == "shopping" else "retrieval"] = (
            retrieved.prompt_version
        )
        for sr in retrieved.slots:
            lookup_no = 0
            for call in sr.calls:
                suffix = call.kind
                if call.kind == "lookup":
                    lookup_no += 1
                    suffix = f"lookup {lookup_no}"
                if call.phase:
                    suffix += f" ({call.phase})"
                result.rows.append(
                    StageRow(
                        f"retrieval: {sr.slot.category} / {suffix}",
                        call.provider,
                        call.input_tokens,
                        call.output_tokens,
                        call.web_searches,
                        call.cost_usd,
                        call.seconds,
                        call.time_to_first_token_s,
                        slot=sr.slot.category,
                        kind=call.kind,
                        cost_estimated=call.estimated,
                    )
                )
            result.retrieval.append(
                {
                    "slot": asdict(sr.slot),
                    "candidates": [c.model_dump() for c in sr.candidates],
                    "dropped_unverified": sr.dropped,
                    "note": sr.note,
                    "searches": sr.completion.searches,
                    "summary": sr.summary,
                    "over_budget_options": [o.model_dump() for o in sr.over_budget_options],
                    "unverified_alternates": [c.model_dump() for c in sr.unverified_alternates],
                }
            )
        if with_proposal:
            ran = propose_and_check(
                perceived.perception,
                retrieved,
                budget_usd=budget_usd,
                style=style,
                budget=budget,
                run_id=run_id,
                kept=[(k["id"], k["category"]) for k in result.kept_items],
                set_notes=set_notes,
                room_image=prepared,
                thumbnails={
                    (sr.slot.category, pid): img
                    for sr in retrieved.slots
                    for pid, img in sr.thumbnails.items()
                },
            )
            result.prompt_versions.update(ran.prompt_versions)
            for call in ran.calls:
                result.rows.append(
                    StageRow(
                        call.label,
                        call.provider,
                        call.input_tokens,
                        call.output_tokens,
                        call.web_searches,
                        call.cost_usd,
                        call.seconds,
                        call.time_to_first_token_s,
                        kind=call.kind,
                        group=call.group,
                    )
                )
            result.proposal = ran.proposal.model_dump()
            result.proposal_dropped = ran.dropped
            result.seller_concentration = seller_concentration(ran.proposal.items)
            picks = sum(1 for i in ran.proposal.items if i.pick is not None)
            if retrieved.plan is not None and picks < n_slots:
                empty = [i.slot for i in ran.proposal.items if i.pick is None]
                message = item_count_message(retrieved.plan.report, picks, empty)
                result.item_count_report = {
                    **retrieved.plan.report, "found": picks, "empty_slots": empty,
                    "message": message,
                }  # fmt: skip
        result.steps = budget.steps
        result.wall_clock_seconds = time.perf_counter() - run_started
        run_rec.update(
            total_cost_usd=round(result.total_cost_usd, 5),
            anthropic_cost_usd=round(result.anthropic_cost_usd, 5),
            provider_cost_usd_estimated=round(result.provider_cost_usd, 5),
            total_seconds=round(result.total_seconds, 3),
            critical_path_seconds=round(result.critical_path_seconds, 3),
            wall_clock_seconds=round(result.wall_clock_seconds, 3),
            steps=budget.steps,
        )
    return result


def _option_line(option: dict[str, Any]) -> str:
    cand = option["candidate"]
    if option["shipping_known"]:
        price = f"${cand['price']:.2f} + ${(option['item_total'] - cand['price']):.2f} shipping"
        price += f" = ${option['item_total']:.2f}"
    else:
        price = f"${cand['price']:.2f}, shipping unknown"
    why = "over per-item limit" if option["reason"] == "over_item_limit" else (
        "over total cap after shipping"
    )  # fmt: skip
    if option["also_over_total_budget"]:
        why += "; also over your total budget"
    return f"       - {cand['name']} - {cand['retailer']}: {price}   {cand['url']}  [{why}]"


def _empty_slot_lines(n: int, item: dict[str, Any]) -> list[str]:
    """An empty slot, explained by code (never by the model): the limit, what was seen, and the
    closest real products over the limit, which are shown but never counted."""
    slot = item["slot"]
    lines = [f"{n}. {slot}: nothing found within your ${item['limit_usd']:.0f} per-item limit"]
    if item.get("replaces"):
        lines.append(f"     Replaces: {item['replaces']['category']} ({item['replaces']['id']})")
    stats = item.get("price_stats") or {}
    options = item.get("over_budget_options") or []
    priced = stats.get("priced") or 0
    all_over = priced > 0 and (stats.get("over_ceiling") or 0) >= priced
    if all_over or any(o["reason"] == "over_total_cap" for o in options):
        seen = stats.get("min_usd")
        if seen is not None:
            lines.append(
                f"     Cheapest {slot} seen: ${seen:.2f} (median ${stats['median_usd']:.2f}, "
                f"{priced} results). Raise the budget or loosen the style to fit one."
            )
    elif item.get("note"):
        lines.append(f"     {item['note']}")
    if options:
        lines.append("     Over your limit, not counted in the total:")
        lines += [_option_line(o) for o in options]
    return lines


def format_final_list(
    proposal: dict[str, Any],
    *,
    mock: bool = False,
    kept: list[dict[str, str]] | None = None,
    warnings: list[str] | None = None,
) -> str:
    """The proposal as a short, human-readable list. Failed checks are always shown."""
    totals = proposal["totals"]
    header = "FINAL LIST" + ("  (MOCK DATA: fixtures, not real products)" if mock else "")
    if warnings:
        header += f"  [WARNING: {len(warnings)} run warning(s) above]"
    lines = [header]
    for n, item in enumerate(proposal["items"], start=1):
        pick = item["pick"]
        if pick is None and item.get("no_finalists") and item.get("limit_usd") is not None:
            lines += _empty_slot_lines(n, item)
            continue
        if pick is None:
            lines.append(f"{n}. {item['slot']}: (none) - {item['note'] or 'no pick'}")
            if item.get("replaces"):
                lines.append(
                    f"     Replaces: {item['replaces']['category']} ({item['replaces']['id']})"
                )
            if item["rationale"]:
                lines.append(f"     why: {item['rationale']}")
            continue
        shipping = pick["shipping"]
        status = pick.get("shipping_status", "unknown")
        if status == "free":
            ship = "free shipping"
        elif status == "unknown" or shipping is None:
            ship = "shipping unknown"
        else:
            ship = f"+ ${shipping:.2f} shipping"
        link = pick["url"] or "(no link)"
        kind = "" if pick["url_kind"] == "retailer_product" else f"  [{pick['url_kind']}]"
        replaced = item.get("replaces")
        lines += [
            f"{n}. {item['slot']}: {pick['name']} - {pick['retailer']}",
            f"     ${pick['price']:.2f} {ship} = ${item['item_total']:.2f}   {link}{kind}",
            f"     why: {item['rationale']}",
            f"     fit: {item['fit_status']}"
            + (f" - {item['fit_note']}" if item["fit_note"] else "")
            + f"   style: {item['style_match']}"
            + (f" ({item['style_basis']})" if item.get("style_basis", "none") != "none" else "")
            + (f" - {item['style_note']}" if item["style_note"] else "")
            + (
                f"; clash {item['style_clash']} with {item['style_clash_with'] or 'a kept item'}"
                if item.get("style_clash", "none") != "none"
                else ""
            ),
        ]
        if replaced:
            lines.insert(
                len(lines) - 3,
                f"     Replaces: {replaced['category']} ({replaced['id']})",
            )
        if item.get("replace_size_note"):
            lines.append(f"     size note: {item['replace_size_note']}")
        for alt in item.get("unverified_alternates") or []:
            lines.append(
                f"     alternate (retailer link on request): {alt['name']} - {alt['retailer']}, "
                f"${alt['price']:.2f} at search   {alt['url']}  [{alt['url_kind']}]"
            )
        lines += [f"     WARNING {w}" for w in item["warnings"]]
    if kept:
        lines.append("Keeping: " + ", ".join(f"{k['category']} ({k['id']})" for k in kept))
    unknown = totals.get("item_count", 0) - totals.get("shipping_known_count", 0)
    if totals.get("budget") is None:  # freeplay: no budget or remaining line
        if unknown > 0:
            lines.append(
                f"Total: ${totals['total']:.2f} (items ${totals['items_total']:.2f}, shipping "
                f"known for {totals['shipping_known_count']} of {totals['item_count']} items, "
                "unknown excluded)"
            )
        else:
            lines.append(
                f"Total: ${totals['total']:.2f} (items ${totals['items_total']:.2f}, "
                f"shipping ${totals['shipping_total']:.2f})"
            )
    elif unknown > 0:
        lines.append(
            f"Total: ${totals['total']:.2f} of ${totals['budget']:.2f} budget "
            f"(items ${totals['items_total']:.2f}, shipping known for "
            f"{totals['shipping_known_count']} of {totals['item_count']} items, unknown "
            f"excluded; ${totals['remaining']:.2f} remaining)"
        )
    else:
        lines.append(
            f"Total: ${totals['total']:.2f} of ${totals['budget']:.2f} budget "
            f"(${totals['remaining']:.2f} remaining; items ${totals['items_total']:.2f}, "
            f"shipping ${totals['shipping_total']:.2f})"
        )
    sellers: dict[str, int] = {}
    for item in proposal["items"]:
        if item["pick"] is not None:
            name = item["pick"]["retailer"] or "?"
            sellers[name] = sellers.get(name, 0) + 1
    if sellers:
        total_picks = sum(sellers.values())
        top, top_n = max(sellers.items(), key=lambda kv: kv[1])
        others = ", ".join(f"{k} {v}" for k, v in sellers.items() if k != top)
        lines.append(
            f"Sellers: {top} {top_n} of {total_picks} picks ({top_n / total_picks:.0%})"
            + (f"; others: {others}" if others else "")
        )
    if proposal["summary"]:
        lines.append(f"Summary: {proposal['summary']}")
    checks = ", ".join(f"{k} {v}" for k, v in proposal["check_summary"].items())
    revised = "yes" if proposal["revised"] else "no"
    lines.append(f"Checks: {checks}   Revised: {revised}")
    lines += [f"  revision: {c}" for c in proposal["revision_changes"]]
    lines += [f"WARNING {w}" for w in proposal["warnings"]]
    return "\n".join(lines)


def format_table(result: SpikeResult) -> str:
    """Per-stage table, totals, and the informational comparison to the proposed targets."""
    headers = [
        "stage", "model", "in tok", "out tok", "searches", "dollars", "seconds",
        "time_to_first_token",
    ]  # fmt: skip
    body = [
        [r.stage, r.model, str(r.input_tokens), str(r.output_tokens), str(r.web_searches),
         f"${r.cost_usd:.4f}{'*' if r.cost_estimated else ''}", f"{r.seconds:.2f}",
         "-" if r.time_to_first_token_seconds is None else f"{r.time_to_first_token_seconds:.2f}"]
        for r in result.rows
    ]  # fmt: skip
    totals = [
        "TOTAL", "",
        str(sum(r.input_tokens for r in result.rows)),
        str(sum(r.output_tokens for r in result.rows)),
        str(sum(r.web_searches for r in result.rows)),
        f"${result.total_cost_usd:.4f}", f"{result.total_seconds:.2f}", "",
    ]  # fmt: skip
    rows = [headers, *body, totals]
    widths = [max(len(r[i]) for r in rows) for i in range(len(headers))]

    def fmt(r: list[str]) -> str:
        cells = [c.ljust(widths[i]) if i < 2 else c.rjust(widths[i]) for i, c in enumerate(r)]
        return "  ".join(cells)

    rule = "  ".join("-" * w for w in widths)
    lines: list[str] = []
    if result.llm_mode == "mock":
        lines += [
            "*** MOCK MODE: no API call was made. Tokens, dollars and seconds below are fake",
            "*** plumbing values, not measurements. Candidates are fake (retailer MOCK).",
            "",
        ]
    lines += [
        f"run {result.run_id}  mode={result.llm_mode}  photo={result.photo_name}  "
        + (
            f"freeplay tier={result.price_tier} items={result.items}"
            if result.mode == "freeplay"
            else f"budget=${(result.budget_usd or 0):.0f} items={result.items}"
        )
        + f"  style={result.style!r}"
        + ("" if result.perception_only else f"  retrieval={result.retrieval_mode}")
        + (f"/{result.search_provider}" if result.search_provider else "")
        + ("  [perception only]" if result.perception_only else ""),
        f"prompts: {result.prompt_versions}  image: {result.image.get('width')}x"
        f"{result.image.get('height')} (from {result.image.get('original_width')}x"
        f"{result.image.get('original_height')})  steps: {result.steps}",
        "",
        fmt(headers), rule, *[fmt(r) for r in body], rule, fmt(totals),
    ]  # fmt: skip
    caps = result.caps or {}
    cap_a, cap_p = caps.get("max_cost_usd", 0.0), caps.get("max_provider_cost_usd", 0.0)
    lines += [
        "",
        f"Anthropic cost: ${result.anthropic_cost_usd:.4f} (cap ${cap_a:.2f})   "
        f"Provider cost (estimated): ${result.provider_cost_usd:.4f} (cap ${cap_p:.2f})",
    ]
    for w in result.warnings:
        lines += ["", f"WARNING {w}"]
    plan = result.slot_plan
    if result.item_count_report:
        lines += ["", f"NOTE {result.item_count_report['message']}"]
    elif plan and plan.get("notes"):
        for note in plan["notes"]:
            lines += ["", f"NOTE {note}"]
    if plan and plan.get("dropped_by_cap"):
        lines += [
            "",
            f"NOTE slots over the cap of {len(plan['slots'])} were dropped: "
            + "; ".join(plan["dropped_by_cap"]),
        ]
    if result.proposal:
        lines += [
            "",
            format_final_list(
                result.proposal,
                mock=result.llm_mode == "mock",
                kept=result.kept_items,
                warnings=result.warnings,
            ),
        ]
    if any(r.cost_estimated for r in result.rows):
        lines += [
            "",
            "* estimated: provider calls (search, lookup) are costed from",
            "  APP_SERPER_USD_PER_QUERY / APP_SERPAPI_USD_PER_SEARCH, which are assumptions;",
            "  free-tier usage costs $0 in reality.",
        ]
    if result.unpriced_models:
        lines += [
            "",
            f"WARNING: no price for {result.unpriced_models}; costed at the highest known rate",
        ]

    lines += [
        "",
        "Timing (seconds): the TOTAL row is the plain sum of stage seconds; retrieval slots run",
        "concurrently, so the real wait is the critical path.",
        f"  sum of stage seconds : {result.total_seconds:.2f}",
        f"  critical path        : {result.critical_path_seconds:.2f}  "
        + (
            "(prepare_image, perception, slowest slot, proposal, self-check, revision if any)"
            if result.proposal
            else "(prepare_image, perception, slowest slot)"
        ),
        f"  wall clock (measured): {result.wall_clock_seconds:.2f}",
    ]
    if result.perception_output_stats:
        lines.append(f"  perception output    : {result.perception_output_stats}")
    if result.perception_only:
        return "\n".join(lines)

    pr = [r for r in result.rows if r.stage == "perception" or r.stage.startswith("retrieval")]
    pr_cost = sum(r.cost_usd for r in pr)
    pr_secs = result.perception_retrieval_seconds
    t = PROPOSED_TARGETS
    lines += [
        "",
        "Proposed targets, informational only (not locked, not enforced; SPEC.md):",
        f"  perception+retrieval cost : ${pr_cost:.4f}  "
        f"vs proposed <= ${t['perception_plus_retrieval_cost_usd']:.2f}",
        f"  perception+retrieval time : {pr_secs:.1f}s (critical path)  "
        f"vs proposed <= {t['perception_plus_retrieval_seconds']:.0f}s",
        f"  full-run mean cost / p95  : n/a for one run "
        f"(proposed <= ${t['full_run_mean_cost_usd']:.2f} mean, "
        f"p95 <= {t['full_run_p95_seconds']:.0f}s)",
    ]
    return "\n".join(lines)


def write_outputs(result: SpikeResult, out_root: Path) -> Path:
    """Write result.json and table.txt under `out_root/<run_id>/` (trace.jsonl goes there too)."""
    out_dir = out_root / result.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(json.dumps(result.to_json(), indent=2, default=str) + "\n")
    (out_dir / "table.txt").write_text(format_table(result) + "\n")
    return out_dir


def point_trace_at(out_root: Path, run_id: str) -> Path:
    """Send this run's trace lines to `out_root/<run_id>/trace.jsonl`."""
    path = out_root / run_id / "trace.jsonl"
    tracing.TRACE_PATH = path
    return path
