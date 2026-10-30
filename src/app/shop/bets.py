"""A seller bet: exposure that matches the evidence behind it, and a bet that is checked (ADR 0012,
stage 2s).

A learned seller used to get up to half a batch from a first sample of 4 rated and 2 liked items,
and what the boost itself earned was read only inside the overall like rate, one batch late (a
seller held 12 of 24 and 11 of 12 were disliked). Now each seller has one allowance, the same for
its chip, its named searches and a pin:

* a new bet gets `SELLER_FIRST_SHARE` of a batch as total slots (4 of 24), never below the
  ordinary per-seller cap;
* it grows by `SELLER_GROW_STEP_SHARE` (2 slots) each time the boosted items of a batch it was
  judged on (at least `SELLER_GROW_MIN_RATED` rated) were liked at `SELLER_GROW_MIN_RATE`, up to a
  third of the batch, and up to half only when the last two judged batches were liked at
  `SELLER_TOP_MIN_RATE` (a pinned seller's ceiling is the pinned share);
* it is revoked when at least `SELLER_REVOKE_MIN_RATED` boosted items are rated and their like
  rate is under `SELLER_REVOKE_MAX_RATE` or under the session baseline: the chip, the allowance
  and the named searches go, and the seller can return only after `SELLER_REDISMISS_RATED` more
  ratings.

"Boosted" means shown through the seller's named search, or above the ordinary cap. Code decides;
nothing here asks the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.shop.models import BetBatch, SellerBet
from app.shop.thresholds import (
    RESULT_COUNT,
    SELLER_FIRST_SHARE,
    SELLER_GROW_MIN_RATE,
    SELLER_GROW_MIN_RATED,
    SELLER_GROW_STEP_SHARE,
    SELLER_MAX_SHARE,
    SELLER_PINNED_SHARE,
    SELLER_PROVEN_MAX_SHARE,
    SELLER_REDISMISS_RATED,
    SELLER_REVOKE_MAX_RATE,
    SELLER_REVOKE_MIN_RATED,
    SELLER_TOP_MIN_RATE,
    seller_cap,
    seller_slots,
)

DISMISS_PREFIX = "seller:"  # the key in `StoredProfile.dismissed` ("seller:<seller key>")
LIKED = {"love", "like"}


@dataclass
class BetUpdate:
    bets: dict[str, SellerBet]
    revoked: list[dict[str, Any]] = field(default_factory=list)  # name, key, rated, liked, rate
    returned: list[str] = field(default_factory=list)  # names that came back after the wait


def dismissed_keys(dismissed: dict[str, int], rated_count: int) -> set[str]:
    """Seller keys still inside their wait after a revocation."""
    return {
        k.removeprefix(DISMISS_PREFIX)
        for k, at in dismissed.items()
        if k.startswith(DISMISS_PREFIX) and rated_count - at < SELLER_REDISMISS_RATED
    }


def boosted_outcome(ids: list[str], ratings: dict[str, str]) -> tuple[int, int, float]:
    """(rated, liked, like rate) of the boosted items `ids` (`ratings`: item id -> rating)."""
    rated = [ratings[i] for i in ids if i in ratings]
    liked = sum(1 for r in rated if r in LIKED)
    return len(rated), liked, (liked / len(rated) if rated else 0.0)


def ceiling_share(bet: SellerBet, ratings: dict[str, str], pinned: bool) -> float:
    """The most of a batch this bet may reach: a third when proven, half when the last two judged
    batches were liked at `SELLER_TOP_MIN_RATE`, the pinned share for a pinned seller."""
    if pinned:
        return SELLER_PINNED_SHARE
    judged = [b for b in bet.batches if b.judged]
    last = judged[-2:]
    if len(last) == 2 and all(
        boosted_outcome(b.ids, ratings)[0] >= SELLER_GROW_MIN_RATED
        and boosted_outcome(b.ids, ratings)[2] >= SELLER_TOP_MIN_RATE
        for b in last
    ):
        return SELLER_MAX_SHARE
    return SELLER_PROVEN_MAX_SHARE


def allowance(
    bet: SellerBet, ratings: dict[str, str], pinned: bool = False, size: int = RESULT_COUNT
) -> dict[str, Any]:
    """The seller's total slots in a batch of `size` and why: first try or grown, with the ceiling.
    Never below the ordinary per-seller cap."""
    first = seller_slots(SELLER_FIRST_SHARE, size)
    step = seller_slots(SELLER_GROW_STEP_SHARE, size)
    top = seller_slots(ceiling_share(bet, ratings, pinned), size)
    wanted = first + step * bet.level
    slots = max(seller_cap(size), min(wanted, top))
    if bet.level == 0:
        why = "first try"
    else:
        why = f"{bet.level} liked batch{'es' if bet.level != 1 else ''} so far"
    if wanted > top:
        why += f", at its limit of {top}"
    return {"slots": slots, "limit": top, "level": bet.level, "why": why}


def update_bets(
    previous: dict[str, SellerBet],
    ratings: dict[str, str],
    baseline_rate: float,
    eligible: dict[str, str],
    rated_count: int,
) -> BetUpdate:
    """The bets after reading the ratings that arrived since the last Shop Again.

    `ratings` maps item id to rating, `eligible` maps seller key to name for every seller the
    evidence earns a chip for now (revoked ones inside their wait left out by the caller). Revokes
    first (cumulative boosted items), then grows on the judged batches, then opens a bet for each
    newly eligible seller (and re-opens one that waited out its dismissal)."""
    bets = {k: b.model_copy(deep=True) for k, b in previous.items()}
    out = BetUpdate(bets)
    for key, bet in bets.items():
        if bet.status == "revoked":
            waited = rated_count - (bet.revoked_at or 0) >= SELLER_REDISMISS_RATED
            if waited and key in eligible:
                bets[key] = SellerBet(key=key, name=eligible[key])
                out.returned.append(eligible[key])
            continue
        all_ids = [i for b in bet.batches for i in b.ids]
        n, liked, rate = boosted_outcome(all_ids, ratings)
        if n >= SELLER_REVOKE_MIN_RATED and (rate < SELLER_REVOKE_MAX_RATE or rate < baseline_rate):
            bet.status = "revoked"
            bet.revoked_at = rated_count
            out.revoked.append(
                {"key": key, "name": bet.name, "rated": n, "liked": liked, "rate": rate}
            )
            continue
        for batch in bet.batches:
            if batch.judged:
                continue
            bn, _, brate = boosted_outcome(batch.ids, ratings)
            if bn >= SELLER_GROW_MIN_RATED:  # fewer ratings than that change nothing yet
                batch.judged = True
                if brate >= SELLER_GROW_MIN_RATE:
                    bet.level += 1
    for key, name in eligible.items():
        if key not in bets:
            bets[key] = SellerBet(key=key, name=name)
    return out


def record_batch(bets: dict[str, SellerBet], boosted: dict[str, str], index: int) -> None:
    """Remember the items shown under each seller's boost in the batch with record `index`."""
    by_seller: dict[str, list[str]] = {}
    for rid, key in boosted.items():
        by_seller.setdefault(key, []).append(rid)
    for key, ids in by_seller.items():
        if key in bets and bets[key].status == "active":
            bets[key].batches.append(BetBatch(index=index, ids=sorted(ids)))


def revocation_message(name: str) -> str:
    return f"Tried more {name}; you did not like most of it, so I stopped."


def bet_rows(
    bets: dict[str, SellerBet],
    ratings: dict[str, str],
    active: dict[str, dict[str, Any]],
    rated_count: int,
) -> list[dict[str, Any]]:
    """What the panel shows for each bet: the allowance and why for an active one, the revocation
    and the wait for a revoked one."""
    rows: list[dict[str, Any]] = []
    for key, bet in sorted(bets.items()):
        all_ids = [i for b in bet.batches for i in b.ids]
        n, liked, rate = boosted_outcome(all_ids, ratings)
        row: dict[str, Any] = {
            "key": key, "seller": bet.name, "status": bet.status, "level": bet.level,
            "boosted_rated": n, "boosted_liked": liked,
        }  # fmt: skip
        if bet.status == "revoked":
            left = max(0, SELLER_REDISMISS_RATED - (rated_count - (bet.revoked_at or 0)))
            row["text"] = (
                f"{bet.name}: revoked ({liked} of {n} boosted items liked); it can come back "
                f"after {left} more rating{'s' if left != 1 else ''}"
            )
        elif key in active:
            a = active[key]
            row.update(slots=a["slots"], limit=a["limit"], why=a["why"])
            row["text"] = f"{bet.name}: {a['slots']} slots, {a['why']}"
            if n:
                row["text"] += f" ({liked} of {n} boosted items liked)"
        else:
            continue
        rows.append(row)
    return rows
