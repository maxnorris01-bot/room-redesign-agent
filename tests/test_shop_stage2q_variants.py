"""Stage 2q, phase 2: search variants keep every hard word of the request and add no attribute word;
every shown item says which search found it; the eval report breaks shown items down by source.
Mock model, scripted search providers, no network."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from evals.shop import report

from app.config import Config
from app.llm import Budget, Completion
from app.prompts import load_prompt
from app.shop import hardwords as hw
from app.shop import queries as q
from app.shop.models import ShopQuery
from app.shop.providers import ShopFixtureLookup
from app.shop.service import ShopService
from app.shop.store import SessionStore
from app.shopping import SearchCandidate
from test_shop import _loopback_only  # noqa: F401  (the autouse guard must apply here)


def request(**kw: Any) -> ShopQuery:
    base: dict[str, Any] = {
        "request": "mid-century dining chairs under $150 each",
        "search_text": "mid-century dining chairs", "category": "dining chairs",
        "category_key": "dining chair", "style_terms": ["mid-century"],
    }  # fmt: skip
    return ShopQuery(**{**base, **kw})


def inp(base: ShopQuery | None = None, **kw: Any) -> q.QueryInput:
    base = base or request()
    return q.QueryInput(base=base, adjusted=base, **kw)


def ok(text: str, query_input: q.QueryInput | None = None) -> bool:
    return q.validate_query(text, query_input or inp())[1] == ""


# ---- hard words ------------------------------------------------------------------------------


def test_the_hard_words_are_the_category_and_every_typed_style_type_size_colour_and_material() -> (
    None
):
    base = request(
        request="navy velvet mesh 8x10 sectional rug", category="rug", style_terms=["boho"],
        material_terms=["velvet", "mesh"], color_terms=["navy_dark_blue"], must_have=["sectional"],
    )  # fmt: skip
    got = {(w.kind, w.label) for w in hw.hard_words(base, base)}
    assert got == {
        ("category", "rug"), ("style", "boho"), ("material", "velvet"), ("material", "mesh"),
        ("color", "navy"), ("must_have", "sectional"), ("size", "8x10"),
    }  # fmt: skip


@pytest.mark.parametrize(
    ("text", "accepted"),
    [
        ("affordable mid-century dining chairs", True),
        ("mid century dining chair set", True),  # a spelling of the same word
        ("midcentury dining chair", True),
        ("kitchen chair mid-century", True),  # the category's listed synonym
        ("premium mid-century dining chairs", True),
        ("retro modern wooden dining side chair", False),  # drops mid-century, adds words
        ("affordable dining chairs", False),  # drops mid-century
        ("mid-century chairs", False),  # drops "dining"
        ("mid-century dining chairs upholstered", False),  # adds a material
        ("affordable midcentury upholstered dining seat chairs", False),  # the live case-04 variant
        ("mid-century dining chairs walnut", False),  # adds a wood
        ("mid-century dining chairs black", False),  # adds a colour
    ],
)
def test_a_variant_keeps_every_hard_word_and_adds_no_attribute_word(
    text: str, accepted: bool
) -> None:
    got, why = q.validate_query(text, inp())
    assert bool(got) is accepted, (text, why)
    if not accepted:
        assert "missing" in why or "did not use" in why


def test_the_reasons_name_the_missing_or_added_word() -> None:
    assert "typed style 'mid-century'" in q.validate_query("affordable dining chairs", inp())[1]
    assert "category word 'chair'" in q.validate_query("mid-century dining", inp())[1]
    assert "'upholstered'" in q.validate_query("mid-century upholstered dining chair", inp())[1]


def test_a_listed_synonym_replaces_a_hard_word_but_a_big_group_does_not() -> None:
    mesh = request(
        request="ergonomic mesh desk chairs", search_text="ergonomic mesh desk chair",
        category="desk chair", style_terms=[], material_terms=["mesh"], must_have=["ergonomic"],
    )  # fmt: skip
    one = inp(mesh)
    assert ok("ergonomic netting desk chair", one)  # mesh <-> netting: a three-word group
    assert not ok("ergonomic velvet desk chair", one)  # velvet is not a listed synonym of mesh
    assert not ok("ergonomic desk chair", one)  # mesh dropped
    velvet = request(
        request="navy velvet accent chairs", search_text="navy velvet accent chair",
        category="accent chair", style_terms=[], material_terms=["velvet"],
        color_terms=["navy_dark_blue"],
    )  # fmt: skip
    two = inp(velvet)
    assert ok("premium navy velvet accent chair", two)
    assert not ok("premium navy linen accent chair", two)  # the upholstered group is a bucket
    assert not ok("premium navy leather accent chair", two)
    assert not ok("premium velvet accent chair", two)  # colour dropped
    assert not ok("premium green velvet accent chair", two)  # colour added


def test_a_size_is_a_hard_word() -> None:
    rug = request(
        request="wool area rug 8x10", search_text="wool area rug 8x10", category="area rug",
        style_terms=[], material_terms=["wool"],
    )  # fmt: skip
    one = inp(rug)
    assert ok("premium wool area rug 8x10", one) and ok("wool area rug 8 x 10", one)
    assert not ok("premium wool area rug", one) and not ok("wool area rug 5x8", one)


def test_the_neutral_fillers_add_no_attribute_word() -> None:
    for word in q.VARIANT_MODIFIERS:
        assert not hw.added_attribute_word(word, set()), word
    for word in ("modern", "classic", "contemporary"):
        assert word not in q.VARIANT_MODIFIERS  # style words are not fillers


def test_fallback_variants_never_drift_from_the_request() -> None:
    one = inp(used=["mid-century dining chairs"])
    got = q.fallback_variants(one)
    assert len(got) == 2
    for text in got:
        assert ok(text, one) and "mid" in text and "chair" in text.lower()


def test_fallback_variants_restore_a_hard_word_the_search_words_lack() -> None:
    base = request(search_text="dining chairs")  # the parser's search words lost the style
    got = q.fallback_variants(inp(base, used=["dining chairs"]))
    assert got and all("mid-century" in t for t in got)


# ---- the model's variants are checked in code ------------------------------------------------


def fake_complete(answer: list[str]) -> Any:
    def complete(**kw: Any) -> Completion:
        return Completion(text="", tool_input={"queries": answer})

    return complete


def test_the_writer_rejects_variants_that_lose_a_hard_word_or_add_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drifting = ["affordable midcentury upholstered dining seat chairs"]
    drifting += ["retro modern wooden dining side chair", "premium midcentury dining chair"]
    monkeypatch.setattr(q, "complete", fake_complete(drifting))
    cfg = Config(llm_mode="mock", tracing_disabled=True)
    out = q.write_queries(inp(used=["mid-century dining chairs"]), Budget(cfg), cfg)
    assert out.accepted == ["premium midcentury dining chair"]
    reasons = " | ".join(d["reason"] for d in out.dropped)
    assert "'upholstered'" in reasons and "typed style" in reasons


def test_when_every_variant_is_rejected_fewer_searches_run_and_the_note_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        q, "complete", fake_complete(["modern wooden dining chair", "upholstered chair"])
    )
    monkeypatch.setattr(q, "VARIANT_MODIFIERS", ("modern",))  # a filler that adds a style word
    cfg = Config(llm_mode="mock", tracing_disabled=True, shop_model_queries=True)
    out = q.variant_queries(inp(used=["mid-century dining chairs"]), Budget(cfg), cfg)
    assert out.accepted == [] and out.fallback == 0
    assert (
        "No variant kept every word of your request" in out.note and "no variant search" in out.note
    )
    assert len(out.dropped) >= 2


def test_one_surviving_variant_says_how_many_searches_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(q, "complete", fake_complete(["premium midcentury dining chair"]))
    monkeypatch.setattr(q, "VARIANT_MODIFIERS", ("modern",))
    cfg = Config(llm_mode="mock", tracing_disabled=True, shop_model_queries=True)
    out = q.variant_queries(inp(used=["mid-century dining chairs"]), Budget(cfg), cfg)
    assert len(out.accepted) == 1 and "Only 1 of 2 variants" in out.note


def test_the_prompt_is_version_four_and_older_versions_are_kept() -> None:
    current = load_prompt("shop_queries")
    assert current.version == "4"
    assert "NEVER adds a style, material, colour or finish word" in current.text
    assert "keeps EVERY hard word" in current.text
    assert [load_prompt("shop_queries", v).version for v in ("1", "2", "3")] == ["1", "2", "3"]
    assert "Never repeat a used query" in load_prompt("shop_queries", "3").text


# ---- scripted search: every item says where it came from -------------------------------------


class Scripted:
    """Serper stand-in: the typed words give 10 chairs on page 1 only, any other phrasing 12."""

    name = "serper"
    thread_safe = True

    def __init__(self, base: str) -> None:
        self.base = " ".join(re.findall(r"[a-z0-9]+", base.lower()))
        self.calls: list[tuple[str, int]] = []

    def search(
        self, query: str, gl: str = "us", hl: str = "en", page: int = 1
    ) -> list[SearchCandidate]:
        key = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
        self.calls.append((key, page))
        n = (10 if page == 1 else 0) if key == self.base else (12 if page == 1 else 0)
        tag = re.sub(r"[^a-z0-9]", "", key)[:10] or "q"
        return [
            SearchCandidate(f"{tag}-{page}-{i}", f"Mid Century Dining Chair {tag} {i}",
                            f"Seller {i % 9}", 60.0 + i, "USD", None, None,
                            "https://www.google.com/search?q=x")
            for i in range(n)
        ]  # fmt: skip


def service(tmp_path: Path, search: Scripted, **kw: Any) -> ShopService:
    cfg = Config(tracing_disabled=True, **kw)
    return ShopService(cfg, SessionStore(tmp_path / "d"), search, ShopFixtureLookup())


def test_a_thin_first_search_labels_base_and_variant_items(tmp_path: Path) -> None:
    search = Scripted("mid century dining chair")
    svc = service(tmp_path, search, shop_thin_variants=True)
    view = svc.search(None, "mid century dining chair")["search"]
    kinds = {r["source"]["kind"] for r in view["results"]}
    assert kinds == {"base", "thin_variant"}
    for r in view["results"]:
        assert r["source"]["query"] and "http" not in r["source"]["query"]
        if r["source"]["kind"] == "base":
            assert r["source"]["query"] == "mid century dining chair"
        else:
            assert r["source"]["query"] != "mid century dining chair"
    assert "note" in view["trace"]["thin"]


def test_variants_that_drift_do_not_run_as_searches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(q, "VARIANT_MODIFIERS", ("modern",))  # would add a style word
    search = Scripted("mid century dining chair")
    svc = service(tmp_path, search, shop_thin_variants=True)
    view = svc.search(None, "mid century dining chair")["search"]
    assert all(call[0] == "mid century dining chair" for call in search.calls)
    thin = view["trace"]["thin"]
    assert thin["variants"] == [] and "No variant kept every word" in thin["note"]
    assert {r["source"]["kind"] for r in view["results"]} == {"base"}


def test_a_shop_again_labels_writer_and_base_items(tmp_path: Path) -> None:
    search = Scripted("mid century dining chair")
    svc = service(tmp_path, search, shop_model_queries=True)
    first = svc.search(None, "mid century dining chair")
    sid = first["session_id"]
    for r in first["search"]["results"][:2]:
        svc.rate(sid, r["id"], "like")
    again = svc.shop_again(sid)["search"]
    kinds = {r["source"]["kind"] for r in again["results"]}
    assert kinds <= {"base", "writer", "seller", "peer", "supply", "refill"} and "writer" in kinds
    queries = again["trace"]["batch"]["model_queries"]
    assert "note" in queries
    for r in again["results"]:
        if r["source"]["kind"] == "writer":
            assert r["source"]["query"] in queries["accepted"]


# ---- the eval records and reports sources ----------------------------------------------------


def item(kind: str | None, attr: str, persona: str) -> dict[str, Any]:
    row: dict[str, Any] = {"id": "x", "attributes": attr, "persona": persona}
    if kind:
        row["source"] = {"kind": kind, "query": "q"}
    return row


def results_file(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"cases": [{"id": "case-04", "rounds": [{"round": 1, "items": items}]}]}


def test_the_report_table_breaks_shown_items_down_by_source() -> None:
    data = results_file([
        item("base", "yes", "like"), item("base", "yes", "dislike"), item("base", "no", "like"),
        item("writer", "no", "dislike"), item("writer", "cannot_tell", "love"),
    ])  # fmt: skip
    rows = report.source_rows(data["cases"][0])
    assert [(r[1], r[2]) for r in rows] == [("base", 3), ("writer", 2)]
    base, writer = rows[0], rows[1]
    assert base[3]["good"] == 2 and base[3]["scored"] == 3
    assert base[4]["good"] == 2 and writer[3]["good"] == 0 and writer[4]["good"] == 1
    text = "\n".join(report.source_section(data))
    assert "| case-04 | 1 | base | 3 | 2/3 (67%) | 2/3 (67%) |" in text
    assert "| case-04 | 1 | writer | 2 | 0/2 (0%) | 1/2 (50%) |" in text
    assert "no threshold" in text


def test_an_older_results_file_without_source_still_renders() -> None:
    data = results_file([item(None, "yes", "like")])
    assert report.source_rows(data["cases"][0]) == []
    assert "No source data" in "\n".join(report.source_section(data))
    saved = Path(__file__).resolve().parents[1] / "evals" / "shop" / "results"
    old = sorted(saved.glob("shop-mock-20261027-04*.json"))
    for path in old[:1]:
        assert "## Where shown items came from" in report.render(json.loads(path.read_text()))


def test_the_mock_eval_writes_a_source_on_every_item() -> None:
    from evals.shop.runner import build_env, run_case
    from evals.shop.spec import load_cases

    case = next(c for c in load_cases() if c.id == "case-04")
    done = run_case(case, build_env("mock"))
    items = [i for r in done["rounds"] for i in r["items"]]
    assert items and all(i["source"] and i["source"]["kind"] for i in items)
    assert any(i["source"]["kind"] == "base" for i in items)
