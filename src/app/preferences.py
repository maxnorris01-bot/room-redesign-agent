"""Keep-or-replace preferences: which existing furniture stays and which gets replaced (ADR 0007).

The visitor names items with `--keep` / `--replace` (comma separated item ids such as `f1`, or
category names such as `sofa`; names match through the same synonym folding the slot policy uses).
Items not named default from perception's `keepable` flag: "unlikely" is suggested-replace,
"likely" and "unclear" are kept. There is no preferences file and no interactive prompt.
"""

from __future__ import annotations

import re
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
    via: str = ""  # set when `--replace-matching` made this a replacement ("set of f1")


@dataclass
class Preferences:
    keep: list[str] = field(default_factory=list)  # names exactly as given
    replace: list[str] = field(default_factory=list)
    resolved: list[ResolvedItem] = field(default_factory=list)
    # Kept items in a set with an item being replaced (they may clash with the new piece)
    set_warnings: list[dict[str, object]] = field(default_factory=list)

    def decided(self, decision: Decision, source: Source | None = None) -> list[ResolvedItem]:
        return [
            r
            for r in self.resolved
            if r.decision == decision and (source is None or r.source == source)
        ]

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {
            "keep": self.keep,
            "replace": self.replace,
            "resolved": [
                {"id": r.id, "category": r.category, "decision": r.decision, "source": r.source}
                | ({"via": r.via} if r.via else {})
                for r in self.resolved
            ],
        }
        if self.set_warnings:
            out["set_warnings"] = self.set_warnings
        return out


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


@dataclass(frozen=True)
class SetLink:
    a: str
    b: str
    inferred: bool  # True when found by reading the note text, not from `matches_id`


_MATCH_WORD = re.compile(r"\bmatch(?:es|ed|ing)?\b", re.I)


def set_links(perception: RoomPerception) -> list[SetLink]:
    """Pairs of furniture items that are visibly part of one set.

    From `matches_id` when perception gave it; otherwise inferred from a note that says an item
    "matches"/"matching" another, by id (f1) or by category name ("matches sofa").
    """
    by_id = {f.id: f for f in perception.furniture}
    links: dict[frozenset[str], SetLink] = {}
    for item in perception.furniture:
        if item.matches_id and item.matches_id in by_id and item.matches_id != item.id:
            links[frozenset((item.id, item.matches_id))] = SetLink(item.id, item.matches_id, False)
    for item in perception.furniture:
        m = _MATCH_WORD.search(item.note)
        if not m:
            continue
        after = item.note[m.end() :].lower()
        for other in perception.furniture:
            if other.id == item.id:
                continue
            names = {norm(other.category), canonical_category(other.category)}
            mentioned = re.search(rf"\b{re.escape(other.id)}\b", after) or any(
                re.search(rf"\b{re.escape(n)}\b", after) for n in names
            )
            if mentioned:
                links.setdefault(frozenset((item.id, other.id)), SetLink(item.id, other.id, True))
    return list(links.values())


def _components(links: list[SetLink]) -> list[tuple[set[str], bool]]:
    """Connected sets of ids; the flag is True if every link inside was inferred from text."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for link in links:
        parent[find(link.a)] = find(link.b)
    groups: dict[str, set[str]] = {}
    for x in list(parent):
        groups.setdefault(find(x), set()).add(x)
    out = []
    for ids in groups.values():
        inferred = all(link.inferred for link in links if link.a in ids)
        out.append((ids, inferred))
    return out


def resolve_preferences(
    perception: RoomPerception,
    keep: list[str] | None = None,
    replace: list[str] | None = None,
    replace_matching: bool = False,
) -> Preferences:
    """Decide keep or replace for every existing furniture item. Raises `PreferenceError`.

    A kept item that is part of a set with a replaced item gets a warning (it may clash with the new
    piece). With `replace_matching` such items become replacement requests instead, unless the
    visitor explicitly asked to keep them.
    """
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

    by_id = {f.id: f for f in perception.furniture}
    explicit_keep = {i for i, (d, _) in requested.items() if d == "keep"}
    warnings: list[dict[str, object]] = []
    for ids, inferred in _components(set_links(perception)):
        replaced = [r for r in resolved if r.id in ids and r.decision == "replace"]
        for r in [x for x in resolved if x.id in ids and x.decision == "keep"]:
            if not replaced:
                continue
            if replace_matching and r.id not in explicit_keep:
                idx = resolved.index(r)
                resolved[idx] = ResolvedItem(
                    r.id, r.category, "replace", "requested", via=f"set of {replaced[0].id}"
                )
                continue
            partner = replaced[0]
            kept_item = by_id[r.id]
            text = (
                f"your {kept_item.category} ({r.id}) is a matching piece of the "
                f"{partner.category} you are replacing and may clash with the new one; add "
                f"--replace {kept_item.category} to replace it too"
            )
            if inferred:
                text += " (inferred from the room notes)"
            warnings.append(
                {
                    "kept_id": r.id, "kept_category": kept_item.category,
                    "replaced_id": partner.id, "replaced_category": partner.category,
                    "inferred": inferred, "text": text,
                }
            )  # fmt: skip
    return Preferences(keep=keep, replace=replace, resolved=resolved, set_warnings=warnings)
