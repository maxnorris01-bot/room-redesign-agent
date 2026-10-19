"""Retrieval stage, configuration A: Claude's web search tool, one call per furniture slot.

Slots are derived in code (not by an LLM call) from the perception result, so the number of
searching calls is bounded and the derivation is inspectable. Every candidate URL must have been
returned by the search tool in that same call; anything else is dropped (the guard from the
Claim Verification repo, ADR 0003 there).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urldefrag

from app.llm import Budget, Completion, complete, web_search_tool
from app.prompts import load_prompt
from app.schemas import Candidate, RoomPerception, SlotCandidates, parse_model, to_output_schema
from app.tracing import span

RETRIEVAL_MAX_TOKENS = 3000

# Used only when perception marks nothing as missing or replaceable: common first purchases per
# room type, most impactful first. Style and budget then shape the search, not the category.
_DEFAULT_SLOTS: dict[str, list[str]] = {
    "living": ["sofa", "coffee table", "area rug", "floor lamp", "bookshelf"],
    "bed": ["bed frame", "nightstand", "dresser", "area rug", "reading lamp"],
    "office": ["desk", "office chair", "bookshelf", "desk lamp", "area rug"],
    "dining": ["dining table", "dining chairs", "sideboard", "pendant light", "area rug"],
}
_GENERIC_SLOTS = ["seating", "side table", "area rug", "lamp", "storage"]


@dataclass(frozen=True)
class Slot:
    category: str
    reason: str
    source: str  # "replaceable" | "gap" | "default"
    budget_usd: float
    size_hint: str = ""


@dataclass
class SlotResult:
    slot: Slot
    candidates: list[Candidate]
    dropped: list[dict[str, Any]]
    note: str
    completion: Completion
    seconds: float = 0.0


@dataclass
class RetrievalResult:
    slots: list[SlotResult] = field(default_factory=list)
    prompt_version: str = ""


def _norm(category: str) -> str:
    return " ".join(category.lower().split())


def _already_covered(category: str, taken: list[str]) -> bool:
    c = _norm(category)
    return any(c in t or t in c for t in taken)


def derive_slots(perception: RoomPerception, total_budget_usd: float, max_slots: int) -> list[Slot]:
    """Up to `max_slots` slots: replaceable furniture, then missing categories, then defaults.

    The budget is split equally across the chosen slots (a deliberately simple spike heuristic;
    a smarter split by category is a later concern). Furniture marked keepable is never a slot.
    """
    if max_slots < 1:
        return []
    picks: list[tuple[str, str, str, str]] = []  # category, reason, source, size_hint
    for item in perception.furniture:
        if item.keepable == "unlikely":
            hint = f"current one is about {item.size.width_m:.1f} m x {item.size.depth_m:.1f} m"
            picks.append(
                (
                    item.category,
                    f"replace existing {item.category}: {item.note}",
                    "replaceable",
                    hint,
                )
            )
    for gap in perception.furniture_gaps:
        picks.append((gap.category, gap.reason, "gap", ""))
    if not picks:
        room = perception.room_type.lower()
        defaults = next((v for k, v in _DEFAULT_SLOTS.items() if k in room), _GENERIC_SLOTS)
        owned = [_norm(f.category) for f in perception.furniture]
        picks = [(c, "common first purchase for this room type", "default", "") for c in defaults
                 if not _already_covered(c, owned)]  # fmt: skip

    chosen: list[tuple[str, str, str, str]] = []
    taken: list[str] = []
    for category, reason, source, hint in picks:
        if _already_covered(category, taken):
            continue
        chosen.append((category, reason, source, hint))
        taken.append(_norm(category))
        if len(chosen) == max_slots:
            break
    share = total_budget_usd / len(chosen) if chosen else 0.0
    return [Slot(c, r, s, round(share, 2), h) for c, r, s, h in chosen]


def _norm_url(url: str) -> str:
    return urldefrag(url.strip())[0].rstrip("/")


def verify_candidates(
    candidates: list[Candidate], retrieved_urls: set[str]
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """Keep only candidates whose URL the search tool returned (modulo fragment/trailing slash)."""
    allowed = {_norm_url(u) for u in retrieved_urls}
    kept: list[Candidate] = []
    dropped: list[dict[str, Any]] = []
    for cand in candidates:
        if _norm_url(cand.url) in allowed:
            kept.append(cand)
        else:
            dropped.append(
                {"name": cand.name, "url": cand.url, "reason": "url_not_in_search_results"}
            )
    return kept, dropped


def _slot_user_text(slot: Slot, perception: RoomPerception, style: str, max_uses: int) -> str:
    d = perception.dimensions
    styles = ", ".join(perception.style_descriptors) or "unknown"
    lines = [
        f"Slot: {slot.category}",
        f"Why this slot: {slot.reason}",
        f"Requested style: {style}",
        f"Slot budget (soft ceiling): {slot.budget_usd:.0f} USD",
        f"Search limit: at most {max_uses} searches.",
        f"Room: {perception.room_type}, about {d.width_m:.1f} m x {d.length_m:.1f} m "
        f"(confidence {d.confidence:.1f}); current style: {styles}",
    ]
    if slot.size_hint:
        lines.append(f"Size hint: {slot.size_hint}")
    return "\n".join(lines)


def retrieve(
    perception: RoomPerception,
    *,
    budget_usd: float,
    style: str,
    max_slots: int,
    budget: Budget,
    run_id: str | None = None,
) -> RetrievalResult:
    """Run web-search retrieval for each derived slot, sequentially, under `budget`'s caps."""
    config = budget.config
    prompt = load_prompt("retrieval")
    slots = derive_slots(perception, budget_usd, max_slots)
    result = RetrievalResult(prompt_version=prompt.version)
    with span(
        "stage.retrieval",
        config=config,
        prompt_version=prompt.version,
        prompt_name=prompt.name,
        model=config.model_retrieval,
        run_id=run_id,
        slots=[s.category for s in slots],
        web_search_tool=config.web_search_tool,
        max_searches_per_slot=config.max_searches_per_slot,
    ) as rec:
        per_slot: list[dict[str, Any]] = []
        for slot in slots:
            started = time.perf_counter()
            completion = complete(
                span_name="llm.retrieval",
                prompt=prompt,
                model=config.model_retrieval,
                user_text=_slot_user_text(slot, perception, style, config.max_searches_per_slot),
                budget=budget,
                max_tokens=RETRIEVAL_MAX_TOKENS,
                schema=to_output_schema(SlotCandidates),
                tools=[web_search_tool(config.max_searches_per_slot, config.web_search_tool)],
                effort=config.effort_retrieval,
                stream=True,
            )
            parsed = parse_model(
                SlotCandidates, completion.text, what=f"retrieval[{slot.category}]"
            )
            kept, dropped = verify_candidates(parsed.candidates, completion.retrieved_urls)
            result.slots.append(
                SlotResult(
                    slot,
                    kept,
                    dropped,
                    parsed.note,
                    completion,
                    round(time.perf_counter() - started, 3),
                )
            )
            queries = [s["query"] for s in completion.searches if "query" in s]
            counts = [s["results"] for s in completion.searches if "results" in s]
            errors = [s["error"] for s in completion.searches if "error" in s]
            per_slot.append(
                {
                    "slot": slot.category,
                    "queries": queries,
                    "result_counts": counts,
                    "search_errors": errors,
                    "candidates_returned": len(parsed.candidates),
                    "candidates_kept": len(kept),
                    "dropped_unverified_urls": len(dropped),
                }
            )
        rec.update(
            per_slot=per_slot, cost_usd=round(sum(r.completion.cost_usd for r in result.slots), 5)
        )
    return result
