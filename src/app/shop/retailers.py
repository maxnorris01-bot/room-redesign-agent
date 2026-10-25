"""Retailers, their tiers and their peers (ADR 0012, stage 2f amendment).

THIS TABLE IS OWNER-EDITABLE CONFIGURATION AND IT IS JUDGMENT, NOT DATA. Which retailer counts as
"design premium" and which stores are peers of which is an opinion about taste and price positioning;
check it and change it freely. Each row is `Retailer(name, tier, search_name, peers, aliases)`:

  * `name`: the canonical display name (what a seller chip says);
  * `tier`: one of `TIERS`;
  * `search_name`: the words put in a Serper query to find that retailer's products;
  * `peers`: canonical names of similar retailers, most similar first (a retailer's peers are what a
    Shop Again searches when the shopper likes it);
  * `aliases`: other spellings that are the same retailer ("crate and barrel", "lowes").

A retailer that is not in the table has no tier and no peers; it still gets a canonical key.
Nothing here is looked up on the web.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

DESIGN_PREMIUM = "design_premium"
DESIGN_MAINSTREAM = "design_mainstream"
MARKETPLACE_BUDGET = "marketplace_budget"
TIERS = (DESIGN_PREMIUM, DESIGN_MAINSTREAM, MARKETPLACE_BUDGET)
# tiers that can earn a "retailers like X" chip and peer searches: liking a Lowe's table says
# something about the table, not about wanting more marketplace sellers
AFFINITY_TIERS = (DESIGN_PREMIUM, DESIGN_MAINSTREAM)
TIER_LABELS = {
    DESIGN_PREMIUM: "design premium",
    DESIGN_MAINSTREAM: "design mainstream",
    MARKETPLACE_BUDGET: "marketplace and budget",
}


@dataclass(frozen=True)
class Retailer:
    name: str
    tier: str
    search_name: str
    peers: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


_PREMIUM_ORDER = (
    "Article", "CB2", "Crate & Barrel", "Room & Board", "Pottery Barn", "Joybird", "Blu Dot",
    "Burrow", "Rejuvenation", "Design Within Reach", "Anthropologie", "RH",
    "Williams Sonoma Home", "Herman Miller", "West Elm",
)  # fmt: skip


def _peers(name: str, *first: str) -> tuple[str, ...]:
    """The named peers first, then the rest of the design premium tier, never the retailer itself."""
    rest = [r for r in _PREMIUM_ORDER if r != name and r not in first]
    return (*first, *rest)


RETAILERS: tuple[Retailer, ...] = (
    # design premium
    Retailer("West Elm", DESIGN_PREMIUM, "West Elm", _peers("West Elm", "Article", "CB2", "Crate & Barrel")),
    Retailer("CB2", DESIGN_PREMIUM, "CB2", _peers("CB2", "West Elm", "Article", "Crate & Barrel")),
    Retailer("Crate & Barrel", DESIGN_PREMIUM, "Crate and Barrel", _peers("Crate & Barrel", "CB2", "West Elm", "Pottery Barn"), ("crate and barrel", "crate barrel")),
    Retailer("Pottery Barn", DESIGN_PREMIUM, "Pottery Barn", _peers("Pottery Barn", "Crate & Barrel", "West Elm", "RH")),
    Retailer("Article", DESIGN_PREMIUM, "Article", _peers("Article", "West Elm", "CB2", "Burrow")),
    Retailer("Room & Board", DESIGN_PREMIUM, "Room and Board", _peers("Room & Board", "Crate & Barrel", "West Elm", "Design Within Reach"), ("room and board",)),
    Retailer("Design Within Reach", DESIGN_PREMIUM, "Design Within Reach", _peers("Design Within Reach", "Herman Miller", "Room & Board", "Blu Dot"), ("dwr",)),
    Retailer("Anthropologie", DESIGN_PREMIUM, "Anthropologie", _peers("Anthropologie", "West Elm", "Pottery Barn", "Rejuvenation")),
    Retailer("Rejuvenation", DESIGN_PREMIUM, "Rejuvenation", _peers("Rejuvenation", "Pottery Barn", "Room & Board", "West Elm")),
    Retailer("Joybird", DESIGN_PREMIUM, "Joybird", _peers("Joybird", "Article", "West Elm", "Burrow")),
    Retailer("Burrow", DESIGN_PREMIUM, "Burrow", _peers("Burrow", "Article", "Joybird", "West Elm")),
    Retailer("Blu Dot", DESIGN_PREMIUM, "Blu Dot", _peers("Blu Dot", "Design Within Reach", "West Elm", "CB2")),
    Retailer("Herman Miller", DESIGN_PREMIUM, "Herman Miller", _peers("Herman Miller", "Design Within Reach", "Blu Dot")),
    Retailer("RH", DESIGN_PREMIUM, "RH", _peers("RH", "Pottery Barn", "Crate & Barrel"), ("restoration hardware",)),
    Retailer("Williams Sonoma Home", DESIGN_PREMIUM, "Williams Sonoma Home", _peers("Williams Sonoma Home", "Pottery Barn", "Crate & Barrel"), ("williams sonoma",)),
    # design mainstream
    Retailer("AllModern", DESIGN_MAINSTREAM, "AllModern", ("West Elm", "Article", "CB2", "Wayfair"), ("all modern",)),
    Retailer("World Market", DESIGN_MAINSTREAM, "World Market", ("Target", "Crate & Barrel", "Pottery Barn", "IKEA"), ("cost plus world market",)),
    Retailer("Target", DESIGN_MAINSTREAM, "Target", ("World Market", "IKEA", "Wayfair")),
    Retailer("Wayfair", DESIGN_MAINSTREAM, "Wayfair", ("AllModern", "Target", "IKEA")),
    Retailer("IKEA", DESIGN_MAINSTREAM, "IKEA", ("Target", "Wayfair", "World Market")),
    # marketplaces and budget
    Retailer("Walmart", MARKETPLACE_BUDGET, "Walmart", ("Target", "Amazon", "Overstock")),
    Retailer("Amazon", MARKETPLACE_BUDGET, "Amazon", ("Walmart", "Overstock", "Wayfair"), ("amazon com",)),
    Retailer("Lowe's", MARKETPLACE_BUDGET, "Lowe's", ("Home Depot", "Walmart", "Overstock"), ("lowes",)),
    Retailer("Home Depot", MARKETPLACE_BUDGET, "Home Depot", ("Lowe's", "Walmart", "Overstock")),
    Retailer("Overstock", MARKETPLACE_BUDGET, "Overstock", ("Wayfair", "Walmart", "Amazon")),
    Retailer("Temu", MARKETPLACE_BUDGET, "Temu"),
    Retailer("AliExpress", MARKETPLACE_BUDGET, "AliExpress"),
    Retailer("Wish", MARKETPLACE_BUDGET, "Wish"),
)  # fmt: skip


_SUFFIXES = re.compile(
    r"\s+(furniture|official store|official|store|inc|llc)\.?$", flags=re.IGNORECASE
)


def seller_key(name: str) -> str:
    """The one seller canonicalization (used by the retailer table, pins, caps and de-duplication).

    Case, punctuation, "&" against "and", a leading "the" or "www.", ".com", a marketplace sub-seller suffix
    (" - JerFit Life") and a trailing " Furniture" or " Official Store" are ignored: "west elm",
    "Room&Board", "Joybird Furniture", "Walmart - JerFit Life" and "crateandbarrel.com" are
    "westelm", "roomandboard", "joybird", "walmart" and "crateandbarrel".
    """
    lowered = name.lower().strip()
    lowered = re.sub(r"^(https?://)?www\.", "", lowered)
    lowered = re.split(r"\s+[-\u2013\u2014|]\s+", lowered, maxsplit=1)[0]
    lowered = lowered.replace("&", " and ")
    lowered = re.sub(r"\.(com|net|org|co)\b", "", lowered)
    lowered = re.sub(r"^the\s+", "", lowered.strip())
    lowered = _SUFFIXES.sub("", lowered.strip())
    return re.sub(r"[^a-z0-9]+", "", lowered) or "?"


_BY_KEY: dict[str, Retailer] = {}
for _r in RETAILERS:
    for _spelling in (_r.name, *_r.aliases):
        _BY_KEY[seller_key(_spelling)] = _r


def retailer_of(name: str) -> Retailer | None:
    """The table row for a seller name, or None for a retailer that is not in the table."""
    return _BY_KEY.get(seller_key(name))


def canonical_name(name: str) -> str:
    """The table's display name for the seller, else the name with a sub-seller suffix removed."""
    found = retailer_of(name)
    if found is not None:
        return found.name
    head = re.split(r"\s+[-–—|]\s+", " ".join(name.split()), maxsplit=1)[0]
    head = _SUFFIXES.sub("", head)
    return head[:40] or name[:40]


def tier_of(name: str) -> str | None:
    found = retailer_of(name)
    return found.tier if found is not None else None


def tier_members(tier: str) -> list[Retailer]:
    return [r for r in RETAILERS if r.tier == tier]


def peers_of(name: str) -> list[Retailer]:
    """The table rows for a retailer's peers, most similar first (empty for an unknown retailer)."""
    found = retailer_of(name)
    if found is None:
        return []
    return [p for n in found.peers if (p := _BY_KEY.get(seller_key(n))) is not None]


def count_sellers(names: Iterable[str]) -> dict[str, int]:
    """How many of `names` are each seller, grouped by `seller_key` ("west elm", "West Elm" and
    "westelm.com" are one row) and shown under the canonical display name; most first. Every trace
    table uses this, so a variant spelling can never split a row or hide one."""
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for name in names:
        key = seller_key(name)
        display.setdefault(key, canonical_name(name))
        counts[key] = counts.get(key, 0) + 1
    return {
        display[k]: n for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], display[kv[0]]))
    }
