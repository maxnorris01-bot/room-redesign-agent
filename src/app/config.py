"""Runtime configuration, read from environment variables.

Every agent loop must respect `max_steps` and `max_cost_usd`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv

LLM_MODES = ("mock", "live")


@dataclass(frozen=True)
class Config:
    max_steps: int = 12
    # Starting point, not a measured number - see evals/thresholds.yaml's comment and
    # docs/lessons-learned.md. Raise it once a real `make eval-fast-live` run shows what a
    # legitimately heavy call actually costs; don't just guess a second time.
    max_cost_usd: float = 0.25
    tracing_disabled: bool = False
    # "mock" (default): app.llm.get_client returns app.mock_llm.MockAnthropicClient - zero API
    # calls, zero cost, no key required. "live": the real Anthropic client, real cost. Never
    # default to "live" - every entry point (CLI, eval harness) should be free unless explicitly
    # opted in via APP_LLM_MODE=live, e.g. a dedicated `make eval-fast-live` target.
    llm_mode: str = "mock"

    def __post_init__(self) -> None:
        if self.llm_mode not in LLM_MODES:
            raise ValueError(f"APP_LLM_MODE must be one of {LLM_MODES}, got {self.llm_mode!r}")

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            max_steps=int(os.environ.get("APP_MAX_STEPS", "12")),
            max_cost_usd=float(os.environ.get("APP_MAX_COST_USD", "0.25")),
            tracing_disabled=os.environ.get("APP_TRACING_DISABLED", "0") == "1",
            llm_mode=os.environ.get("APP_LLM_MODE", "mock"),
        )


def load_env() -> None:
    """Load `.env` from the working directory (entry points call this once; never overrides)."""
    load_dotenv(find_dotenv(usecwd=True))


class BudgetExceededError(RuntimeError):
    """Raised when an agent run exceeds its step or cost cap."""


def check_budget(config: Config, steps: int, cost_usd: float) -> None:
    if steps > config.max_steps:
        raise BudgetExceededError(f"step cap exceeded: {steps} > {config.max_steps}")
    if cost_usd > config.max_cost_usd:
        raise BudgetExceededError(
            f"cost cap exceeded: ${cost_usd:.3f} > ${config.max_cost_usd:.3f}"
        )
