"""Runtime configuration, read from environment variables.

Every agent loop must respect `max_steps` and `max_cost_usd`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv

from app.pricing import WEB_SEARCH_USD_PER_REQUEST

LLM_MODES = ("mock", "live")
RETRIEVAL_MODES = ("shopping", "web_search")
SEARCH_PROVIDERS = ("serper", "serpapi")


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
    # Model per pipeline stage: configuration, never hardcoded in logic. Defaults verified against
    # the models overview page on 2026-10-18; see also .env.example.
    model_perception: str = "claude-sonnet-5-5"
    model_retrieval: str = "claude-haiku-5-5"
    # Basic web search works on every model; _20260209+ adds dynamic filtering (runs code
    # execution under the hood). Config, so the two can be compared live without a code change.
    web_search_tool: str = "web_search_20250305"
    max_searches_per_slot: int = 3
    default_slots: int = 3
    image_max_edge: int = 1568
    # The SDK default (2) can bill a timed-out call and then retry it. Keep it low on expensive
    # live calls.
    sdk_max_retries: int = 1
    # Thinking depth per stage (output_config.effort). Starting guesses to explore live, not
    # measured: thinking tokens bill as output tokens, so effort is a direct cost lever.
    effort_perception: str = "medium"
    effort_retrieval: str = "low"
    # Which perception prompt file to load (prompts/perception.md is current; older ones are kept
    # as perception.v<N>.md for comparison). Set per run via scripts/spike.py, not from the env.
    perception_prompt_version: str = "2"
    # Retrieval: "shopping" (default) = structured Google Shopping data, ranked by a model that only
    # picks ids (ADR 0005); "web_search" = Claude's web search tool reading pages (ADR 0003 in the
    # claim-verification repo; kept selectable). API keys are NOT config fields: they are read
    # from SERPER_API_KEY / SERPAPI_API_KEY by app.shopping when a live provider is built.
    retrieval_mode: str = "shopping"
    search_provider: str = "serper"
    finalists_per_slot: int = 2
    # Model for the rank stage; empty means "same as model_retrieval". Use `rank_model`.
    model_rank: str = ""
    # Dollar estimates for provider calls. ASSUMPTIONS to set from the real plan (free-tier calls
    # cost $0 in reality): Serper 2 credits per query at about $0.0003 a credit; SerpApi at its
    # per-search plan rate. Lookups are always SerpApi, whichever provider searched.
    serper_usd_per_query: float = 0.0006
    serpapi_usd_per_search: float = 0.025
    # Per-search cost estimate (USD); the API returns no dollar figure. Default: pricing.py.
    web_search_usd: float = WEB_SEARCH_USD_PER_REQUEST

    def __post_init__(self) -> None:
        if self.llm_mode not in LLM_MODES:
            raise ValueError(f"APP_LLM_MODE must be one of {LLM_MODES}, got {self.llm_mode!r}")
        if self.retrieval_mode not in RETRIEVAL_MODES:
            raise ValueError(
                f"APP_RETRIEVAL_MODE must be one of {RETRIEVAL_MODES}, got {self.retrieval_mode!r}"
            )
        if self.search_provider not in SEARCH_PROVIDERS:
            raise ValueError(
                f"APP_SEARCH_PROVIDER must be one of {SEARCH_PROVIDERS}, "
                f"got {self.search_provider!r}"
            )
        if self.finalists_per_slot < 1:
            raise ValueError("APP_FINALISTS_PER_SLOT must be >= 1")

    @property
    def rank_model(self) -> str:
        return self.model_rank or self.model_retrieval

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            max_steps=int(os.environ.get("APP_MAX_STEPS", "12")),
            max_cost_usd=float(os.environ.get("APP_MAX_COST_USD", "0.25")),
            tracing_disabled=os.environ.get("APP_TRACING_DISABLED", "0") == "1",
            llm_mode=os.environ.get("APP_LLM_MODE", "mock"),
            model_perception=os.environ.get("APP_MODEL_PERCEPTION", cls.model_perception),
            model_retrieval=os.environ.get("APP_MODEL_RETRIEVAL", cls.model_retrieval),
            web_search_tool=os.environ.get("APP_WEB_SEARCH_TOOL", cls.web_search_tool),
            max_searches_per_slot=int(
                os.environ.get("APP_MAX_SEARCHES_PER_SLOT", cls.max_searches_per_slot)
            ),
            default_slots=int(os.environ.get("APP_DEFAULT_SLOTS", cls.default_slots)),
            image_max_edge=int(os.environ.get("APP_IMAGE_MAX_EDGE", cls.image_max_edge)),
            sdk_max_retries=int(os.environ.get("APP_SDK_MAX_RETRIES", cls.sdk_max_retries)),
            effort_perception=os.environ.get("APP_EFFORT_PERCEPTION", cls.effort_perception),
            effort_retrieval=os.environ.get("APP_EFFORT_RETRIEVAL", cls.effort_retrieval),
            web_search_usd=float(os.environ.get("APP_WEB_SEARCH_USD", cls.web_search_usd)),
            retrieval_mode=os.environ.get("APP_RETRIEVAL_MODE", cls.retrieval_mode),
            search_provider=os.environ.get("APP_SEARCH_PROVIDER", cls.search_provider),
            finalists_per_slot=int(
                os.environ.get("APP_FINALISTS_PER_SLOT", cls.finalists_per_slot)
            ),
            model_rank=os.environ.get("APP_MODEL_RANK", cls.model_rank),
            serper_usd_per_query=float(
                os.environ.get("APP_SERPER_USD_PER_QUERY", cls.serper_usd_per_query)
            ),
            serpapi_usd_per_search=float(
                os.environ.get("APP_SERPAPI_USD_PER_SEARCH", cls.serpapi_usd_per_search)
            ),
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
