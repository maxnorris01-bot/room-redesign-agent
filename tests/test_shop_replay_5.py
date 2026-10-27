"""The fifth replay fixture (stage 3): the owner's curtains session, exported and redacted."""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.shop.replay import REPLAY_FILE_5, load_replay, seed_session
from app.shop.store import SessionStore

FIXTURE = Path(__file__).parent / "fixtures" / "shop" / REPLAY_FILE_5


def test_the_fifth_replay_is_the_curtains_session_in_miniature() -> None:
    data = load_replay(REPLAY_FILE_5)
    assert data["request"] == "boho curtains that pair well with a dark blue room"
    assert len(data["rated"]) == 24 and len(data["pool"]) == 75
    assert [r["comment"] for r in data["rated"] if r.get("comment")] == ["duplicate"]
    assert all(r["rating"] in ("love", "like", "dislike") for r in data["rated"])


def test_the_fifth_replay_carries_nothing_identifying() -> None:
    text = FIXTURE.read_text()
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)  # no email
    urls = re.findall(r"https?://[^\"\s]+", text)
    assert all(u.startswith("https://mock.example/") for u in urls)
    assert "session_id" not in json.loads(text) and "created_at" not in text


def test_the_fifth_replay_seeds_a_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = seed_session(store, replay=REPLAY_FILE_5)
    rated = [r for r in session.ratings.values() if r.rating is not None]
    assert len(rated) == 24 and len(session.results) >= 24
