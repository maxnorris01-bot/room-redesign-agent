"""Stage 2r, phase 4: a diversity guard is off when the pool offers no alternative value (so it
cannot shrink a batch), and a typed shape ("round black metal side tables") is a 70% quota like a
typed colour, material or finish."""

from __future__ import annotations

import pytest

from app.config import Config
from app.shop import typed as ty
from app.shop.models import ShopQuery
from app.shop.parse import build_query
from app.shop.providers import build_shop_providers
from app.shop.search import build_results, effective_guards
from app.shop.service import ApiError, ShopService
from app.shop.store import SessionStore
from app.shop.thresholds import GUARD_ALT_SUPPLY_MIN, RESULT_COUNT
from app.shop.vocab import title_tags, typed_shape_words, typed_values
from app.shopping import SearchCandidate
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)


def cands(shapes: list[str], sellers: int = 13) -> list[tuple[SearchCandidate, str]]:
    return [
        (
            SearchCandidate(f"p{i}", f"{shape.title()} Rug {i}", f"Seller {i % sellers}",
                            100.0 + i, "USD", None, None, "https://www.google.com/search?q=x"),
            "",
        )
        for i, shape in enumerate(shapes)
    ]  # fmt: skip


def tags(c: SearchCandidate) -> dict[str, str | None]:
    return dict(title_tags(c.title))


QUERY = ShopQuery(request="8x10 rug", search_text="8x10 rug", category="rug")


def walk(ordered: list[tuple[SearchCandidate, str]], caps: dict[str, float]) -> int:
    index = {c.product_id: i + 1 for i, (c, _) in enumerate(ordered)}
    results, _, _ = build_results(ordered, QUERY, index, guard_caps=caps, tags=tags)
    return len(results)


# ---- guards ----------------------------------------


def test_a_pool_with_no_other_shape_turns_the_guard_off_and_fills_the_batch() -> None:
    ordered = cands(["rectangular"] * 44)  # the post-2p case-06 pool: 100% rectangular
    assert walk(ordered, {"shape": 0.6}) == 14  # the guard as it was: 14 of 24
    caps, off = effective_guards(ordered, {"shape": 0.6}, tags)
    assert caps == {} and [g["dimension"] for g in off] == ["shape"]
    assert off[0]["top"] == "rectangular" and off[0]["alternatives"] == 0 and off[0]["cap"] == 14
    assert (
        "Not mixing shape: only 0 of the 44 new candidates are not rectangular" in off[0]["message"]
    )
    assert walk(ordered, caps) == RESULT_COUNT


def test_with_alternatives_the_guard_stays_fully_active() -> None:
    ordered = cands(["rectangular"] * 34 + ["round"] * GUARD_ALT_SUPPLY_MIN)
    caps, off = effective_guards(ordered, {"shape": 0.6}, tags)
    assert caps == {"shape": 0.6} and off == []
    assert walk(ordered, caps) == 20  # fully active: 14 rectangular and the 6 round ones
    mixed = cands(["rectangular", "round"] * 20)
    assert effective_guards(mixed, {"shape": 0.6}, tags)[1] == []


def test_the_boundary_is_six_alternatives() -> None:
    five = cands(["rectangular"] * 35 + ["round"] * (GUARD_ALT_SUPPLY_MIN - 1))
    assert [g["dimension"] for g in effective_guards(five, {"shape": 0.6}, tags)[1]] == ["shape"]
    six = cands(["rectangular"] * 35 + ["round"] * GUARD_ALT_SUPPLY_MIN)
    assert effective_guards(six, {"shape": 0.6}, tags)[1] == []


def test_a_guard_that_would_not_bite_is_left_alone() -> None:
    small = cands(["rectangular"] * 12)  # 12 fit under the cap of 14: nothing to switch off
    assert effective_guards(small, {"shape": 0.6}, tags) == ({"shape": 0.6}, [])
    assert effective_guards([], {"shape": 0.6}, tags) == ({"shape": 0.6}, [])  # nothing known


def test_each_dimension_is_judged_on_its_own() -> None:
    pool = cands(["rectangular"] * 40)  # shape: one value; finish: nothing tagged
    caps, off = effective_guards(pool, {"shape": 0.6, "color_finish": 0.6}, tags)
    assert "color_finish" in caps and "shape" not in caps and len(off) == 1


# ---- a typed shape ----------------------------------------


def test_the_typed_shape_words_and_their_match_rules() -> None:
    assert typed_shape_words("round black metal side tables") == ["round"]
    assert typed_shape_words("round or oval coffee tables") == ["round", "oval"]
    assert typed_shape_words("rectangle desk, circular rug") == ["rectangular", "round"]
    assert typed_shape_words("brass drum floor lamps") == []  # a tagging synonym, not a typed shape
    assert typed_values("shape", "round") == frozenset({"round"})  # round accepts round only
    assert typed_values("shape", "oval") == frozenset({"oval"})
    assert typed_values("shape", "square") != typed_values("shape", "rectangular")


def test_the_parser_reads_the_typed_shape_into_the_query() -> None:
    from app.shop.models import ShopParse

    parsed = ShopParse(
        search_text="round black metal side table", category="side tables", price_min=None,
        price_max=None, style_terms=[], material_terms=["metal"], must_have=[], avoid=[],
        color_terms=["black"], context=[], suggested_colors=[], avoid_colors=[], color_reason="",
    )  # fmt: skip
    query = build_query("round black metal side tables", parsed)
    assert query.shape_terms == ["round"]
    labels = [a.label for a in ty.typed_attributes(query)]
    assert labels == ["color black", "material metal", "shape round"]


def test_a_typed_shape_is_a_seventy_percent_quota_with_the_same_banner() -> None:
    query = ShopQuery(request="round black metal side tables", search_text="side table",
                      category="side table", shape_terms=["round"])  # fmt: skip
    attrs = ty.typed_attributes(query)
    assert [a.dim for a in attrs] == ["shape"] and attrs[0].values == frozenset({"round"})
    # 40 tables: 30 not round and 10 round; the batch should hold 17 round ones
    ordered = cands(["rectangular"] * 30 + ["round"] * 10, sellers=20)
    index = {c.product_id: i + 1 for i, (c, _) in enumerate(ordered)}
    results, _, _ = build_results(ordered, QUERY, index, tags=tags, typed=attrs)
    batch = [tags(next(c for c, _ in ordered if c.product_id == r.product_id)) for r in results]
    assert sum(1 for t in batch if t["shape"] == "round") == 10  # all the supply that exists
    short = ty.shortfalls(attrs, batch, [tags(c) for c, _ in ordered])
    assert short[0]["attribute"] == "shape round" and short[0]["wanted"] == ty.target(len(batch))
    assert ty.banner(short[0]).startswith(
        "Only 10 of 24 results match your shape round: the pool had 10"
    )
    full = cands(["round"] * 40, sellers=20)
    index = {c.product_id: i + 1 for i, (c, _) in enumerate(full)}
    got, _, _ = build_results(full, QUERY, index, tags=tags, typed=attrs)
    assert len(got) == 24 and ty.shortfalls(attrs, [tags(c) for c, _ in full[:24]], []) == []


def test_oval_is_not_round() -> None:
    attrs = ty.typed_attributes(ShopQuery(request="oval tables", search_text="t", category="table",
                                          shape_terms=["oval"]))  # fmt: skip
    assert not ty.matches({"shape": "round"}, attrs[0]) and ty.matches({"shape": "oval"}, attrs[0])
    assert not ty.matches(None, attrs[0]) and not ty.matches({"shape": "other"}, attrs[0])


def test_deleting_the_shape_chip_removes_the_quota(tmp_path: pytest.TempPathFactory) -> None:
    cfg = Config(tracing_disabled=True)
    search, lookup = build_shop_providers(cfg)
    svc = ShopService(cfg, SessionStore(tmp_path / "d"), search, lookup)  # type: ignore[operator]
    view = svc.search(None, "round black metal side tables")
    sid = view["session_id"]
    assert view["search"]["query"]["shape_terms"] == ["round"]
    out = svc.remove_context(sid, "shape_term", "round")
    assert out["query"]["shape_terms"] == [] and out["removed"] == "round"
    assert svc.session_view(sid)["search"]["query"]["shape_terms"] == []
    with pytest.raises(ApiError):
        svc.remove_context(sid, "shape_term", "round")
    base = ShopQuery.model_validate(out["query"])
    assert [a.dim for a in ty.typed_attributes(base)] == ["color", "material"]
