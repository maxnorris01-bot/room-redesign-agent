"""Group like-rate evidence (ADR 0012, stage 2i amendment).

For any group of rated items (a seller, a retailer tier, or one value of shape, material, finish, frame
color or style) this module counts the items rated, the items liked (loved or liked, one each) and the
group's like rate, and compares it with the baseline: the like rate over ALL rated items of the session.
The count does not depend on how often the group was shown, so it removes the exposure bias without the
lift test. Three decisions use it:

  * a seller chip or a retailer tier chip: the group has at least `GROUP_MIN_RATED` rated items and
    `GROUP_MIN_LIKED` liked, and its like rate is at least `GROUP_RATE_RATIO` times the baseline and at
    least `GROUP_MIN_RATE`. Marketplace and budget retailers never earn a positive chip;
  * a leaning on a soft dimension value: "toward" on the same test, "away" when the value has at least
    `LEAN_AWAY_MIN_RATED` rated items and a like rate of at most `LEAN_AWAY_MAX_RATIO` times the baseline;
  * the marketplace exclusion: the marketplace and budget tier has at least `MARKET_MIN_RATED` rated
    items and a like rate of at most `MARKET_MAX_RATIO` times the baseline and `MARKET_MAX_RATE`.

All of this is judgment from a handful of live sessions (see `app.shop.thresholds`). Tags are the best
available for each item: photo, else the title model, else title keywords (the callers decide).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from app.shop.models import ProfileEntry
from app.shop.retailers import (
    AFFINITY_TIERS,
    MARKETPLACE_BUDGET,
    TIER_LABELS,
    canonical_name,
    seller_key,
    tier_of,
)
from app.shop.textutil import Rated
from app.shop.thresholds import (
    GROUP_MIN_LIKED,
    GROUP_MIN_RATE,
    GROUP_MIN_RATED,
    GROUP_RATE_RATIO,
    LEAN_AWAY_MAX_RATIO,
    LEAN_AWAY_MIN_RATED,
    LIKE_WEIGHT,
    LOVE_WEIGHT,
    MARKET_MAX_RATE,
    MARKET_MAX_RATIO,
    MARKET_MIN_RATED,
    MARKET_REDISMISS_RATED,
    MAX_SELLER_CHIPS,
    TABLE_MIN_RATED,
)
from app.shop.vocab import DIMENSIONS, FAMILY_LABELS, LABELS, OTHER, style_families

LIKED = ("love", "like")
RATED = ("love", "like", "dislike")
MARKETPLACE_TEXT = "exclude marketplace retailers"
SOFT_DIMENSIONS = tuple(DIMENSIONS)  # shape, material, finish, frame color, style (labels)


@dataclass(frozen=True)
class Stat:
    """One group: its rated and liked items."""

    kind: str  # seller | tier | shape | material | color_finish | frame_color | style
    key: str
    label: str
    rated: int
    liked: int
    weight: int  # like mass: loved 3, liked 1
    liked_ids: tuple[str, ...] = ()
    disliked: int = 0

    @property
    def rate(self) -> float:
        return self.liked / self.rated if self.rated else 0.0


@dataclass(frozen=True)
class Baseline:
    rated: int
    liked: int

    @property
    def rate(self) -> float:
        return self.liked / self.rated if self.rated else 0.0


def baseline_of(rated: dict[str, Rated]) -> Baseline:
    items = [r for r in rated.values() if r.rating in RATED]
    return Baseline(len(items), sum(1 for r in items if r.rating in LIKED))


def _stat(kind: str, key: str, label: str, items: Iterable[Rated]) -> Stat:
    rows = list(items)
    liked = [r for r in rows if r.rating in LIKED]
    return Stat(
        kind, key, label, len(rows), len(liked),
        sum(LOVE_WEIGHT if r.rating == "love" else LIKE_WEIGHT for r in liked),
        tuple(sorted(r.id for r in liked)),
        sum(1 for r in rows if r.rating == "dislike"),
    )  # fmt: skip


def group_stat(
    kind: str, key: str, label: str, rated: dict[str, Rated], member: Callable[[Rated], bool]
) -> Stat:
    """The one function every group goes through: rated, liked and like rate of the rated items
    for which `member` is true (independent of how often the group was shown)."""
    return _stat(kind, key, label, (r for r in rated.values() if r.rating in RATED and member(r)))


def seller_stats(rated: dict[str, Rated]) -> list[Stat]:
    groups: dict[str, list[Rated]] = {}
    for r in rated.values():
        if r.rating in RATED and r.seller:
            groups.setdefault(seller_key(r.seller), []).append(r)
    return [
        _stat("seller", key, canonical_name(rows[0].seller), rows)
        for key, rows in sorted(groups.items())
    ]


def tier_stats(rated: dict[str, Rated]) -> list[Stat]:
    groups: dict[str, list[Rated]] = {}
    for r in rated.values():
        tier = tier_of(r.seller) if r.seller else None
        if r.rating in RATED and tier:
            groups.setdefault(tier, []).append(r)
    return [_stat("tier", tier, TIER_LABELS[tier], rows) for tier, rows in sorted(groups.items())]


def value_stats(
    rated: dict[str, Rated], dim: str, values_of: Callable[[str, str], list[str]]
) -> list[Stat]:
    """One group per value of `dim`; `values_of(item id, dim)` gives an item's values (style: its
    labels, not families). `other` is a read value, never a group."""
    groups: dict[str, list[Rated]] = {}
    for r in rated.values():
        if r.rating not in RATED:
            continue
        for value in dict.fromkeys(values_of(r.id, dim)):
            if value != OTHER:
                groups.setdefault(value, []).append(r)
    return [
        _stat(dim, value, FAMILY_LABELS.get(value, value), rows)
        for value, rows in sorted(groups.items())
    ]


def toward_fails(stat: Stat, base: Baseline) -> list[str]:
    """Why a group is NOT a seller, tier or leaning-toward finding (empty: it is)."""
    need = max(GROUP_RATE_RATIO * base.rate, GROUP_MIN_RATE)
    fails: list[str] = []
    if stat.rated < GROUP_MIN_RATED:
        fails.append(f"rated {stat.rated}, needs {GROUP_MIN_RATED}")
    if stat.liked < GROUP_MIN_LIKED:
        fails.append(f"liked {stat.liked}, needs {GROUP_MIN_LIKED}")
    if stat.rate < need:
        fails.append(
            f"like rate {stat.rate:.0%} is under {need:.0%} "
            f"({GROUP_RATE_RATIO:g} times the baseline {base.rate:.0%}, at least {GROUP_MIN_RATE:.0%})"
        )
    return fails


def away_fails(stat: Stat, base: Baseline) -> list[str]:
    need = LEAN_AWAY_MAX_RATIO * base.rate
    fails: list[str] = []
    if stat.rated < LEAN_AWAY_MIN_RATED:
        fails.append(f"rated {stat.rated}, needs {LEAN_AWAY_MIN_RATED} to lean away")
    if stat.rate > need:
        fails.append(
            f"like rate {stat.rate:.0%} is over {need:.0%} ({LEAN_AWAY_MAX_RATIO:g} times the baseline {base.rate:.0%})"
        )
    return fails


def _stat_numbers(stat: Stat, base: Baseline) -> str:
    return f"liked {stat.liked} of {stat.rated} ({stat.rate:.0%}) against {base.rate:.0%} overall"


# ---- seller and tier chips ------------------------------------------------------------------------


def seller_entries(
    rated: dict[str, Rated], excluded: set[str] | frozenset[str] = frozenset()
) -> tuple[list[ProfileEntry], dict[str, Any]]:
    """Up to `MAX_SELLER_CHIPS` seller chips (highest like rate first) and the trace of every seller
    with a rated item. `excluded` are seller keys whose bet was revoked and is still waiting out its
    ratings (stage 2s): they earn no chip."""
    base = baseline_of(rated)
    total_mass = sum(
        LOVE_WEIGHT if r.rating == "love" else LIKE_WEIGHT
        for r in rated.values()
        if r.rating in LIKED
    )
    stats = seller_stats(rated)
    candidates: list[dict[str, Any]] = []
    earned: list[Stat] = []
    for s in stats:
        fails = toward_fails(s, base)
        if tier_of(s.label) == MARKETPLACE_BUDGET:
            fails.append("marketplace and budget retailers never earn a positive chip")
        if s.key in excluded:
            fails.append("its boost did not work out; it waits for more ratings")
        candidates.append(
            {
                "seller": s.label, "rated": s.rated, "liked": s.liked, "like_rate": round(s.rate, 4),
                "baseline": round(base.rate, 4), "like_mass": s.weight,
                "verdict": "chip" if not fails else "no chip: " + "; ".join(fails),
            }
        )  # fmt: skip
        if not fails:
            earned.append(s)
    earned.sort(key=lambda s: (-s.rate, -s.weight, s.label))
    candidates.sort(key=lambda c: (-c["like_rate"], -c["like_mass"], c["seller"]))
    entries: list[ProfileEntry] = []
    for s in earned[:MAX_SELLER_CHIPS]:
        titles = [" ".join(rated[i].title.split())[:100] for i in s.liked_ids[:3]]
        entries.append(
            ProfileEntry(
                text=f"seller: {s.label}",
                attr_kind="seller",
                polarity="like",
                evidence=list(s.liked_ids[:6]),
                evidence_titles=titles,
                confidence=1.0,
                weight=s.weight,
                share=round(s.weight / total_mass, 4) if total_mass else None,
                dimension="seller",
                detail=_stat_numbers(s, base),
            )  # fmt: skip
        )
    trace: dict[str, Any] = {
        "candidates": candidates[:10],
        "baseline": round(base.rate, 4),
        "decision": "chip" if entries else "none",
        "reason": (
            "seller like-rate evidence"
            if entries
            else (candidates[0]["verdict"] if candidates else "no rated item has a seller")
        ),
    }
    return entries, trace


def tier_entry(rated: dict[str, Rated]) -> tuple[ProfileEntry | None, dict[str, Any]]:
    """The retailer tier chip ("retailers like West Elm"), if any, and the numbers behind it."""
    base = baseline_of(rated)
    total_mass = sum(
        LOVE_WEIGHT if r.rating == "love" else LIKE_WEIGHT
        for r in rated.values()
        if r.rating in LIKED
    )
    candidates: list[dict[str, Any]] = []
    best: Stat | None = None
    for s in tier_stats(rated):
        fails = toward_fails(s, base)
        if s.key not in AFFINITY_TIERS:
            fails.append("marketplace and budget retailers never earn a retailer chip")
        sellers: dict[str, int] = {}
        for r in rated.values():
            if r.rating in LIKED and r.seller and tier_of(r.seller) == s.key:
                name = canonical_name(r.seller)
                sellers[name] = sellers.get(name, 0) + (
                    LOVE_WEIGHT if r.rating == "love" else LIKE_WEIGHT
                )
        candidates.append(
            {
                "tier": s.key, "label": s.label, "rated": s.rated, "liked": s.liked,
                "like_rate": round(s.rate, 4), "baseline": round(base.rate, 4), "like_mass": s.weight,
                "sellers": dict(sorted(sellers.items(), key=lambda kv: (-kv[1], kv[0]))),
                "verdict": "chip" if not fails else "no chip: " + "; ".join(fails),
            }
        )  # fmt: skip
        if not fails and (best is None or (s.rate, s.weight) > (best.rate, best.weight)):
            best = s
    candidates.sort(key=lambda c: (-c["like_rate"], c["tier"]))
    trace: dict[str, Any] = {
        "candidates": candidates,
        "baseline": round(base.rate, 4),
        "decision": "chip" if best else "none",
        "reason": "retailer tier like-rate evidence"
        if best
        else (candidates[0]["verdict"] if candidates else "no rated item has a known retailer"),
    }
    if best is None:
        return None, trace
    pick = next(c for c in candidates if c["tier"] == best.key)
    top = next(iter(pick["sellers"]))
    titles = [" ".join(rated[i].title.split())[:100] for i in best.liked_ids[:3]]
    entry = ProfileEntry(
        text=f"retailers like {top}", attr_kind="tier", polarity="like",
        evidence=list(best.liked_ids[:6]), evidence_titles=titles, confidence=1.0,
        weight=best.weight, share=round(best.weight / total_mass, 4) if total_mass else None,
        dimension="tier", values=[best.key],
        detail=_stat_numbers(best, base) + "; liked: " + ", ".join(pick["sellers"]),
    )  # fmt: skip
    return entry, trace


# ---- leanings on the soft dimensions ----------------------------------------------------------------


def lean_text(direction: str, dim: str, value: str) -> str:
    label = FAMILY_LABELS.get(value, value)
    where = LABELS[dim].lower()
    verb = "leaning toward" if direction == "toward" else "leaning away from"
    return f"{verb} {label} ({where})"


def leaning_entries(
    rated: dict[str, Rated],
    values_of: Callable[[str, str], list[str]],
    converged: dict[str, str],
    skip: Callable[[str, str, str], bool],
) -> tuple[list[ProfileEntry], list[dict[str, Any]]]:
    """The leaning chips and the evidence rows for every soft dimension value with at least
    `TABLE_MIN_RATED` rated items. `converged` maps a dimension to the value it already converged
    on (no leaning toward that value); `skip(direction, dim, value)` is true for a value the shopper
    typed or deleted."""
    base = baseline_of(rated)
    entries: list[ProfileEntry] = []
    rows: list[dict[str, Any]] = []
    for dim in SOFT_DIMENSIONS:
        for s in value_stats(rated, dim, values_of):
            if s.rated < TABLE_MIN_RATED:
                continue
            toward = toward_fails(s, base)
            away = away_fails(s, base)
            direction = ""
            if converged.get(dim) == s.key or (
                dim == "style" and converged.get(dim, "-") in style_families(s.key)
            ):
                verdict, reason = "converged", "the dimension already converged on this value"
            elif not toward:
                direction = "toward"
                verdict, reason = "leaning toward", _stat_numbers(s, base)
            elif not away:
                direction = "away"
                verdict, reason = "leaning away", _stat_numbers(s, base)
            else:
                verdict = "nothing"
                reason = "; ".join(toward if s.rate >= base.rate else away)
            if direction and skip(direction, dim, s.key):
                verdict, reason = "nothing", reason + " (typed in the request, or you deleted it)"
                direction = ""
            rows.append(
                {
                    "kind": dim, "name": s.label, "rated": s.rated, "liked": s.liked,
                    "like_rate": round(s.rate, 4), "baseline": round(base.rate, 4),
                    "verdict": verdict, "reason": reason,
                }
            )  # fmt: skip
            if direction:
                titles = [" ".join(rated[i].title.split())[:100] for i in s.liked_ids[:3]]
                entries.append(
                    ProfileEntry(
                        text=lean_text(direction, dim, s.key),
                        attr_kind=f"leaning_{direction}",
                        polarity="like",
                        status="leaning",
                        dimension=dim,
                        values=[s.key],
                        detail=_stat_numbers(s, base),
                        evidence=list(s.liked_ids[:6]),
                        evidence_titles=titles,
                        weight=s.liked,
                        share=round(s.rate, 4),
                    )  # fmt: skip
                )
    return entries, rows


def group_rows(rated: dict[str, Rated]) -> list[dict[str, Any]]:
    """The evidence rows of the sellers and the tiers (with at least `TABLE_MIN_RATED` rated)."""
    base = baseline_of(rated)
    rows: list[dict[str, Any]] = []
    for s in [*seller_stats(rated), *tier_stats(rated)]:
        if s.rated < TABLE_MIN_RATED:
            continue
        fails = toward_fails(s, base)
        market = (s.kind == "seller" and tier_of(s.label) == MARKETPLACE_BUDGET) or (
            s.kind == "tier" and s.key not in AFFINITY_TIERS
        )
        if market:
            fails.append("marketplace and budget retailers never earn a positive chip")
        rows.append(
            {
                "kind": s.kind, "name": s.label, "rated": s.rated, "liked": s.liked,
                "like_rate": round(s.rate, 4), "baseline": round(base.rate, 4),
                "verdict": "chip" if not fails else "nothing",
                "reason": _stat_numbers(s, base) if not fails else "; ".join(fails),
            }
        )  # fmt: skip
    return rows


# ---- the marketplace exclusion --------------------------------------------------------------------


def marketplace_decision(
    rated: dict[str, Rated], dismissed_at: int | None = None
) -> tuple[ProfileEntry | None, dict[str, Any]]:
    """The "exclude marketplace retailers" chip, if the evidence holds and it was not dismissed.

    The marketplace and budget tier needs `MARKET_MIN_RATED` rated items and a like rate of at most
    `MARKET_MAX_RATIO` times the baseline and `MARKET_MAX_RATE`. A dismissed chip (deleted by the
    shopper at `dismissed_at` rated items) returns only after `MARKET_REDISMISS_RATED` more ratings,
    and only while the evidence still holds."""
    base = baseline_of(rated)
    stat = next((s for s in tier_stats(rated) if s.key == MARKETPLACE_BUDGET), None)
    trace: dict[str, Any] = {"baseline": round(base.rate, 4), "decision": "none"}
    if stat is None:
        trace["reason"] = "no rated item is from a marketplace or budget retailer"
        return None, trace
    trace.update(rated=stat.rated, liked=stat.liked, like_rate=round(stat.rate, 4))
    fails: list[str] = []
    if stat.rated < MARKET_MIN_RATED:
        fails.append(f"rated {stat.rated}, needs {MARKET_MIN_RATED}")
    if stat.rate > MARKET_MAX_RATIO * base.rate:
        fails.append(
            f"like rate {stat.rate:.0%} is over {MARKET_MAX_RATIO:g} times the baseline {base.rate:.0%}"
        )
    if stat.rate > MARKET_MAX_RATE:
        fails.append(f"like rate {stat.rate:.0%} is over {MARKET_MAX_RATE:.0%}")
    if not fails and dismissed_at is not None:
        since = base.rated - dismissed_at
        if since < MARKET_REDISMISS_RATED:
            fails.append(
                f"you deleted the chip; it returns after {MARKET_REDISMISS_RATED} more ratings "
                f"({max(0, since)} so far)"
            )
    if fails:
        trace["reason"] = "; ".join(fails)
        return None, trace
    ids = [
        r.id
        for r in rated.values()
        if r.rating in RATED and tier_of(r.seller) == MARKETPLACE_BUDGET
    ]
    entry = ProfileEntry(
        text=MARKETPLACE_TEXT, attr_kind="exclude_marketplace", polarity="like", status="filter",
        dimension="marketplace", detail=f"liked {stat.liked} of {stat.rated}",
        evidence=sorted(ids)[:6], evidence_titles=[], weight=stat.rated, share=round(stat.rate, 4),
    )  # fmt: skip
    trace.update(decision="chip", reason=_stat_numbers(stat, base))
    return entry, trace


@dataclass
class Evidence:
    """What the evidence layer decided for one set of ratings (the trace shows all of it)."""

    sellers: list[ProfileEntry] = field(default_factory=list)
    tier: ProfileEntry | None = None
    leanings: list[ProfileEntry] = field(default_factory=list)
    marketplace: ProfileEntry | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
