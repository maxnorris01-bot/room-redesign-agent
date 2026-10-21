from __future__ import annotations

import copy
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from app import llm
from app.config import BudgetExceededError, Config
from app.llm import Budget
from app.mock_llm import (
    MockAnthropicClient,
    mock_perception_payload,
    response,
    text_block,
)
from app.retrieval import retrieve
from app.schemas import RoomPerception


def _perception(n_gaps: int) -> RoomPerception:
    payload = copy.deepcopy(mock_perception_payload())
    payload["furniture"] = []
    payload["furniture_gaps"] = [
        {"category": f"item{i}", "reason": "MOCK: nothing visible"} for i in range(n_gaps)
    ]
    return RoomPerception.model_validate(payload)


class BarrierClient:
    """Every call waits until all `n` calls are in flight at once, so a sequential
    implementation would deadlock (and fail on the barrier timeout) instead of passing."""

    def __init__(self, n: int) -> None:
        self.barrier = threading.Barrier(n, timeout=10)
        self.inner = MockAnthropicClient()
        self.threads: set[int] = set()
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.threads.add(threading.get_ident())
        self.barrier.wait()
        return self.inner._create(**kwargs)


def test_slots_run_concurrently_with_exact_totals_and_stable_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    n = 6
    client = BarrierClient(n)
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    budget = Budget(Config(max_steps=n, max_cost_usd=10.0))

    result = retrieve(_perception(n), budget_usd=600, style="x", max_slots=n, budget=budget)

    assert len(client.threads) == n  # genuinely parallel: n distinct worker threads
    assert [r.slot.category for r in result.slots] == [f"item{i}" for i in range(n)]
    assert budget.steps == n  # exact, no lost updates
    per_call = result.slots[0].completion.cost_usd
    assert budget.cost_usd == pytest.approx(n * per_call)
    assert budget.cost_usd == pytest.approx(sum(r.completion.cost_usd for r in result.slots))
    assert all(r.completion.steps == 1 for r in result.slots)


def test_order_is_slot_order_even_when_calls_finish_out_of_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last slot's call is released first; results must still come back in slot order."""
    n = 3
    gate = threading.Event()
    arrived = threading.Semaphore(0)
    inner = MockAnthropicClient()

    def create(**kwargs: Any) -> Any:
        text = kwargs["messages"][0]["content"]
        if "Slot: item2" in text:
            arrived.release()
            return inner._create(**kwargs)  # item2 finishes immediately
        arrived.release()
        assert gate.wait(timeout=10)  # the others wait until item2 is done
        return inner._create(**kwargs)

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)

    def release_after_all_started() -> None:
        for _ in range(n):
            assert arrived.acquire(timeout=10)
        gate.set()

    watcher = threading.Thread(target=release_after_all_started)
    watcher.start()
    result = retrieve(
        _perception(n), budget_usd=300, style="x", max_slots=n, budget=Budget(Config())
    )
    watcher.join(timeout=10)
    assert [r.slot.category for r in result.slots] == ["item0", "item1", "item2"]


def test_step_cap_is_exact_under_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MockAnthropicClient()
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    budget = Budget(Config(max_steps=2, max_cost_usd=10.0))

    with pytest.raises(BudgetExceededError, match="step cap"):
        retrieve(_perception(5), budget_usd=500, style="x", max_slots=5, budget=budget)

    assert budget.steps == 2  # the cap held: 5 slots asked, exactly 2 requests were allowed out
    assert len(client.calls) == 2  # and the others never reached the API


def test_cost_cap_trips_under_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MockAnthropicClient()
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    budget = Budget(Config(max_steps=20, max_cost_usd=0.015))  # a mock call costs about $0.0104

    with pytest.raises(BudgetExceededError, match="cost cap"):
        retrieve(_perception(4), budget_usd=400, style="x", max_slots=4, budget=budget)

    assert budget.cost_usd > 0.015  # tripped on the call that crossed the line


def test_budget_counters_survive_hammering_from_many_threads() -> None:
    budget = Budget(Config(max_steps=100_000, max_cost_usd=1e9))

    def work() -> None:
        for _ in range(500):
            budget.start_step()
            budget.add_cost(0.001)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert budget.steps == 4000
    assert budget.cost_usd == pytest.approx(4.0)


def test_a_failed_slot_surfaces_after_the_others_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    inner = MockAnthropicClient()

    def create(**kwargs: Any) -> Any:
        if "Slot: item1" in kwargs["messages"][0]["content"]:
            return response([text_block("not json at all")])
        return inner._create(**kwargs)

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(llm, "get_client", lambda mode, max_retries=1: client)
    from app.schemas import SchemaError

    with pytest.raises(SchemaError, match="item1"):
        retrieve(_perception(3), budget_usd=300, style="x", max_slots=3, budget=Budget(Config()))
