"""Replay a recorded session offline: seed a session store with rated items and what was shown.

`tests/fixtures/shop/owner_session_replay.json` is the owner's real session in miniature (stage 2d):
ten rated items with the attributes a model should read from them, and invented shown extras. The
tests and the browser check use `seed_session` to put it in a store, then run Shop Again against it
(mock tags come from the recorded attributes by id, see `app.mock_llm`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.shop.models import FilterCounts, ShopQuery, ShopResult
from app.shop.store import RatingRecord, SearchRecord, Session, SessionStore, now_iso, now_iso_ms

REPLAY_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "shop"
REPLAY_FILE = "owner_session_replay.json"  # the first real session (West Elm)
REPLAY_FILE_2 = "owner_session_replay_2.json"  # the second (Home Depot, 91% rectangular batch)
REPLAY_FILE_3 = "owner_session_replay_3.json"  # the third (West Elm, design retailer peers)
REPLAY_FILE_4 = (
    "owner_session_replay_4.json"  # the fourth (46 ratings, duplicates, tagging failures)
)
REPLAY_PATH = REPLAY_DIR / REPLAY_FILE


def load_replay(name: str = REPLAY_FILE) -> dict[str, Any]:
    data = json.loads((REPLAY_DIR / name).read_text())
    assert isinstance(data, dict)
    return data


def _result(rid: str, title: str, price: float, seller: str, rank: int) -> ShopResult:
    return ShopResult(
        id=rid, title=title, price=price, currency="USD", seller=seller,
        thumbnail_url=f"https://mock.example/thumb/{rid}.jpg",
        link=f"https://www.google.com/shopping/product/{rid}", link_kind="google_panel",
        size_hint=None, product_id=rid, rank=rank, pool_position=rank,
    )  # fmt: skip


def seed_session(
    store: SessionStore,
    west_elm_extras: int | None = None,
    comments: dict[str, str] | None = None,
    extra_ratings: dict[str, tuple[str, str]] | None = None,
    replay: str = REPLAY_FILE,
    shown_titles: dict[str, str] | None = None,
) -> Session:
    """A new session holding the replay: one typed search that showed the rated items and the extras
    (`west_elm_extras` of the 30 extras are West Elm; the rest keep their other sellers), and the
    ratings (`comments` by id; `extra_ratings` rates shown extras too; `shown_titles` replaces the
    title of a shown extra by id). Saved and returned."""
    data = load_replay(replay)
    session = store.create()
    results: list[ShopResult] = []
    for n, r in enumerate(data["rated"], start=1):
        results.append(_result(r["id"], r["title"], r["price"], r["seller"], n))
    others = ["IKEA", "Target", "Wayfair", "Article", "Walmart", "Overstock", "Etsy", "Home Depot"]
    for n, x in enumerate(data["shown_extras"], start=len(results) + 1):
        seller = x["seller"]
        if west_elm_extras is not None:
            index = n - len(data["rated"]) - 1
            seller = "West Elm" if index < west_elm_extras else others[index % len(others)]
        title = (shown_titles or {}).get(x["id"], x["title"])
        results.append(_result(x["id"], title, x["price"], seller, n))
    for res in results:
        session.results[res.id] = res
        session.first_seen[res.id] = 1
    query = ShopQuery(
        request=data["request"],
        search_text="wood and metal coffee table with storage",
        category="coffee table",
        category_key="coffee table",
        material_terms=["wood", "metal"],
        must_have=["storage"],
    )
    session.searches.append(
        SearchRecord(
            index=1, request=data["request"], query=query, created_at=now_iso(),
            result_ids=[r.id for r in results[:24]], serper_calls=2, seconds={"total": 0.0},
            anthropic_usd=0.0, provider_usd=0.0, total_usd=0.0, counts=FilterCounts(), ranked=True,
            serper_credits=4, pages={"wood and metal coffee table with storage": 2},
        )
    )  # fmt: skip
    for r in data["rated"]:
        session.ratings[r["id"]] = RatingRecord(
            rating=r["rating"],
            comment=(comments or {}).get(r["id"], r["comment"]),
            rated_at=now_iso_ms(),
            search_index=1,
        )
    for rid, (rating, comment) in (extra_ratings or {}).items():  # more ratings, on shown extras
        session.ratings[rid] = RatingRecord(
            rating=rating,  # type: ignore[arg-type]
            comment=comment,
            rated_at=now_iso_ms(),
            search_index=1,
        )
    store.save(session)
    return session
