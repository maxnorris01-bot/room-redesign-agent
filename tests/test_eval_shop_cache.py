"""Stage 2r, phase 6: the eval-only Serper cache, the zero-spend structural replay, the results lint
and the lift diagnosis. Synthetic data only: no real Google Shopping response is in the repo."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from evals.shop import cache as cache_mod
from evals.shop import diagnose_lift, lint, report, structural
from evals.shop.__main__ import main, replay_command
from evals.shop.cache import CacheStats, CachingSerper, request_key, write_entry
from evals.shop.mockdata import RouterSearch
from evals.shop.runner import build_env, run_all
from evals.shop.spec import load_cases

from app.shopping import ProviderError, SearchCandidate

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400.0


def payload(n: int = 3, tag: str = "a") -> dict[str, Any]:
    return {"shopping": [
        {"productId": f"{tag}{i}", "title": f"Synthetic Chair {tag}{i}", "source": f"Shop {i}",
         "price": f"${50 + i}.00", "rating": 4.5, "ratingCount": 10, "link": "https://x.test/p",
         "imageUrl": "https://img.test/x.jpg"} for i in range(n)
    ]}  # fmt: skip


class Fake(CachingSerper):
    """A CachingSerper whose network is a counter."""

    def __init__(self, stats: CacheStats, directory: Path, now: float = 1_000_000.0) -> None:
        super().__init__("sk-secret-never-written", stats, directory, 2.0, now=lambda: self.clock)
        self.clock = now
        self.live = 0

    def _request(self, send: Any) -> dict[str, Any]:
        self.live += 1
        return payload(tag=f"l{self.live}")


def test_the_key_depends_on_query_page_and_location() -> None:
    base = {"q": "oak table", "gl": "us", "hl": "en"}
    assert request_key(base) == request_key(dict(base))
    assert request_key(base) != request_key({**base, "q": "oak tables"})
    assert request_key(base) != request_key({**base, "page": 2})
    assert request_key(base) != request_key({**base, "gl": "ca"})


def test_record_saves_every_live_response_without_headers_or_keys(tmp_path: Path) -> None:
    stats = CacheStats(mode="record")
    serper = Fake(stats, tmp_path)
    assert len(serper.search("oak table")) == 3 and len(serper.search("oak table")) == 3
    assert serper.live == 2 and stats.recorded == 2 and stats.hits == 0  # record never reads
    entry = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert set(entry) == {"request", "recorded_at", "recorded_epoch", "response"}
    assert entry["request"]["q"] == "oak table" and entry["response"]["shopping"]
    text = next(tmp_path.glob("*.json")).read_text()
    assert "sk-secret-never-written" not in text and "X-API-KEY" not in text


def test_use_serves_a_fresh_entry_free_and_records_a_miss(tmp_path: Path) -> None:
    stats = CacheStats(mode="use")
    serper = Fake(stats, tmp_path)
    serper.search("oak table")  # a miss: live, saved
    again = serper.search("oak table")  # a hit
    assert serper.live == 1 and stats.hits == 1 and stats.misses == 1 and stats.recorded == 1
    assert stats.credits_saved == 2.0 and stats.take_saved() == 2.0 and stats.take_saved() == 0.0
    assert [c.title for c in again] == [f"Synthetic Chair l1{i}" for i in range(3)]  # the saved one


def test_use_ignores_an_entry_older_than_fourteen_days(tmp_path: Path) -> None:
    stats = CacheStats(mode="use")
    serper = Fake(stats, tmp_path, now=1_000_000.0)
    serper.search("oak table")
    serper.clock += 13 * DAY
    serper.search("oak table")
    assert serper.live == 1 and stats.hits == 1  # 13 days: still fresh
    serper.clock += 2 * DAY  # 15 days after the recording
    serper.search("oak table")
    assert serper.live == 2 and stats.stale == 1  # ignored, fetched again, saved again


def test_replay_never_goes_to_the_network_and_a_miss_is_an_error(tmp_path: Path) -> None:
    key = request_key({"q": "oak table", "gl": "us", "hl": "en"})
    write_entry(tmp_path, key, {"q": "oak table", "gl": "us", "hl": "en"}, payload(), 1_000_000.0)
    stats = CacheStats(mode="replay")
    serper = Fake(stats, tmp_path)
    assert len(serper.search("oak table")) == 3
    with pytest.raises(ProviderError, match="replay"):
        serper.search("never saved")
    assert serper.live == 0 and stats.missing == ["never saved (page 1)"] and stats.hits == 1


def test_the_app_never_reads_the_cache() -> None:
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text()
        assert "evals.shop.cache" not in text and "EVAL_SHOP_SERPER_CACHE" not in text, path
    assert "evals/shop/cache/" in (ROOT / ".gitignore").read_text()


def test_the_mode_comes_from_the_environment_and_a_bad_word_is_an_error() -> None:
    assert cache_mod.cache_mode({}) == "off" and cache_mod.cache_mode({}, "record") == "record"
    assert cache_mod.cache_mode({"EVAL_SHOP_SERPER_CACHE": "USE"}) == "use"
    with pytest.raises(ValueError):
        cache_mod.cache_mode({"EVAL_SHOP_SERPER_CACHE": "yes"})
    assert (
        "EVAL_SHOP_SERPER_CACHE=$(or $(EVAL_SHOP_SERPER_CACHE),record)"
        in (ROOT / "Makefile").read_text()
    )


# ---- a replay run on a synthetic cache ------------------------------


class Recorder(RouterSearch):
    """The mock catalogue, recording every response in the synthetic cache."""

    def __init__(self, directory: Path, fixture_case: bool = False) -> None:
        super().__init__(fixture_case)
        self.directory = directory

    def search(
        self, query: str, gl: str = "us", hl: str = "en", page: int = 1
    ) -> list[SearchCandidate]:
        got = super().search(query, gl, hl, page)
        body: dict[str, Any] = {"q": query, "gl": gl, "hl": hl}
        if page > 1:
            body["page"] = page
        data = {"shopping": [
            {"productId": c.product_id, "title": c.title, "source": c.seller,
             "price": f"${c.price:.2f}" if c.price is not None else None,
             "rating": c.rating, "ratingCount": c.rating_count, "link": c.google_panel_url,
             "imageUrl": c.thumbnail_url} for c in got
        ]}  # fmt: skip
        import time

        write_entry(self.directory, request_key(body), body, data, time.time())
        return got


def record_case(tmp_path: Path, case_id: str) -> Path:
    case = next(c for c in load_cases() if c.id == case_id)
    env = build_env("mock", serper_cache="off")
    from app.shop.providers import ShopFixtureLookup

    env.make_providers = lambda _c: (Recorder(tmp_path), ShopFixtureLookup())
    run_all([case], env)
    return tmp_path


def test_a_replay_on_a_recorded_cache_has_hits_no_misses_and_says_cached(tmp_path: Path) -> None:
    folder = record_case(tmp_path, "case-07")
    assert list(folder.glob("*.json"))
    case = next(c for c in load_cases() if c.id == "case-07")
    env = build_env("mock", serper_cache="replay", cache_dir=folder)
    payload_ = run_all([case], env)
    cache = payload_["serper_cache"]
    assert cache["mode"] == "replay" and cache["hits"] > 0 and cache["missing"] == []
    assert cache["note"] == "cached pool, not a fresh-world run" and cache["credits_saved"] > 0
    assert payload_["totals"]["serper_credits"] == 0  # every call was a hit: nothing spent
    text = report.render(payload_)
    assert "## Serper cache" in text and "Cached pool, not a fresh-world run" in text
    out = structural.render(payload_)
    assert "structural only; not a quality result" in out and "case-07" in out


def test_the_replay_command_fails_clearly_on_an_empty_cache(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert replay_command(load_cases()[:1], tmp_path / "none") == 2
    err = capsys.readouterr().err
    assert "run a live eval once with recording" in err


def test_the_replay_command_lists_what_the_cache_lacks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_entry(tmp_path, "x" * 8, {"q": "unrelated", "gl": "us", "hl": "en"}, payload(), 1.0)
    assert replay_command([c for c in load_cases() if c.id == "case-07"], tmp_path) == 1
    err = capsys.readouterr().err
    assert "were not in the saved responses" in err and "brass floor lamps" in err


def test_the_replay_target_is_zero_spend_and_mock(capsys: pytest.CaptureFixture[str]) -> None:
    make = (ROOT / "Makefile").read_text()
    block = re.search(r"^eval-shop-replay:\n(.*)$", make, flags=re.M)
    assert block and "APP_LLM_MODE=mock" in block.group(1) and "live" not in block.group(1)
    assert main(["lint", "--results", "no/such/file.json"]) == 2


# ---- the lint ------------------------------


def row(rnd: int, count: int = 24, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "round": rnd, "kind": "search" if rnd == 1 else "shop_again", "seconds": 10.0,
        "count": count, "items": [],
        "footer": "Shop Again took 10.0 s · 4 Serper calls = 8 credits",
        "trace": {"warnings": []},
    }  # fmt: skip
    base.update(kw)
    return base


def results(rounds: list[dict[str, Any]], cost: float = 0.02, status: str = "ok") -> dict[str, Any]:
    return {"run_id": "r1", "mode": "live", "thresholds": {"cost_per_request_usd_max": 0.05},
            "cases": [{"id": "case-xx", "status": status, "error": None, "rounds": rounds,
                       "cost": {"pipeline_usd": cost}}]}  # fmt: skip


def test_the_lint_finds_each_anomaly_and_sorts_by_severity() -> None:
    items = (
        [{"attributes": "no"}] * 3
        + [{"attributes": "cannot_tell"}] * 12
        + [{"attributes": "yes"}] * 9
    )
    bad = row(1, items=items)
    short = row(2, 5, trace={"warnings": ["Only 5 new results found: 5 seen or rated. Try again.",
                                          "Could not keep shape mixed: the pool is mostly round.",
                                          "Candidate tagging failed: APITimeoutError after 1.0 s"],
                             "typed": {"photo": {"wanted": 36, "tagged": 12, "failed_calls": 2,
                                                 "abandoned_calls": 1, "reason": "x"}}},
               footer="Shop Again took 31.5 s · 8 Serper calls = 16 credits",
               seconds=31.5)  # fmt: skip
    text = lint.render(results([bad, short], cost=0.046))
    lines = text.splitlines()
    assert lines[0].startswith("Shop eval lint") and "high" in lines[0]
    sev = [ln.split()[0] for ln in lines[2:]]
    assert sev == sorted(sev, key=["HIGH", "MEDIUM", "LOW"].index)
    for needle in ("5 of 24 results: Only 5 new results found", "within 10% of the $0.05",
                   "cannot_tell on 12 of 24", "3 outright attribute 'no'",
                   "photo tagging: 2 failed, 1 abandoned",
                   "Candidate tagging failed", "8 of 9 Serper calls", "Shop Again took 31.5 s",
                   "Could not keep shape mixed"):  # fmt: skip
        assert needle in text, needle
    assert len(lines) <= 40


def test_a_quiet_file_and_a_failed_case_are_reported_plainly() -> None:
    assert "nothing to report" in lint.render(results([row(1), row(2), row(3)]))
    assert "case failed: boom" in lint.render(
        {
            **results([], status="failed"),
            "cases": [{"id": "c", "status": "failed", "error": "boom", "rounds": []}],
        }
    )


@pytest.mark.parametrize(
    "name", ["shop-live-20261029-034000-3f9fa0.json", "shop-live-20261029-033423-ddf257.json"]
)
def test_the_lint_runs_on_the_saved_live_files_when_they_are_here(name: str) -> None:
    path = ROOT / "evals" / "shop" / "results" / name
    if not path.is_file():
        pytest.skip("saved live results are gitignored")
    text = lint.lint_file(path)
    assert text.startswith("Shop eval lint of ") and len(text.splitlines()) <= 40


# ---- the lift diagnosis ------------------------------


def it(title: str, persona: str, seller: str = "S") -> dict[str, Any]:
    return {"title": title, "persona": persona, "seller": seller}


def lift_case(
    r1: list[dict[str, Any]], r2: list[dict[str, Any]], chips: list[dict[str, Any]]
) -> dict[str, Any]:
    learned = {"chips": chips, "alignment": {"matches": 5, "total": len(r2)}}
    return {"id": "case-x", "status": "ok", "request": "x", "rounds": [
        {"round": 1, "items": r1, "trace": {"learned": {"chips": []}}},
        {"round": 2, "items": r2, "trace": {"learned": learned}}]}  # fmt: skip


WOOD = {
    "kind": "material",
    "text": "wood",
    "status": "converged",
    "dimension": "material",
    "values": ["wood"],
}


def test_a_chip_that_predicts_likes_is_the_right_thing() -> None:
    r2 = [it("Oak Wood Chair", "like")] * 12 + [it("Steel Chair", "dislike")] * 12
    got = diagnose_lift.diagnose_case(lift_case([it("Wood Chair", "like")] * 6, r2, [WOOD]))
    assert got["verdict"] == "learned the right thing"
    assert got["matching"]["rate"] == 1.0 and got["contradicting"]["rate"] == 0.0


def test_a_chip_that_does_not_predict_likes_is_said_so() -> None:
    r2 = [it("Oak Wood Chair", "dislike")] * 12 + [it("Steel Chair", "like")] * 12
    got = diagnose_lift.diagnose_case(lift_case([it("Wood Chair", "like")] * 6, r2, [WOOD]))
    assert got["verdict"] == "learned something that does not predict likes"


def test_nothing_short_batches_and_ceilings() -> None:
    steel = [it("Steel Chair", "like")] * 24
    assert diagnose_lift.diagnose_case(lift_case([], steel, []))["verdict"].startswith(
        "nothing learned"
    )
    assert (
        diagnose_lift.diagnose_case(lift_case([], steel[:10], [WOOD]))["verdict"]
        == "supply limited"
    )
    ceiling = [it("Wood Chair", "like")] * 12
    got = diagnose_lift.diagnose_case(lift_case(ceiling, steel, [WOOD]))
    assert got["ceiling"] and got["verdict"].startswith("nothing learned")
    unseen = diagnose_lift.diagnose_case(
        lift_case([it("x", "dislike")] * 6, [it("Chair", "like")] * 24, [WOOD])
    )
    assert unseen["verdict"] == "learned something the titles cannot confirm"


def test_the_diagnosis_renders_a_table_from_a_results_file() -> None:
    data = {
        "cases": [
            lift_case(
                [it("Wood Chair", "like")] * 6,
                [it("Oak Wood Chair", "like")] * 12 + [it("Steel", "dislike")] * 12,
                [WOOD],
            )
        ]
    }
    text = diagnose_lift.render(data)
    assert text.startswith("| Case |") and "learned the right thing" in text and "Verdicts:" in text
