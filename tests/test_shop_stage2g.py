"""Shop Mode stage 2g: reliable and bounded candidate tagging (a forced tool call, batches of 40, one
tagging budget, one retry), only the top candidates tagged, concurrent searches, a latency budget with
a hard stop, background jobs with phases, one de-duplication, short batches explained, a short banner
and the owner's fourth session replayed offline. Time is a fake clock: nothing here sleeps.

Offline like the other Shop Mode tests (same loopback-only socket guard).
"""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.shop.candidates as candidates_mod
from app.config import Config
from app.llm import Budget
from app.mock_llm import mock_candidate_tool_input, response, text_block
from app.shop.candidates import (
    MODE_TOOL,
    TAG_TOOL_DEF,
    TagBook,
    Taggable,
    parse_answer,
    parse_tool_input,
    tag_results,
)
from app.shop.clock import FakeClock
from app.shop.dedupe import Dedupe, Listing, prices_close, title_key
from app.shop.jobs import JobError, JobManager
from app.shop.providers import ReplayPoolSearch
from app.shop.replay import REPLAY_FILE_4, seed_session
from app.shop.retailers import canonical_name, seller_key
from app.shop.search import AgainContext, build_results, run_query
from app.shop.service import ApiError, ShopService, headline
from app.shop.store import SearchRecord, SessionStore
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
from test_shop_stage2e import rows_in, tag_calls

ROOT = Path(__file__).resolve().parents[1]
PHRASE = "wood and metal coffee table with storage"


def taggables(n: int, start: int = 0) -> list[Taggable]:
    return [
        Taggable(f"r{i}", f"Round Walnut Table {i}", 100.0 + i, f"S{i}")
        for i in range(start, start + n)
    ]


class ScriptClient(RouterClient):
    """A router double whose candidate tagging answers come from `script(n, kwargs)`, which returns
    a response (or raises) and may advance a fake clock to simulate how long the call took."""

    def __init__(self, script: Any) -> None:
        super().__init__()
        self.script = script
        self.tag_n = 0
        self._lock = threading.Lock()

    def _create(self, **kwargs: Any) -> Any:
        tool = (kwargs.get("tool_choice") or {}).get("name") == "record_tags"
        rows = "output_config" in kwargs and (
            kwargs["output_config"]["format"]["schema"]["title"] == "CandidateTags"
        )
        if tool or rows:
            with self._lock:
                self.tag_n += 1
                n = self.tag_n
                self.calls.append(
                    {**kwargs, "output_config": {"format": {"schema": {"title": "CandidateTags"}}}}
                )
            return self.script(n, kwargs)
        return super()._create(**kwargs)


def user_text(kwargs: dict[str, Any]) -> str:
    content = kwargs["messages"][0]["content"]
    return content if isinstance(content, str) else str(content)


def tool_answer(kwargs: dict[str, Any]) -> Any:
    block = SimpleNamespace(
        type="tool_use",
        id="t",
        name="record_tags",
        input=mock_candidate_tool_input(user_text(kwargs)),
    )
    return response([block], stop_reason="tool_use")


# ---- the forced tool call and its fallback ------------------------------------------------------------------


def test_the_tagging_call_is_a_forced_tool_call_with_a_strict_schema_and_no_sampling_knob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    real = candidates_mod.complete

    def spy(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(candidates_mod, "complete", spy)
    use_client(monkeypatch, RouterClient())
    book = TagBook()
    tag_results(taggables(5), book, Budget(config()), workers=1)
    assert seen["tool_choice"] == {"type": "tool", "name": "record_tags"}
    assert seen["tools"] == [TAG_TOOL_DEF] and "temperature" not in seen  # the SDK has none
    assert "effort" not in seen and "thinking" not in seen  # no extended thinking, no effort knob
    assert seen["timeout_s"] == config().shop_tag_call_timeout_s == 12.0
    assert seen["stream"] is True and seen["max_tokens"] == candidates_mod.tag_max_tokens(
        5, MODE_TOOL
    )
    schema = TAG_TOOL_DEF["input_schema"]
    item = schema["properties"]["items"]["items"]
    assert TAG_TOOL_DEF["strict"] is True and schema["additionalProperties"] is False
    assert item["required"] == [
        "index",
        "shape",
        "material",
        "finish",
        "frame_color",
        "style",
        "color",
    ]
    assert item["properties"]["style"] == {"type": "array", "items": {"type": "string"}}
    assert item["additionalProperties"] is False
    assert book.tagged == 5 and book.log[0]["mode"] == MODE_TOOL
    assert book.log[0]["block_types"] == ["tool_use"] and book.log[0]["stop_reason"] == "tool_use"


def test_tool_input_is_parsed_and_folded_into_the_vocabularies() -> None:
    data = {
        "items": [
            {"index": 1, "shape": "Round", "material": "walnut", "finish": "brown wood",
             "frame_color": "metal base", "style": ["industrial", "glam", "modern"]},
            {"index": "2", "shape": None, "material": None, "finish": None, "frame_color": None, "style": []},
            {"index": 3, "shape": "kidney"},  # missing fields are unknown
            {"index": 9, "shape": "round"},  # outside the batch
            {"index": 1, "shape": "oval"},  # a repeat
            "junk",
            {"index": "x"},
        ]
    }  # fmt: skip
    got = parse_tool_input(data, 4)
    assert sorted(got) == [1, 2, 3]
    assert got[1] == {
        "shape": "round", "material": "wood", "color_finish": "warm wood",
        "frame_color": "other", "style": "industrial,glam", "color": None,
    }  # fmt: skip
    assert (
        got[2] == dict.fromkeys(got[2]) and got[3]["shape"] == "other" and got[3]["style"] is None
    )
    assert parse_tool_input({"items": "nope"}, 3) == {} and parse_tool_input({}, 3) == {}


def test_the_text_rows_format_still_reads_older_answers_and_tool_json_in_text() -> None:
    rows, ok = parse_answer(json.dumps({"rows": ["1|round|wood|warm wood|black|modern"]}), 2)
    assert ok and rows[1]["shape"] == "round"
    as_items = parse_answer(json.dumps({"items": [{"index": 2, "shape": "oval"}]}), 2)
    assert as_items[1] is True and as_items[0][2]["shape"] == "oval"
    fenced = parse_answer("```json\n1|round|wood\n```", 2)
    assert fenced[0][1]["material"] == "wood"


def test_a_text_answer_to_the_tool_call_is_read_by_the_fallback_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def script(n: int, kwargs: dict[str, Any]) -> Any:
        return response(
            [text_block({"rows": ["1|round|wood|warm wood|black|modern", "2|-|-|-|-|-"]})]
        )

    use_client(monkeypatch, ScriptClient(script))
    book = TagBook()
    tag_results(taggables(2), book, Budget(config()), workers=1)
    assert book.tagged == 2 and not book.failed and book.log[0]["json_ok"] is True


# ---- batches of 40, concurrency, one failing batch ----------------------------------------------------------------


def test_titles_go_in_batches_of_forty_and_run_at_the_same_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(3, timeout=10)  # three calls must be in flight together

    def script(n: int, kwargs: dict[str, Any]) -> Any:
        barrier.wait()
        return tool_answer(kwargs)

    client = ScriptClient(script)
    use_client(monkeypatch, client)
    book = TagBook()
    tag_results(taggables(100), book, Budget(config()), workers=5)
    assert sorted(len(rows_in(c)) for c in tag_calls(client)) == [20, 40, 40]
    assert book.tagged == 100 and book.calls == 3 and not book.failed


def test_one_failing_batch_does_not_stop_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    def script(n: int, kwargs: dict[str, Any]) -> Any:
        text = user_text(kwargs)
        if "Round Walnut Table 45 |" in text:  # the second batch (40 to 79) always fails
            return response([], stop_reason="end_turn")
        return tool_answer(kwargs)

    client = ScriptClient(script)
    use_client(monkeypatch, client)
    book = TagBook()
    tag_results(taggables(100), book, Budget(config()), workers=3)
    assert book.tagged == 60 and book.failed and book.requested == 100
    assert "no text content" in book.reason
    assert all(book.get(f"r{i}") is not None for i in list(range(40)) + list(range(80, 100)))
    assert all(book.get(f"r{i}") is None for i in range(40, 80))


# ---- the per-call timeout, the tagging budget, the single retry ----------------------------------------------------------------


def test_a_call_with_no_text_content_in_under_two_seconds_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()

    def script(n: int, kwargs: dict[str, Any]) -> Any:
        clock.advance(0.4)
        return response([], stop_reason="end_turn")

    client = ScriptClient(script)
    use_client(monkeypatch, client)
    book = TagBook()
    tag_results(taggables(10), book, Budget(config()), clock, workers=1)
    assert book.calls == 1 and book.tagged == 0 and book.failed  # no retry: it would fail again
    entry = book.log[0]
    assert "no text content" in entry["error"] and entry["seconds"] == pytest.approx(0.4)
    assert entry["stop_reason"] == "end_turn" and entry["block_types"] == []
    assert "stop reason end_turn" in book.reason and "blocks: none" in book.reason


def test_a_slow_no_text_call_is_retried_once_as_the_same_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()

    def script(n: int, kwargs: dict[str, Any]) -> Any:
        if n == 1:
            clock.advance(5.0)
            return response([], stop_reason="end_turn")
        return response([text_block({"rows": [f"{i}|round|wood|-|-|-" for i in range(1, 11)]})])

    client = ScriptClient(script)
    use_client(monkeypatch, client)
    book = TagBook()
    tag_results(taggables(10), book, Budget(config()), clock, workers=1)
    assert book.calls == 3 and book.tagged == 10 and not book.failed
    assert [e["mode"] for e in book.log] == [MODE_TOOL] * 3  # never another format
    assert [e["retry"] for e in book.log] == [False, True, True]
    assert [e["items"] for e in book.log] == [10, 5, 5]  # a smaller batch size
    assert all("tool_choice" in c for c in client.calls[1:])


def test_there_is_at_most_one_retry_per_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()

    def script(n: int, kwargs: dict[str, Any]) -> Any:
        clock.advance(3.0)
        return response([text_block('{"rows": ["1|rou')], stop_reason="max_tokens")

    client = ScriptClient(script)
    use_client(monkeypatch, client)
    book = TagBook()
    tag_results(taggables(40), book, Budget(config()), clock, workers=1)
    assert book.calls == 3  # the call and ONE retry round (two pieces of 20): no second round
    assert book.failed and "cut off at the" in book.reason and "after 0 of 20 rows" in book.reason
    assert book.failed_usd > 0 and book.usd >= book.failed_usd  # a failed call still costs


def test_the_tagging_budget_ends_the_work_and_the_rest_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()

    def script(n: int, kwargs: dict[str, Any]) -> Any:
        clock.advance(6.0)  # each call takes 6 s of the 15 s budget
        return tool_answer(kwargs)

    client = ScriptClient(script)
    use_client(monkeypatch, client)
    book = TagBook()
    tag_results(taggables(200), book, Budget(config()), clock, workers=1)  # five batches of 40
    # stage 2r: a call needs at least MIN_TAG_CALL_TIMEOUT_S (8 s) left, so the call at 12 s (3 s of
    # budget left) is not started: calls at 0 and 6 s only
    assert book.calls == 2 and book.tagged == 80
    assert book.skipped == 120 and all(book.get(f"r{i}") is None for i in range(80, 200))
    assert clock.now() - 1000.0 == pytest.approx(12.0)  # two calls, then it stopped


def test_the_hard_stop_shortens_the_tagging_deadline_and_the_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    seen: list[float] = []
    real = candidates_mod.complete

    def spy(**kwargs: Any) -> Any:
        seen.append(kwargs["timeout_s"])
        return real(**kwargs)

    monkeypatch.setattr(candidates_mod, "complete", spy)
    use_client(monkeypatch, RouterClient())
    tag_results(
        taggables(5), TagBook(), Budget(config()), clock, workers=1, until=clock.now() + 9.5
    )
    assert seen == [9.5]  # never longer than the time left (12 s per call otherwise)
    short = TagBook()  # stage 2r: under the 8 s minimum nothing is started (it would only time out)
    tag_results(taggables(5), short, Budget(config()), clock, workers=1, until=clock.now() + 4.5)
    assert short.calls == 0 and short.skipped == 5 and seen == [9.5]
    late = TagBook()
    tag_results(taggables(5), late, Budget(config()), clock, workers=1, until=clock.now() - 1)
    assert late.calls == 0 and late.skipped == 5  # already past the stop: nothing is sent


# ---- tag only what matters --------------------------------------------------------------------------------------


def pool_candidates(n: int, prefix: str = "p") -> list[SearchCandidate]:
    return [
        cand(
            f"{prefix}{i}",
            100.0 + i,
            title=f"Rectangular Wood Coffee Table {prefix}{i}",
            seller=f"{prefix}S{i}",
        )
        for i in range(n)
    ]


def run_pool(
    monkeypatch: pytest.MonkeyPatch,
    pool: list[SearchCandidate],
    cfg: Config | None = None,
    **kw: Any,
) -> tuple[Any, ScriptClient]:
    client = ScriptClient(lambda n, kwargs: tool_answer(kwargs))
    use_client(monkeypatch, client)
    search = FakeSearch([])
    search.sequence = [pool] + [[] for _ in range(8)]
    cfg = cfg or config()
    again = AgainContext(max_calls=1, max_calls_total=1, tag_book=TagBook(), **kw)
    outcome = run_query(
        query(request="coffee tables", search_text="coffee table", category="coffee table"),
        search, cfg, Budget(cfg), [], time.perf_counter(), again,
    )  # fmt: skip
    return outcome, client


def test_only_the_top_sixty_candidates_by_rank_are_tagged(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome, client = run_pool(monkeypatch, pool_candidates(80))
    sent = [r.split(" | ")[1] for c in tag_calls(client) for r in rows_in(c)]
    assert len(sent) == 60 == len(set(sent))  # not the whole pool of 80
    ranked = [
        f"Rectangular Wood Coffee Table p{i}" for i in range(80)
    ]  # the mock ranker keeps order
    assert sorted(sent) == sorted(ranked[:60])
    comp = outcome.composition["pool_before"]
    assert comp["size"] == 80 and comp["tagged"] == 60 and comp["keyword_tagged"] == 20
    assert comp["tag_top_n"] == 60
    assert (
        outcome.composition["batch"]["tag_sources"]["model"] == 24
    )  # the batch came from the top 60
    cheap = run_pool(monkeypatch, pool_candidates(80), config(shop_tag_top_n=10))[1]
    assert len([r for c in tag_calls(cheap) for r in rows_in(c)]) == 10  # APP_SHOP_TAG_TOP_N
    none = run_pool(monkeypatch, pool_candidates(80), config(shop_tag_top_n=0))[1]
    assert tag_calls(none) == []


def test_the_keyword_reader_stands_in_for_candidates_the_model_did_not_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = pool_candidates(30) + [
        cand(f"q{i}", 90.0 + i, title=f"Round Oak Coffee Table q{i}", seller=f"qS{i}")
        for i in range(30)
    ]
    # the first 24 ranked candidates are rectangular; the guard (60%) must see the round ones too
    cfg = config(shop_tag_top_n=0)
    outcome, _ = run_pool(monkeypatch, pool, cfg, guard_caps={"shape": 0.6})
    shapes = outcome.composition["batch"]["shape"]
    assert max(shapes.values()) <= 14  # the guard acted on keyword tags alone
    assert outcome.composition["batch"]["tag_sources"] == {"model": 0, "keyword": 24}


# ---- concurrent searches, the nine-call cap -------------------------------------------------------------------------------


class ConcurrentSearch:
    """A thread safe fake that proves its calls overlap: each waits for the others at a barrier."""

    name = "serper"
    thread_safe = True

    def __init__(self, parties: int) -> None:
        self.barrier = threading.Barrier(parties, timeout=10) if parties > 1 else None
        self.calls: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def search(
        self, query: str, gl: str = "us", hl: str = "en", page: int = 1
    ) -> list[SearchCandidate]:
        with self._lock:
            self.calls.append((query, page))
        if self.barrier is not None and page == 1 and len(self.calls) <= self.barrier.parties:
            self.barrier.wait()
        tag = "".join(c for c in query if c.isalnum())[:12]
        return [
            cand(
                f"{tag}{page}-{i}",
                50.0 + i,
                title=f"Rectangular Wood Table {tag} {page} {i}",
                seller=f"{tag}S{i}",
            )
            for i in range(10)
        ]


def run_concurrent(
    monkeypatch: pytest.MonkeyPatch,
    search: Any,
    total: int,
    extra: int = 2,
    peers: int = 3,
    **kw: Any,
) -> Any:
    use_client(monkeypatch, RouterClient())
    cfg = config(
        shop_again_max_serper_calls=4,
        shop_again_max_serper_calls_total=total,
        shop_search_workers=5,
    )
    again = AgainContext(
        max_calls=4,
        max_calls_total=total,
        tag_book=TagBook(),
        extra_queries=[f"phrasing {n}" for n in range(extra)],
        peer_queries=[(f"peer{n}", f"Peer{n}", f"Peer{n} coffee table") for n in range(peers)],
        **kw,
    )
    return run_query(
        query(request="coffee tables", search_text="coffee table", category="coffee table"),
        search, cfg, Budget(cfg), [], time.perf_counter(), again,
    )  # fmt: skip


def test_searches_run_at_the_same_time(monkeypatch: pytest.MonkeyPatch) -> None:
    search = ConcurrentSearch(parties=5)  # three phrasings and two peers, all in flight together
    outcome = run_concurrent(monkeypatch, search, total=9, extra=2, peers=2)
    assert outcome.serper_calls >= 5 and outcome.peer_log and len(outcome.peer_log) == 2
    assert {q for q, _ in search.calls[:5]} == {
        "coffee table", "phrasing 0", "phrasing 1", "Peer0 coffee table", "Peer1 coffee table",
    }  # fmt: skip


@pytest.mark.parametrize("total", [4, 5, 7, 9])
def test_concurrent_searches_respect_the_call_cap(
    monkeypatch: pytest.MonkeyPatch, total: int
) -> None:
    search = ConcurrentSearch(parties=1)
    outcome = run_concurrent(monkeypatch, search, total=total, extra=3, peers=3)
    assert len(search.calls) <= total and outcome.serper_calls <= total
    assert outcome.serper_credits == 2 * outcome.serper_calls
    assert len(search.calls) == outcome.serper_calls


def test_a_failing_peer_search_does_not_fail_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.shopping import ProviderError

    class Flaky(ConcurrentSearch):
        def search(self, query: str, gl: str = "us", hl: str = "en", page: int = 1) -> Any:
            if query.startswith("Peer1"):
                raise ProviderError("serper", "boom")
            return super().search(query, gl, hl, page)

    outcome = run_concurrent(monkeypatch, Flaky(parties=1), total=9, extra=0, peers=3)
    assert [p["error"] for p in outcome.peer_log] == ["", "search failed", ""]
    assert any("Search for Peer1 unavailable" in w for w in outcome.warnings)
    assert outcome.results


def test_a_failing_first_search_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.shopping import ProviderError

    class Down(ConcurrentSearch):
        def search(self, query: str, gl: str = "us", hl: str = "en", page: int = 1) -> Any:
            raise ProviderError("serper", "HTTP 401")

    with pytest.raises(ProviderError):
        run_concurrent(monkeypatch, Down(parties=1), total=9, extra=1, peers=1)


# ---- the latency budget -----------------------------------------------------------------------------------------


class ClockedPool(ReplayPoolSearch):
    """The recorded pool, each search taking `seconds` of fake time."""

    def __init__(self, replay: str, clock: FakeClock, seconds: float) -> None:
        super().__init__(replay)
        self.clock, self.seconds = clock, seconds

    def search(self, query: str, gl: str = "us", hl: str = "en", page: int = 1) -> Any:
        self.clock.advance(self.seconds)
        return super().search(query, gl, hl, page)


def replay4(
    tmp_path: Path,
    clock: FakeClock | None = None,
    seconds: float = 0.0,
    cfg: Config | None = None,
    **kw: Any,
) -> tuple[ShopService, str, ReplayPoolSearch, FakeClock]:
    clock = clock or FakeClock()
    cfg = cfg or config()
    search = ClockedPool(REPLAY_FILE_4, clock, seconds)
    store = SessionStore(tmp_path / "shop-data")
    svc = ShopService(cfg, store, search, FakeLookup(), clock=clock)
    session = seed_session(store, replay=REPLAY_FILE_4, **kw)
    return svc, session.session_id, search, clock


def test_phases_are_reported_in_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.shop.again import run_shop_again

    svc, sid, search, clock = replay4(tmp_path)
    use_client(monkeypatch, RouterClient())
    phases: list[str] = []
    run_shop_again(svc.store.load(sid), search, svc.config, None, clock, phases.append)
    assert phases[0] == "Reading your ratings"
    assert phases[1].startswith("Searching")
    assert phases.index("Ranking") < phases.index("Tagging results")
    assert phases[-1] == "Tagging results"


def test_a_slow_shop_again_stops_at_forty_five_seconds_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, sid, _, clock = replay4(tmp_path, seconds=9.0)  # every search takes 9 s of fake time
    use_client(monkeypatch, RouterClient())
    start = clock.now()
    view = svc.shop_again(sid)
    elapsed = clock.now() - start
    found = view["search"]
    assert elapsed < 80 and len(found["results"]) > 0  # finished, with what existed
    assert any("Stopped at the 45-second limit before" in w for w in found["warnings"])
    timings = found["trace"]["batch"]["timings"]
    assert (
        timings["stopped_before"] and timings["hard_stop_s"] == 45.0 and timings["target_s"] == 30.0
    )
    assert "Stopped early before" in found["headline"]


def test_failing_tagging_still_finishes_inside_the_budget_with_a_fake_clock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, sid, _, clock = replay4(tmp_path, seconds=0.5)
    state = {"calls": 0}

    def script(n: int, kwargs: dict[str, Any]) -> Any:
        state["calls"] += 1
        clock.advance(1.5)  # a failure that takes 1.5 s: under the 2 s no-retry line
        return response([], stop_reason="end_turn")

    use_client(monkeypatch, ScriptClient(script))
    start = clock.now()
    view = svc.shop_again(sid)
    elapsed = clock.now() - start
    found = view["search"]
    assert elapsed <= 45.0, elapsed  # inside the hard stop, with every tagging call failing
    assert len(found["results"]) == 24 and found["tagging"]["tag_reason"]
    assert found["tagging"]["tag_failed_usd"] > 0  # the failed calls are in the cost
    assert any("Candidate tagging failed:" in w for w in found["warnings"])
    assert "Candidate tagging failed:" in found["headline"]
    assert found["trace"]["tag_calls"] and all(
        "no text content" in c["error"] for c in found["trace"]["tag_calls"]
    )


def test_slow_failing_tagging_is_bounded_by_the_tagging_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # one worker: calls run one after the other, so the shared fake clock is true elapsed time
    svc, sid, _, clock = replay4(
        tmp_path, seconds=0.5, cfg=config(shop_search_workers=1, shop_tag_workers=1)
    )

    def script(n: int, kwargs: dict[str, Any]) -> Any:
        clock.advance(10.0)  # each failure is slow (a timeout): the retry rule applies
        return response([], stop_reason="end_turn")

    client = ScriptClient(script)
    use_client(monkeypatch, client)
    start = clock.now()
    view = svc.shop_again(sid)
    assert clock.now() - start <= 45.0 + 10.0  # the hard stop, plus the one call in flight
    assert client.tag_n <= 4  # a call and one retry for a batch, then the budget is spent
    assert len(view["search"]["results"]) > 0


# ---- background jobs ---------------------------------------------------------------------------------------------


def test_a_job_reports_its_phase_while_running_and_its_result_when_done() -> None:
    manager = JobManager(FakeClock())
    gate, reached = threading.Event(), threading.Event()

    def work(say: Any) -> dict[str, Any]:
        say("Tagging results")
        reached.set()
        gate.wait(10)
        say("Ranking")
        return {"session_id": "s1", "search": {"results": []}}

    job = manager.start("s1", "shop_again", work, lambda e: (500, "x"))
    reached.wait(10)
    running = manager.status(job, "s1")
    assert running["state"] == "running" and running["phase"] == "Tagging results"
    assert running["kind"] == "shop_again" and [s["id"] for s in running["steps"]] == [
        "ratings",
        "searching",
        "tagging",
        "ranking",
    ]
    gate.set()
    manager.wait(job, 10)
    done = manager.status(job, "s1")
    assert done["state"] == "done" and done["result"]["session_id"] == "s1"


def test_job_ids_are_unguessable_and_bound_to_their_session() -> None:
    manager = JobManager(FakeClock())
    ids = {manager.start(f"s{i}", "search", lambda say: {}, lambda e: (500, "x")) for i in range(5)}
    for jid in ids:
        manager.wait(jid, 10)
    assert len(ids) == 5 and all(len(j) >= 22 for j in ids)  # 128 random bits
    job = next(iter(ids))
    with pytest.raises(JobError) as wrong:
        manager.status(job, "someone-else")
    assert wrong.value.status == 404  # the same answer as an unknown job
    with pytest.raises(JobError) as none:
        manager.status(job, None)
    assert none.value.status == 404
    for bad in ("", "short", "../../etc", "a" * 100, "x" * 22):
        with pytest.raises(JobError) as exc:
            manager.status(bad, "s0")
        assert exc.value.status == 404


def test_one_job_runs_per_session_and_the_total_is_bounded() -> None:
    manager = JobManager(FakeClock())
    gate = threading.Event()
    held = manager.start("s1", "search", lambda say: gate.wait(10) and {}, lambda e: (500, "x"))
    with pytest.raises(JobError) as busy:
        manager.start("s1", "shop_again", lambda say: {}, lambda e: (500, "x"))
    assert busy.value.status == 409
    other = manager.start("s2", "search", lambda say: {}, lambda e: (500, "x"))
    manager.wait(other, 10)
    gates = [threading.Event() for _ in range(8)]
    started = [
        manager.start(f"t{i}", "search", lambda say, g=g: g.wait(10) and {}, lambda e: (500, "x"))
        for i, g in enumerate(gates[:6])
    ]  # 1 held + 6 running = 7 of 8
    manager.start("t9", "search", lambda say: gates[6].wait(10) and {}, lambda e: (500, "x"))
    with pytest.raises(JobError) as many:
        manager.start("t10", "search", lambda say: {}, lambda e: (500, "x"))
    assert many.value.status == 429
    gate.set()
    for g in gates:
        g.set()
    for jid in [held, *started]:
        manager.wait(jid, 10)


def test_a_failed_job_reports_a_safe_error_and_finished_jobs_expire() -> None:
    clock = FakeClock()
    manager = JobManager(clock, ttl_s=600)

    def failure(exc: Exception) -> tuple[int, str]:
        return (
            (exc.status, exc.message)
            if isinstance(exc, ApiError)
            else (500, "Something went wrong")
        )

    def limited(say: Any) -> dict[str, Any]:
        raise ApiError(429, "Shop Again limit reached (10 per session)")

    def crashed(say: Any) -> dict[str, Any]:
        raise RuntimeError("secret path /Users/x and a key sk-123")

    a = manager.start("s1", "shop_again", limited, failure)
    manager.wait(a, 10)
    failed = manager.status(a, "s1")
    assert failed["state"] == "error" and failed["kind"] == "shop_again" and failed["status"] == 429
    assert failed["error"] == "Shop Again limit reached (10 per session)"
    assert "steps" in failed and "elapsed_s" in failed
    b = manager.start("s2", "search", crashed, failure)
    manager.wait(b, 10)
    state = manager.status(b, "s2")
    assert (
        state["status"] == 500
        and "secret" not in json.dumps(state)
        and "sk-123" not in json.dumps(state)
    )
    clock.advance(599)
    assert manager.status(a, "s1")["state"] == "error"  # still there
    clock.advance(2)
    with pytest.raises(JobError) as gone:
        manager.status(a, "s1")  # expired
    assert gone.value.status == 404


def test_the_service_starts_jobs_and_the_server_answers_202_then_the_view(tmp_path: Path) -> None:
    from app.shop.server import CSP, ShopApp

    svc, sid, _, _ = replay4(tmp_path)
    app = ShopApp(svc, port=8765)
    hdrs = {"Host": "127.0.0.1:8765", "Content-Type": "application/json"}
    body = json.dumps({"session_id": sid}).encode()
    started = app.handle("POST", "/api/shop-again", hdrs, body)
    assert started.status == 202 and started.all_headers()["Content-Security-Policy"] == CSP
    job = json.loads(started.body)["job_id"]
    svc.jobs.wait(job, 30)
    wrong = app.handle(
        "GET", f"/api/job/{job}?session=zzzzzzzzzzzzzzzz", {"Host": "127.0.0.1:8765"}
    )
    assert wrong.status == 404
    right = app.handle("GET", f"/api/job/{job}?session={sid}", {"Host": "127.0.0.1:8765"})
    state = json.loads(right.body)
    assert right.status == 200 and state["state"] == "done" and state["result"]["search"]["results"]
    assert app.handle("GET", f"/api/job/{job}", {"Host": "evil.example"}).status == 403
    assert app.handle("GET", "/api/job/not-a-job-id", {"Host": "127.0.0.1:8765"}).status == 404
    assert app.handle("POST", "/api/job/x", hdrs, b"{}").status == 404  # POST is not a job route
    # a typed search starts a job too, and a new session is created when it finishes
    first = app.handle(
        "POST", "/api/search", hdrs, json.dumps({"request": "boho curtains"}).encode()
    )
    assert first.status == 202
    job2 = json.loads(first.body)["job_id"]
    svc.jobs.wait(job2, 30)
    done = json.loads(app.handle("GET", f"/api/job/{job2}", {"Host": "127.0.0.1:8765"}).body)
    assert done["state"] == "done" and done["result"]["session_id"]


def test_limits_are_still_enforced_before_a_job_starts(tmp_path: Path) -> None:
    svc, sid, _, _ = replay4(tmp_path)
    for _ in range(svc.config.shop_agains_per_session):
        session = svc.store.load(sid)
        session.shop_agains_used += 1
        svc.store.save(session)
    with pytest.raises(ApiError) as exc:
        svc.start_shop_again(sid)
    assert exc.value.status == 429
    with pytest.raises(ApiError) as empty:
        svc.start_search(None, "   ")
    assert empty.value.status == 400
    with pytest.raises(ApiError) as nosession:
        svc.start_shop_again("a" * 20)
    assert nosession.value.status == 404


# ---- one de-duplication ---------------------------------------------------------------------------------------


def listing(
    pid: str,
    title: str,
    seller: str = "Wayfair",
    price: float | None = 100.0,
    thumb: str = "",
    link: bool = True,
    rated: bool = False,
) -> Listing:
    return Listing(pid, title, seller, price, thumb, link, rated)


def run_dedupe(items: list[Listing], seen: list[Listing] | None = None) -> tuple[list[str], Any]:
    kept, stats = Dedupe(lambda x: x).run(items, seen or [])
    return [x.product_id for x in kept], stats


def test_the_same_product_id_is_one_listing() -> None:
    kept, stats = run_dedupe(
        [listing("a", "Oak Table"), listing("a", "Another title", "Target", 5.0)]
    )
    assert kept == ["a"] and stats.removed["same_product_id"] == 1


def test_the_same_thumbnail_is_one_listing() -> None:
    kept, stats = run_dedupe(
        [
            listing("a", "Oak Table", thumb="https://i/1.jpg"),
            listing("b", "Walnut Desk", "Target", 55.0, "https://i/1.jpg"),
        ]
    )
    assert kept == ["b"] and stats.removed["same_thumbnail"] == 1  # b: the lower price is kept
    assert run_dedupe(
        [listing("a", "Oak Table", thumb=""), listing("b", "Walnut Desk", "Target", 55.0, "")]
    )[0] == ["a", "b"]


def test_the_same_normalized_title_and_a_price_within_one_percent_is_one_listing_whatever_the_seller() -> (
    None
):
    items = [
        listing("a", 'Davison Walnut Coffee Table 40" Black', "CB2", 1099.0),
        listing("b", "DAVISON  WALNUT COFFEE TABLE, 36 in.", "Wayfair", 1105.0),  # 0.5% apart
        listing("c", "Davison Walnut Coffee Table", "Etsy", 1110.0),  # 1.0% apart from a: still
        listing("d", "Davison Walnut Coffee Table", "Target", 1200.0),  # 9% apart: another listing
    ]
    kept, stats = run_dedupe(items)
    assert kept == ["a", "d"] and stats.removed["same_title_and_price"] == 2
    assert prices_close(100.0, 101.0) and not prices_close(100.0, 101.5)
    assert not prices_close(None, 100.0) and not prices_close(0.0, 0.0)


def test_the_same_normalized_title_from_the_same_seller_is_one_listing() -> None:
    items = [
        listing("a", "Oak Table 40in", "West Elm", 500.0),
        listing("b", "Oak Table", "west elm", 800.0),  # another price, the same seller
        listing("c", "Oak Table", "Wayfair", 800.0),  # another seller and price: kept
    ]
    kept, stats = run_dedupe(items)
    assert kept == ["a", "c"] and stats.removed["same_title_and_seller"] == 1


def test_titles_lose_sizes_and_plain_colours_but_keep_the_brand_and_the_finish() -> None:
    assert (
        title_key('Article Oak Lift-top Round Plinth Coffee Table 36"')
        == "article oak lift top round plinth coffee table"
    )
    assert (
        title_key("West Elm Volume 36 in. Storage Coffee Table (Black)")
        == "west elm volume storage coffee table"
    )
    assert title_key("Coffee Table 47.5 x 24 x 18 White") == "coffee table"
    assert title_key("Walnut Coffee Table") != title_key(
        "Oak Coffee Table"
    )  # a finish is a product
    assert title_key("Brand A Coffee Table") != title_key("Brand B Coffee Table")  # the brand stays
    assert title_key("  ") == ""


def test_the_best_listing_of_a_group_is_kept() -> None:
    items = [
        listing("a", "Oak Table", "Wayfair", 117.5, link=False),
        listing("b", "Oak Table", "Target", 118.0, link=True),  # the one with a retailer link...
        listing("c", "Oak Table", "IKEA", 117.0, link=True),  # ...and then the lowest price
        listing("d", "Oak Table", "Etsy", 117.0, link=True),  # a tie: the earlier one
    ]
    kept, stats = run_dedupe(items)
    assert kept == ["c"] and stats.duplicates == 3
    assert run_dedupe(
        [listing("a", "Oak Table", price=None), listing("b", "Oak Table", "wayfair.com", 300.0)]
    )[0] == ["b"]


def test_what_was_shown_or_rated_before_always_wins_and_is_counted_separately() -> None:
    seen = [
        listing("r1", "Rated Table", "West Elm", 699.0, rated=True),
        listing("s1", "Shown Table", "Wayfair", 250.0),
    ]
    items = [
        listing("n1", "Rated Table", "Home Depot", 699.0),  # a copy of a rated item, other seller
        listing("n2", "Shown Table", "Wayfair", 250.0),  # the same title and seller as a shown item
        listing("s1", "Brand new title", "IKEA", 10.0),  # the same product id as a shown item
        listing("n3", "A New Table", "Target", 90.0),
    ]
    kept, stats = run_dedupe(items, seen)
    assert kept == ["n3"]
    assert stats.rated_before == 1 and stats.shown_before == 2 and stats.duplicates == 0
    out = stats.as_dict()
    assert (
        out["candidates"] == 4
        and out["kept"] == 1
        and out["by_rule"] == dict.fromkeys(out["by_rule"], 0)
    )  # type: ignore[call-overload]


def test_the_original_search_order_is_kept() -> None:
    items = [listing(f"i{n}", f"Table {n}", price=float(100 + 10 * n)) for n in range(6)]
    assert run_dedupe(items[::-1])[0] == [f"i{n}" for n in range(5, -1, -1)]


def test_the_final_batch_never_shows_the_same_product_twice() -> None:
    pool = [
        (cand("a", 100.0, title="Oak Table 40in", seller="Wayfair"), ""),
        (
            cand("b", 100.5, title="OAK TABLE 36 in", seller="Target"),
            "",
        ),  # a duplicate that got through
        (cand("c", 100.0, title="Walnut Table", seller="Etsy"), ""),
    ]
    index = {c.product_id: n for n, (c, _) in enumerate(pool, 1)}
    results, _, _ = build_results(pool, query(request="t"), index)
    assert [r.product_id for r in results] == ["a", "c"]


def test_dedupe_is_used_for_typed_searches_and_counts_are_in_the_filter_counts() -> None:
    from app.shop.search import apply_filters

    items = [cand("a", 100.0, title="Oak Desk"), cand("b", 100.0, title="Oak Desk", seller="Other")]
    pool, counts = apply_filters(items, query())
    assert [c.product_id for c in pool] == ["a"] and counts.duplicates == 1
    assert (
        counts.dedupe["by_rule"]["same_title_and_price"] == 1 and counts.dedupe["candidates"] == 2
    )


def test_the_live_session_seller_spellings_fold_into_one_seller() -> None:
    assert seller_key("west elm") == seller_key("West Elm") == "westelm"
    assert (
        seller_key("Room&Board")
        == seller_key("Room & Board")
        == seller_key("room and board")
        == "roomandboard"
    )
    assert seller_key("Joybird Furniture") == seller_key("Joybird") == "joybird"
    assert (
        seller_key("Walmart - JerFit Life")
        == seller_key("Walmart.com")
        == seller_key("walmart")
        == "walmart"
    )
    assert (
        seller_key("crateandbarrel.com")
        == seller_key("Crate and Barrel")
        == seller_key("Crate & Barrel")
        == "crateandbarrel"
    )
    assert seller_key("The Home Depot") == seller_key("homedepot.com") == "homedepot"
    assert seller_key("Hartwell & Co") == "hartwellandco"  # a real "Co" is not a suffix to strip
    assert seller_key("") == "?" and seller_key("!!!") == "?"
    assert (
        canonical_name("Joybird Furniture") == "Joybird"
        and canonical_name("Room&Board") == "Room & Board"
    )
    assert canonical_name("Small Shop Furniture") == "Small Shop"
    # pins, caps and the retailers table all use this one function
    from app.shop import decide, prefs, retailers, search, store

    for module in (decide, prefs, search, store):
        assert module.seller_key is retailers.seller_key  # type: ignore[attr-defined]


# ---- fewer than 24 results ---------------------------------------------------------------------------------------


def test_a_short_batch_is_explained_and_one_refill_search_is_tried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_client(monkeypatch, RouterClient())
    search = FakeSearch([])
    first = pool_candidates(8)
    search.sequence = [first, pool_candidates(6, "r")]
    cfg = config()
    again = AgainContext(
        converged={"shape": "rectangular"},
        max_calls=1,
        max_calls_total=5,
        tag_book=TagBook(),
        seen=[Listing("old1", "Old Table", "S", 10.0)],
    )
    outcome = run_query(
        query(request="coffee tables", search_text="coffee table", category="coffee table"),
        search, cfg, Budget(cfg), [], time.perf_counter(), again,
    )  # fmt: skip
    short = outcome.composition["shortfall"]
    assert short["wanted"] == 24 and short["got"] == len(outcome.results) == 14
    assert short["explanation"] == "0 seen or rated, 0 duplicates, 0 filtered out"
    assert any(
        w.startswith("Only 14 new results found: 0 seen or rated, 0 duplicates, 0 filtered out.")
        for w in outcome.warnings
    )
    refill = outcome.composition["refill"]
    assert len(refill) == 1 and refill[0]["value"] == "rectangular" and refill[0]["found"] == 6
    assert refill[0]["before"] == 8 and refill[0]["after"] == 14
    assert [q for q, _ in search.calls] == ["coffee table", "rectangular coffee table"]
    assert outcome.serper_calls == 2


def test_no_refill_when_the_call_cap_is_used_up_or_there_is_nothing_to_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_client(monkeypatch, RouterClient())
    cfg = config()
    for again in (
        AgainContext(
            open_dims={"shape": ["rectangular", "round"]},
            max_calls=1,
            max_calls_total=1,
            tag_book=TagBook(),
        ),
        AgainContext(
            max_calls=1, max_calls_total=5, tag_book=TagBook()
        ),  # nothing converged or open
    ):
        search = FakeSearch([])
        search.sequence = [pool_candidates(8), pool_candidates(6, "r")]
        outcome = run_query(
            query(request="coffee tables", search_text="coffee table", category="coffee table"),
            search, cfg, Budget(cfg), [], time.perf_counter(), again,
        )  # fmt: skip
        assert outcome.composition["refill"] == [] and len(search.calls) == 1


def test_a_seen_and_duplicate_heavy_session_says_how_many_of_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, sid, _, _ = replay4(tmp_path)
    use_client(monkeypatch, RouterClient())
    view = svc.shop_again(sid)
    short = view["search"]["trace"]["batch"]["shortfall"]
    dd = view["search"]["trace"]["batch"]["dedupe"]
    assert dd["rated_before"] + dd["shown_before"] == short["seen_or_rated"] > 0
    assert dd["duplicates"] == short["duplicates"] > 0
    assert (
        dd["by_rule"]["same_product_id"] > 0
        and dd["candidates"] == dd["kept"] + dd["duplicates"] + short["seen_or_rated"]
    )


# ---- the banner ----------------------------------------------------------------------------------------


def record(
    n: int,
    trace: dict[str, Any] | None = None,
    tagging: dict[str, Any] | None = None,
    kind: str = "shop_again",
) -> SearchRecord:
    from app.shop.models import FilterCounts, ShopQuery

    return SearchRecord(
        index=1, request="x", query=ShopQuery(request="x", search_text="x", category=""),
        created_at="t", result_ids=[f"r{i}" for i in range(n)], serper_calls=1, seconds={},
        anthropic_usd=0.0, provider_usd=0.0, total_usd=0.0, counts=FilterCounts(), ranked=True,
        kind=kind, trace=trace or {}, tagging=tagging or {},
    )  # fmt: skip


def test_the_headline_is_one_short_sentence() -> None:
    assert headline(record(24)) == "24 new results."
    assert headline(record(0, kind="search")) == ""  # a typed search keeps its own short messages
    batch = {
        "batch": {"shortfall": {"explanation": "81 seen or rated, 63 duplicates, 22 filtered out"}}
    }
    assert (
        headline(record(17, batch))
        == "Only 17 new results (81 seen or rated, 63 duplicates, 22 filtered out)."
    )
    failed = headline(
        record(
            24,
            {},
            {"tag_reason": "the answer was cut off at the 928-token limit after 0 of 24 rows"},
        )
    )
    assert (
        failed
        == "24 new results. Candidate tagging failed: the answer was cut off at the 928-token limit after 0 of 24 rows."
    )
    long = headline(record(24, {}, {"tag_reason": "x" * 400}))
    assert len(long) < 130 and long.endswith("...")  # the reason is cut: the rest is in the trace
    stopped = headline(record(24, {"batch": {"timings": {"stopped_before": "tagging"}}}))
    assert stopped == "24 new results. Stopped early before tagging."
    assert (
        headline(record(24, {}, {"tag_reason": ""})) == "24 new results."
    )  # nothing about tagging


def test_the_page_shows_the_headline_and_a_details_link_and_keeps_every_warning_in_the_panel() -> (
    None
):
    from app.shop.server import STATIC_DIR

    code = (STATIC_DIR / "app.js").read_text()
    html = (STATIC_DIR / "index.html").read_text()
    assert "s.headline" in code and "status-details" in code
    assert 'id="status-details"' in html and 'href="#why"' in html
    block = code[code.index("function renderWhy") : code.index("function removePref")]
    assert "'Warning: ' + w" in block  # every warning is listed in the panel
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in code
    assert "Stopped" in code or "stopped before" in code  # the hard stop is in the trace panel
    assert "POLL_MS" in code and "/api/job/" in code and "'Tagging results'" not in code
    assert "Dedupe" not in code and "Candidates ' + dd.candidates" in code


# ---- cost accounting ----------------------------------------------------------------------------------------


def test_failed_calls_are_counted_in_the_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    def script(n: int, kwargs: dict[str, Any]) -> Any:
        if n == 1:
            return response([], stop_reason="end_turn", input_tokens=1000, output_tokens=0)
        return tool_answer(kwargs)

    use_client(monkeypatch, ScriptClient(script))
    book = TagBook()
    budget = Budget(config())
    tag_results(taggables(80), book, budget, workers=1)  # two batches, the first fails fast
    assert book.tagged == 40 and book.failed
    assert book.failed_usd > 0 and book.usd > book.failed_usd
    assert budget.cost_usd == pytest.approx(book.usd, rel=1e-6)  # everything billed is in the total
    info = book.info()
    assert info["failed_usd"] == pytest.approx(book.failed_usd, abs=1e-6) and info["skipped"] == 0


def test_the_footer_shows_phase_timings_and_failed_cost() -> None:
    from app.shop.server import STATIC_DIR

    code = (STATIC_DIR / "app.js").read_text()
    for needle in ("searches ", "preferences ", "'rank '", "failed tagging calls", "tagging "):
        assert needle in code, needle
    assert "timingText(s)" in code


# ---- the fourth session --------------------------------------------------------------------------------------


def test_the_fourth_replay_fixture_is_the_owners_fourth_session() -> None:
    from app.shop.replay import load_replay

    data = load_replay(REPLAY_FILE_4)
    rated = data["rated"]
    assert len(rated) == 46 and [r["rating"] for r in rated].count("love") == 2
    assert [r["rating"] for r in rated].count("like") == 6 and [r["rating"] for r in rated].count(
        "dislike"
    ) == 38
    loved = [r for r in rated if r["rating"] == "love"]
    assert {r["seller"] for r in loved} == {"Article", "West Elm"} and {
        r["price"] for r in loved
    } == {699.0}
    prices = {r["title"]: r["price"] for r in rated if r["rating"] == "like"}
    assert prices["Vernon Pine Wood Rectangular Coffee Table"] == 959.0
    assert prices['Davison 40" Round Fluted Warm Oak Storage Coffee Table'] == 1099.0
    assert prices["World Market Khalani Wood and Metal Fluted Coffee Table"] == 449.99
    assert {r["seller"] for r in rated if r["rating"] == "dislike"} >= {
        "CB2",
        "Crate & Barrel",
        "Lowe's",
    }
    assert len(data["pool"]) == 265 and len({p["id"] for p in data["pool"]}) < 265  # repeated ids
    sellers = {p["seller"] for p in data["pool"]}
    assert {
        "west elm",
        "Room&Board",
        "Joybird Furniture",
        "Walmart - JerFit Life",
    } <= sellers
    assert "crateandbarrel.com" in {r["seller"] for r in data["rated"]}


def test_replay_four_decisions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    svc, sid, _, _ = replay4(tmp_path)
    use_client(monkeypatch, RouterClient())
    view = svc.shop_again(sid)
    chips = {(e["attr_kind"], e["text"]): e for e in view["profile"]["inferred"]}
    trace = view["profile"]["trace"]
    # stage 2i: 8 of 46 liked (17%). West Elm is 3 of 3 but 3 rated is under the 4 a seller needs,
    # and the premium tier is 6 of 28 (21%), under the 35% floor: no seller or tier chip
    assert not [e for e in view["profile"]["inferred"] if e["attr_kind"] in ("seller", "tier")]
    west = next(c for c in trace["seller"]["candidates"] if c["seller"] == "West Elm")
    assert west["rated"] == 3 and west["liked"] == 3 and "rated 3, needs 4" in west["verdict"]
    tier = next(c for c in trace["tier"]["candidates"] if c["label"] == "design premium")
    assert tier["rated"] == 28 and tier["liked"] == 6
    assert "like rate 21% is under 35%" in tier["verdict"]
    shape = trace["dimensions"]["shape"]
    assert (
        shape["decision"] == "open"
        and shape["like_mass"]["round"] == 8
        and shape["like_mass"]["rectangular"] == 4
    )
    assert round(shape["like_mass"]["round"] / shape["total_mass"], 2) == 0.67
    finish = trace["dimensions"]["color_finish"]
    assert (
        finish["decision"] in ("open", "converged", "none") and finish["reason"]
    )  # the trace says why
    assert finish["like_mass"]["warm wood"] == 10 and finish["top"] == "warm wood"
    assert ("price_mixed", "price: mixed") in chips


def test_replay_four_removes_the_duplicates_and_the_batch_has_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, sid, _, _ = replay4(tmp_path)
    use_client(monkeypatch, RouterClient())
    view = svc.shop_again(sid)
    dd = view["search"]["trace"]["batch"]["dedupe"]
    assert dd["duplicates"] > 0 and dd["by_rule"]["same_product_id"] > 0
    assert dd["shown_before"] > 0 and dd["rated_before"] >= 0  # excluded, counted apart
    results = view["search"]["results"]
    assert len(results) == 24
    assert len({r["product_id"] for r in results}) == 24
    assert len({r["title"] for r in results}) == 24
    from app.shop.dedupe import title_key as key

    assert len({(key(r["title"]), round(r["price"] / 5)) for r in results}) == 24
    thumbs = [r["thumbnail_url"] for r in results if r["thumbnail_url"]]
    assert len(thumbs) == len(set(thumbs))
    seen_titles = {
        r.title
        for r in svc.store.load(sid).results.values()
        if r.id not in {x["id"] for x in results}
    }
    assert not {r["title"] for r in results} & seen_titles  # nothing already shown or rated returns


# ---- the diagnostic script ------------------------------------------------------------------------------------------


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "shop_diagnose_tagging", ROOT / "scripts" / "shop_diagnose_tagging.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_diagnostic_prints_the_documented_fields_and_never_a_secret(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET-VALUE-123")
    monkeypatch.setenv("SERPER_API_KEY", "serper-SECRET-VALUE-456")
    script = load_script()
    assert script.main(["--mock", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out and "sk-ant" not in out and "serper-" not in out
    assert "Candidate tagging diagnostic" in out and "Titles from the replay-4 fixture pool" in out
    for label in ("A rows, 100 titles", "B rows, 40 titles", "C forced tool call, 40 titles"):
        assert f"--- {label}" in out
    for field in (
        "model:",
        "max_tokens:",
        "latency:",
        "outcome:",
        "block types:",
        "stop reason:",
        "usage:",
        "parsed:",
        "text:",
    ):
        assert out.count(field) == 3, field
    assert "tool_use" in out and "parsed:       40 of 40" in out
    assert "Expected cost" in script.COST_NOTE and "$0.003" in script.COST_NOTE


def test_the_diagnostic_without_a_key_says_so_without_printing_one(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    script = load_script()
    monkeypatch.setattr(script, "load_env", lambda: None)  # never read a .env file in a test
    assert script.main(["--data-dir", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY is not set" in captured.err and "Expected cost" in captured.out


def test_the_diagnostic_uses_the_newest_session_titles(tmp_path: Path) -> None:
    script = load_script()
    old, new = tmp_path / "old.json", tmp_path / "new.json"
    old.write_text(json.dumps({"results": {"a": {"title": "Old Title"}}}))
    new.write_text(json.dumps({"results": {"b": {"title": "New Title"}, "c": {"title": "Other"}}}))
    import os

    os.utime(old, (1, 1))
    titles, source = script.latest_titles(tmp_path)
    assert titles == ["New Title", "Other"] and "new.json" in source
    assert script.latest_titles(tmp_path / "missing")[1].startswith("the replay-4 fixture pool")
    text = (ROOT / "Makefile").read_text()
    assert "shop-diagnose:" in text and "scripts/shop_diagnose_tagging.py" in text


def test_no_script_or_module_sleeps() -> None:
    for path in [*ROOT.glob("scripts/shop*.py"), *ROOT.glob("src/app/shop/*.py")]:
        assert "sleep(" not in path.read_text(), path.name
