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
PRICE_TIERS = ("value", "mid", "premium")


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
    perception_prompt_version: str = "3"
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
    # Cap on ESTIMATED provider dollars (Serper/SerpApi calls), separate from `max_cost_usd`, which
    # now bounds Anthropic dollars only (ADR 0004/0006). Provider dollars are assumptions and are
    # $0 on free tiers, so they must not eat the Anthropic budget (or the reverse).
    max_provider_cost_usd: float = 0.30
    # Proposal and self-check (ADR 0006). One structured call picks the final list; a text-only
    # style call checks it; up to `max_revisions` automatic revisions fix failed checks.
    model_proposal: str = "claude-sonnet-5-5"
    effort_proposal: str = "low"
    model_stylecheck: str = "claude-haiku-5-5"
    effort_stylecheck: str = "low"
    max_revisions: int = 1
    # No single item's total may exceed this fraction of the budget (unless it is the only item).
    # UNTUNED GUESS, like the three fit constants below: change them only from evidence.
    item_cap_fraction: float = 0.6
    # Per-item search ceiling = budget * item_cap_fraction / (1 + shipping_buffer): the buffer
    # leaves room for shipping, which the search results do not show (ADR 0007). UNTUNED GUESS.
    shipping_buffer: float = 0.10
    # Which proposal prompt to load (prompts/proposal.md is current; older ones are kept as
    # proposal.v<N>.md). Set per run via scripts/spike.py, not from the env.
    proposal_prompt_version: str = "2"
    # Which rank prompt to load (v3 adds the over-limit pool wording; v2 is kept). Per run via the
    # CLI, not the env.
    rank_prompt_version: str = "3"
    # Freeplay (no --budget, ADR 0008): its own prompt versions; budget mode keeps the two above.
    # A CLI version flag overrides both modes.
    freeplay_rank_prompt_version: str = "5"
    freeplay_proposal_prompt_version: str = "3"
    # Freeplay price tier: value (cheapest third of the search results), mid, or premium.
    price_tier: str = "mid"
    # Pieces to shop for (slots). None = the mode default: `default_items_freeplay` in freeplay,
    # `default_slots` in budget mode. Hard maximum `max_items`; the minimum is 2.
    items: int | None = None
    default_items_freeplay: int = 5
    max_items: int = 10
    # Product lookups per slot (each ~ one SerpApi search). None = 1 in freeplay, 2 in budget mode.
    # With 1, the top-ranked finalist is looked up and the next is tried if that fails (2 attempts
    # at most); other finalists stay unverified alternates (Google product link, no retailer link).
    lookups_per_slot: int | None = None
    # Eagerly look up the unverified alternates too (testing; the UI will do it on demand).
    lookup_alternates: bool = False
    # Fetch one product thumbnail per slot for the image-based style check (ADR 0010).
    thumbnails: bool = True
    # Which style-check prompt to load: 2 (default) sees the room photo and thumbnails; 1 is the
    # text-only original. Per run via the CLI, not the env.
    style_prompt_version: str = "2"
    # Shop Mode (ADR 0012): product lookups (about one SerpApi search each) allowed per session,
    # spent only when the visitor opens or loves a result. A guess; past it the Google link is used.
    shop_lookups_per_session: int = 12
    # Shop Again (ADR 0012, stage 2): per session. A guess.
    shop_agains_per_session: int = 10
    # Serper calls one Shop Again may make across its (up to 3) search phrasings. A guess.
    shop_again_max_serper_calls: int = 4
    # stages 2e and 2f: the most Serper calls one Shop Again may make in all, counting its
    # phrasings (up to the cap above), peer retailer searches and targeted supply searches
    shop_again_max_serper_calls_total: int = 9
    # stage 2g: latency and tagging budgets of a Shop Again (seconds), what is tagged, how
    # searches run
    shop_target_s: float = 30.0  # the time a Shop Again aims for (shown in the trace)
    shop_hard_stop_s: float = 45.0  # past this, finish with what exists and say so
    shop_tag_budget_s: float = (
        15.0  # all candidate tagging together; what is tagged by then is used
    )
    shop_tag_call_timeout_s: float = 12.0  # one tagging request
    shop_tag_top_n: int = 60  # candidates tagged by the model: the top N untagged by rank
    shop_search_workers: int = 4  # concurrent Serper searches (4 to 5)
    shop_max_steps: int = 40  # model calls one Shop Again may make (tagging batches add up)
    # Serper bills credits, not calls: a shopping call took 2 credits in the live test. USD per
    # credit is the owner's figure from the live test (about $0.001), not a documented price.
    # Shop Mode costs Serper from these two; the room pipeline still uses serper_usd_per_query.
    serper_credits_per_call: int = 2
    serper_usd_per_credit: float = 0.001
    # Fit (coarse, code-only): fail if an item's longer side is over this fraction of the room's
    # shorter side, or its footprint is over `fit_max_footprint_fraction` of the floor area.
    # UNTUNED GUESSES. Rugs and other large flat items will trip them; see ADR 0006.
    fit_max_side_fraction: float = 0.6
    fit_max_footprint_fraction: float = 0.12
    # Below this perception confidence, fit messages say the room size is a low-confidence estimate.
    # UNTUNED GUESS.
    fit_low_confidence: float = 0.5

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
        if not 0 < self.item_cap_fraction <= 1:
            raise ValueError("APP_ITEM_CAP_FRACTION must be in (0, 1]")
        if self.shipping_buffer < 0:
            raise ValueError("APP_SHIPPING_BUFFER must be >= 0")
        if self.price_tier not in PRICE_TIERS:
            raise ValueError(
                f"APP_PRICE_TIER must be one of {PRICE_TIERS}, got {self.price_tier!r}"
            )
        if self.max_items < 2:
            raise ValueError("APP_MAX_ITEMS must be >= 2")
        if self.items is not None and not 2 <= self.items <= self.max_items:
            raise ValueError(f"items must be between 2 and {self.max_items}, got {self.items}")
        if self.lookups_per_slot is not None and self.lookups_per_slot < 1:
            raise ValueError("APP_LOOKUPS_PER_SLOT must be >= 1")
        if self.max_revisions < 0:
            raise ValueError("APP_MAX_REVISIONS must be >= 0")
        if self.shop_lookups_per_session < 0:
            raise ValueError("APP_SHOP_LOOKUPS_PER_SESSION must be >= 0")
        if self.shop_agains_per_session < 0:
            raise ValueError("APP_SHOP_AGAINS_PER_SESSION must be >= 0")
        if self.shop_again_max_serper_calls < 1:
            raise ValueError("APP_SHOP_AGAIN_MAX_SERPER_CALLS must be >= 1")
        if not 0 < self.shop_target_s <= self.shop_hard_stop_s:
            raise ValueError("APP_SHOP_TARGET_S must be positive and at most the hard stop")
        if self.shop_tag_top_n < 0 or not 1 <= self.shop_search_workers <= 8:
            raise ValueError("APP_SHOP_TAG_TOP_N must be >= 0 and APP_SHOP_SEARCH_WORKERS 1 to 8")
        if self.shop_again_max_serper_calls_total < self.shop_again_max_serper_calls:
            raise ValueError("APP_SHOP_AGAIN_MAX_SERPER_CALLS_TOTAL must be >= the normal cap")
        if self.serper_credits_per_call < 0 or self.serper_usd_per_credit < 0:
            raise ValueError(
                "APP_SERPER_CREDITS_PER_CALL and APP_SERPER_USD_PER_CREDIT must be >= 0"
            )

    @property
    def serper_usd_per_call(self) -> float:
        """Estimated dollars for one Serper call, from its credits and the price of a credit."""
        return self.serper_credits_per_call * self.serper_usd_per_credit

    def items_for(self, freeplay: bool) -> int:
        """Slots to shop for: the explicit count, else the mode's default."""
        if self.items is not None:
            return self.items
        return self.default_items_freeplay if freeplay else self.default_slots

    def lookups_for(self, freeplay: bool) -> int:
        if self.lookups_per_slot is not None:
            return self.lookups_per_slot
        return 1 if freeplay else 2

    def rank_version_for(self, freeplay: bool) -> str:
        return self.freeplay_rank_prompt_version if freeplay else self.rank_prompt_version

    def proposal_version_for(self, freeplay: bool) -> str:
        return self.freeplay_proposal_prompt_version if freeplay else self.proposal_prompt_version

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
            max_provider_cost_usd=float(
                os.environ.get("APP_MAX_PROVIDER_COST_USD", cls.max_provider_cost_usd)
            ),
            model_proposal=os.environ.get("APP_MODEL_PROPOSAL", cls.model_proposal),
            effort_proposal=os.environ.get("APP_EFFORT_PROPOSAL", cls.effort_proposal),
            model_stylecheck=os.environ.get("APP_MODEL_STYLECHECK", cls.model_stylecheck),
            effort_stylecheck=os.environ.get("APP_EFFORT_STYLECHECK", cls.effort_stylecheck),
            max_revisions=int(os.environ.get("APP_MAX_REVISIONS", cls.max_revisions)),
            item_cap_fraction=float(os.environ.get("APP_ITEM_CAP_FRACTION", cls.item_cap_fraction)),
            shipping_buffer=float(os.environ.get("APP_SHIPPING_BUFFER", cls.shipping_buffer)),
            price_tier=os.environ.get("APP_PRICE_TIER", cls.price_tier),
            thumbnails=os.environ.get("APP_THUMBNAILS", "1") != "0",
            default_items_freeplay=int(
                os.environ.get("APP_DEFAULT_ITEMS_FREEPLAY", cls.default_items_freeplay)
            ),
            max_items=int(os.environ.get("APP_MAX_ITEMS", cls.max_items)),
            shop_lookups_per_session=int(
                os.environ.get("APP_SHOP_LOOKUPS_PER_SESSION", cls.shop_lookups_per_session)
            ),
            shop_agains_per_session=int(
                os.environ.get("APP_SHOP_AGAINS_PER_SESSION", cls.shop_agains_per_session)
            ),
            shop_again_max_serper_calls=int(
                os.environ.get("APP_SHOP_AGAIN_MAX_SERPER_CALLS", cls.shop_again_max_serper_calls)
            ),
            shop_target_s=float(os.environ.get("APP_SHOP_TARGET_S", cls.shop_target_s)),
            shop_hard_stop_s=float(os.environ.get("APP_SHOP_HARD_STOP_S", cls.shop_hard_stop_s)),
            shop_tag_budget_s=float(os.environ.get("APP_SHOP_TAG_BUDGET_S", cls.shop_tag_budget_s)),
            shop_tag_call_timeout_s=float(
                os.environ.get("APP_SHOP_TAG_CALL_TIMEOUT_S", cls.shop_tag_call_timeout_s)
            ),
            shop_tag_top_n=int(os.environ.get("APP_SHOP_TAG_TOP_N", cls.shop_tag_top_n)),
            shop_search_workers=int(
                os.environ.get("APP_SHOP_SEARCH_WORKERS", cls.shop_search_workers)
            ),
            shop_again_max_serper_calls_total=int(
                os.environ.get(
                    "APP_SHOP_AGAIN_MAX_SERPER_CALLS_TOTAL", cls.shop_again_max_serper_calls_total
                )
            ),
            serper_credits_per_call=int(
                os.environ.get("APP_SERPER_CREDITS_PER_CALL", cls.serper_credits_per_call)
            ),
            serper_usd_per_credit=float(
                os.environ.get("APP_SERPER_USD_PER_CREDIT", cls.serper_usd_per_credit)
            ),
            lookups_per_slot=(
                int(os.environ["APP_LOOKUPS_PER_SLOT"])
                if os.environ.get("APP_LOOKUPS_PER_SLOT")
                else None
            ),
        )


def load_env() -> None:
    """Load `.env` from the working directory (entry points call this once; never overrides)."""
    load_dotenv(find_dotenv(usecwd=True))


class BudgetExceededError(RuntimeError):
    """Raised when an agent run exceeds its step or cost cap."""


def check_budget(
    config: Config, steps: int, cost_usd: float, provider_cost_usd: float = 0.0
) -> None:
    """Raise `BudgetExceededError` past the step cap, the Anthropic cost cap, or the provider cap.

    `cost_usd` is Anthropic dollars only; `provider_cost_usd` is estimated Serper/SerpApi dollars.
    """
    if steps > config.max_steps:
        raise BudgetExceededError(f"step cap exceeded: {steps} > {config.max_steps}")
    if cost_usd > config.max_cost_usd:
        raise BudgetExceededError(
            f"cost cap exceeded: ${cost_usd:.3f} > ${config.max_cost_usd:.3f}"
        )
    if provider_cost_usd > config.max_provider_cost_usd:
        raise BudgetExceededError(
            f"provider cost cap exceeded: ${provider_cost_usd:.3f} > "
            f"${config.max_provider_cost_usd:.3f}"
        )
