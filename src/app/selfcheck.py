"""Self-check: four checks on a proposal (ADR 0006). Each returns pass, fail or unknown.

  budget    (code) total within budget; no single item over a fraction of it (unless the only one)
  fit       (code) coarse: stated product size against the room's size; unknown with no stated size
  style     (model) one text-only Haiku call; it cannot see images
  grounded  (code) the pick is a real finalist, its link is a retailer product page, its price came
            from provider data

The code checks and the style call run concurrently. Nothing here edits the proposal; failures are
returned as `CheckResult`s and attached to items as warnings by `apply_results`.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.categories import is_flat_item
from app.config import Config
from app.llm import Budget, Completion, LLMOutputError, complete
from app.prompts import load_prompt
from app.retrieval import CallRecord, is_product_page_url
from app.schemas import (
    CheckResult,
    ProductCandidate,
    ProposalItem,
    RoomPerception,
    SchemaError,
    StyleCheckOutput,
    parse_model,
    to_output_schema,
)
from app.tracing import span

EPS = 1e-9
STYLE_MAX_TOKENS = 1000
LOW_CONFIDENCE_NOTE = "room size is a low-confidence estimate"
NO_SIZE_NOTE = "no size found; check the product page"
FLAT_NOTE = "flat item; footprint rule not applied"
MISMATCH_NOTE = "store page title disagrees with search title (size or material)"

# Materials compared between a search title and the store page's title/slug ("oak" matches
# "smoked oak"). Sizes are compared only between those two texts; they are never used for fit.
MATERIALS = frozenset(
    "oak walnut ash maple pine teak birch cherry mahogany marble glass steel brass rattan linen"
    " leather".split()
)
_SIZE_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[\"\u201d\u2033]|inches|inch|in|cm|mm|w)(?![a-z])", re.I
)
_TO_INCHES = {"cm": 1 / 2.54, "mm": 1 / 25.4}
SIZE_TOLERANCE_IN = 1.0  # inches (or 3%, whichever is larger): titles round differently


def _sizes_in(text: str) -> set[float]:
    """Numbers with a unit (", in, inch, cm, mm, W) in `text`, as inches."""
    out: set[float] = set()
    for m in _SIZE_RE.finditer(text):
        unit = m.group("unit").lower()
        out.add(round(float(m.group("num")) * _TO_INCHES.get(unit, 1.0), 2))
    return out


def _slug_words(url: str) -> str:
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if not segments:
        return ""
    return (
        re.sub(r"\.(html?|php|aspx?)$", "", segments[-1], flags=re.I)
        .replace("-", " ")
        .replace("_", " ")
    )


def title_mismatch(search_title: str, store_title: str, url: str) -> bool:
    """True when the store page (its title plus URL slug) clearly disagrees with the search title.

    Disagreement means: both sides state sizes and none match, or both name materials and the
    sets are disjoint. A bare 2-3 digit number in the slug counts as a size in inches (slugs like
    `leif-54-media-unit`); in titles only numbers with a unit count. Silent when either side says
    nothing, so it only catches clear contradictions.
    """
    slug = _slug_words(url)
    store_text = f"{store_title} {slug}"
    search_sizes = _sizes_in(search_title)
    store_sizes = _sizes_in(store_text) | {
        float(t) for t in re.findall(r"(?<![\w.])\d{2,3}(?![\w.])", slug)
    }
    if (
        search_sizes
        and store_sizes
        and not any(
            abs(a - b) <= max(SIZE_TOLERANCE_IN, 0.03 * max(a, b))
            for a in search_sizes
            for b in store_sizes
        )
    ):
        return True
    search_mats = MATERIALS & set(re.findall(r"[a-z]+", search_title.lower()))
    store_mats = MATERIALS & set(re.findall(r"[a-z]+", store_text.lower()))
    return bool(search_mats and store_mats and search_mats.isdisjoint(store_mats))


def item_total(c: ProductCandidate) -> tuple[float, bool]:
    """(price plus shipping, shipping_unknown). Unknown shipping counts as 0."""
    if c.total is not None:
        return c.total, False
    if c.shipping is not None:
        return c.price + c.shipping, False
    return c.price, True


def item_shipping(c: ProductCandidate) -> float | None:
    if c.shipping is not None:
        return c.shipping
    if c.total is not None:
        return max(c.total - c.price, 0.0)
    return None


def _picked(items: list[ProposalItem]) -> list[tuple[ProposalItem, ProductCandidate]]:
    return [(i, i.pick) for i in items if i.pick is not None]


# ---- budget ---------------------------------------------------------------------------------


def check_budget(items: list[ProposalItem], budget_usd: float, config: Config) -> list[CheckResult]:
    picked = _picked(items)
    if not picked:
        return [CheckResult(check="budget", status="pass", message="no items picked")]
    totals = {i.slot: item_total(c) for i, c in picked}
    grand = sum(t for t, _ in totals.values())
    unknown = [slot for slot, (_, unk) in totals.items() if unk]
    ok = grand <= budget_usd + EPS
    results = [
        CheckResult(
            check="budget",
            status="pass" if ok else "fail",
            message=(
                f"total ${grand:.2f} is {'within' if ok else 'over'} the ${budget_usd:.2f} budget"
            ),
        )
    ]
    cap = config.item_cap_fraction * budget_usd
    for slot, (total, _) in totals.items():
        if len(picked) == 1:
            results.append(
                CheckResult(
                    check="budget",
                    status="pass",
                    item=slot,
                    message="only item; the per-item cap does not apply",
                )  # fmt: skip
            )
            continue
        over = total > cap + EPS
        results.append(
            CheckResult(
                check="budget",
                status="fail" if over else "pass",
                item=slot,
                message=(
                    f"${total:.2f} is over the per-item cap ${cap:.2f} "
                    f"({config.item_cap_fraction:.0%} of the budget)"
                    if over
                    else f"${total:.2f} is within the per-item cap ${cap:.2f}"
                ),
                warning=(
                    f"shipping unknown for {slot}; counted as $0" if slot in unknown else None
                ),
            )
        )
    return results


# ---- fit ------------------------------------------------------------------------------------


def check_fit(
    items: list[ProposalItem], perception: RoomPerception, config: Config
) -> list[CheckResult]:
    room = perception.dimensions
    shorter = min(room.width_m, room.length_m)
    area = room.width_m * room.length_m
    low = room.confidence < config.fit_low_confidence
    results: list[CheckResult] = []
    for item, cand in _picked(items):
        if is_flat_item(item.slot):
            results.append(
                CheckResult(check="fit", status="unknown", item=item.slot, message=FLAT_NOTE)
            )
            continue
        dims = cand.dimensions_m
        if dims is None or dims.width_m is None or dims.depth_m is None:
            results.append(
                CheckResult(check="fit", status="unknown", item=item.slot, message=NO_SIZE_NOTE)
            )
            continue
        longer = max(dims.width_m, dims.depth_m)
        footprint = dims.width_m * dims.depth_m
        reasons: list[str] = []
        if longer > config.fit_max_side_fraction * shorter + EPS:
            reasons.append(
                f"longer side {longer:.2f} m is over {config.fit_max_side_fraction:.0%} of the "
                f"room's shorter side ({shorter:.2f} m)"
            )
        if footprint > config.fit_max_footprint_fraction * area + EPS:
            reasons.append(
                f"footprint {footprint:.2f} m2 is over {config.fit_max_footprint_fraction:.0%} of "
                f"the floor area ({area:.1f} m2)"
            )
        message = "; ".join(reasons) if reasons else (
            f"{dims.width_m:.2f} x {dims.depth_m:.2f} m fits the room's proportions"
        )  # fmt: skip
        if low:
            message += f" ({LOW_CONFIDENCE_NOTE}: confidence {room.confidence:.1f})"
        results.append(
            CheckResult(
                check="fit", status="fail" if reasons else "pass", item=item.slot, message=message
            )
        )
    return results


# ---- grounded -------------------------------------------------------------------------------


def check_grounded(
    items: list[ProposalItem], finalist_ids: dict[str, set[str]]
) -> list[CheckResult]:
    """`finalist_ids` maps slot name to the ids of that slot's finalists."""
    results: list[CheckResult] = []
    for item, cand in _picked(items):
        problems: list[str] = []
        warning: str | None = None
        if item.pick_id not in finalist_ids.get(item.slot, set()):
            problems.append("pick is not among the slot's finalists")
        if cand.url_kind == "none":
            problems.append("no link")
        elif cand.url_kind == "google_panel":
            warning = "link is a Google Shopping page, not the retailer"
        elif not is_product_page_url(cand.url):
            problems.append("link does not look like a product page")
        if cand.url_kind == "retailer_product" and title_mismatch(
            cand.name, cand.store_title, cand.url
        ):
            problems.append(MISMATCH_NOTE)
        if cand.price_source not in ("search", "lookup"):
            problems.append("price did not come from provider data")
        results.append(
            CheckResult(
                check="grounded",
                status="fail" if problems else "pass",
                item=item.slot,
                message="; ".join(problems)
                if problems
                else "link and price come from provider data",
                warning=warning,
            )
        )
    return results


# ---- style (model) ------------------------------------------------------------------------------


def _style_user_text(items: list[ProposalItem], perception: RoomPerception, style: str) -> str:
    lines = [
        f"Style goal: {style}",
        f"Room's current style: {', '.join(perception.style_descriptors) or 'unknown'}",
        "",
        "Items:",
    ]
    for item, cand in _picked(items):
        lines.append(
            f"- slot: {item.slot} | {cand.name[:100]} | retailer: {cand.retailer or '?'} "
            f"| category: {item.slot}"
        )
    return "\n".join(lines)


def check_style(
    items: list[ProposalItem], perception: RoomPerception, style: str, budget: Budget
) -> tuple[list[CheckResult], Completion | None]:
    picked = _picked(items)
    if not picked:
        return [], None
    config = budget.config
    prompt = load_prompt("style_check")
    try:
        completion = complete(
            span_name="llm.style_check",
            prompt=prompt,
            model=config.model_stylecheck,
            user_text=_style_user_text(items, perception, style),
            budget=budget,
            max_tokens=STYLE_MAX_TOKENS,
            schema=to_output_schema(StyleCheckOutput),
            effort=config.effort_stylecheck,
            stream=True,
        )
        parsed = parse_model(StyleCheckOutput, completion.text, what="style_check")
    except (SchemaError, LLMOutputError) as exc:  # BudgetExceededError is not caught: it propagates
        return [
            CheckResult(
                check="style",
                status="unknown",
                item=i.slot,
                message=f"style check failed to run: {exc}",
            )  # fmt: skip
            for i, _ in picked
        ], None
    by_slot = {s.slot: s for s in parsed.items}
    results: list[CheckResult] = []
    for item, _ in picked:
        s = by_slot.get(item.slot)
        if s is None:
            results.append(
                CheckResult(
                    check="style",
                    status="unknown",
                    item=item.slot,
                    message="style check did not return this item",
                )  # fmt: skip
            )
        else:
            results.append(
                CheckResult(
                    check="style",
                    status="fail" if s.match == "poor" else "pass",
                    item=item.slot,
                    message=f"{s.match}: {s.note}",
                    value=s.match,
                )
            )
    return results, completion


# ---- orchestration -------------------------------------------------------------------------------


@dataclass
class SelfCheckRun:
    results: list[CheckResult]
    calls: list[CallRecord] = field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == "fail"]


def run_self_check(
    items: list[ProposalItem],
    perception: RoomPerception,
    style: str,
    budget_usd: float,
    finalist_ids: dict[str, set[str]],
    budget: Budget,
    *,
    group: str = "selfcheck",
    label_prefix: str = "",
    run_id: str | None = None,
) -> SelfCheckRun:
    """All four checks. The style call runs in a worker thread while the code checks run here."""
    config = budget.config
    with span("stage.self_check", config=config, run_id=run_id, group=group) as rec:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="style") as pool:
            started = time.perf_counter()
            style_future = pool.submit(check_style, items, perception, style, budget)

            code_started = time.perf_counter()
            code_results = [
                *check_budget(items, budget_usd, config),
                *check_fit(items, perception, config),
                *check_grounded(items, finalist_ids),
            ]
            code_seconds = round(time.perf_counter() - code_started, 4)

            style_results, completion = style_future.result()
            style_seconds = round(time.perf_counter() - started, 4)
        calls = [
            CallRecord(
                "code_check",
                f"{label_prefix}self-check / code",
                "code",
                seconds=code_seconds,
                group=group,
            )  # fmt: skip
        ]
        if completion is not None:
            calls.append(
                CallRecord(
                    "style_check",
                    f"{label_prefix}self-check / style",
                    completion.model,
                    completion.input_tokens,
                    completion.output_tokens,
                    0,
                    completion.cost_usd,
                    style_seconds,
                    completion.time_to_first_token_s,
                    group=group,
                )  # fmt: skip
            )
        results = [*code_results, *style_results]
        rec.update(
            failures=[(r.check, r.item) for r in results if r.status == "fail"],
            unknown=[(r.check, r.item) for r in results if r.status == "unknown"],
        )
    return SelfCheckRun(results, calls)


def apply_results(items: list[ProposalItem], results: list[CheckResult]) -> list[str]:
    """Write check outcomes onto the items; return proposal-level warnings.

    Failures are never dropped: an item-level failure becomes a warning on that item, a
    whole-proposal failure (for example the total) becomes a proposal-level warning.
    """
    by_slot = {i.slot: i for i in items}
    proposal_warnings: list[str] = []

    def add(item: ProposalItem, text: str) -> None:
        if text not in item.warnings:
            item.warnings.append(text)

    for r in results:
        item = by_slot.get(r.item) if r.item else None
        label = f"{r.check}: {r.message}"
        if item is None:
            if r.status == "fail":
                proposal_warnings.append(label)
            if r.warning:
                proposal_warnings.append(f"{r.check}: {r.warning}")
            continue
        if r.check == "fit":
            item.fit_status, item.fit_note = r.status, r.message
        elif r.check == "style" and r.value in ("good", "ok", "poor"):
            item.style_match = r.value  # type: ignore[assignment]
            item.style_note = r.message.split(":", 1)[-1].strip()
        elif r.check == "style":
            item.style_note = r.message
        if r.status == "fail":
            add(item, label)
        if r.warning:
            add(item, f"{r.check}: {r.warning}")
    return proposal_warnings


def summarize(results: list[CheckResult]) -> dict[str, str]:
    """Worst status per check (fail > unknown > pass); every check is present."""
    order = {"pass": 0, "unknown": 1, "fail": 2}
    out = {name: "pass" for name in ("budget", "fit", "style", "grounded")}
    for r in results:
        if order[r.status] > order[out[r.check]]:
            out[r.check] = r.status
    return out
