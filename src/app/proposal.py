"""Proposal stage: one structured call picks the final list; code builds it from real data.

The model sees each slot's finalists (id, title, retailer, price, shipping, total, rating, link,
stated size) and returns, per slot, a pick id, alternate ids, a rationale and a fit note. Code then
validates every id against that slot's own finalists and builds each item from the real
`ProductCandidate`: the model never supplies a price, seller or URL (anything extra it writes is
ignored). After the proposal, `propose_and_check` runs the self-check and, on failure, up to
`Config.max_revisions` automatic revisions (default 1). Failures that remain are shown as warnings.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.images import PreparedImage
from app.llm import Budget, LLMOutputError, complete
from app.prompts import load_prompt
from app.retrieval import CallRecord, RetrievalResult
from app.schemas import (
    CheckResult,
    OverBudgetOption,
    ProductCandidate,
    Proposal,
    ProposalItem,
    ProposalOutput,
    ProposalTotals,
    ProposedSlot,
    ReplacedItem,
    RoomPerception,
    SchemaError,
    parse_model,
    to_output_schema,
)
from app.selfcheck import (
    SelfCheckRun,
    StyleContext,
    apply_results,
    grounded_problems,
    item_shipping,
    item_total,
    run_self_check,
    summarize,
)
from app.tracing import span

PROPOSAL_MAX_TOKENS = 3000
# A replacement whose longest side differs from the old piece's width by more than this gets a
# note (never a failure). UNTUNED GUESS.
REPLACE_SIZE_TOLERANCE = 0.25


@dataclass
class SlotInput:
    name: str
    reason: str
    note: str  # why the slot has no finalists, when it has none
    finalists: dict[str, ProductCandidate]  # ordered; keys are ids scoped to this slot
    replaces: ReplacedItem | None = None  # the existing item this slot would replace
    limit_usd: float | None = None  # the slot's per-item price ceiling
    price_stats: dict[str, float | int | None] | None = None
    over_budget_options: list[OverBudgetOption] = field(default_factory=list)  # display only
    unverified_alternates: list[ProductCandidate] = field(default_factory=list)  # display only


def slot_inputs(
    retrieved: RetrievalResult, perception: RoomPerception | None = None
) -> list[SlotInput]:
    """Finalists per slot, keyed by id. Candidates without a product id (web_search mode) get a
    synthetic id, "<slot number>.<position>", so the model can still name them."""
    out: list[SlotInput] = []
    for s_index, sr in enumerate(retrieved.slots, start=1):
        finalists: dict[str, ProductCandidate] = {}
        for c_index, cand in enumerate(sr.candidates, start=1):
            cid = cand.product_id or f"{s_index}.{c_index}"
            while cid in finalists:
                cid += "'"
            finalists[cid] = cand
        replaces = None
        if perception is not None and sr.slot.replaces_id is not None:
            old = next((f for f in perception.furniture if f.id == sr.slot.replaces_id), None)
            if old is not None:
                replaces = ReplacedItem(
                    id=old.id,
                    category=old.category,
                    width_m=old.size.width_m,
                    depth_m=old.size.depth_m,
                )
        out.append(
            SlotInput(
                sr.slot.category,
                sr.slot.reason,
                sr.note,
                finalists,
                replaces,
                sr.slot.budget_usd,
                sr.summary.get("price_stats"),
                list(sr.over_budget_options),
                list(sr.unverified_alternates),
            )
        )
    return out


def _size(c: ProductCandidate) -> str:
    d = c.dimensions_m
    if d is None:
        return "unknown"
    if d.width_m is not None and d.depth_m is not None:
        return f"{d.width_m:.2f} x {d.depth_m:.2f} m"
    longest = max((v for v in (d.width_m, d.depth_m, d.length_m) if v is not None), default=None)
    if longest is None:
        return "unknown"
    approx = "approximate, from the URL" if c.size_confidence == "low" else "stated"
    return f"longest side {longest:.2f} m ({approx})"


def _link(c: ProductCandidate) -> str:
    return {"retailer_product": "retailer", "google_panel": "google", "none": "none"}[c.url_kind]


def _candidate_line(cid: str, c: ProductCandidate) -> str:
    total, _ = item_total(c)
    shipping = item_shipping(c)
    rating = f"rating {c.rating:g} ({c.rating_count})" if c.rating is not None else "no rating"
    return (
        f"- id: {cid} | {c.name[:100]} | retailer: {c.retailer or '?'} | price {c.currency} "
        f"{c.price:.2f} | shipping {'unknown' if shipping is None else f'{shipping:.2f}'} "
        f"| total {total:.2f} | {rating} | link: {_link(c)} | size: {_size(c)}"
    )


def proposal_user_text(
    perception: RoomPerception,
    budget_usd: float | None,
    style: str,
    slots: list[SlotInput],
    previous: list[ProposalItem] | None = None,
    failed: list[CheckResult] | None = None,
    kept: list[tuple[str, str]] | None = None,
    set_notes: list[str] | None = None,
) -> str:
    d = perception.dimensions
    lines = [
        (
            "No budget set: choose by style, quality and fit; do not mention money."
            if budget_usd is None
            else f"Total budget: {budget_usd:.2f} USD (picked totals must stay within it)"
        ),
        f"Style goal: {style}",
        f"Room: {perception.room_type}, about {d.width_m:.1f} m x {d.length_m:.1f} m "
        f"(confidence {d.confidence:.1f}), ceiling {d.ceiling_height_m:.1f} m",
        f"Existing furniture: {', '.join(f.category for f in perception.furniture) or 'none'}",
        f"Existing decor: {', '.join(x.kind for x in perception.art_and_decor) or 'none'}",
        f"Current style: {', '.join(perception.style_descriptors) or 'unknown'}",
    ]
    if kept:
        lines.append("Keeping (stays in the room): " + ", ".join(f"{c} ({i})" for i, c in kept))
    for note in set_notes or []:  # data, not instructions: the summary should acknowledge it
        lines.append(f"Set note: {note}")
    for slot in slots:
        lines += ["", f"Slot: {slot.name}", f"Why this slot: {slot.reason}"]
        if slot.replaces is not None:
            rep = slot.replaces
            lines.append(
                f"Replaces: {rep.category} ({rep.id}), "
                f"about {rep.width_m:.1f} m x {rep.depth_m:.1f} m"
            )
        if not slot.finalists:
            # An empty slot is data, not a prompt rewrite: nothing outside the lists exists.
            lines.append(
                "Finalists: none (empty slot: nothing was found within the per-item limit; do not "
                "recommend or mention any product for it)"
            )
        else:
            lines.append("Finalists:")
            lines += [_candidate_line(cid, c) for cid, c in slot.finalists.items()]
    if previous is not None:
        lines += ["", "Previous proposal:"]
        for item in previous:
            pick = f"{item.pick_id} ({item.pick.name[:60]})" if item.pick else "empty"
            lines.append(f"- {item.slot}: {pick}")
        lines += ["", "Failed checks:"]
        for r in failed or []:
            where = r.item or "whole proposal"
            lines.append(f"- [{r.check}] {where}: {r.message}")
    return "\n".join(lines)


def validate_proposal(
    output: ProposalOutput, slots: list[SlotInput]
) -> tuple[list[ProposalItem], list[dict[str, str]]]:
    """Build items from real candidates. Returns (items in slot order, dropped model output)."""
    dropped: list[dict[str, str]] = []
    known = {s.name.strip().lower(): s for s in slots}
    proposed: dict[str, ProposedSlot] = {}
    for ps in output.slots:
        key = ps.slot.strip().lower()
        if key not in known:
            dropped.append({"slot": ps.slot, "reason": "unknown_slot"})
        elif key not in proposed:
            proposed[key] = ps
    items: list[ProposalItem] = []
    for slot in slots:
        chosen = proposed.get(slot.name.strip().lower())
        if not slot.finalists:
            items.append(
                ProposalItem(
                    slot=slot.name,
                    replaces=slot.replaces,
                    note=slot.note or "no finalists found",
                    no_finalists=True,
                    limit_usd=slot.limit_usd,
                    price_stats=slot.price_stats,
                    over_budget_options=slot.over_budget_options,
                )
            )
            continue
        if chosen is None:
            items.append(
                ProposalItem(
                    slot=slot.name,
                    replaces=slot.replaces,
                    note="the proposal did not address this slot",
                )
            )
            continue
        pick_id, pick, note = "", None, ""
        if chosen.pick_id:
            if chosen.pick_id in slot.finalists:
                pick_id, pick = chosen.pick_id, slot.finalists[chosen.pick_id]
            else:
                dropped.append(
                    {"slot": slot.name, "id": chosen.pick_id, "reason": "id_not_in_finalists"}
                )
                note = "the proposal named an unknown product; slot left empty"
        else:
            note = "left empty by the proposal"
        alt_ids: list[str] = []
        for aid in chosen.alternate_ids:
            if aid not in slot.finalists:
                dropped.append({"slot": slot.name, "id": aid, "reason": "id_not_in_finalists"})
            elif aid != pick_id and aid not in alt_ids:
                problems, _ = grounded_problems(slot.finalists[aid])
                if problems:  # an alternate must pass the same link and price rules as a pick
                    dropped.append(
                        {
                            "slot": slot.name,
                            "id": aid,
                            "reason": "alternate_failed_grounded: " + "; ".join(problems),
                        }
                    )
                else:
                    alt_ids.append(aid)
        items.append(
            ProposalItem(
                slot=slot.name,
                replaces=slot.replaces,
                limit_usd=slot.limit_usd,
                price_stats=slot.price_stats,
                unverified_alternates=slot.unverified_alternates if pick else [],
                pick_id=pick_id,
                pick=pick,
                alternate_ids=alt_ids,
                alternates=[slot.finalists[a] for a in alt_ids],
                rationale=chosen.rationale,
                model_fit_note=chosen.fit_note,
                note=note,
                item_total=item_total(pick)[0] if pick else None,
            )
        )
    return items, dropped


def _totals(items: list[ProposalItem], budget_usd: float | None) -> ProposalTotals:
    picks = [i.pick for i in items if i.pick is not None]
    items_total = sum(c.price for c in picks)
    shipping_total = sum(s for c in picks if (s := item_shipping(c)) is not None)
    total = sum(item_total(c)[0] for c in picks)
    known = sum(1 for c in picks if item_shipping(c) is not None)
    return ProposalTotals(
        items_total=round(items_total, 2),
        shipping_total=round(shipping_total, 2),
        total=round(total, 2),
        item_count=len(picks),
        shipping_known_count=known,
        shipping_note=(
            f"shipping known for {known} of {len(picks)} items; unknown shipping is excluded "
            "from the total, not counted as $0 in what you will pay"
            if known < len(picks)
            else ""
        ),
        budget=budget_usd,
        remaining=None if budget_usd is None else round(budget_usd - total, 2),
    )


def seller_concentration(items: list[ProposalItem]) -> dict[str, Any]:
    """Picks per seller and the largest seller's share (reported, never enforced across slots:
    slots run concurrently and each looks up only its top finalist, so balancing them would need a
    barrier before the lookups; ADR 0010)."""
    counts: dict[str, int] = {}
    for item in items:
        if item.pick is not None:
            name = item.pick.retailer or "?"
            counts[name] = counts.get(name, 0) + 1
    picks = sum(counts.values())
    largest = max(counts.items(), key=lambda kv: kv[1], default=("", 0))
    return {
        "picks": picks,
        "picks_by_seller": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "largest_seller": largest[0],
        "largest_share": round(largest[1] / picks, 2) if picks else 0.0,
    }


def annotate_replacements(items: list[ProposalItem]) -> None:
    """Note (never fail) when a replacement is much longer or shorter than the piece it replaces."""
    for item in items:
        if item.replaces is None or item.pick is None or item.pick.dimensions_m is None:
            continue
        d = item.pick.dimensions_m
        sides = [v for v in (d.width_m, d.depth_m, d.length_m) if v is not None]
        old = item.replaces.width_m
        if not sides or old <= 0:
            continue
        new = max(sides)
        change = (new - old) / old
        if abs(change) > REPLACE_SIZE_TOLERANCE:
            approx = (
                " (approximate, from the listing URL)" if item.pick.size_confidence == "low" else ""
            )
            item.replace_size_note = (
                f"new piece is about {new:.2f} m long{approx} vs your old {item.replaces.category} "
                f"at {old:.2f} m ({abs(change):.0%} {'longer' if change > 0 else 'shorter'})"
            )


def _describe_change(before: list[ProposalItem], after: list[ProposalItem]) -> list[str]:
    old = {i.slot: i for i in before}
    changes: list[str] = []
    for item in after:
        prev = old.get(item.slot)
        if prev is None or prev.pick_id == item.pick_id:
            continue
        was = prev.pick.name if prev.pick else "empty"
        now = item.pick.name if item.pick else "empty"
        changes.append(f"{item.slot}: {was} -> {now}")
    return changes


@dataclass
class ProposalRun:
    proposal: Proposal
    calls: list[CallRecord] = field(default_factory=list)
    prompt_versions: dict[str, str] = field(default_factory=dict)
    dropped: list[dict[str, str]] = field(default_factory=list)


def _call_model(
    perception: RoomPerception,
    budget_usd: float | None,
    style: str,
    slots: list[SlotInput],
    budget: Budget,
    *,
    kind: str,
    previous: list[ProposalItem] | None = None,
    failed: list[CheckResult] | None = None,
    kept: list[tuple[str, str]] | None = None,
    set_notes: list[str] | None = None,
) -> tuple[list[ProposalItem], str, list[dict[str, str]], CallRecord, str]:
    config = budget.config
    prompt = load_prompt("proposal", config.proposal_version_for(budget_usd is None))
    started = time.perf_counter()
    completion = complete(
        span_name=f"llm.{kind}",
        prompt=prompt,
        model=config.model_proposal,
        user_text=proposal_user_text(
            perception, budget_usd, style, slots, previous, failed, kept, set_notes
        ),
        budget=budget,
        max_tokens=PROPOSAL_MAX_TOKENS,
        schema=to_output_schema(ProposalOutput),
        effort=config.effort_proposal,
        stream=True,
    )
    seconds = round(time.perf_counter() - started, 3)
    output = parse_model(ProposalOutput, completion.text, what=kind)
    items, dropped = validate_proposal(output, slots)
    record = CallRecord(
        kind, kind, completion.model, completion.input_tokens, completion.output_tokens, 0,
        completion.cost_usd, seconds, completion.time_to_first_token_s,
    )  # fmt: skip
    return items, output.summary, dropped, record, prompt.version


def _check(
    items: list[ProposalItem],
    perception: RoomPerception,
    style: str,
    budget_usd: float | None,
    slots: list[SlotInput],
    budget: Budget,
    *,
    group: str,
    prefix: str,
    run_id: str | None,
    ctx: StyleContext | None = None,
) -> SelfCheckRun:
    ids = {s.name: set(s.finalists) for s in slots}
    return run_self_check(
        items, perception, style, budget_usd, ids, budget,
        group=group, label_prefix=prefix, run_id=run_id, style_context=ctx,
    )  # fmt: skip


def propose_and_check(
    perception: RoomPerception,
    retrieved: RetrievalResult,
    *,
    budget_usd: float | None,
    style: str,
    budget: Budget,
    run_id: str | None = None,
    kept: list[tuple[str, str]] | None = None,
    set_notes: list[str] | None = None,
    room_image: PreparedImage | None = None,
    thumbnails: dict[tuple[str, str], PreparedImage] | None = None,
) -> ProposalRun:
    """Proposal, self-check, and up to `max_revisions` revisions. Raises on Budget/schema errors
    in the first proposal; a failed revision keeps the previous proposal and says so. `kept` is
    (id, category) of existing furniture that stays, shown to the model so it does not duplicate."""
    config = budget.config
    slots = slot_inputs(retrieved, perception)
    # What the style check may look at: the room photo, prefetched thumbnails, the furniture that
    # stays (with the colours perception noted) and any set notes.
    kept_ids = {k[0] for k in kept or []}
    ctx = StyleContext(
        room_image=room_image,
        thumbnails=dict(thumbnails or {}),
        kept=[(f.id, f.category, f.note) for f in perception.furniture if f.id in kept_ids],
        set_notes=list(set_notes or []),
    )
    calls: list[CallRecord] = []
    dropped: list[dict[str, str]] = []
    versions: dict[str, str] = {}
    warnings: list[str] = []
    revised = False
    changes: list[str] = []

    with span(
        "stage.proposal", config=config, run_id=run_id, model=config.model_proposal,
        prompt_name="proposal", slots=[s.name for s in slots],
    ) as rec:  # fmt: skip
        if not any(s.finalists for s in slots):
            # nothing to choose from: carry every slot through, spend nothing
            items = [ProposalItem(slot=s.name, note=s.note or "no finalists found") for s in slots]
            summary = "No candidates were found for any slot."
            check = _check(items, perception, style, budget_usd, slots, budget,
                           group="selfcheck", prefix="", run_id=run_id, ctx=ctx)  # fmt: skip
            calls += check.calls
        else:
            items, summary, dropped, record, versions["proposal"] = _call_model(
                perception,
                budget_usd,
                style,
                slots,
                budget,
                kind="proposal",
                kept=kept,
                set_notes=set_notes,
            )
            calls.append(record)
            check = _check(items, perception, style, budget_usd, slots, budget,
                           group="selfcheck", prefix="", run_id=run_id, ctx=ctx)  # fmt: skip
            calls += check.calls
            versions["style_check"] = load_prompt(
                "style_check", config.style_prompt_version
            ).version

            for n in range(1, config.max_revisions + 1):
                if not check.failures:
                    break
                try:
                    new_items, new_summary, new_dropped, record, _ = _call_model(
                        perception, budget_usd, style, slots, budget, kind="revision",
                        previous=items, failed=check.failures, kept=kept, set_notes=set_notes,
                    )  # fmt: skip
                except (SchemaError, LLMOutputError) as exc:  # BudgetExceededError propagates
                    revised = True
                    warnings.append(f"revision failed ({exc}); kept the previous proposal")
                    break
                revised = True
                calls.append(record)
                dropped += [{**d, "stage": "revision"} for d in new_dropped]
                suffix = "" if n == 1 else f" {n}"
                new_check = _check(
                    new_items, perception, style, budget_usd, slots, budget,
                    group=f"recheck{suffix.strip()}", prefix=f"revision{suffix} / ", run_id=run_id,
                    ctx=ctx,
                )  # fmt: skip
                calls += new_check.calls
                if len(new_check.failures) > len(check.failures):
                    changes.append(
                        f"revision made things worse ({len(check.failures)} -> "
                        f"{len(new_check.failures)} failed checks); kept the previous proposal"
                    )
                    break
                changes += _describe_change(items, new_items) or ["revision changed no picks"]
                items, summary, check = new_items, new_summary or summary, new_check

        warnings += apply_results(items, check.results)
        annotate_replacements(items)
        proposal = Proposal(
            items=items,
            totals=_totals(items, budget_usd),
            summary=summary,
            checks=check.results,
            check_summary=summarize(check.results),
            revised=revised,
            revision_changes=changes,
            warnings=warnings,
        )
        rec.update(
            picks=sum(1 for i in items if i.pick is not None),
            dropped_ids=dropped,
            revised=revised,
            check_summary=proposal.check_summary,
            failures=len(check.failures),
        )
    return ProposalRun(proposal, calls, versions, dropped)
