"""Shop Mode stage 2k: request understanding (product words against setting words), the colour
dimension, colour suggestions as soft guidance, and the first search that uses them.

Offline like the other Shop Mode tests (same loopback-only socket guard).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.shop.candidates import TAG_TOOL_DEF, TAG_TOOL_PHOTO_DEF, parse_tool_input, tag_max_tokens
from app.shop.decide import decide
from app.shop.models import ItemTags, ParseContext, ShopParse
from app.shop.parse import build_query
from app.shop.queries import QueryInput, user_text
from app.shop.search import build_results, has_setting, rank_user_text, rank_version, setting_lines
from app.shop.service import ApiError
from app.shop.textutil import Rated
from app.shop.vocab import COLOR_LABELS, DIMENSIONS, VOCAB, normalize, title_value
from app.shopping import SearchCandidate
from test_shop import (  # noqa: F401  (the autouse guard must be imported to apply here)
    FakeLookup,
    FakeSearch,
    RouterClient,
    _loopback_only,
    cand,
    config,
    make_service,
    query,
    use_client,
)
from test_shop_learn import mock_service

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "app" / "shop" / "static"
OWNER = "boho curtains that pair well with a dark blue room"


def parsed(**kw: Any) -> ShopParse:
    base: dict[str, Any] = {
        "search_text": "boho curtains", "category": "curtains", "price_min": None,
        "price_max": None, "style_terms": ["boho"], "material_terms": [], "must_have": [],
        "avoid": [],
    }  # fmt: skip
    return ShopParse(**{**base, **kw})


# ---- the owner's request ------------------------------------------------------------------------


def test_the_owners_request_is_split_into_product_and_setting(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    q = svc.search(None, OWNER)["search"]["query"]
    assert q["search_text"] == "boho curtains" and q["category"] == "curtains"
    assert q["style_terms"] == ["boho"] and q["color_terms"] == []
    assert q["context"] == [{"kind": "room_color", "value": "dark blue", "approximate": False}]
    assert q["suggested_colors"] == ["cream", "rust_terracotta", "yellow_mustard", "white"]
    assert q["avoid_colors"] == ["navy_dark_blue", "blue"] and q["color_reason"]
    assert "dark blue" not in q["search_text"]


def test_a_colour_that_describes_the_product_stays_a_product_attribute(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    q = svc.search(None, "dark blue curtains")["search"]["query"]
    assert q["search_text"] == "dark blue curtains" and q["color_terms"] == ["navy_dark_blue"]
    assert q["context"] == [] and q["suggested_colors"] == [] and q["avoid_colors"] == []
    sofa = svc.search(None, "navy sofa under $1500")["search"]["query"]
    assert sofa["search_text"] == "navy sofa" and sofa["price_max"] == 1500.0
    assert sofa["color_terms"] == ["navy_dark_blue"] and sofa["suggested_colors"] == []


@pytest.mark.parametrize(
    ("request_text", "kinds", "suggested"),
    [
        ("curtains for a navy bedroom", ["room_color", "room_type"], True),
        ("lamp for a brown leather sofa", ["existing_item"], True),
        ("coffee table for a small apartment", ["room_size"], False),
    ],
)
def test_the_other_context_requests(
    tmp_path: Path, request_text: str, kinds: list[str], suggested: bool
) -> None:
    svc = mock_service(tmp_path)
    q = svc.search(None, request_text)["search"]["query"]
    assert [c["kind"] for c in q["context"]] == kinds
    assert bool(q["suggested_colors"]) is suggested and q["color_terms"] == []
    assert q["search_text"] == q["category"]  # nothing of the setting is searched


def test_a_room_size_becomes_a_soft_compact_preference_for_the_ranker() -> None:
    q = build_query(
        "coffee table for a small apartment",
        parsed(
            search_text="coffee table", category="coffee table", style_terms=[],
            context=[ParseContext(kind="room_size", value="small apartment")],
        ),
    )  # fmt: skip
    lines = setting_lines(q)
    assert "Size preference: compact (small apartment)" in lines and has_setting(q)


# ---- validation in code ---------------------------------------------------------------------------


def test_the_search_text_never_keeps_the_rooms_colour() -> None:
    q = build_query(
        OWNER,
        parsed(
            search_text="boho curtains dark blue",
            context=[ParseContext(kind="room_color", value="dark blue")],
            suggested_colors=["cream"], avoid_colors=["navy"], color_reason="x",
        ),
    )  # fmt: skip
    assert q.search_text == "boho curtains"


def test_colours_are_checked_against_the_fixed_vocabulary_and_bounded() -> None:
    q = build_query(
        OWNER,
        parsed(
            context=[ParseContext(kind="room_color", value="dark blue")],
            suggested_colors=["Cream", "rust", "mustard", "white", "green", "pink", "chartreuse"],
            avoid_colors=["navy", "cream", "blue", "black", "gray"],
            color_reason="  because " * 40,
        ),
    )  # fmt: skip
    assert q.suggested_colors == [
        "rust_terracotta",
        "yellow_mustard",
        "white",
        "green",
    ]  # no overlap
    assert q.avoid_colors == ["navy_dark_blue", "cream", "blue"]
    assert len(q.color_reason) <= 140
    assert any(i.kind == "other" and i.approximate and i.value == "chartreuse" for i in q.context)
    assert "chartreuse" not in q.suggested_colors


def test_an_unknown_context_kind_is_approximate_free_text() -> None:
    q = build_query(
        "x",
        parsed(context=[ParseContext.model_construct(kind="vibe", value="Sunny")]),  # type: ignore[arg-type]
    )
    assert (
        q.context[0].kind == "other" and q.context[0].approximate and q.context[0].value == "sunny"
    )


def test_suggestions_need_a_colour_in_the_setting_and_no_product_colour() -> None:
    no_color = build_query(
        "x",
        parsed(
            context=[ParseContext(kind="room_size", value="small")], suggested_colors=["cream"],
            avoid_colors=["navy"],
        ),
    )  # fmt: skip
    assert no_color.suggested_colors == [] and no_color.avoid_colors == []
    typed = build_query(
        "x",
        parsed(
            color_terms=["dark blue"], context=[ParseContext(kind="room_color", value="beige")],
            suggested_colors=["cream"],
        ),
    )  # fmt: skip
    assert typed.color_terms == ["navy_dark_blue"] and typed.suggested_colors == []


# ---- the colour dimension --------------------------------------------------------------------------


def test_the_colour_vocabulary_is_small_and_unambiguous() -> None:
    assert "color" in DIMENSIONS and DIMENSIONS[-1] == "color"
    assert VOCAB["color"][:-1] == (
        "white", "cream", "beige", "tan", "brown", "black", "gray", "blue", "navy_dark_blue",
        "green", "red", "rust_terracotta", "yellow_mustard", "pink", "purple", "orange",
        "metallic", "multicolor_pattern",
    )  # fmt: skip
    assert normalize("color", "Navy blue") == "navy_dark_blue"
    assert normalize("color", "dark blue") == "navy_dark_blue"
    assert normalize("color", "light blue") == "blue" and normalize("color", "ivory") == "cream"
    assert normalize("color", "terracotta") == "rust_terracotta"
    assert normalize("color", "zebra") == "other" and normalize("color", "") is None
    assert title_value("color", "Navy Velvet Curtains 84 inch") == "navy_dark_blue"
    assert title_value("color", "Boho Curtains") is None  # no colour in the title: unknown
    assert COLOR_LABELS["navy_dark_blue"] == "navy"


def test_the_tagging_tool_has_a_colour_field_and_the_old_prompt_is_kept() -> None:
    for tool in (TAG_TOOL_DEF, TAG_TOOL_PHOTO_DEF):
        item = tool["input_schema"]["properties"]["items"]["items"]
        assert "color" in item["properties"] and "color" in item["required"]
        assert tool["strict"] is True
    from app.prompts import load_prompt

    assert load_prompt("shop_candidate_tags").version == "5"
    assert "dominant colour" in load_prompt("shop_candidate_tags").text
    assert load_prompt("shop_candidate_tags", "4").version == "4"
    assert (
        load_prompt("shop_parse").version == "2" and load_prompt("shop_parse", "1").version == "1"
    )
    assert (
        load_prompt("shop_queries").version == "3"
        and load_prompt("shop_queries", "1").version == "1"
        and load_prompt("shop_queries", "2").version == "2"
    )
    # 8 more tokens an item: a batch of 40 gets 2 * (100 + 48 * 40)
    assert tag_max_tokens(40, "tool") == 4040
    got = parse_tool_input({"items": [{"index": 1, "color": "Navy", "shape": None}]}, 1)
    assert got[1]["color"] == "navy_dark_blue"


def liked_colors(
    n_cream: int, n_navy: int, n_other: int
) -> tuple[dict[str, Rated], dict[str, ItemTags], dict[str, dict[str, str | None]]]:
    """Rated curtains: cream ones liked, navy ones disliked, a few of others mixed."""
    rated: dict[str, Rated] = {}
    cache: dict[str, dict[str, str | None]] = {}
    tags: dict[str, ItemTags] = {}
    spec = [("like", "cream")] * n_cream + [("dislike", "navy_dark_blue")] * n_navy
    spec += [("dislike", "green")] * (n_other // 2) + [("like", "green")] * (n_other - n_other // 2)
    for i, (rating, color) in enumerate(spec):
        rid = f"c{i}"
        rated[rid] = Rated(rid, rating, "", f"Curtains {i}", 50.0, f"S{i % 5}", "")
        cache[rid] = {"color": color}
        tags[rid] = ItemTags(
            id=rid, shape=None, material=None, color_finish=None, frame_color=None, style=[],
            quality_cues=[],
        )  # fmt: skip
    return rated, tags, cache


def test_colour_is_a_soft_dimension_in_the_evidence() -> None:
    rated, tags, cache = liked_colors(6, 8, 6)  # 9 of 20 liked (45%): cream 6 of 6, navy 0 of 8
    out = decide(rated, tags, [], [], [], cached_tags=cache)
    leans = {e.text: e.attr_kind for e in out.entries if e.status == "leaning"}
    assert leans["leaning toward cream (color)"] == "leaning_toward"
    assert leans["leaning away from navy (color)"] == "leaning_away"
    assert out.trace["dimensions"]["color"]["like_mass"]["cream"] == 6
    rows = {(r["kind"], r["name"]): r for r in out.trace["evidence"]["rows"]}
    assert rows[("color", "cream")]["verdict"] in ("leaning toward", "converged")
    # a colour the request typed is never learned back as a chip
    typed = decide(rated, tags, [], [], ["cream"], cached_tags=cache)
    assert "leaning toward cream (color)" not in {e.text for e in typed.entries}


# ---- ranking and selection ---------------------------------------------------------------------------


def test_the_ranker_gets_the_setting_as_soft_guidance_and_the_right_prompt() -> None:
    q = build_query(
        OWNER,
        parsed(
            context=[ParseContext(kind="room_color", value="dark blue")],
            suggested_colors=["cream", "rust"], avoid_colors=["navy"], color_reason="r",
        ),
    )  # fmt: skip
    text = rank_user_text(q, [cand("a")], 24)
    assert (
        "Setting (where the product goes; NOT what it is, never require it): room color: dark blue"
        in text
    )
    assert "Suggested product colors (prefer): cream, rust" in text
    assert "Colors to avoid (demote; the product would blend in or clash): navy" in text
    assert rank_version(q, None) == "6" and rank_version(query(), None) == "1"
    plain = rank_user_text(query(), [cand("a")], 24)
    assert "Setting" not in plain and "Suggested" not in plain
    from app.prompts import load_prompt

    assert (
        "never require" in load_prompt("shop_rank").text.lower()
        or "Never require" in load_prompt("shop_rank").text
    )


def color_pool(n: int = 60) -> list[tuple[SearchCandidate, str]]:
    names = ["Navy", "Navy", "Cream", "Gray", "Rust", "Blue", "Black"]
    return [
        (
            cand(
                f"k{i}",
                40.0 + i,
                title=f"{names[i % len(names)]} Curtains {i}",
                seller=f"S{i % 20}",
            ),
            "",
        )
        for i in range(n)
    ]


def test_a_batch_takes_at_least_40_percent_suggested_and_at_most_15_percent_avoid() -> None:
    ordered = color_pool()  # in rank order the navy and blue ones come first
    index = {c.product_id: i + 1 for i, (c, _) in enumerate(ordered)}
    q = query(category="curtains")
    plain, _, _ = build_results(ordered, q, index)
    assert sum(1 for r in plain if "Navy" in r.title or "Blue" in r.title) >= 8
    out, _, _ = build_results(
        ordered, q, index, colors=(["cream", "rust_terracotta"], ["navy_dark_blue", "blue"])
    )
    suggested = sum(1 for r in out if r.title.startswith(("Cream", "Rust")))
    avoid = sum(1 for r in out if r.title.startswith(("Navy", "Blue")))
    assert len(out) == 24 and suggested >= 10 and avoid <= 3


def test_the_avoid_cap_still_holds_when_the_pool_has_no_suggested_colours() -> None:
    ordered = [(c, r) for c, r in color_pool() if c.title.startswith(("Navy", "Gray"))]
    index = {c.product_id: i + 1 for i, (c, _) in enumerate(ordered)}
    out, _, _ = build_results(ordered, query(), index, colors=(["cream"], ["navy_dark_blue"]))
    navy = sum(1 for r in out if r.title.startswith("Navy"))
    assert navy == 3 and len(out) == 3 + sum(1 for r in out if r.title.startswith("Gray"))
    # a result is never re-added past the cap: only the gray ones fill the rest


# ---- the first search ----------------------------------------------------------------------------------


def test_the_first_search_runs_the_plain_and_the_coloured_query_instead_of_page_two(
    tmp_path: Path,
) -> None:
    from app.shop.providers import ShopFixtureSearch

    cfg = config()
    search = ShopFixtureSearch()
    svc = make_service(tmp_path, search, FakeLookup(), cfg)
    view = svc.search(None, OWNER)
    found = view["search"]
    assert sorted(search.calls) == [("boho curtains", 1), ("boho curtains cream rust", 1)]
    assert found["serper_calls"] == 2 and found["serper_credits"] == 4  # the same credits as before
    assert found["queries"] == ["boho curtains", "boho curtains cream rust"]
    trace = found["trace"]["request"]
    assert trace["second_query"] == "boho curtains cream rust" and trace["results"] == len(
        found["results"]
    )
    assert trace["from_suggested"] >= 10 and trace["from_avoid"] <= 3
    assert trace["suggested"] == ["cream", "rust", "mustard", "white"]


def test_page_two_is_only_the_fallback_when_the_pool_is_thin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_client(monkeypatch, RouterClient(parse=None))
    thin = [cand(f"t{i}", 40.0 + i, title=f"Cream Curtain T{i}", seller=f"S{i}") for i in range(5)]
    more = [cand(f"m{i}", 60.0 + i, title=f"Rust Curtain M{i}", seller=f"R{i}") for i in range(5)]
    search = FakeSearch([thin, more])
    search.sequence = [
        thin,
        more,
        [cand(f"p{i}", 80.0 + i, title=f"Gray Curtain P{i}", seller=f"Q{i}") for i in range(30)],
    ]
    svc = make_service(tmp_path, search, FakeLookup(), config())
    view = svc.search(None, OWNER)
    assert [p for _, p in search.calls] == [1, 1, 2]  # both first pages, then page two
    assert view["search"]["serper_calls"] == 3


def test_a_request_without_colour_suggestions_still_uses_one_or_two_pages(tmp_path: Path) -> None:
    from app.shop.providers import ShopFixtureSearch

    search = ShopFixtureSearch()
    svc = make_service(tmp_path, search, FakeLookup(), config())
    svc.search(None, "boho curtains")
    assert all(text == "boho curtains" for text, _ in search.calls) and len(search.calls) <= 2


# ---- deleting chips ---------------------------------------------------------------------------------------


def test_each_chip_can_be_deleted_and_the_effect_goes_with_it(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    sid = svc.search(None, OWNER)["session_id"]
    gone = svc.remove_context(sid, "suggested", "rust")
    assert gone["query"]["suggested_colors"] == ["cream", "yellow_mustard", "white"]
    gone = svc.remove_context(sid, "avoid", "navy")
    assert gone["query"]["avoid_colors"] == ["blue"]
    stored = svc.session_view(sid)["search"]["query"]
    assert stored["suggested_colors"] == ["cream", "yellow_mustard", "white"]  # the view agrees
    whole = svc.remove_context(sid, "context", "dark blue")
    assert whole["query"]["context"] == [] and whole["query"]["suggested_colors"] == []
    assert whole["query"]["avoid_colors"] == [] and whole["query"]["color_reason"] == ""
    for bad in (("nope", "x"), ("suggested", ""), ("avoid", "x" * 41), ("suggested", "cream")):
        with pytest.raises(ApiError) as err:
            svc.remove_context(sid, *bad)
        assert err.value.status in (400, 404)


def test_a_deleted_suggestion_stays_out_of_the_next_shop_again(tmp_path: Path) -> None:
    from app.shop.again import base_query

    svc = mock_service(tmp_path)
    view = svc.search(None, OWNER)
    sid, first = view["session_id"], view["search"]["results"]
    svc.rate(sid, first[0]["id"], "love")
    svc.remove_context(sid, "suggested", "cream")
    base = base_query(svc.store.load(sid))
    assert base is not None and "cream" not in base.suggested_colors
    again = svc.shop_again(sid)
    assert "cream" not in again["search"]["query"]["suggested_colors"]


def test_the_setting_belongs_to_the_topic_and_clears_with_a_new_one(tmp_path: Path) -> None:
    svc = mock_service(tmp_path)
    sid = svc.search(None, OWNER)["session_id"]
    couch = svc.search(sid, "modern couches under $1000")["search"]["query"]
    assert couch["context"] == [] and couch["suggested_colors"] == []


def test_the_query_writer_is_told_the_setting_and_the_suggested_colours() -> None:
    q = build_query(
        OWNER,
        parsed(
            context=[ParseContext(kind="room_color", value="dark blue")],
            suggested_colors=["cream"], avoid_colors=["navy"],
        ),
    )  # fmt: skip
    text = user_text(QueryInput(base=q, adjusted=q, setting=setting_lines(q)))
    assert "room color: dark blue" in text and "Suggested product colors (prefer): cream" in text


# ---- the page --------------------------------------------------------------------------------------------------


def test_the_page_draws_the_setting_chips_and_the_trace_with_text_nodes_only() -> None:
    js = (STATIC / "app.js").read_text()
    html = (STATIC / "index.html").read_text()
    css = (STATIC / "style.css").read_text()
    assert "/api/context/remove" in js and "ctxChip" in js and "renderRequestTrace" in js
    assert "'Suggested'" in js and "'Avoid color'" in js and "approximate" in js
    assert 'id="request-trace"' in html and ".chip.ctx" in css
    assert "innerHTML" not in js
    from app.shop.server import API_PATHS

    assert "/api/context/remove" in API_PATHS
