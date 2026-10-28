"""Refine search (ADR 0012, stage 2l amendment): plain instructions that change the next Shop Agains.

The shopper types "don't show anything from Walmart", "focus on metal frames", "under $400". A forced
tool call (`record_refinements`, prompt `shop_refine` v1, strict schema, thinking disabled) turns the
text into at most `REFINE_MAX_PER_SUBMIT` interpretations; code validates each one against the fixed
kinds and vocabularies (`app.shop.retailers`, `app.shop.vocab`) and anything that does not validate
becomes an approximate free note, used only as a soft hint. The instruction is untrusted text: it is
cleaned of links, sits between delimiters, and the call has no tool except `record_refinements`.

A refine chip outranks everything inferred from ratings. Exclusions are hard filters on the candidate
pool, focus chips take `REFINE_FOCUS_MIN_SHARE` of a batch where the pool has them, prices replace the
typed range, must-haves extend the request's, and every chip reaches the ranker and the query writer.
Chips live in the topic (`Topic.refinements`) until deleted, a new topic or Start over.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.config import Config
from app.llm import Budget, LLMOutputError, complete
from app.prompts import load_prompt
from app.shop.models import (
    MAX_TERM_CHARS,
    REFINE_KINDS,
    RefineChip,
    RefineItem,
    ShopQuery,
    StoredProfile,
)
from app.shop.retailers import TIER_LABELS, TIERS, canonical_name, retailer_of, seller_key
from app.shop.thresholds import (
    FREE_NOTES_MAX,
    NOTE_MAX_CHARS,
    REFINE_MAX_CHARS,
    REFINE_MAX_CHIPS,
    REFINE_MAX_PER_SUBMIT,
)
from app.shop.vocab import DIMENSIONS, OTHER, normalize

REFINE_TOOL = "record_refinements"
REFINE_MAX_TOKENS = 500
HARD_KINDS = ("exclude_seller", "exclude_tier", "exclude_value", "price_max", "price_min")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
REJECTED = (
    "I could not turn that into a change to the search. Try something like "
    "“no Walmart”, “focus on cream” or “under $400”."
)


def _item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(REFINE_KINDS)},
            "target": {"type": "string"},
            "strength": {"type": "string", "enum": ["hard", "focus"]},
            "label": {"type": "string"},
            "note_mode": {"type": "string", "enum": ["focus", "prefer", "exclude", "none"]},
        },
        "required": ["kind", "target", "strength", "label", "note_mode"],
        "additionalProperties": False,
    }


REFINE_TOOL_DEF: dict[str, Any] = {
    "name": REFINE_TOOL,
    "description": "Record the structured refinements of the shopper's instruction.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"items": {"type": "array", "items": _item_schema()}},
        "required": ["items"],
        "additionalProperties": False,
    },
}


def clean_instruction(text: str) -> str:
    """The instruction as it is kept and sent: printable characters, no links, no angle brackets
    (they delimit it in the prompt), whitespace collapsed, at most `REFINE_MAX_CHARS`."""
    printable = "".join(c if c.isprintable() else " " for c in text)
    printable = _URL.sub(" ", printable).replace("<", " ").replace(">", " ")
    return " ".join(printable.split())[:REFINE_MAX_CHARS]


def _label(text: str, fallback: str) -> str:
    cleaned = " ".join(_URL.sub(" ", text).replace("<", " ").replace(">", " ").split())
    return (cleaned or fallback)[:40]


def chip_id(kind: str, target: str) -> str:
    return hashlib.sha1(f"{kind}|{target}".encode()).hexdigest()[:10]


def _price(raw: str) -> str | None:
    match = re.search(r"\d[\d,]*(?:\.\d+)?", raw)
    if match is None:
        return None
    value = float(match.group(0).replace(",", ""))
    if not 0 < value <= 1_000_000:
        return None
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _tier(raw: str) -> str | None:
    low = raw.strip().lower().replace(" ", "_")
    if low in TIERS:
        return low
    for tier, label in TIER_LABELS.items():
        if raw.strip().lower() in (label, label.split()[0]):
            return tier
    return None


def _dimension_value(raw: str) -> str | None:
    if ":" not in raw:
        return None
    dim, _, value = raw.partition(":")
    dim = dim.strip().lower()
    if dim not in DIMENSIONS:
        return None
    got = normalize(dim, value.strip())
    if got is None or got == OTHER:
        return None
    return f"{dim}:{got}"


NOTE_MODES = ("focus", "prefer", "exclude")


def _note_phrase(raw: str) -> str:
    """A free note's phrase: letters, digits, apostrophes and spaces, no links, at most
    `NOTE_MAX_CHARS` characters. It is only ever shown to a model between delimiters, as data."""
    return " ".join(re.findall(r"[a-z0-9' ]+", _URL.sub(" ", raw.lower())))[:NOTE_MAX_CHARS].strip()


def _mode_of(kind: str) -> str:
    """The strength a note gets when the model gave none: what the kind it tried to be implies."""
    if kind.startswith("exclude"):
        return "exclude"
    if kind.startswith("focus"):
        return "focus"
    return "prefer"


def validate_item(item: RefineItem, text: str = "") -> RefineChip | None:
    """One interpretation checked against the fixed kinds and vocabularies. Anything that does not
    check out becomes an approximate free note (a soft hint only); an empty label is dropped."""
    label = _label(item.label, item.target)
    target: str | None = None
    kind = item.kind
    if kind in ("exclude_seller", "focus_seller"):
        found = retailer_of(item.target)
        target = found.name if found is not None else None
    elif kind == "exclude_tier":
        target = _tier(item.target)
    elif kind in ("exclude_value", "focus_value"):
        target = _dimension_value(item.target)
    elif kind in ("price_max", "price_min"):
        target = _price(item.target)
    elif kind == "must_have":
        words = " ".join(re.findall(r"[a-z0-9]+", item.target.lower()))[:MAX_TERM_CHARS]
        target = words or None
    else:
        target = _note_phrase(item.target) or None
    approximate = kind == "free_note"  # a note is never mapped onto a fixed kind
    mode = ""
    if target is None:
        if not label:
            return None
        kind, target, approximate = "free_note", _note_phrase(label) or label.lower(), True
    if kind == "free_note":
        mode = item.note_mode if item.note_mode in NOTE_MODES else _mode_of(item.kind)
    strength: Literal["hard", "focus"] = "hard" if kind in HARD_KINDS else "focus"
    return RefineChip(
        id=chip_id(kind, target), kind=kind, target=target, strength=strength,
        label=label or target, approximate=approximate, text=text,
        note_mode=mode,  # type: ignore[arg-type]
    )  # fmt: skip


@dataclass
class RefineResult:
    chips: list[RefineChip] = field(default_factory=list)
    message: str = ""  # why nothing was understood
    usd: float = 0.0
    error: str = ""


def interpret(text: str, budget: Budget, config: Config | None = None) -> RefineResult:
    """Interpret one instruction. Never raises except on a budget error."""
    config = config or budget.config
    cleaned = clean_instruction(text)
    if len(re.findall(r"[A-Za-z]{2,}|\d", cleaned)) == 0:
        return RefineResult(message=REJECTED)
    before = budget.cost_usd
    try:
        completion = complete(
            span_name="llm.shop_refine",
            prompt=load_prompt("shop_refine"),
            model=config.rank_model,
            user_text=f"<instruction>{cleaned}</instruction>",
            budget=budget,
            max_tokens=REFINE_MAX_TOKENS,
            tools=[REFINE_TOOL_DEF],
            tool_choice={"type": "tool", "name": REFINE_TOOL},
            stream=True,
            timeout_s=config.shop_tag_call_timeout_s,
            disable_thinking=True,
        )
        raw = (completion.tool_input or {}).get("items", [])
    except LLMOutputError as exc:
        return RefineResult(
            message="I could not read that just now; try again.",
            error=str(exc)[:160],
            usd=round(budget.cost_usd - before, 6),
        )
    usd = round(budget.cost_usd - before, 6)
    chips: list[RefineChip] = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            item = RefineItem.model_validate(entry)
        except ValueError:
            continue
        chip = validate_item(item, cleaned)
        if chip is not None and chip.id not in {c.id for c in chips}:
            chips.append(chip)
        if len(chips) >= REFINE_MAX_PER_SUBMIT:
            break
    return RefineResult(chips=chips, message="" if chips else REJECTED, usd=usd)


_OPPOSITE = {"exclude_seller": "focus_seller", "focus_seller": "exclude_seller",
             "exclude_value": "focus_value", "focus_value": "exclude_value"}  # fmt: skip
_ONE_OF = ("price_max", "price_min")


def add_chips(
    active: list[RefineChip], new: list[RefineChip]
) -> tuple[list[RefineChip], list[str]]:
    """The active chips with `new` added. A chip with the same target and the opposite direction
    replaces the old one (and says so); a new price replaces the old one of its kind; a repeat is
    ignored; at most `REFINE_MAX_CHIPS` stay (the rest are refused with a notice)."""
    out = list(active)
    notices: list[str] = []
    for chip in new:
        if any(c.id == chip.id for c in out):
            continue
        opposite = _OPPOSITE.get(chip.kind)
        clash = [
            c for c in out
            if (opposite is not None and c.kind == opposite and c.target == chip.target)
            or (chip.kind in _ONE_OF and c.kind == chip.kind)
        ]  # fmt: skip
        for old in clash:
            out.remove(old)
            notices.append(f"Replaced “{old.label}” with “{chip.label}”.")
        if len(out) >= REFINE_MAX_CHIPS:
            notices.append(f"At most {REFINE_MAX_CHIPS} refinements at once; remove one first.")
            break
        if (
            chip.kind == "free_note"
            and sum(1 for c in out if c.kind == "free_note") >= FREE_NOTES_MAX
        ):
            notices.append(f"At most {FREE_NOTES_MAX} written notes at once; remove one first.")
            continue
        out.append(chip)
    return out, notices


# ---- applying the chips -----------------------------------------------------------------------------


@dataclass
class RefineSet:
    """The active chips sorted by what they do."""

    chips: list[RefineChip] = field(default_factory=list)
    exclude_sellers: dict[str, str] = field(default_factory=dict)  # seller key -> chip id
    exclude_tiers: dict[str, str] = field(default_factory=dict)  # tier -> chip id
    exclude_values: dict[tuple[str, str], str] = field(default_factory=dict)  # (dim, value) -> id
    focus_values: dict[tuple[str, str], str] = field(default_factory=dict)
    focus_sellers: dict[str, str] = field(default_factory=dict)  # seller name -> chip id
    price_max: float | None = None
    price_min: float | None = None
    must_have: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # stage 2p: free notes by strength, chip id -> phrase (a focus note reserves a share of the batch
    # from items the tagging flagged as matching; an exclude note removes them; prefer is text only)
    focus_notes: dict[str, str] = field(default_factory=dict)
    exclude_notes: dict[str, str] = field(default_factory=dict)
    prefer_notes: dict[str, str] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return bool(self.chips)

    def focus_pairs(self) -> list[tuple[str, str]]:
        """(dimension, value) for the selection; sellers use the pseudo-dimension "seller" and a
        focus note the pseudo-dimension "note" (its value is the chip id)."""
        return [
            *self.focus_values,
            *(("seller", seller_key(n)) for n in self.focus_sellers),
            *(("note", cid) for cid in self.focus_notes),
        ]

    def note_specs(self) -> list[tuple[str, str]]:
        """(chip id, phrase) of every active free note, at most `FREE_NOTES_MAX`: what the tagging
        calls return a match flag for."""
        both = {**self.focus_notes, **self.exclude_notes, **self.prefer_notes}
        return list(both.items())[:FREE_NOTES_MAX]


def build_set(chips: list[RefineChip]) -> RefineSet:
    rs = RefineSet(chips=list(chips))
    for c in chips:
        if c.kind == "exclude_seller":
            rs.exclude_sellers[seller_key(c.target)] = c.id
        elif c.kind == "exclude_tier":
            rs.exclude_tiers[c.target] = c.id
        elif c.kind in ("exclude_value", "focus_value"):
            dim, _, value = c.target.partition(":")
            (rs.exclude_values if c.kind == "exclude_value" else rs.focus_values)[(dim, value)] = (
                c.id
            )
        elif c.kind == "focus_seller":
            rs.focus_sellers[canonical_name(c.target)] = c.id
        elif c.kind == "price_max":
            rs.price_max = float(c.target)
        elif c.kind == "price_min":
            rs.price_min = float(c.target)
        elif c.kind == "must_have":
            rs.must_have.append(c.target)
        else:
            rs.notes.append(c.label + (" (approximate)" if c.approximate else ""))
            mode = c.note_mode or "prefer"
            bucket = {"focus": rs.focus_notes, "exclude": rs.exclude_notes}.get(
                mode, rs.prefer_notes
            )
            bucket[c.id] = c.target
    return rs


def apply_to_query(query: ShopQuery, rs: RefineSet) -> ShopQuery:
    """The query with the refine prices (which replace the typed range) and must-haves."""
    update: dict[str, Any] = {}
    if rs.price_max is not None:
        update["price_max"] = rs.price_max
    if rs.price_min is not None:
        update["price_min"] = rs.price_min
    if update.get("price_max") is not None and (update.get("price_min") or query.price_min):
        low = update.get("price_min", query.price_min)
        if low is not None and low > update["price_max"]:
            update["price_min"] = None  # a lower cap than the typed floor: the cap wins
    if rs.must_have:
        update["must_have"] = [
            *query.must_have,
            *[m for m in rs.must_have if m not in query.must_have],
        ][:8]
    return query.model_copy(update=update) if update else query


def suppress_inferred(
    profile: StoredProfile, query: ShopQuery, rs: RefineSet
) -> tuple[StoredProfile, ShopQuery, list[str]]:
    """Drop what the ratings inferred where it contradicts a refine chip (the chip outranks it).
    Returns the profile, the query and a plain list of what was suppressed, for the trace."""
    if not rs.active:
        return profile, query, []
    kept = []
    gone: list[str] = []
    for e in profile.inferred:
        why = ""
        value = (e.dimension, e.values[0]) if e.values else None
        if (
            e.attr_kind == "seller"
            and seller_key(e.text.removeprefix("seller: ")) in rs.exclude_sellers
        ):
            why = "you excluded that seller"
        elif e.attr_kind == "tier" and e.values and e.values[0] in rs.exclude_tiers:
            why = "you excluded that kind of retailer"
        elif e.attr_kind in ("leaning_toward", "converged") or (e.status == "converged" and value):
            if value in rs.exclude_values:
                why = "you excluded that value"
        if not why and e.attr_kind == "leaning_away" and value in rs.focus_values:
            why = "you asked to focus on that value"
        if why:
            gone.append(f"{e.text} ({why})")
        else:
            kept.append(e)
    update: dict[str, Any] = {}
    suggested = [c for c in query.suggested_colors if ("color", c) not in rs.exclude_values]
    avoid = [c for c in query.avoid_colors if ("color", c) not in rs.focus_values]
    if suggested != query.suggested_colors:
        gone += [
            f"suggested color {c} (you excluded it)"
            for c in query.suggested_colors
            if c not in suggested
        ]
        update["suggested_colors"] = suggested
    if avoid != query.avoid_colors:
        gone += [
            f"avoid color {c} (you asked to focus on it)"
            for c in query.avoid_colors
            if c not in avoid
        ]
        update["avoid_colors"] = avoid
    return (
        profile.model_copy(update={"inferred": kept})
        if len(kept) != len(profile.inferred)
        else profile,
        query.model_copy(update=update) if update else query,
        gone,
    )


def ranker_lines(rs: RefineSet) -> list[str]:
    """What the ranker is told: the shopper's explicit instructions outrank everything inferred."""
    if not rs.active:
        return []
    favour = [c.label for c in rs.chips if c.strength == "focus" and c.kind != "free_note"]
    favour += [c.label for c in rs.chips if c.kind == "free_note" and c.note_mode == "focus"]
    excluded = [c.label for c in rs.chips if c.kind.startswith("exclude")]
    excluded += [c.label for c in rs.chips if c.kind == "free_note" and c.note_mode == "exclude"]
    soft = [
        c.label + (" (approximate)" if c.approximate and not c.note_mode else "")
        for c in rs.chips
        if c.kind == "free_note" and c.note_mode in ("", "prefer")
    ]
    lines = []
    if favour:
        lines.append(
            "The shopper explicitly asked to favour (must favor; outranks everything inferred): "
            + "; ".join(favour)
        )
    if excluded:
        lines.append(
            "The shopper explicitly excluded (already removed from the list): "
            + "; ".join(excluded)
        )
    if soft:
        lines.append("Notes from the shopper (soft hints, approximate): " + "; ".join(soft))
    return lines


def writer_lines(rs: RefineSet) -> list[str]:
    if not rs.active:
        return []
    return [
        "Shopper's explicit instructions (hard constraints every query must respect): "
        + "; ".join(c.label + (" (approximate)" if c.approximate else "") for c in rs.chips)
    ]
