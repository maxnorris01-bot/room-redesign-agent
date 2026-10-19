import pytest

from app.config import BudgetExceededError, Config, check_budget
from app.pipeline import run
from app.prompts import load_prompt


def test_prompt_loads_with_version() -> None:
    prompt = load_prompt("example")
    # Any positive integer version is valid. Don't hardcode a number here: it changes every time a
    # prompt is revised (Claim Verification's copy of this test broke on a v1 -> v3 bump).
    assert prompt.version.isdigit() and int(prompt.version) >= 1
    assert prompt.text


def test_pipeline_runs() -> None:
    assert run("hello") == "hello"


def test_budget_caps_enforced() -> None:
    cfg = Config(max_steps=3, max_cost_usd=0.10)
    check_budget(cfg, steps=3, cost_usd=0.10)
    with pytest.raises(BudgetExceededError):
        check_budget(cfg, steps=4, cost_usd=0.0)
    with pytest.raises(BudgetExceededError):
        check_budget(cfg, steps=1, cost_usd=0.11)
