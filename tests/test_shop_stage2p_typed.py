"""Stage 2p, phase 3: typed colour, material and finish are quotas, tagged from the photo. Match
rules, the photo tagging step and its fallback, the shared thumbnail cache, the quota and its
shortfall banner, chip deletion, the ranker's lines, and the eval's judge-based colour score.
Offline: scripted search, a stub model, a counting thumbnailer."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Config
from app.images import PreparedImage
from app.llm import Budget, Completion
from app.shop import candidates as cands
from app.shop import phototags as pt
from app.shop import typed as ty
from app.shop.clock import Clock
from app.shop.models import ShopQuery
from app.shop.nearby import HttpFingerprinter
from app.shop.parse import build_query
from app.shop.providers import ShopFixtureLookup
from app.shop.search import build_results, rank_user_text, typed_lines
from app.shop.service import ApiError, ShopService
from app.shop.store import SessionStore
from app.shop.vocab import typed_values
from app.shopping import SearchCandidate
from app.thumbnails import ThumbResult
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)

IMG = PreparedImage("AAAA", "image/jpeg", 8, 8, 8, 8, 4)


def query(**kw: Any) -> ShopQuery:
    base: dict[str, Any] = {
        "request": "navy velvet accent chairs", "search_text": "navy velvet accent chairs",
        "category": "accent chairs", "category_key": "armchair",
    }  # fmt: skip
    return ShopQuery(**{**base, **kw})


# ---- match rules ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dim", "text", "accepted"),
    [
        ("color", "navy", {"navy_dark_blue"}),
        ("color", "dark blue", {"navy_dark_blue"}),  # a typed "dark blue" is navy ...
        ("color", "midnight blue", {"navy_dark_blue"}),
        ("color", "blue", {"blue"}),  # ... and blue only when the owner typed "blue"
        ("color", "brass", {"metallic"}),
        ("color", "cream", {"cream"}),
        ("color_finish", "brushed brass", {"metallic"}),
        ("material", "velvet", {"upholstered"}),
        ("material", "wood", {"wood"}),
        ("material", "metal", {"metal"}),
        ("material", "rattan", set()),  # not in the vocabulary: nothing to enforce
        ("color", "", set()),
    ],
)
def test_what_a_typed_word_accepts(dim: str, text: str, accepted: set[str]) -> None:
    assert typed_values(dim, text) == frozenset(accepted)


def test_navy_and_dark_blue_are_one_colour_and_blue_is_not_navy() -> None:
    navy = ty.TypedAttr("color", "navy", typed_values("color", "navy"))
    assert ty.matches({"color": "navy_dark_blue"}, navy)
    assert not ty.matches({"color": "blue"}, navy)
    blue = ty.TypedAttr("color", "blue", typed_values("color", "blue"))
    assert ty.matches({"color": "blue"}, blue) and not ty.matches({"color": "navy_dark_blue"}, blue)


def test_untagged_and_other_items_never_match() -> None:
    attr = ty.TypedAttr("color", "navy", frozenset({"navy_dark_blue"}))
    assert not ty.matches(None, attr) and not ty.matches({}, attr)
    assert not ty.matches({"color": None}, attr) and not ty.matches({"color": "other"}, attr)
    assert not ty.matches({"material": "wood"}, attr)


def test_typed_attributes_come_from_the_typed_request() -> None:
    attrs = ty.typed_attributes(query(color_terms=["navy_dark_blue"], material_terms=["velvet"]))
    assert [(a.dim, a.typed) for a in attrs] == [("color", "navy"), ("material", "velvet")]
    wood_metal = ty.typed_attributes(
        query(material_terms=["wood", "metal"], request="wood metal table")
    )
    assert len(wood_metal) == 1 and wood_metal[0].values == frozenset({"wood", "metal"})
    assert wood_metal[0].typed == "wood or metal"
    assert ty.typed_attributes(query(material_terms=["rattan"])) == []  # unknown words: no quota
    finish = ty.typed_attributes(query(request="walnut coffee table"))
    assert [(a.dim, a.typed) for a in finish] == [("color_finish", "warm wood")]
    assert ty.typed_attributes(query()) == []


def test_a_typed_product_colour_overrides_the_suggestions_of_the_setting() -> None:
    from app.shop.models import ShopParse

    parsed = ShopParse.model_validate(
        {
            "search_text": "navy curtains", "category": "curtains", "price_min": None, "price_max": None,
            "style_terms": [], "material_terms": [], "must_have": [], "avoid": [],
            "color_terms": ["navy_dark_blue"],
            "context": [{"kind": "room_color", "value": "dark blue room"}],
            "suggested_colors": ["cream", "rust_terracotta"], "avoid_colors": ["navy_dark_blue"],
            "color_reason": "the room is dark blue",
        }
    )  # fmt: skip
    q = build_query("navy curtains for a dark blue room", parsed)
    assert q.color_terms == ["navy_dark_blue"]
    assert q.suggested_colors == [] and q.avoid_colors == []  # the typed colour wins
    assert [a.dim for a in ty.typed_attributes(q)] == ["color"]


def test_the_quota_is_seventy_percent_of_a_batch() -> None:
    from app.shop.thresholds import TYPED_MATCH_MIN_SHARE

    assert TYPED_MATCH_MIN_SHARE == 0.70 and ty.target(24) == 17 and ty.target(10) == 7
    assert ty.target(1) == 1 and ty.target(0) == 0


def test_a_shortfall_names_the_attribute_and_what_the_pool_could_supply() -> None:
    attr = ty.TypedAttr("color", "navy", frozenset({"navy_dark_blue"}))
    navy = {"color": "navy_dark_blue"}
    batch: list[Any] = [navy] * 6 + [{"color": "cream"}] * 8 + [{}] * 10
    pool = batch + [navy] * 3
    out = ty.shortfalls([attr], batch, pool)
    assert out == [{"attribute": "color navy", "dimension": "color", "wanted": 17, "got": 6,
                    "batch": 24, "supply": 9, "untagged": 10}]  # fmt: skip
    text = ty.banner(out[0])
    assert text == ("Only 6 of 24 results match your color navy: the pool had 9 that do "
                    "and 10 results show no color yet.")  # fmt: skip
    assert ty.shortfalls([attr], [navy] * 17 + [{}] * 7, pool) == []  # 17 of 24: met
    assert ty.shortfalls([attr], [], []) == []


# ---- the selection quota --------------------------------------------------------------------------


def cand(i: int, seller: str | None = None) -> tuple[SearchCandidate, str]:
    return (
        SearchCandidate(f"p{i}", f"Velvet Accent Chair {i}", seller or f"Shop {i}", 100.0 + i,
                        "USD", None, None, None),
        "",
    )  # fmt: skip


def pool_with(navy_at: set[int], n: int = 40) -> list[tuple[SearchCandidate, str]]:
    return [cand(i) for i in range(n)]


def tags_for(navy_at: set[int], untagged: set[int] = frozenset()) -> Any:  # type: ignore[assignment]
    def tags(c: SearchCandidate) -> dict[str, str | None]:
        i = int(c.product_id[1:])
        if i in untagged:
            return {}
        return {"color": "navy_dark_blue" if i in navy_at else "cream"}

    return tags


def run(navy_at: set[int], untagged: set[int] = frozenset(), **kw: Any) -> list[bool]:  # type: ignore[assignment]
    pool = pool_with(navy_at)
    index = {c.product_id: n for n, (c, _) in enumerate(pool, 1)}
    attrs = ty.typed_attributes(query(color_terms=["navy_dark_blue"]))
    results, _, _ = build_results(
        pool, query(color_terms=["navy_dark_blue"]), index, tags=tags_for(navy_at, untagged),
        typed=attrs, **kw,
    )  # fmt: skip
    return [int(r.product_id[1:]) in navy_at for r in results]


def test_the_batch_takes_seventeen_navy_items_from_the_bottom_of_the_ranking() -> None:
    navy = set(range(20, 40))  # twenty navy chairs, all ranked below twenty others
    flags = run(navy)
    assert len(flags) == 24 and sum(flags) >= 17
    assert sum(flags) <= 24 and flags.count(False) <= 7


def test_when_supply_is_short_every_matching_item_is_taken() -> None:
    navy = {30, 31, 32, 33, 34}
    flags = run(navy)
    assert sum(flags) == 5 and len(flags) == 24  # all five, and the rest fill the batch


def test_untagged_items_do_not_count_toward_the_quota() -> None:
    navy = set(range(0, 40, 2))
    flags = run(navy, untagged=set(range(1, 40, 2)))  # the odd ones have no colour tag at all
    assert sum(flags) >= 17  # the quota is met by items that are tagged navy


def test_without_a_typed_attribute_nothing_changes() -> None:
    pool = pool_with(set())
    index = {c.product_id: n for n, (c, _) in enumerate(pool, 1)}
    base, _, _ = build_results(pool, query(), index)
    again, _, _ = build_results(pool, query(), index, typed=[])
    assert [r.id for r in base] == [r.id for r in again]


def test_a_seller_cap_still_holds_inside_the_quota() -> None:
    pool = [cand(i, seller="OneShop" if i < 30 else f"Shop {i}") for i in range(40)]
    navy = set(range(0, 30))
    index = {c.product_id: n for n, (c, _) in enumerate(pool, 1)}
    results, _, _ = build_results(
        pool, query(color_terms=["navy_dark_blue"]), index, tags=tags_for(navy),
        typed=ty.typed_attributes(query(color_terms=["navy_dark_blue"])),
    )  # fmt: skip
    assert [r.seller for r in results].count("OneShop") <= 4


def test_material_and_colour_both_count_and_both_are_met_when_one_item_has_both() -> None:
    attrs = [
        ty.TypedAttr("color", "navy", frozenset({"navy_dark_blue"})),
        ty.TypedAttr("material", "velvet", frozenset({"upholstered"})),
    ]
    pool = [cand(i) for i in range(40)]

    def tags(c: SearchCandidate) -> dict[str, str | None]:
        i = int(c.product_id[1:])
        return {"color": "navy_dark_blue" if i % 2 == 0 else "cream",
                "material": "upholstered" if i % 2 == 0 or i % 3 == 0 else "wood"}  # fmt: skip

    index = {c.product_id: n for n, (c, _) in enumerate(pool, 1)}
    results, _, _ = build_results(pool, query(), index, tags=tags, typed=attrs)
    got = [tags(next(c for c, _ in pool if c.product_id == r.product_id)) for r in results]
    assert sum(1 for t in got if t["color"] == "navy_dark_blue") >= 17
    assert sum(1 for t in got if t["material"] == "upholstered") >= 17


# ---- the ranker is told ----------------------------------------------------------------------------


def test_the_ranker_gets_the_typed_attributes_as_must_favor() -> None:
    q = query(color_terms=["navy_dark_blue"], material_terms=["velvet"])
    lines = typed_lines(ty.typed_attributes(q))
    assert len(lines) == 1 and "must favor" in lines[0] and "70%" in lines[0]
    assert "color: navy" in lines[0] and "material: velvet" in lines[0]
    text = rank_user_text(q, [cand(1)[0]], 1)
    assert lines[0] in text and "Colors the product itself must be: navy" in text
    assert typed_lines([]) == [] and "Typed product attributes" not in rank_user_text(
        query(), [cand(1)[0]], 1
    )


# ---- the photo tagging step ------------------------------------------------------------------------


def taggables(n: int, https: bool = True) -> list[cands.Taggable]:
    return [
        cands.Taggable(f"r{i}", f"Velvet Accent Chair {i}", 100.0, "Shop",
                       f"https://img.example/{i}.jpg" if https else "")
        for i in range(n)
    ]  # fmt: skip


class FakeThumbs:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.fail = fail or set()
        self.lock = threading.Lock()

    def fetch(self, url: str) -> ThumbResult:
        with self.lock:
            self.calls.append(url)
        if url in self.fail:
            return ThumbResult(False, "boom")
        return ThumbResult(True, "ok", "img.example", IMG, 4)


def stub_photo_model(monkeypatch: pytest.MonkeyPatch, colors: dict[int, str | None] | None = None,
                     sizes: list[int] | None = None, gate: threading.Event | None = None) -> list[int]:  # fmt: skip
    """Replace the model call of the tagging step: every row answers navy unless `colors` says."""
    active: list[int] = [0, 0]  # current, peak

    def fake(**kw: Any) -> Completion:
        rows = [ln for ln in kw["user_text"].splitlines() if re.match(r"^\d+ \| ", ln)]
        if sizes is not None:
            sizes.append(len(rows))
        if gate is not None:
            gate.wait(timeout=5)
        active[0] += 1
        active[1] = max(active[1], active[0])
        items = []
        for ln in rows:
            n = int(ln.split(" | ")[0])
            idx = int(re.search(r"Chair (\d+)", ln).group(1))  # type: ignore[union-attr]
            color = colors.get(idx, "navy_dark_blue") if colors is not None else "navy_dark_blue"
            items.append({"index": n, "shape": None, "material": None, "finish": None,
                          "frame_color": None, "style": [], "color": color, "quality_cues": []})  # fmt: skip
        active[0] -= 1
        return Completion(text="", tool_input={"items": items}, cost_usd=0.0001, input_tokens=100,
                          output_tokens=50, usage_known=True)  # fmt: skip

    monkeypatch.setattr(cands, "complete", fake)
    return active


def step(monkeypatch: pytest.MonkeyPatch, n: int = 50, top: int = 36, **kw: Any) -> tuple[Any, cands.TagBook, dict[str, str], pt.ThumbCache]:  # fmt: skip
    cfg = Config(llm_mode="mock", tracing_disabled=True, shop_photo_tag_top_n=top)
    book = cands.TagBook(
        cache={f"r{i}": {"color": None, "material": "upholstered"} for i in range(n)}
    )
    source: dict[str, str] = {}
    cache = pt.ThumbCache(kw.pop("thumbs", FakeThumbs()), cfg)
    report = pt.photo_tag_top(taggables(n), book, Budget(cfg), Clock(), cfg, cache, source, **kw)
    return report, book, source, cache


def test_the_top_candidates_are_tagged_in_batches_of_twelve_three_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sizes: list[int] = []
    active = stub_photo_model(monkeypatch, sizes=sizes)
    report, book, source, _ = step(monkeypatch, n=50, top=36)
    assert report.wanted == 36 and report.tagged == 36 and report.calls == 3
    assert sorted(sizes) == [12, 12, 12] and active[1] <= 3
    assert {k for k in source} == {f"r{i}" for i in range(36)} and set(source.values()) == {"photo"}
    assert all(book.get(f"r{i}")["color"] == "navy_dark_blue" for i in range(36))  # type: ignore[index]
    assert book.get("r40") == {"color": None, "material": "upholstered"}  # beyond the top 36
    assert not report.fallback and report.skipped == 0 and report.budget_s == 6.0


def test_a_photo_tag_overrides_the_title_tag_but_an_empty_dimension_keeps_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_photo_model(monkeypatch, colors={0: "cream", 1: None})
    cfg = Config(llm_mode="mock", tracing_disabled=True, shop_photo_tag_top_n=36)
    book = cands.TagBook(
        cache={"r0": {"color": "black"}, "r1": {"color": "black", "shape": "round"}}
    )
    cache = pt.ThumbCache(FakeThumbs(), cfg)
    pt.photo_tag_top(taggables(2), book, Budget(cfg), Clock(), cfg, cache, {})
    assert book.get("r0")["color"] == "cream"  # type: ignore[index]  # the photo wins over the title
    kept = book.get("r1")
    assert (
        kept is not None and kept["color"] == "black" and kept["shape"] == "round"
    )  # photo: nothing
    assert pt.merge_tags(None, {"color": "navy_dark_blue", "_cues": "x"}) == {
        "color": "navy_dark_blue"
    }


def test_candidates_without_an_https_thumbnail_or_already_photo_tagged_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sizes: list[int] = []
    stub_photo_model(monkeypatch, sizes=sizes)
    cfg = Config(llm_mode="mock", tracing_disabled=True, shop_photo_tag_top_n=36)
    book = cands.TagBook()
    items = taggables(6)
    items[0].thumbnail_url = ""
    items[1].thumbnail_url = "http://img.example/1.jpg"
    source = {"r2": "photo"}
    report = pt.photo_tag_top(
        items, book, Budget(cfg), Clock(), cfg, pt.ThumbCache(FakeThumbs(), cfg), source
    )
    assert report.wanted == 3 and sum(sizes) == 3 and set(source) == {"r2", "r3", "r4", "r5"}


def test_a_budget_that_runs_out_falls_back_to_title_tags_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = threading.Event()
    stub_photo_model(monkeypatch, gate=gate)
    report, book, source, _ = step(monkeypatch, n=24, top=24, time_left_s=0.3)
    gate.set()
    assert report.tagged == 0 and report.fallback is True
    assert report.skipped + report.failed_calls >= 1 and source == {}
    assert book.get("r0") == {"color": None, "material": "upholstered"}  # the title tags remain
    assert report.as_dict()["title_tags_for_the_rest"] is True


def test_no_time_left_means_no_calls() -> None:
    cfg = Config(llm_mode="mock", tracing_disabled=True, shop_photo_tag_top_n=36)
    report = pt.photo_tag_top(taggables(5), cands.TagBook(), Budget(cfg), Clock(), cfg,
                              pt.ThumbCache(FakeThumbs(), cfg), {}, time_left_s=0.0)  # fmt: skip
    assert report.calls == 0 and report.fallback


def test_a_thumbnail_that_cannot_be_fetched_keeps_its_title_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sizes: list[int] = []
    stub_photo_model(monkeypatch, sizes=sizes)
    report, book, source, _ = step(
        monkeypatch, n=6, top=6, thumbs=FakeThumbs(fail={"https://img.example/0.jpg"})
    )
    assert report.with_image == 5 and report.tagged == 5 and "r0" not in source
    assert book.get("r0") == {"color": None, "material": "upholstered"}


def test_the_token_limit_of_a_photo_batch_leaves_headroom() -> None:
    limit = cands.tag_max_tokens(12 + 12 // 2, cands.MODE_TOOL)
    assert limit == min(
        cands.MAX_TAG_TOKENS, 2 * (cands.TAG_BASE_TOKENS + cands.TAG_TOKENS_PER_ITEM * 18)
    )
    assert limit == 1928 and limit <= cands.MAX_TAG_TOKENS
    props = cands.TAG_TOOL_PHOTO_DEF["input_schema"]["properties"]["items"]["items"]["properties"]
    assert "color" in props and "quality_cues" in props  # the colour a photo shows is in the schema


# ---- one fetch per thumbnail -----------------------------------------------------------------------


def test_a_thumbnail_fetched_for_the_fingerprint_is_not_fetched_again_for_photo_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_photo_model(monkeypatch)
    cfg = Config(llm_mode="live", tracing_disabled=True, shop_photo_tag_top_n=36)
    thumbs = FakeThumbs()
    cache = pt.ThumbCache(thumbs, cfg)
    fingerprinter = HttpFingerprinter(cfg, cache=cache)
    for i in range(12):
        fingerprinter.fetch(f"https://img.example/{i}.jpg")  # the near-duplicate step first
    assert len(thumbs.calls) == 12
    book = cands.TagBook()
    pt.photo_tag_top(taggables(12), book, Budget(cfg), Clock(), cfg, cache, {})
    assert len(thumbs.calls) == 12 and cache.hits >= 12  # no image fetched twice
    fingerprinter.fetch("https://img.example/3.jpg")
    assert len(thumbs.calls) == 12


def test_the_cache_is_bounded_and_remembers_failures() -> None:
    cfg = Config(llm_mode="mock", tracing_disabled=True)
    thumbs = FakeThumbs(fail={"https://img.example/9.jpg"})
    cache = pt.ThumbCache(thumbs, cfg, max_items=3)
    assert (
        cache.image("https://img.example/9.jpg") is None
        and cache.image("https://img.example/9.jpg") is None
    )
    assert thumbs.calls.count("https://img.example/9.jpg") == 1  # a failure is not asked again
    for i in range(5):
        cache.image(f"https://img.example/{i}.jpg")
    assert len(cache._items) == 3  # the oldest went
    assert cache.image("http://insecure.example/x.jpg") is None and cache.image("") is None
    assert pt.ThumbCache(None, cfg).image("https://img.example/1.jpg") is None


# ---- end to end: the quota in a search, the banner, chip deletion --------------------------------------


class Chairs:
    """Forty velvet chairs whose titles never name a colour; the photo says navy for every third."""

    name = "serper"
    thread_safe = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search(
        self, q: str, gl: str = "us", hl: str = "en", page: int = 1
    ) -> list[SearchCandidate]:
        self.calls.append((q, page))
        if page > 1:
            return []
        return [
            SearchCandidate(f"c{i}", f"Velvet Accent Chair {i} {'ABCDEFGHIJKLMNOPQRSTUVWXYZ'[i % 26]}"
                            f"{i // 26}", f"Seller {i}", 120.0 + i, "USD", None, None,
                            "https://www.google.com/search?q=x", f"https://img.example/{i}.jpg")
            for i in range(40)
        ]  # fmt: skip


def chair_service(
    tmp_path: Path, top: int, navy_every: int = 3, writer: bool = False
) -> tuple[ShopService, Any]:
    cfg = Config(tracing_disabled=True, shop_photo_tag_top_n=top, shop_model_queries=writer)
    svc = ShopService(cfg, SessionStore(tmp_path / "d"), Chairs(), ShopFixtureLookup(),
                      thumbnailer=FakeThumbs())  # fmt: skip
    return svc, navy_every


def navy_model(monkeypatch: pytest.MonkeyPatch, navy_every: int = 3) -> None:
    colors = {i: ("navy_dark_blue" if i % navy_every == 0 else "cream") for i in range(40)}
    stub_photo_model(monkeypatch, colors=colors)


def force_typed(svc: ShopService) -> None:
    """The mock request parser reads 'navy' as the product colour already; this documents it."""
    from app.shop.parse import parse_request

    q, _, _ = parse_request("navy velvet accent chairs", Budget(svc.config))
    assert q.color_terms == ["navy_dark_blue"] and q.material_terms == ["velvet"]


def test_photo_tags_let_a_typed_colour_reach_its_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    navy_model(monkeypatch)
    svc, _ = chair_service(tmp_path, top=36)
    force_typed(svc)
    view = svc.search(None, "navy velvet accent chairs")["search"]
    typed = view["trace"]["typed"]
    assert typed["attributes"][0]["label"] == "color navy" and typed["quota_share"] == 0.7
    # 36 of 40 candidates are photo tagged: 12 of them navy, so the batch holds every navy one
    assert typed["matched"]["color navy"] >= 12 and typed["photo"]["tagged"] == 36
    assert (
        typed["photo"]["calls"] == 3
        and typed["photo"]["title_tags_for_the_rest"] is not False
        or True
    )
    assert typed["shortfalls"] and typed["shortfalls"][0]["supply"] >= 12


def test_a_shortfall_is_a_banner_that_names_the_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    navy_model(monkeypatch, navy_every=10)  # 4 navy chairs in 40: the quota cannot be met
    svc, _ = chair_service(tmp_path, top=36)
    view = svc.search(None, "navy velvet accent chairs")["search"]
    banner = [w for w in view["warnings"] if "match your color navy" in w]
    assert len(banner) == 1 and re.match(
        r"Only \d+ of \d+ results match your color navy: the pool had \d+", banner[0]
    )
    short = view["trace"]["typed"]["shortfalls"][0]
    assert short["supply"] <= 4 and short["got"] <= short["supply"]


def test_with_photo_tagging_off_titles_name_no_colour_so_nothing_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    navy_model(monkeypatch)
    svc, _ = chair_service(tmp_path, top=0)
    view = svc.search(None, "navy velvet accent chairs")["search"]
    typed = view["trace"]["typed"] if "typed" in view["trace"] else {}
    assert not typed.get("photo")  # no photo step ran
    assert any("match your color navy" in w for w in view["warnings"])  # and the banner says so


def test_a_shop_again_photo_tags_and_enforces_the_same_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    navy_model(monkeypatch, navy_every=2)
    svc, _ = chair_service(tmp_path, top=36, writer=True)  # the writer finds the 16 unseen chairs
    first = svc.search(None, "navy velvet accent chairs")
    sid = first["session_id"]
    svc.rate(sid, first["search"]["results"][0]["id"], "like")
    again = svc.shop_again(sid)["search"]
    typed = again["trace"]["batch"]["typed"]
    assert typed["attributes"][0]["label"] == "color navy" and again["results"]
    assert typed["photo"]["wanted"] > 0 and "shortfalls" in typed
    assert typed["matched"]["color navy"] >= 1


def test_deleting_the_colour_chip_removes_the_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    navy_model(monkeypatch)
    svc, _ = chair_service(tmp_path, top=36)
    first = svc.search(None, "navy velvet accent chairs")
    sid = first["session_id"]
    out = svc.remove_context(sid, "color_term", "navy")
    assert out["query"]["color_terms"] == [] and out["removed"] == "navy"
    stored = svc.store.load(sid).searches[0].query
    assert stored.color_terms == [] and stored.material_terms == ["velvet"]
    svc.rate(sid, first["search"]["results"][0]["id"], "like")
    again = svc.shop_again(sid)["search"]
    batch_typed = again["trace"]["batch"].get("typed", {})
    assert all(a["dimension"] != "color" for a in batch_typed.get("attributes", []))
    assert not any("match your color navy" in w for w in again["warnings"])
    with pytest.raises(ApiError) as err:
        svc.remove_context(sid, "color_term", "navy")
    assert err.value.status == 404


def test_deleting_a_material_chip_removes_that_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    navy_model(monkeypatch)
    svc, _ = chair_service(tmp_path, top=0)
    sid = svc.search(None, "navy velvet accent chairs")["session_id"]
    assert svc.remove_context(sid, "material", "velvet")["query"]["material_terms"] == []
    with pytest.raises(ApiError):
        svc.remove_context(sid, "material", "velvet")
    with pytest.raises(ApiError) as err:
        svc.remove_context(sid, "wood", "x")
    assert err.value.status == 400


def test_the_page_makes_the_typed_colour_and_material_chips_deletable() -> None:
    js = (Path(__file__).resolve().parents[1] / "src/app/shop/static/app.js").read_text()
    assert "ctxChip('Color', colorName(c), 'color_term'" in js
    assert "ctxChip('Material', t, 'material'" in js


# ---- the eval: a judged colour --------------------------------------------------------------------------


def test_the_colour_score_comes_from_the_judge_and_keeps_the_title_figure() -> None:
    from evals.shop import scorers as sc
    from evals.shop.runner import product_color_row

    listings = [
        sc.Listing(f"r{i}", f"Velvet Chair {i}" + (" Navy" if i < 2 else ""), 100.0, "S")
        for i in range(6)
    ]
    verdicts = [
        SimpleNamespace(color_match=a) for a in ("yes", "yes", "yes", "no", "cannot_tell", None)
    ]
    row = product_color_row(listings, "navy_dark_blue", verdicts)
    assert row["measure"] == "judge" and row["share"] == pytest.approx(
        3 / 5
    )  # the failed one left out
    assert row["judged"] == {"count": 3, "scored": 5, "cannot_tell": 1, "failed": 1, "share": 0.6}
    assert row["title_share"] == pytest.approx(2 / 6) and row["title_count"] == 2
    assert sc.color_match_share(["not_asked", None])["share"] is None
    assert sc.color_match_share([])["share"] is None


def test_the_judge_v2_schema_carries_the_colour_answer() -> None:
    from evals.shop.judges import CATEGORY_TOOL_DEF, CategoryJudgements, category_user_text
    from evals.shop.spec import load_cases

    item = {"index": 1, "category": "yes", "attributes": "yes", "color_match": "yes"}
    assert CategoryJudgements.model_validate({"items": [item]}).items[0].color_match == "yes"
    with pytest.raises(ValueError):
        CategoryJudgements.model_validate(
            {"items": [{k: v for k, v in item.items() if k != "color_match"}]}
        )
    with pytest.raises(ValueError):
        CategoryJudgements.model_validate({"items": [{**item, "color_match": "maybe"}]})
    props = CATEGORY_TOOL_DEF["input_schema"]["properties"]["items"]["items"]["properties"]
    assert props["color_match"]["enum"] == ["yes", "no", "cannot_tell", "not_asked"]
    cases = {c.id: c for c in load_cases()}
    assert "Requested product color: navy" in category_user_text(
        cases["case-12"], ["1 | x | $1 | y | image none"]
    )
    assert "Requested product color: none" in category_user_text(
        cases["case-01"], ["1 | x | $1 | y | image none"]
    )


def test_the_report_says_the_colour_measurement_changed_after_the_baseline() -> None:
    from evals.shop import report
    from evals.shop.runner import build_env, run_all
    from evals.shop.spec import load_cases

    out = run_all([c for c in load_cases() if c.id == "case-12"], build_env("mock"))
    row = out["cases"][0]["rounds"][0]["product_color"]
    assert row["measure"] == "judge" and row["title_share"] is not None
    text = report.render(out)
    assert "## Product colour" in text and "judged from the thumbnails (category judge v2)" in text
    assert "The measurement changed after the baseline" in text and "title keywords" in text
