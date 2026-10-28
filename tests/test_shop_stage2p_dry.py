"""Stage 2p, phase 2: Shop Mode never runs dry. The query writer runs on every Shop Again with the
queries already used, rejects repeats, a thin first search tries variants, and an exhausted search
says so. Mock model, scripted search providers, no network."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.llm import Completion
from app.shop import queries as q
from app.shop.again import used_queries
from app.shop.models import ShopQuery
from app.shop.providers import ShopFixtureLookup
from app.shop.search import EXHAUSTED_MESSAGE
from app.shop.service import ShopService
from app.shop.store import SessionStore
from app.shopping import SearchCandidate
from test_shop import _loopback_only, use_client  # noqa: F401  (the autouse guard must apply here)


def query(**kw: Any) -> ShopQuery:
    base: dict[str, Any] = {
        "request": "walnut dining chairs", "search_text": "walnut dining chair",
        "category": "dining chair", "category_key": "dining chair",
    }  # fmt: skip
    return ShopQuery(**{**base, **kw})


def qin(used: list[str], **kw: Any) -> q.QueryInput:
    return q.QueryInput(base=query(), adjusted=query(), used=used, **kw)


# ---- novelty ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("new", "used", "repeat"),
    [
        ("walnut dining chair", ["walnut dining chair"], True),
        ("dining chairs walnut", ["walnut dining chair"], True),  # a reordering and a plural
        ("walnut dining chair set", ["modern walnut dining chair set"], True),  # one word less
        ("modern walnut dining chair", ["walnut dining chair"], False),  # 3 of 4 words: under 0.8
        ("walnut dining chair", ["Walnut  Dining Chair!"], True),
        ("premium solid oak dining chair", ["walnut dining chair"], False),
        ("affordable walnut dining chair set", ["walnut dining chair", "oak dining chair"], False),
        ("walnut dining chair", [], False),
    ],
)
def test_a_variant_that_repeats_a_used_query_is_rejected(
    new: str, used: list[str], repeat: bool
) -> None:
    why = q.too_close_to_used(new, used)
    assert bool(why) is repeat
    if repeat:
        assert "already used" in why


def fake_complete(answer: list[str]) -> Any:
    def complete(**kw: Any) -> Completion:
        return Completion(text="", tool_input={"queries": answer})

    return complete


def test_the_writer_drops_a_query_that_repeats_a_used_one_and_keeps_new_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(q, "complete", fake_complete(
        ["dining chair walnut", "premium oak dining chair", "stylish walnut dining chair seat"]
    ))  # fmt: skip
    cfg = Config(llm_mode="mock", tracing_disabled=True)
    from app.llm import Budget

    out = q.write_queries(qin(["walnut dining chair"], existing=[]), Budget(cfg), cfg)
    assert (
        out.accepted == ["premium oak dining chair"] or "premium oak dining chair" in out.accepted
    )
    assert any("already used" in d["reason"] for d in out.dropped)
    assert "dining chair walnut" not in out.accepted


def test_the_writer_is_told_what_was_used_and_what_was_seen() -> None:
    text = q.user_text(qin(["walnut dining chair", "oak dining chair"], seen=48, rated=12))
    assert "Results already seen: 48; rated: 12" in text
    assert "Queries already used for this topic (write different ones):" in text
    assert "  * walnut dining chair" in text and "  * oak dining chair" in text
    assert "Write two NEW queries" in text
    assert "http" not in text


def test_the_writer_prompt_is_version_three_and_the_old_ones_are_kept() -> None:
    from app.prompts import load_prompt

    current = load_prompt("shop_queries")
    assert (
        current.version == "3"
        and "TWO NEW" in current.text
        and "Never repeat a used query" in current.text
    )
    assert (
        load_prompt("shop_queries", "2").version == "2"
        and "TWO NEW" not in load_prompt("shop_queries", "2").text
    )
    assert load_prompt("shop_queries", "1").version == "1"


def test_fallback_variants_keep_every_constraint_and_are_new() -> None:
    inp = q.QueryInput(
        base=query(), adjusted=query(must_have=["upholstered"], material_terms=["walnut"]),
        used=["walnut dining chair upholstered"],
    )  # fmt: skip
    got = q.fallback_variants(inp)
    assert len(got) == 2
    for text in got:
        words = set(re.findall(r"[a-z]+", text))
        assert {"walnut", "dining", "chair"} <= words  # nothing typed is dropped
        assert q.validate_query(text, inp)[1] == "" and not q.too_close_to_used(text, inp.used)
    assert got[0] != got[1] and q.too_close_to_used(got[0], [got[1]]) == ""


def test_variant_queries_use_the_model_first_and_the_fallback_for_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.llm import Budget

    cfg = Config(llm_mode="mock", tracing_disabled=True, shop_model_queries=True)
    monkeypatch.setattr(q, "complete", fake_complete(["premium oak dining chair"]))
    got = q.variant_queries(qin(["walnut dining chair"]), Budget(cfg), cfg)
    assert (
        len(got.accepted) == 2
        and got.fallback == 1
        and got.accepted[0] == "premium oak dining chair"
    )
    monkeypatch.setattr(
        q, "complete", fake_complete(["walnut dining chair", "dining chair walnut"])
    )
    got = q.variant_queries(qin(["walnut dining chair"]), Budget(cfg), cfg)
    assert (
        len(got.accepted) == 2 and got.fallback == 2 and len(got.dropped) == 2
    )  # all repeats dropped
    off = Config(llm_mode="mock", tracing_disabled=True, shop_model_queries=False)
    got = q.variant_queries(qin(["walnut dining chair"]), Budget(off), off)
    assert (
        not got.asked and len(got.accepted) == 2 and got.fallback == 2
    )  # no model, still variants


# ---- scripted search: a query that runs dry, and variants that do not ---------------------------------


class Scripted:
    """Serper stand-in. The typed words give `base_pages` pages of `per_page` chairs each and then
    nothing; any other phrasing gives `fresh` unseen chairs on its first page."""

    name = "serper"
    thread_safe = True

    def __init__(self, base: str, base_pages: int = 3, per_page: int = 8, fresh: int = 24) -> None:
        self.base = " ".join(re.findall(r"[a-z0-9]+", base.lower()))
        self.base_pages, self.per_page, self.fresh = base_pages, per_page, fresh
        self.calls: list[tuple[str, int]] = []

    def search(
        self, query: str, gl: str = "us", hl: str = "en", page: int = 1
    ) -> list[SearchCandidate]:
        key = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
        self.calls.append((key, page))
        if key == self.base:
            n = self.per_page if page <= self.base_pages else 0
        else:
            n = self.fresh if page == 1 else 0
        tag = re.sub(r"[^a-z0-9]", "", key)[:10] or "q"
        return [
            SearchCandidate(
                f"{tag}-{page}-{i}",
                f"Walnut Dining Chair {tag} {page} {i}",
                f"Seller {i % 9}",
                60.0 + i,
                "USD",
                None,
                None,
                "https://www.google.com/search?q=x",
            )
            for i in range(n)
        ]


WORDS = (
    "Walnut",
    "Oak",
    "Black",
    "Cream",
    "Teak",
    "Grey",
    "Ash",
    "Pine",
    "Beech",
    "Rattan",
    "Blue",
    "Green",
    "Maple",
    "Cane",
)


def service(tmp_path: Path, search: Scripted, **kw: Any) -> ShopService:
    cfg = Config(tracing_disabled=True, **kw)
    return ShopService(cfg, SessionStore(tmp_path / "d"), search, ShopFixtureLookup())


def rate_some(svc: ShopService, sid: str, view: dict[str, Any], n: int = 2) -> None:
    for r in view["search"]["results"][:n]:
        svc.rate(sid, r["id"], "like")


def test_an_exhausted_query_recovers_through_variants(tmp_path: Path) -> None:
    search = Scripted("walnut dining chair")
    svc = service(tmp_path, search, shop_model_queries=True)
    first = svc.search(None, "walnut dining chair")
    sid = first["session_id"]
    assert len(first["search"]["results"]) == 16  # pages 1 and 2 of the typed words
    shown: set[str] = {r["id"] for r in first["search"]["results"]}
    rounds = []
    for _ in range(3):
        rate_some(svc, sid, svc.session_view(sid))
        again = svc.shop_again(sid)
        results = again["search"]["results"]
        rounds.append(again)
        assert results, "a Shop Again must not come back empty while variants can find more"
        assert not {r["id"] for r in results} & shown  # nothing repeated
        shown |= {r["id"] for r in results}
        assert again["search"]["serper_calls"] <= svc.config.shop_again_max_serper_calls_total
    assert any(c[0] != search.base for c in search.calls)  # variants really searched
    asked = [r["search"]["trace"]["batch"]["model_queries"] for r in rounds]
    assert all(m["asked"] for m in asked)
    assert any(m["accepted"] for m in asked)


def test_without_the_writer_the_same_search_runs_dry_and_says_so(tmp_path: Path) -> None:
    search = Scripted("walnut dining chair")
    svc = service(tmp_path, search, shop_model_queries=False)
    first = svc.search(None, "walnut dining chair")
    sid = first["session_id"]
    last: dict[str, Any] = {}
    for _ in range(4):
        rate_some(svc, sid, svc.session_view(sid))
        last = svc.shop_again(sid)
        if not last["search"]["results"]:
            break
    found = last["search"]
    assert found["results"] == []
    assert EXHAUSTED_MESSAGE in found["warnings"] and found["headline"] == EXHAUSTED_MESSAGE
    why = found["trace"]["batch"]["exhausted"]
    assert set(why) >= {"reason", "seen_or_rated", "duplicates", "filtered_out", "serper_calls",
                        "queries_used_before", "writer_asked"}  # fmt: skip
    assert why["writer_asked"] is False and why["queries_used_before"] >= 1


def test_the_writer_runs_even_when_nothing_was_liked_or_learned(tmp_path: Path) -> None:
    search = Scripted("walnut dining chair")
    svc = service(tmp_path, search, shop_model_queries=True)
    first = svc.search(None, "walnut dining chair")
    sid = first["session_id"]
    svc.rate(sid, first["search"]["results"][0]["id"], "dislike")  # no like, no leaning
    before = used_queries(svc.store.load(sid))
    again = svc.shop_again(sid)
    model = again["search"]["trace"]["batch"]["model_queries"]
    assert model["asked"] is True and model["accepted"]  # it used to need a liked title
    assert all(not q.too_close_to_used(a, before) for a in model["accepted"])  # all new
    assert again["search"]["results"]  # and they found results


def test_used_queries_are_per_topic_and_listed_once(tmp_path: Path) -> None:
    search = Scripted("walnut dining chair")
    svc = service(tmp_path, search, shop_model_queries=True)
    first = svc.search(None, "walnut dining chair")
    sid = first["session_id"]
    session = svc.store.load(sid)
    assert used_queries(session) == ["walnut dining chair"]
    rate_some(svc, sid, first)
    svc.shop_again(sid)
    after = used_queries(svc.store.load(sid))
    assert after[0] == "walnut dining chair" and len(after) == len(set(after)) >= 2
    other = svc.search(sid, "standing desk")  # a new topic starts clean
    del other
    assert all("desk" in u or "standing" in u for u in used_queries(svc.store.load(sid)))


def test_the_credit_cap_of_a_shop_again_is_unchanged(tmp_path: Path) -> None:
    search = Scripted("walnut dining chair")
    svc = service(tmp_path, search, shop_model_queries=True)
    first = svc.search(None, "walnut dining chair")
    sid = first["session_id"]
    rate_some(svc, sid, first, 3)
    again = svc.shop_again(sid)["search"]
    assert again["serper_calls"] <= 9 == svc.config.shop_again_max_serper_calls_total
    assert (
        again["serper_credits"] == again["serper_calls"] * svc.config.serper_credits_per_call <= 18
    )


# ---- a thin first search -----------------------------------------------------------------------------


def test_a_thin_first_search_tries_variants_inside_the_normal_cap(tmp_path: Path) -> None:
    search = Scripted("walnut dining chair", base_pages=1, per_page=10, fresh=6)
    svc = service(tmp_path, search, shop_thin_variants=True)
    view = svc.search(None, "walnut dining chair")["search"]
    assert len(view["results"]) > 10 and view["serper_calls"] <= 4
    assert view["serper_calls"] <= svc.config.shop_again_max_serper_calls
    thin = view["trace"]["thin"]
    assert thin["before"] <= 10 and thin["after"] > thin["before"]
    assert 1 <= len(thin["variants"]) <= 2 and all(
        v["new_candidates"] > 0 for v in thin["variants"]
    )
    assert thin["from_fallback"] == 2  # the model writer is off here: the deterministic variants
    assert view["serper_credits"] == view["serper_calls"] * svc.config.serper_credits_per_call


def test_a_search_that_is_still_thin_says_how_many_matched_and_why(tmp_path: Path) -> None:
    search = Scripted("walnut dining chair", base_pages=1, per_page=10, fresh=4)
    svc = service(tmp_path, search, shop_thin_variants=True)
    view = svc.search(None, "walnut dining chair")["search"]
    banner = [w for w in view["warnings"] if w.startswith("Only ")]
    assert len(banner) == 1
    assert re.fullmatch(
        rf"Only {len(view['results'])} results matched.*; try broadening the request\.", banner[0]
    )


def test_without_the_flag_a_thin_search_runs_no_variants(tmp_path: Path) -> None:
    search = Scripted("walnut dining chair", base_pages=1, per_page=10, fresh=6)
    svc = service(tmp_path, search, shop_thin_variants=False)
    view = svc.search(None, "walnut dining chair")["search"]
    assert len(view["results"]) == 10 and "thin" not in view["trace"]
    assert all(call[0] == "walnut dining chair" for call in search.calls)


def test_a_full_first_search_asks_for_no_variants(tmp_path: Path) -> None:
    search = Scripted("walnut dining chair", base_pages=2, per_page=24)
    svc = service(tmp_path, search, shop_thin_variants=True)
    view = svc.search(None, "walnut dining chair")["search"]
    assert len(view["results"]) == 24 and "thin" not in view["trace"]
    assert not [w for w in view["warnings"] if w.startswith("Only ")]


# ---- the eval runner records exhaustion --------------------------------------------------------------


def test_the_eval_runner_records_an_exhausted_round_and_the_report_says_so() -> None:
    import dataclasses

    from evals.shop import report
    from evals.shop.runner import build_env, run_all
    from evals.shop.spec import load_cases

    env = build_env("mock")
    env.config = dataclasses.replace(env.config, shop_model_queries=False, shop_thin_variants=False)
    env.judge.config = env.config
    real = env.make_providers

    class Tiny:
        name = "serper"
        thread_safe = True

        def search(
            self, query: str, gl: str = "us", hl: str = "en", page: int = 1
        ) -> list[SearchCandidate]:
            if page != 1:
                return []
            return [
                SearchCandidate(f"t{i}", f"{WORDS[i]} Mid-Century Dining Chair", f"Seller {i}", 90.0 + i * 3,
                                "USD", None, None, "https://www.google.com/search?q=x")
                for i in range(14)
            ]  # fmt: skip

    env.make_providers = lambda case: (Tiny(), real(case)[1])
    out = run_all([load_cases()[3]], env)
    rounds = out["cases"][0]["rounds"]
    assert [r["count"] for r in rounds][0] == 14 and rounds[-1]["exhausted"] is True
    assert rounds[0]["exhausted"] is False
    assert out["cases"][0]["rounds"][-1]["trace"]["exhausted"]["reason"]
    text = report.render(out)
    assert "exhausted" in text.split("## Cases")[1].split("## Shop Again lift")[0]
