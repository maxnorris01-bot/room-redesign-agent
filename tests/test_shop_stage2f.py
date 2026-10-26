"""Shop Mode stage 2f: tolerant candidate tagging with diagnostics and a retry in halves, the net
dislike rule, retailer canonicalization, tiers and peers, the 9-call cap, tier allocation in a
batch, style families, the alignment diagnostic and the owner's third session replayed offline.

Offline like the other Shop Mode tests (same loopback-only socket guard).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.llm import Budget
from app.mock_llm import mock_candidate_tags_payload, response, text_block
from app.shop.again import peer_plan
from app.shop.candidates import (
    TagBook,
    Taggable,
    parse_answer,
    tag_max_tokens,
    tag_results,
)
from app.shop.decide import (
    Exposure,
    build_exposure,
    classify_dimension,
    seller_decision,
    tier_decision,
)
from app.shop.models import ProfileEntry, ShopQuery, ShopResult, StoredProfile
from app.shop.prefs import (
    alignment_counts,
    alignment_signals,
    pin_seller,
    profile_summary,
    remove_entry,
)
from app.shop.providers import ReplayPoolSearch
from app.shop.replay import REPLAY_FILE_3, load_replay, seed_session
from app.shop.retailers import (
    AFFINITY_TIERS,
    RETAILERS,
    TIERS,
    canonical_name,
    peers_of,
    retailer_of,
    seller_key,
    tier_members,
    tier_of,
)
from app.shop.search import AgainContext, build_results, run_query
from app.shop.service import ShopService
from app.shop.store import SessionStore
from app.shop.textutil import Rated
from app.shop.vocab import (
    DIMENSIONS,
    STYLE_FAMILY,
    normalize_style,
    style_families,
    tag_values,
)
from app.shopping import SearchCandidate
from test_shop import (  # noqa: F401  (the autouse guard must be imported to apply here)
    FakeLookup,
    FakeSearch,
    RouterClient,
    _loopback_only,
    cand,
    config,
    query,
    use_client,
)
from test_shop_decide import make_session, tagged
from test_shop_stage2e import rated_sellers, rows_in, shown_sellers, tag_calls

ROOT = Path(__file__).resolve().parents[1]


class TagClient(RouterClient):
    """A router double whose candidate tagging answers come from `script(call_number, text)`, which
    returns the answer text and a stop reason, so a test can serve any malformed answer."""

    def __init__(self, script: Callable[[int, str], tuple[str, str]]) -> None:
        super().__init__()
        self.script = script
        self.tag_n = 0

    def _create(self, **kwargs: Any) -> Any:
        if (
            "output_config" in kwargs
            and kwargs["output_config"]["format"]["schema"]["title"] == "CandidateTags"
            or (kwargs.get("tool_choice") or {}).get("name") == "record_tags"
        ):
            self.calls.append(
                {**kwargs, "output_config": {"format": {"schema": {"title": "CandidateTags"}}}}
            )
            self.tag_n += 1
            content = kwargs["messages"][0]["content"]
            text = content if isinstance(content, str) else str(content)
            answer, stop = self.script(self.tag_n, text)
            return response([text_block(answer)], stop_reason=stop, output_tokens=200)
        return super()._create(**kwargs)


def good_answer(text: str) -> str:
    return json.dumps(mock_candidate_tags_payload(text))


def replay3(tmp_path: Path, **kw: Any) -> tuple[ShopService, str, ReplayPoolSearch]:
    cfg = config()
    search = ReplayPoolSearch(REPLAY_FILE_3)
    store = SessionStore(tmp_path / "shop-data")
    svc = ShopService(cfg, store, search, FakeLookup())
    if "extra_ratings" not in kw:
        # stage 2i: the owner's real session liked 6 of 7. With a baseline that high no seller can
        # beat it by 1.5 times, so these tests also dislike ten shown results from other sellers
        kw["extra_ratings"] = {f"S{n}": ("dislike", "") for n in range(1, 11)}
    session = seed_session(store, replay=REPLAY_FILE_3, **kw)
    return svc, session.session_id, search


def titles(n: int, start: int = 0) -> list[Taggable]:
    return [
        Taggable(f"r{i}", f"Round Walnut Table {i}", 100.0 + i, f"S{i}")
        for i in range(start, start + n)
    ]


ROWS = ["1|round|wood|warm wood|black|modern", "2|rectangular|glass|glass|-|industrial+glam"]


# ---- tolerant parsing -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        json.dumps({"rows": ROWS}),
        "```json\n" + json.dumps({"rows": ROWS}) + "\n```",
        "Here are the tags for your results:\n\n1|round|wood|warm wood|black|modern\n2|rectangular|glass|glass|-|industrial+glam",
        '{"rows": ["2|rectangular|glass|glass|-|industrial+glam", "1|round|wood|warm wood|black|modern"]}',
        "- 1 | ROUND | Wood | Warm Wood | Black | Modern\n- 2 | Rectangular | GLASS | glass | - | industrial + glam",
        '{"rows": ["1|round|wood|warm wood|black|modern", "2|rectangular|glass|glass|-|industrial+glam"',
    ],
    ids=["json", "fenced", "prefaced", "reordered", "markers-and-case", "truncated-json"],
)
def test_tolerant_parsing_reads_every_realistic_shape_of_answer(answer: str) -> None:
    drop = answer.endswith('"industrial+glam"')  # a cut-off answer: its last row may be partial
    got, _ = parse_answer(answer, 2, drop_last=False)
    assert sorted(got) == [1, 2]
    assert got[1] == {
        "shape": "round", "material": "wood", "color_finish": "warm wood",
        "frame_color": "black", "style": "modern", "color": None,
    }  # fmt: skip
    assert got[2]["shape"] == "rectangular" and got[2]["material"] == "glass"
    assert got[2]["frame_color"] is None and got[2]["style"] == "industrial,glam"
    assert drop in (True, False)


def test_json_validity_is_reported_and_a_cut_off_last_row_can_be_dropped() -> None:
    ok = json.dumps({"rows": ROWS})
    assert parse_answer(ok, 2)[1] is True
    cut = '{"rows": ["1|round|wood|warm wood|black|modern", "2|rectangular|gl'
    rows, valid = parse_answer(cut, 2, drop_last=True)
    assert valid is False and sorted(rows) == [1]  # the partial row is not trusted
    kept, _ = parse_answer(cut, 2, drop_last=False)
    assert sorted(kept) == [1, 2] and kept[2]["shape"] == "rectangular"


def test_short_extra_and_out_of_range_rows_are_handled() -> None:
    got, _ = parse_answer("1|round\n2|oval|wood|-|-|modern|extra|more\n7|round|wood\n1|square", 3)
    assert sorted(got) == [1, 2]  # 7 is outside the batch, the second "1" is a repeat
    assert got[1] == {
        "shape": "round", "material": None, "color_finish": None, "frame_color": None, "style": None,
        "color": None,
    }  # fmt: skip
    assert got[2]["shape"] == "oval" and got[2]["style"] == "modern"  # extras are ignored
    assert parse_answer("no rows at all", 5) == ({}, False)
    assert parse_answer("shape: round | wood", 2)[0] == {}  # no leading number: not a row


def test_the_token_limit_is_twice_the_estimate_and_bounded() -> None:
    assert tag_max_tokens(100) == 2 * (100 + 16 * 100) == 3400  # text rows: about 14 tokens each
    assert (
        tag_max_tokens(40, "tool") == 2 * (100 + 48 * 40) == 4040
    )  # a tool-call object, with color
    assert tag_max_tokens(10) == 2 * (100 + 160)
    assert tag_max_tokens(10_000) == 6000


# ---- diagnostics, the retry in halves, and the reason ----------------------------------------------------------


def test_every_call_is_logged_with_items_limit_stop_parse_and_the_start_of_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_client(monkeypatch, TagClient(lambda n, text: (good_answer(text), "end_turn")))
    book = TagBook()
    tag_results(titles(12), book, Budget(config()))
    assert len(book.log) == 1
    entry = book.log[0]
    assert entry["items"] == 12 and entry["max_tokens"] == tag_max_tokens(12, "tool")
    assert entry["mode"] == "tool"
    assert entry["stop_reason"] == "end_turn" and entry["json_ok"] is True
    assert entry["rows_parsed"] == 12 and entry["retry"] is False and entry["error"] == ""
    assert entry["raw"].startswith('{"rows": ["1|round|wood|warm wood') and len(entry["raw"]) <= 200
    assert not book.failed and book.reason == ""


def test_a_cut_off_answer_is_retried_once_for_what_is_still_untagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def script(n: int, text: str) -> tuple[str, str]:
        if n == 1:  # the first call is cut off after 4 of 10 rows
            rows = [r for r in json.loads(good_answer(text))["rows"][:4]]
            return json.dumps({"rows": rows})[:-3], "max_tokens"
        return good_answer(text), "end_turn"

    client = TagClient(script)
    use_client(monkeypatch, client)
    book = TagBook()
    tag_results(titles(10), book, Budget(config()))
    assert book.calls == 3 and book.tagged == 10 and not book.failed  # a call, one retry round
    assert [e["retry"] for e in book.log] == [False, True, True]
    assert [e["items"] for e in book.log] == [10, 5, 2]  # the 7 untagged, in pieces of half (5)
    assert book.log[0]["stop_reason"] == "max_tokens" and book.log[0]["rows_parsed"] == 3
    assert [len(rows_in(c)) for c in tag_calls(client)] == [10, 5, 2]


def test_fewer_than_eighty_percent_of_rows_also_triggers_the_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def script(n: int, text: str) -> tuple[str, str]:
        rows = json.loads(good_answer(text))["rows"]
        return json.dumps({"rows": rows[:7] if n == 1 else rows}), "end_turn"

    use_client(monkeypatch, TagClient(script))
    book = TagBook()
    tag_results(titles(10), book, Budget(config()))
    assert book.calls == 2 and book.tagged == 10  # 7 of 10 is under 80%: one retry
    use_client(
        monkeypatch,
        TagClient(
            lambda n, t: (json.dumps({"rows": json.loads(good_answer(t))["rows"][:8]}), "end_turn")
        ),
    )
    exact = TagBook()
    tag_results(titles(10), exact, Budget(config()))
    assert exact.calls == 1 and exact.tagged == 8  # 8 of 10 is exactly 80%: no retry


def test_a_failure_after_the_retry_keeps_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    use_client(monkeypatch, TagClient(lambda n, t: ("Sorry, I cannot do that.", "end_turn")))
    book = TagBook()
    tag_results(titles(10), book, Budget(config()))
    assert book.failed and book.tagged == 0 and book.calls == 3  # a call, then ONE retry round
    assert "not valid JSON and had no readable rows" in book.reason
    assert "Sorry, I cannot do that." in book.reason
    use_client(monkeypatch, TagClient(lambda n, t: ('{"rows": ["1|rou', "max_tokens")))
    cut = TagBook()
    tag_results(titles(10), cut, Budget(config()))
    assert "cut off at the" in cut.reason and "token limit after 0 of" in cut.reason


def test_the_real_reason_reaches_the_page_not_a_generic_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, sid, _ = replay3(tmp_path)
    use_client(monkeypatch, TagClient(lambda n, t: ('{"rows": ["1|rou', "max_tokens")))
    view = svc.shop_again(sid)
    warning = next(
        w for w in view["search"]["warnings"] if w.startswith("Candidate tagging failed:")
    )
    assert "the answer was cut off at the" in warning and "token limit" in warning
    assert "No attribute preference was applied" in warning
    log = view["search"]["tagging"]["tag_call_log"]
    assert log and log[0]["stop_reason"] == "max_tokens" and log[0]["json_ok"] is False
    assert view["search"]["trace"]["tag_calls"] == log  # the trace carries the same record
    saved = json.loads((tmp_path / "shop-data" / f"{sid}.json").read_text())
    assert saved["searches"][-1]["tagging"]["tag_call_log"][0]["stop_reason"] == "max_tokens"
    assert (
        view["search"]["tagging"]["candidates_tagged"] == 0 and len(view["search"]["results"]) > 0
    )


# ---- seller canonicalization -----------------------------------------------------------------------------------


def test_sellers_fold_case_punctuation_and_marketplace_suffixes() -> None:
    for name in ("Walmart - JerFit Life", "Walmart", "WALMART.com", "walmart - Hommoo Direct"):
        assert seller_key(name) == "walmart", name
    assert seller_key("Crate and Barrel") == seller_key("Crate & Barrel") == "crateandbarrel"
    assert seller_key("The Home Depot") == seller_key("Home Depot") == "homedepot"
    assert seller_key("Lowe's") == seller_key("Lowes") == "lowes"
    assert canonical_name("Walmart - JerFit Life") == "Walmart"
    assert canonical_name("crate and barrel") == "Crate & Barrel"
    assert canonical_name("Some Small Shop - Seller Name") == "Some Small Shop"
    assert canonical_name("Some Small Shop") == "Some Small Shop"
    assert retailer_of("Lowes") is not None and retailer_of("Lowes").name == "Lowe's"  # type: ignore[union-attr]
    assert retailer_of("Totally Unknown Store") is None and tier_of("Totally Unknown Store") is None


def test_marketplace_sub_sellers_are_one_seller_in_the_decisions() -> None:
    rated = rated_sellers(
        [
            ("like", "Walmart - JerFit Life", "1"),
            ("like", "Walmart - Hommoo", "2"),
            ("like", "Walmart", "3"),
            ("dislike", "walmart.com", "4"),
            ("dislike", "Target", "5"),
            ("dislike", "Target", "6"),
        ]
    )
    entry, trace = seller_decision(rated)
    walmart = next(c for c in trace["candidates"] if c["seller"] == "Walmart")
    assert walmart["rated"] == 4 and walmart["liked"] == 3  # four spellings, one seller
    assert entry is None and "marketplace and budget retailers never earn" in walmart["verdict"]


def test_the_retailer_table_is_consistent() -> None:
    names = {r.name for r in RETAILERS}
    assert len(names) == len(RETAILERS) and {r.tier for r in RETAILERS} == set(TIERS)
    for r in RETAILERS:
        assert r.name not in r.peers, r.name
        for peer in r.peers:
            assert retailer_of(peer) is not None, f"{r.name} lists unknown peer {peer}"
    premium = {r.name for r in tier_members("design_premium")}
    assert {
        "West Elm", "CB2", "Crate & Barrel", "Pottery Barn", "Article", "Room & Board",
        "Design Within Reach", "Anthropologie", "Rejuvenation", "Joybird", "Burrow", "Blu Dot",
        "Herman Miller", "RH", "Williams Sonoma Home",
    } == premium  # fmt: skip
    assert {r.name for r in tier_members("design_mainstream")} == {
        "AllModern",
        "World Market",
        "Target",
        "Wayfair",
        "IKEA",
    }
    assert {"Walmart", "Amazon", "Lowe's", "Home Depot", "Overstock"} <= {
        r.name for r in tier_members("marketplace_budget")
    }
    assert [p.name for p in peers_of("West Elm")][:3] == ["Article", "CB2", "Crate & Barrel"]
    assert peers_of("Unknown") == [] and AFFINITY_TIERS == ("design_premium", "design_mainstream")


# ---- the net dislike rule --------------------------------------------------------------------------------------


def rate_case(likes: int, dislikes: int) -> tuple[ProfileEntry | None, dict[str, Any]]:
    """Seller A with `likes` liked and `dislikes` disliked items, in a session where 12 more items
    from other sellers were rated and 2 of them liked (so the baseline moves with A)."""
    spec = [("like", "A", f"l{i}") for i in range(likes)] + [
        ("dislike", "A", f"d{i}") for i in range(dislikes)
    ]
    spec += [("like", "B", f"b{i}") for i in range(2)] + [
        ("dislike", "C", f"c{i}") for i in range(10)
    ]
    return seller_decision(rated_sellers(spec))


def test_dislikes_lower_a_sellers_like_rate_and_the_rule_is_a_rate_not_a_net() -> None:
    assert rate_case(4, 0)[0] is not None  # 4 of 4 against 6 of 16 (38%)
    assert rate_case(4, 1)[0] is not None  # 80%: seven of the old net rule's passes still pass
    assert rate_case(3, 2)[0] is not None  # 60% is over 1.5 times 5 of 17 (29%) and over 35%
    assert rate_case(2, 2)[0] is not None  # 50% of 4 rated against 4 of 16 (25%)
    assert rate_case(2, 5)[0] is None  # 29% of 7 rated: under 35%
    entry, trace = rate_case(1, 3)
    row_a = next(c for c in trace["candidates"] if c["seller"] == "A")
    assert entry is None and "liked 1, needs 2" in row_a["verdict"]
    row = next(c for c in rate_case(2, 6)[1]["candidates"] if c["seller"] == "A")
    assert row["rated"] == 8 and row["liked"] == 2 and row["like_rate"] == 0.25
    assert "like rate 25% is under" in row["verdict"]
    assert rate_case(6, 2)[0] is not None


def test_loved_items_count_once_in_the_like_rate_and_three_in_the_like_mass() -> None:
    rated = rated_sellers(
        [("love", "A", "1"), ("like", "A", "2"), ("dislike", "A", "3"), ("dislike", "A", "4")]
        + [("dislike", "B", f"b{i}") for i in range(6)]
    )
    entry, trace = seller_decision(rated)
    row = trace["candidates"][0]
    assert row["seller"] == "A" and row["liked"] == 2 and row["like_mass"] == 4  # 3 + 1
    assert entry is not None and entry.weight == 4 and entry.share == 1.0  # all the like mass


def test_the_other_seller_conditions_stand_with_the_net_rule() -> None:
    shown = shown_sellers({"A": 4, "B": 20, "C": 20, "D": 20})
    # a seller with 1 item is still out, and so is one under 40% of the like mass
    one = rated_sellers([("love", "A", "1"), ("like", "B", "2")])
    assert seller_decision(one, shown)[0] is None
    spread = rated_sellers(
        [("like", "A", "1"), ("like", "A", "2")]
        + [("like", s, f"x{i}") for i, s in enumerate("BCDBCD")]
    )
    assert seller_decision(spread, shown)[0] is None  # 2 of 8 = 25%


# ---- retailer tiers ------------------------------------------------------------------------------------------


def tier_case(spec: list[tuple[str, str, str]]) -> tuple[ProfileEntry | None, dict[str, Any]]:
    return tier_decision(rated_sellers(spec))


def test_a_tier_needs_the_group_like_rate_rule() -> None:
    entry, trace = tier_case(
        [
            ("love", "West Elm", "1"),
            ("like", "West Elm", "2"),
            ("like", "Article", "3"),
            ("dislike", "CB2", "4"),
        ]
        + [("dislike", "Walmart", f"w{i}") for i in range(4)]
    )  # premium 3 of 4 (75%) against 3 of 8 (38%)
    assert entry is not None and entry.attr_kind == "tier" and entry.values == ["design_premium"]
    assert entry.text == "retailers like West Elm" and entry.detail.endswith(
        "liked: West Elm, Article"
    )
    assert trace["decision"] == "chip" and trace["candidates"][0]["label"] == "design premium"
    # 4 rated and 2 liked is the least; a rate just under 1.5 times the baseline fails
    under, under_trace = tier_case(
        [
            ("like", "West Elm", "1"),
            ("like", "Article", "2"),
            ("dislike", "CB2", "3"),
            ("dislike", "Joybird", "4"),
            ("like", "Lowe's", "5"),
            ("like", "Walmart", "6"),
            ("like", "Etsy", "7"),
            ("dislike", "Etsy", "8"),
        ]
    )  # premium 2 of 4 (50%) against 5 of 8 (62%)
    premium = next(c for c in under_trace["candidates"] if c["tier"] == "design_premium")
    assert under is None and "like rate 50% is under 94%" in premium["verdict"]
    # fewer than 4 rated is never enough, however many were liked
    single, single_trace = tier_case(
        [("love", "West Elm", "1"), ("like", "West Elm", "2"), ("dislike", "Lowe's", "3")]
    )
    assert single is None and "rated 2, needs 4" in single_trace["candidates"][0]["verdict"]


def test_a_marketplace_tier_never_earns_a_chip() -> None:
    entry, trace = tier_case(
        [("love", "Lowe's", "1"), ("love", "Home Depot", "2"), ("like", "Amazon", "3"),
         ("like", "Walmart", "4")] + [("dislike", "Etsy", f"e{i}") for i in range(6)]
    )  # fmt: skip
    assert entry is None
    assert (
        "marketplace and budget retailers never earn a retailer chip"
        in trace["candidates"][0]["verdict"]
    )
    mainstream, _ = tier_case(
        [("love", "Wayfair", "1"), ("like", "Target", "2"), ("like", "IKEA", "3"),
         ("dislike", "Target", "4")] + [("dislike", "Etsy", f"e{i}") for i in range(6)]
    )  # fmt: skip
    assert mainstream is not None and mainstream.values == ["design_mainstream"]  # these tiers can


def test_the_tier_chip_is_deletable_stays_deleted_and_can_be_pinned() -> None:
    chip = ProfileEntry(
        text="retailers like West Elm", attr_kind="tier", polarity="like", dimension="tier",
        values=["design_premium"], detail="liked: West Elm",
    )  # fmt: skip
    profile = StoredProfile(inferred=[chip])
    pinned = pin_seller(profile, "retailers like West Elm")
    assert pinned is not None and pinned.pinned_sellers == ["tier:design_premium"]
    assert pinned.inferred[0].pinned is True
    unpinned = pin_seller(pinned, "retailers like West Elm")
    assert (
        unpinned is not None and unpinned.pinned_sellers == [] and not unpinned.inferred[0].pinned
    )
    assert pin_seller(profile, "no such chip") is None
    gone = remove_entry(profile, "inferred", "retailers like West Elm")
    assert gone is not None and gone.inferred == [] and "retailers like west elm" in gone.removed
    # a recompute from the same ratings does not bring it back
    from app.shop.decide import decide

    rated = rated_sellers(
        [("love", "West Elm", "1"), ("like", "West Elm", "2"), ("like", "Article", "3"),
         ("like", "CB2", "4")] + [("dislike", "Etsy", f"e{i}") for i in range(6)]
    )  # fmt: skip
    again = decide(rated, {}, [], gone.removed, [], exposure=Exposure(), fallback=True)
    assert not [e for e in again.entries if e.attr_kind == "tier"]
    kept = decide(rated, {}, [], [], [], exposure=Exposure(), fallback=True)
    assert [e.text for e in kept.entries if e.attr_kind == "tier"] == ["retailers like West Elm"]


def test_the_summary_says_similar_retailers_for_a_tier_chip() -> None:
    seller = ProfileEntry(
        text="seller: West Elm", attr_kind="seller", polarity="like", dimension="seller"
    )
    tier = ProfileEntry(
        text="retailers like West Elm",
        attr_kind="tier",
        polarity="like",
        dimension="tier",
        values=["design_premium"],
    )
    assert (
        profile_summary(StoredProfile(inferred=[seller, tier]))
        == "Looking for more pieces from West Elm and similar retailers."
    )
    assert (
        profile_summary(StoredProfile(inferred=[tier]))
        == "Looking for more pieces from retailers like West Elm."
    )
    assert (
        profile_summary(StoredProfile(inferred=[seller]))
        == "Looking for more pieces from West Elm."
    )


# ---- peers --------------------------------------------------------------------------------------------------


def peer_session(tmp_path: Path) -> tuple[SessionStore, Any]:
    store = SessionStore(tmp_path / "shop-data")
    return store, seed_session(store, replay=REPLAY_FILE_3)


def west_elm_profile() -> StoredProfile:
    seller = ProfileEntry(
        text="seller: West Elm", attr_kind="seller", polarity="like", dimension="seller"
    )
    tier = ProfileEntry(
        text="retailers like West Elm",
        attr_kind="tier",
        polarity="like",
        dimension="tier",
        values=["design_premium"],
    )
    return StoredProfile(inferred=[seller, tier])


def test_peers_come_from_the_liked_retailer_in_order_and_skip_rated_ones(tmp_path: Path) -> None:
    _, session = peer_session(tmp_path)
    base = query(request="coffee tables", search_text="wood and metal coffee table with storage")
    plan = peer_plan(session, west_elm_profile(), base)
    assert [name for _, name, _ in plan] == ["Article", "CB2", "Crate & Barrel"]
    assert plan[0] == ("article", "Article", "Article wood and metal coffee table with storage")
    assert all("West Elm" not in name for _, name, _ in plan)  # never the liked retailer itself
    assert peer_plan(session, StoredProfile(), base) == []  # no affinity: no peer searches
    market = StoredProfile(
        inferred=[
            ProfileEntry(
                text="seller: Lowe's", attr_kind="seller", polarity="like", dimension="seller"
            )
        ]
    )
    assert peer_plan(session, market, base) == []  # a marketplace seller has no peers to search


def test_peers_rotate_across_shop_agains_and_a_silent_peer_is_never_searched_again(
    tmp_path: Path,
) -> None:
    _, session = peer_session(tmp_path)
    base = query(request="coffee tables", search_text="coffee table")
    profile = west_elm_profile()
    first = [n for _, n, _ in peer_plan(session, profile, base)]
    assert first == ["Article", "CB2", "Crate & Barrel"]
    session.searched_peers = {
        "article": {"name": "Article", "searches": 1, "new": 8, "empty": False},
        "cb2": {"name": "CB2", "searches": 1, "new": 0, "empty": True},  # returned nothing new
        "crateandbarrel": {"name": "Crate & Barrel", "searches": 1, "new": 8, "empty": False},
    }
    second = [n for _, n, _ in peer_plan(session, profile, base)]
    assert second == ["Room & Board", "Pottery Barn", "Joybird"]  # the next three, none repeated
    assert not set(first) & set(second)
    for key in ("pottery", "roomandboard", "joybird"):  # every peer searched (some silent)
        session.searched_peers[key] = {"name": key, "searches": 1, "new": 0, "empty": True}
    searched_all = {seller_key(r.name) for r in tier_members("design_premium")}
    for key in searched_all - {"westelm"}:
        session.searched_peers.setdefault(
            key, {"name": key, "searches": 1, "new": 0, "empty": True}
        )
    session.searched_peers["article"]["empty"] = False
    last = [n for _, n, _ in peer_plan(session, profile, base)]
    assert last == ["Article", "Crate & Barrel"]  # only peers that found something are asked again
    assert "CB2" not in last


def test_two_shop_agains_on_replay_three_search_different_peers(tmp_path: Path) -> None:
    svc, sid, search = replay3(tmp_path)
    first = svc.shop_again(sid)
    peers_one = [p["peer"] for p in first["search"]["trace"]["batch"]["peers"]]
    assert peers_one == ["West Elm", "Article", "CB2"]  # the liked seller first, then peers
    kinds = {p["peer"]: p["kind"] for p in first["search"]["trace"]["batch"]["peers"]}
    assert kinds == {"West Elm": "seller", "Article": "peer", "CB2": "peer"}
    saved = svc.store.load(sid)
    assert set(saved.searched_peers) == {"westelm", "article", "cb2"}
    assert all(v["searches"] == 1 and v["new"] > 0 for v in saved.searched_peers.values())
    second = svc.shop_again(sid)
    peers_two = [p["peer"] for p in second["search"]["trace"]["batch"]["peers"]]
    assert not (set(peers_one) - {"West Elm"}) & set(peers_two)  # peers rotated on: no repeats
    assert len(peers_two) == 3 and peers_two[0] == "West Elm"  # a liked seller is searched again
    assert "Crate & Barrel" in peers_two
    both = svc.store.load(sid).searched_peers
    assert len(both) == 5 and both["westelm"]["searches"] == 2
    texts = [q for q, _ in search.calls]
    assert texts.count("Article wood and metal coffee table with storage") == 1


# ---- the 9-call cap and the cost report --------------------------------------------------------------------


def rect_page(prefix: str, n: int) -> list[SearchCandidate]:
    return [
        cand(
            f"{prefix}{i}",
            100.0 + i,
            title=f"Rectangular Wood Coffee Table {prefix}{i}",
            seller=f"{prefix}S{i}",
        )
        for i in range(n)
    ]


def run_with_peers(
    monkeypatch: pytest.MonkeyPatch, total: int, supply_feed: int = 12
) -> tuple[Any, FakeSearch]:
    use_client(monkeypatch, RouterClient())
    search = FakeSearch([])
    search.sequence = (
        [rect_page("a", 30)]
        + [rect_page(f"p{n}", 8) for n in range(3)]
        + [rect_page(f"z{n}", 8) for n in range(supply_feed)]
    )  # never any round table: the supply step keeps trying
    cfg = config(shop_again_max_serper_calls=min(4, total), shop_again_max_serper_calls_total=total)
    again = AgainContext(
        open_dims={"shape": ["rectangular", "round"]},
        open_mass={"shape": {"rectangular": 11, "round": 5}},
        open_loved={"shape": {"rectangular", "round"}},
        guard_caps={"shape": 0.6},
        max_calls=1,
        max_calls_total=total,
        tag_book=TagBook(),
        peer_queries=[
            (k, n, f"{n} coffee table")
            for k, n in (
                ("article", "Article"),
                ("cb2", "CB2"),
                ("crateandbarrel", "Crate and Barrel"),
            )
        ],
    )
    outcome = run_query(
        query(request="coffee tables", search_text="coffee table", category="coffee table"),
        search, cfg, Budget(cfg), [], time.perf_counter(), again,
    )  # fmt: skip
    return outcome, search


def test_the_total_cap_of_nine_counts_phrasings_peers_and_supply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, search = run_with_peers(monkeypatch, 9)
    texts = [q for q, _ in search.calls]
    assert len(texts) == 9 and outcome.serper_calls == 9 and outcome.serper_credits == 18
    assert texts[:4] == [
        "coffee table",
        "Article coffee table",
        "CB2 coffee table",
        "Crate and Barrel coffee table",
    ]
    assert all(t == "round coffee table" for t in texts[4:])  # the rest is supply
    assert outcome.composition["serper_calls"] == 9 and outcome.composition["serper_credits"] == 18
    assert [p["peer"] for p in outcome.peer_log] == ["Article", "CB2", "Crate and Barrel"]
    assert all(p["new_candidates"] == 8 and p["found"] == 8 for p in outcome.peer_log)
    assert outcome.total_usd == pytest.approx(outcome.anthropic_usd + outcome.provider_usd)
    assert outcome.provider_usd == pytest.approx(9 * 0.002)  # 18 credits at $0.001


def test_a_lower_total_cap_is_never_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome, search = run_with_peers(monkeypatch, 5)
    assert len(search.calls) == 5 and outcome.serper_calls == 5
    assert [p["peer"] for p in outcome.peer_log] == ["Article", "CB2", "Crate and Barrel"]
    assert len([q for q, _ in search.calls if q == "round coffee table"]) == 1
    two, search2 = run_with_peers(monkeypatch, 2)
    assert len(search2.calls) == 2 and len(two.peer_log) == 1  # the typed search, then one peer


def test_peer_searches_are_one_page_each_and_a_failed_one_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.shopping import ProviderError

    use_client(monkeypatch, RouterClient())
    search = FakeSearch([])
    search.sequence = [rect_page("a", 30), ProviderError("serper", "boom"), rect_page("c", 8)]
    cfg = config()
    again = AgainContext(
        max_calls=1, max_calls_total=9, tag_book=TagBook(),
        peer_queries=[("article", "Article", "Article coffee table"), ("cb2", "CB2", "CB2 coffee table")],
    )  # fmt: skip
    warnings: list[str] = []
    outcome = run_query(
        query(request="coffee tables", search_text="coffee table", category="coffee table"),
        search, cfg, Budget(cfg), warnings, time.perf_counter(), again,
    )  # fmt: skip
    assert outcome.serper_calls == 3 and outcome.peer_log[0]["error"] == "search failed"
    assert outcome.peer_log[1]["new_candidates"] == 8
    assert any("Search for Article unavailable" in w for w in warnings)


def test_config_names_the_total_cap_and_validates_it() -> None:
    assert Config().shop_again_max_serper_calls_total == 9
    assert Config().shop_again_max_serper_calls == 4
    with pytest.raises(ValueError):
        Config(shop_again_max_serper_calls=5, shop_again_max_serper_calls_total=4)
    env = (ROOT / ".env.example").read_text()
    assert "APP_SHOP_AGAIN_MAX_SERPER_CALLS_TOTAL=9" in env


# ---- batch allocation: tier, seller caps, pin -------------------------------------------------------------


def tier_pool(
    spec: list[tuple[str, int]], shape: str = "Rectangular"
) -> list[tuple[SearchCandidate, str]]:
    out: list[tuple[SearchCandidate, str]] = []
    for seller, n in spec:
        for i in range(n):
            out.append(
                (
                    cand(
                        f"{seller[:3]}{i}",
                        100.0 + i,
                        title=f"{shape} Coffee Table {seller} {i}",
                        seller=f"{seller}",
                    ),
                    "",
                )
            )
    return out


def run_build(
    pool: list[tuple[SearchCandidate, str]], **kw: Any
) -> tuple[list[ShopResult], dict[str, int]]:
    index = {c.product_id: n for n, (c, _) in enumerate(pool, 1)}
    results, _, _ = build_results(pool, query(request="t"), index, **kw)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.seller] = counts.get(r.seller, 0) + 1
    return results, counts


def tier_count(results: list[ShopResult], tier: str) -> int:
    return sum(1 for r in results if tier_of(r.seller) == tier)


PEERS = ["Article", "CB2", "Crate & Barrel", "Pottery Barn", "Room & Board", "Joybird"]


def test_the_affinity_tier_takes_up_to_half_a_batch_with_four_per_seller() -> None:
    pool = tier_pool([("Walmart", 10), ("Lowe's", 10), ("Target", 10)]) + tier_pool(
        [(p, 6) for p in PEERS]
    )
    results, counts = run_build(pool, tier_limit=("design_premium", 12, 12))
    assert len(results) == 24 and tier_count(results, "design_premium") == 12
    assert all(counts[p] == 4 for p in PEERS[:3])  # four per seller (the default cap) until 12
    assert max(counts.values()) <= 4
    # the best ranked results of the tier go first, then the batch is back in ranked order
    ranks = [r.rank for r in results]
    assert ranks == list(range(1, 25))
    pool_order = [c.seller for c, _ in pool]
    assert [r.seller for r in results] == sorted([r.seller for r in results], key=pool_order.index)


def test_the_tier_cap_holds_even_when_the_tier_is_ranked_first() -> None:
    pool = tier_pool([(p, 6) for p in PEERS]) + tier_pool(
        [("Walmart", 4), ("Lowe's", 4), ("Target", 4), ("IKEA", 4), ("Wayfair", 4), ("Etsy", 4)]
    )
    results, counts = run_build(pool, tier_limit=("design_premium", 12, 16))
    assert tier_count(results, "design_premium") == 12 and len(results) == 24
    assert max(counts.values()) <= 4
    pinned, _ = run_build(
        pool, tier_limit=("design_premium", 19, 19), pinned_sellers={seller_key(p) for p in PEERS}
    )
    assert tier_count(pinned, "design_premium") == 19 and len(pinned) == 24  # pinned: up to 80%
    more, pinned_counts = run_build(
        pool, tier_limit=("design_premium", 19, 19), pinned_sellers={seller_key(p) for p in PEERS}
    )
    assert max(pinned_counts.get(p, 0) for p in PEERS) <= 19


def test_an_affinity_seller_may_hold_half_inside_the_tier_cap() -> None:
    pool = tier_pool([("West Elm", 20), ("Article", 6), ("CB2", 6)]) + tier_pool(
        [("Walmart", 4), ("Lowe's", 4), ("Target", 4), ("IKEA", 4)]
    )
    results, counts = run_build(
        pool, affinity_sellers={"westelm"}, tier_limit=("design_premium", 12, 12)
    )
    assert counts["West Elm"] <= 12 and tier_count(results, "design_premium") <= 12
    assert counts.get("Article", 0) <= 4 and counts.get("CB2", 0) <= 4


def test_tier_allocation_leaves_each_open_shape_its_planned_slots() -> None:
    rect = tier_pool([(p, 6) for p in PEERS], "Rectangular")
    rnd = tier_pool(
        [("Walmart", 4), ("Lowe's", 4), ("Target", 4), ("IKEA", 4), ("Wayfair", 4), ("Etsy", 4)],
        "Round",
    )
    plain = tier_pool([("Overstock", 4), ("Amazon", 4), ("Home Depot", 4)], "Rectangular")
    pool = rect + rnd + plain
    results, _ = run_build(
        pool, guard_caps={"shape": 0.6}, strata=("shape", {"rectangular": 14, "round": 10}),
        tier_limit=("design_premium", 12, 12),
    )  # fmt: skip
    shapes = [r.title.split()[0] for r in results]
    assert shapes.count("Round") == 10 and shapes.count("Rectangular") == 14
    assert tier_count(results, "design_premium") == 12  # the tier fits inside the rectangular slots


# ---- style families -----------------------------------------------------------------------------------------


def test_style_labels_are_multi_label_with_at_most_two_values() -> None:
    assert normalize_style(["industrial", "glam"]) == "industrial,glam"
    assert normalize_style("industrial+glam") == "industrial,glam"
    assert normalize_style(["modern", "mid-century", "boho"]) == "modern,mid-century"  # at most two
    assert normalize_style(["modern", "Modern"]) == "modern" and normalize_style([]) is None
    assert normalize_style(["unclassifiable"]) == "other"
    assert normalize_style(["unclassifiable", "glam"]) == "glam"  # `other` yields to a real value
    assert normalize_style(None) is None and normalize_style("scandinavian") == "modern"
    assert style_families("industrial,glam") == ["modern_leaning"]
    assert style_families("modern,farmhouse") == ["modern_leaning", "traditional_leaning"]
    assert style_families("other") == [] and tag_values("shape", "round") == ["round"]
    assert STYLE_FAMILY["rustic"] == STYLE_FAMILY["farmhouse"] == "traditional_leaning"
    assert STYLE_FAMILY["scandinavian"] == "modern_leaning"


def style_items(spec: list[tuple[str, str]]) -> list[Any]:
    return [tagged(f"s{n}", rating, "Table", style=value) for n, (rating, value) in enumerate(spec)]


STYLE_EXP = Exposure(
    shares={"style": {"modern_leaning": 0.5, "traditional_leaning": 0.5}}, coverage={"style": 0.7}
)


def test_style_converges_on_the_family_not_the_leaf() -> None:
    items = style_items(
        [
            ("love", "industrial"),
            ("love", "glam"),
            ("like", "mid-century"),
            ("like", "modern"),
            ("like", "modern"),
        ]
    )
    res = classify_dimension("style", items, STYLE_EXP)
    assert res.status == "converged" and res.value == "modern_leaning"  # not "industrial 33%"
    assert res.trace["like_mass"]["modern_leaning"] == 9
    assert res.trace["leaves"] == {"glam": 3, "industrial": 3, "mid-century": 1, "modern": 2}
    assert res.share == 1.0 and res.lift == 2.0
    # a multi-label item counts once in its family
    both = style_items([("love", "industrial,glam"), ("love", "modern,mid-century")])
    assert classify_dimension("style", both, STYLE_EXP).trace["like_mass"] == {
        "modern_leaning": 6,
        "(unknown)": 0,
    }


def test_two_families_at_thirty_percent_are_open_and_the_soft_threshold_is_sixty() -> None:
    mixed = style_items([("love", "modern"), ("love", "farmhouse")])
    res = classify_dimension("style", mixed, STYLE_EXP)
    assert res.status == "open" and sorted(res.values) == ["modern_leaning", "traditional_leaning"]
    # 60% of the like mass converges (soft threshold), just under does not
    low = Exposure(
        shares={"style": {"modern_leaning": 0.3, "traditional_leaning": 0.7}},
        coverage={"style": 0.7},
    )
    sixty = style_items([("like", "modern")] * 3 + [("like", "boho")] * 2)
    assert classify_dimension("style", sixty, low).status == "converged"
    fifty_five = style_items([("like", "modern")] * 5 + [("like", "boho")] * 4)
    assert classify_dimension("style", fifty_five, low).status == "open"
    # an item that names both families counts in both, so neither is a majority
    span = style_items([("love", "modern,farmhouse"), ("like", "modern")])
    assert (
        classify_dimension("style", span, STYLE_EXP).trace["like_mass"]["traditional_leaning"] == 3
    )


def test_a_style_chip_says_the_family_and_the_leaves_stay_in_the_trace() -> None:
    from app.shop.decide import decide

    sess = make_session(
        [(f"i{n}", f"Table {n}", 100.0, f"S{n}") for n in range(4)],
        {"i0": ("love", ""), "i1": ("love", ""), "i2": ("like", ""), "i3": ("like", "")},
    )
    from app.shop.models import ItemTags
    from app.shop.prefs import rated_map

    styles = {
        "i0": ["industrial"],
        "i1": ["glam", "modern"],
        "i2": ["mid-century"],
        "i3": ["modern"],
    }
    tags = {
        rid: ItemTags.model_validate(
            {"id": rid, "shape": None, "material": None, "color_finish": None, "frame_color": None,
             "style": style, "quality_cues": []}
        )
        for rid, style in styles.items()
    }  # fmt: skip
    shown = list(sess.results.values())
    exposure = build_exposure(
        shown, {r.id: {"style": "modern" if n % 2 else "farmhouse"} for n, r in enumerate(shown)}
    )
    out = decide(rated_map(sess), tags, shown, [], [], exposure=exposure)
    chip = next(e for e in out.entries if e.dimension == "style")
    assert chip.text == "modern-leaning style" and chip.values == ["modern_leaning"]
    assert "industrial" not in chip.text and "glam" not in chip.text
    assert out.trace["dimensions"]["style"]["leaves"]["industrial"] == 3


# ---- the alignment diagnostic ---------------------------------------------------------------------------------------


def one_result(title: str, seller: str = "S", price: float = 100.0) -> ShopResult:
    return ShopResult(
        id=title, title=title, price=price, currency="USD", seller=seller, thumbnail_url="",
        link="", link_kind="none", size_hint=None, product_id=title, rank=1, pool_position=1,
    )  # fmt: skip


def test_alignment_counts_every_decided_signal_and_none_means_no_signal() -> None:
    assert alignment_signals(None) == 0 and alignment_signals(StoredProfile()) == 0
    seller = ProfileEntry(
        text="seller: West Elm", attr_kind="seller", polarity="like", dimension="seller"
    )
    tier = ProfileEntry(
        text="retailers like West Elm",
        attr_kind="tier",
        polarity="like",
        dimension="tier",
        values=["design_premium"],
    )
    band = ProfileEntry(
        text="upper range, about $600 to $900",
        attr_kind="price_band",
        polarity="like",
        low=600.0,
        high=900.0,
        band="upper",
    )
    mixed = ProfileEntry(
        text="price: mixed", attr_kind="price_mixed", polarity="like", status="mixed"
    )
    opened = ProfileEntry(
        text="shape: open",
        attr_kind="open",
        polarity="like",
        dimension="shape",
        status="open",
        values=["round", "rectangular"],
    )
    profile = StoredProfile(inferred=[seller, tier, band, mixed, opened], likes=["storage"])
    assert (
        alignment_signals(profile) == 4
    )  # seller, tier, band, a stated like: open and mixed are not
    results = [
        one_result("Plain Table", seller="West Elm"),  # the seller
        one_result("Plain Table 2", seller="Article"),  # the tier
        one_result("Plain Table 3", price=700.0),  # the band
        one_result("Table with Storage"),  # the stated like
        one_result("Plain Table 5", price=100.0),  # nothing
    ]
    assert alignment_counts(results, profile) == (4, 5)
    only_open = StoredProfile(inferred=[opened, mixed])
    assert alignment_signals(only_open) == 0 and alignment_counts(results, only_open) == (0, 5)


def test_the_page_says_n_a_when_nothing_is_decided_and_never_0_of_24() -> None:
    from app.shop.server import STATIC_DIR

    code = (STATIC_DIR / "app.js").read_text()
    assert "matches your preferences: n/a (nothing decided yet)" in code
    assert "s.alignment.signals === 0" in code
    assert "affinity tier" in code and "' sellers'" in code  # the new footer diagnostics
    assert "\\u0027" not in code


def test_a_shop_again_with_no_decided_signal_reports_zero_signals(tmp_path: Path) -> None:
    svc, sid, _ = replay3(tmp_path)
    store = svc.store
    sess = store.load(sid)
    for rid in sess.ratings:  # one like and nothing else: nothing can be decided from it
        sess.ratings[rid].rating = "like" if rid == "Z4" else None
    store.save(sess)
    view = svc.shop_again(sid)
    align = view["search"]["alignment"]
    assert align["signals"] == 0 and align["matches"] == 0  # the page shows n/a, not "0 of 24"


def test_the_footer_numbers_include_the_tier_share_and_distinct_sellers(tmp_path: Path) -> None:
    svc, sid, _ = replay3(tmp_path)
    view = svc.shop_again(sid)
    batch = view["search"]["trace"]["batch"]
    assert batch["tier"]["tier"] == "design_premium" and batch["tier"]["cap"] == 12
    assert batch["batch"]["tier"]["design_premium"] <= 12
    assert batch["batch"]["distinct_sellers"] >= 6
    assert view["search"]["alignment"]["signals"] >= 3


# ---- the third session, replayed ------------------------------------------------------------------------------------


def test_the_third_replay_fixture_is_the_owners_third_session() -> None:
    data = load_replay(REPLAY_FILE_3)
    rated = data["rated"]
    assert [r["rating"] for r in rated].count("love") == 3
    assert [r["rating"] for r in rated].count("like") == 3
    assert [r["rating"] for r in rated].count("dislike") == 1
    assert [r["seller"] for r in rated if r["rating"] == "dislike"] == ["West Elm"]
    assert sum(1 for r in rated if r["seller"] == "West Elm") == 4 + 1
    assert [r["price"] for r in rated if r["rating"] == "love"] == [699.0, 699.0, 449.99]
    assert len(data["shown_extras"]) == 41 and len(data["pool"]) == 72  # 48 shown with the rated
    from collections import Counter

    sellers = Counter(p["seller"] for p in data["pool"])
    assert sellers["Lowe's"] == 12 and sellers["Wayfair"] == 10 and sellers["Home Depot"] == 3
    assert sum(n for s, n in sellers.items() if s.startswith("Walmart")) == 20
    assert not [s for s in sellers if tier_of(s) == "design_premium"]  # no premium peers at all


def test_replay_three_decisions(tmp_path: Path) -> None:
    svc, sid, _ = replay3(tmp_path)
    view = svc.shop_again(sid)
    chips = {(e["attr_kind"], e["text"]): e for e in view["profile"]["inferred"]}
    trace = view["profile"]["trace"]
    # West Elm: 4 items and 8 of 12 like mass despite one dislike (8 against 1: the net rule)
    seller = chips[("seller", "seller: West Elm")]
    assert seller["detail"] == "liked 4 of 5 (80%) against 35% overall"
    west = next(c for c in trace["seller"]["candidates"] if c["seller"] == "West Elm")
    assert west["seller"] == "West Elm" and west["rated"] == 5 and west["liked"] == 4
    assert west["like_rate"] == 0.8 and west["verdict"] == "chip"
    tier = chips[("tier", "retailers like West Elm")]
    assert tier["values"] == ["design_premium"] and "liked: West Elm" in tier["detail"]
    premium = next(c for c in trace["tier"]["candidates"] if c["tier"] == "design_premium")
    assert premium["like_rate"] == 0.8
    # style is one family, converged, with the leaves in the trace only
    style = trace["dimensions"]["style"]
    assert style["decision"] == "converged" and style["top"] == "modern_leaning"
    assert style["like_mass"]["modern_leaning"] == 12 and set(style["leaves"]) == {
        "industrial",
        "glam",
        "mid-century",
        "modern",
    }
    assert ("style", "modern-leaning style") in chips
    # shape is open (rectangular 67%, round 33%), price mixed, nothing guessed
    assert trace["dimensions"]["shape"]["decision"] == "open" and ("open", "shape: open") in chips
    assert ("price_mixed", "price: mixed") in chips
    for dim in ("material", "frame_color"):
        assert (
            trace["dimensions"][dim]["decision"] == "none"
            and "under 60%" in trace["dimensions"][dim]["reason"]
        )
    assert "pieces from West Elm and similar retailers" in view["profile_summary"]
    tagging = view["search"]["tagging"]
    assert tagging["candidates_tagged"] >= 60  # the top 60 by rank are tagged by the model
    assert (
        tagging["candidates_tagged"] + tagging["candidates_keyword"] == tagging["candidates_total"]
    )
    assert tagging["candidates_total"] > 70
    assert (
        view["search"]["tagging"]["shown_tagged"] == view["search"]["tagging"]["shown_total"] == 48
    )


def test_replay_three_searches_peers_supplies_round_and_fills_the_batch_within_the_caps(
    tmp_path: Path,
) -> None:
    svc, sid, search = replay3(tmp_path)
    view = svc.shop_again(sid)
    found = view["search"]
    batch = found["trace"]["batch"]
    assert [p["peer"] for p in batch["peers"]] == ["West Elm", "Article", "CB2"]
    assert all(p["new_candidates"] > 0 for p in batch["peers"] if p["peer"] != "West Elm")
    assert found["serper_calls"] <= 9 and found["serper_credits"] == 2 * found["serper_calls"]
    assert found["serper_calls"] == batch["serper_calls"]
    assert batch["pool_before"]["size"] < batch["pool_after"]["size"] or not batch["supply"]
    shapes = batch["batch"]["shape"]
    assert shapes["rectangular"] >= 6 and shapes["round"] >= 6 and max(shapes.values()) <= 14
    tiers = batch["batch"]["tier"]
    assert 1 <= tiers["design_premium"] <= 12  # peer retailers are in, within the 50% cap
    sellers = batch["batch"]["seller"]
    assert (
        max(sellers.values()) <= 12
        and sum(1 for s in sellers if tier_of(s) == "design_premium") >= 3
    )
    peers_in = {r["seller"] for r in found["results"]} & {"West Elm", "Article", "CB2"}
    assert len(peers_in) == 3
    assert batch["tier"] == {"tier": "design_premium", "cap": 12, "quota": 12, "pinned": False}
    queries = [q for q, _ in search.calls]
    assert "Article wood and metal coffee table with storage" in queries


def test_replay_three_pinning_the_tier_chip_raises_its_cap(tmp_path: Path) -> None:
    svc, sid, _ = replay3(tmp_path)
    svc.shop_again(sid)
    pinned = svc.pin_preference(sid, "retailers like West Elm")
    chip = next(e for e in pinned["profile"]["inferred"] if e["attr_kind"] == "tier")
    assert chip["pinned"] is True and pinned["profile"]["pinned_sellers"] == ["tier:design_premium"]
    again = svc.shop_again(sid)
    assert again["search"]["trace"]["batch"]["tier"]["cap"] == 19
    assert again["search"]["trace"]["batch"]["tier"]["pinned"] is True


def test_replays_one_and_two_keep_their_decisions_with_the_new_rules(tmp_path: Path) -> None:
    cfg = config()
    from app.shop.providers import build_shop_providers
    from app.shop.replay import REPLAY_FILE_2

    search, lookup = build_shop_providers(cfg)
    store = SessionStore(tmp_path / "shop-data")
    svc = ShopService(cfg, store, search, lookup)
    one = svc.shop_again(seed_session(store).session_id)
    kinds1 = {(e["attr_kind"], e["text"]) for e in one["profile"]["inferred"]}
    # all ten rated items were liked: the baseline is 100%, no seller or tier can beat it (2i)
    assert not [k for k in kinds1 if k[0] in ("seller", "tier")]
    assert ("open", "shape: open") in kinds1
    two = svc.shop_again(seed_session(store, replay=REPLAY_FILE_2).session_id)
    kinds2 = {(e["attr_kind"], e["text"]) for e in two["profile"]["inferred"]}
    assert not [k for k in kinds2 if k[0] in ("seller", "tier")]  # Home Depot and the budget tier
    assert ("color_finish", "warm wood") in kinds2 and ("open", "shape: open") in kinds2
    assert "Home Depot" not in two["profile_summary"]


# ---- unchanged behaviour ------------------------------------------------------------------------------------------------


def test_prompts_are_versioned_and_the_old_ones_kept() -> None:
    from app.prompts import load_prompt

    assert load_prompt("shop_prefs").version == "6"
    for v in ("1", "2", "3", "4", "5"):
        assert load_prompt("shop_prefs", v).version == v
    assert load_prompt("shop_candidate_tags").version == "5"
    assert load_prompt("shop_candidate_tags", "4").version == "4"
    assert load_prompt("shop_candidate_tags", "3").version == "3"
    assert load_prompt("shop_candidate_tags", "2").version == "2"
    assert load_prompt("shop_candidate_tags", "1").version == "1"
    assert "LIST of up to" in load_prompt("shop_prefs").text
    assert "modern+glam" in load_prompt("shop_candidate_tags", "2").text
    assert "(the style the item itself shows" in load_prompt("shop_prefs", "5").text


def test_the_pin_endpoint_accepts_the_tier_chip_and_the_csp_is_unchanged(tmp_path: Path) -> None:
    from app.shop.server import CSP, ShopApp

    svc, sid, _ = replay3(tmp_path)
    svc.shop_again(sid)
    app = ShopApp(svc, port=8765)
    hdrs = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}
    body = json.dumps({"session_id": sid, "phrase": "retailers like West Elm"}).encode()
    ok = app.handle("POST", "/api/preference/pin", hdrs, body)
    assert ok.status == 200 and ok.all_headers()["Content-Security-Policy"] == CSP
    long = json.dumps({"session_id": sid, "phrase": "x" * 200}).encode()
    assert app.handle("POST", "/api/preference/pin", hdrs, long).status == 400
    evil = app.handle("POST", "/api/preference/pin", {"Host": "evil.example"}, body)
    assert evil.status == 403


def test_the_trace_panel_renders_peers_tiers_and_tag_calls_from_text() -> None:
    from app.shop.server import STATIC_DIR

    code = (STATIC_DIR / "app.js").read_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in code
    block = code[code.index("function batchBlock") : code.index("function removePref")]
    for needle in ("Peer retailer", "Affinity tier", "Tagging call", "retailer tier"):
        assert needle in block or needle in code, needle
    assert "e.attr_kind === 'tier'" in code  # a tier chip can be pinned like a seller chip


def test_every_dimension_still_has_its_trace_and_old_decisions_load() -> None:
    assert DIMENSIONS == ("shape", "material", "color_finish", "frame_color", "style", "color")
    r = Rated("a", "love", "", "Table", 1.0, "West Elm", "")
    assert r.seller == "West Elm"
    assert ShopQuery(request="x", search_text="x", category="").style_terms == []
    old = StoredProfile.model_validate(
        {"inferred": [{"text": "x", "attr_kind": "seller", "polarity": "like"}]}
    )
    assert old.inferred[0].detail == "" and old.pinned_sellers == []
    assert re.fullmatch(r"[a-z0-9]+", seller_key("Crate & Barrel!!"))
