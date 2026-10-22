"""Keep-or-replace preferences: which existing furniture stays and which gets replaced (ADR 0007).

The visitor names items with `--keep` / `--replace` (comma separated item ids such as `f1`, or
category names such as `sofa`; names match through the same synonym folding the slot policy uses).
Items not named default from perception's `keepable` flag: "unlikely" is suggested-replace,
"likely" and "unclear" are kept. There is no preferences file and no interactive prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.categories import canonical_category, norm
from app.schemas import FurnitureItem, RoomPerception


class PreferenceError(ValueError):
    """A --keep / --replace name is unknown or contradictory. The message lists valid names."""


Decision = Literal["keep", "replace"]
Source = Literal["requested", "suggested", "default"]


@dataclass(frozen=True)
class ResolvedItem:
    id: str
    category: str
    decision: Decision
    source: Source  # requested = named by the visitor; suggested = perception said "unlikely"


@dataclass
class Preferences:
    keep: list[str] = field(default_factory=list)  # names exactly as given
    replace: list[str] = field(default_factory=list)
    resolved: list[ResolvedItem] = field(default_factory=list)

    def decided(self, decision: Decision, source: Source | None = None) -> list[ResolvedItem]:
        return [
            r
            for r in self.resolved
            if r.decision == decision and (source is None or r.source == source)
        ]

    def to_json(self) -> dict[str, object]:
        return {
            "keep": self.keep,
            "replace": self.replace,
            "resolved": [
                {"id": r.id, "category": r.category, "decision": r.decision, "source": r.source}
                for r in self.resolved
            ],
        }


def split_names(text: str | None) -> list[str]:
    """ "sofa, f2" -> ["sofa", "f2"]. Empty entries are ignored."""
    return [n.strip() for n in (text or "").split(",") if n.strip()]


def valid_names_message(perception: RoomPerception) -> str:
    ids = ", ".join(f"{f.id} ({f.category})" for f in perception.furniture) or "none"
    cats = ", ".join(sorted({f.category for f in perception.furniture})) or "none"
    return f"valid ids: {ids}; valid categories: {cats}"


def _matches(name: str, item: FurnitureItem) -> bool:
    key = norm(name)
    if key == item.id.lower():
        return True
    return key == norm(item.category) or canonical_category(name) == canonical_category(
        item.category
    )


def _find(name: str, flag: str, perception: RoomPerception) -> list[FurnitureItem]:
    found = [f for f in perception.furniture if _matches(name, f)]
    if not found:
        raise PreferenceError(
            f"unknown name {name!r} in {flag}: it matches no existing furniture "
            f"({valid_names_message(perception)})"
        )
    return found


def check_flag_overlap(keep: list[str], replace: list[str]) -> None:
    """Cheap pre-flight (no perception needed): the same literal name in both flags."""
    both = {norm(k) for k in keep} & {norm(r) for r in replace}
    if both:
        raise PreferenceError(f"{sorted(both)[0]!r} is named in both --keep and --replace")


def resolve_preferences(
    perception: RoomPerception, keep: list[str] | None = None, replace: list[str] | None = None
) -> Preferences:
    """Decide keep or replace for every existing furniture item. Raises `PreferenceError`."""
    keep, replace = list(keep or []), list(replace or [])
    check_flag_overlap(keep, replace)
    requested: dict[str, tuple[Decision, str]] = {}  # item id -> (decision, name that asked)
    for names, decision, flag in ((keep, "keep", "--keep"), (replace, "replace", "--replace")):
        for name in names:
            for item in _find(name, flag, perception):
                previous = requested.get(item.id)
                if previous is not None and previous[0] != decision:
                    raise PreferenceError(
                        f"{item.category} ({item.id}) is named in both --keep and --replace "
                        f"({previous[1]!r} and {name!r})"
                    )
                requested[item.id] = (decision, name)  # type: ignore[assignment]
    resolved: list[ResolvedItem] = []
    for item in perception.furniture:
        if item.id in requested:
            decision, _ = requested[item.id]
            resolved.append(ResolvedItem(item.id, item.category, decision, "requested"))
        elif item.keepable == "unlikely":
            resolved.append(ResolvedItem(item.id, item.category, "replace", "suggested"))
        else:
            resolved.append(ResolvedItem(item.id, item.category, "keep", "default"))
    return Preferences(keep=keep, replace=replace, resolved=resolved)
