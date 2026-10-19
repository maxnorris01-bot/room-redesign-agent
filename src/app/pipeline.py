"""PLACEHOLDER pipeline. Replace with the real system under test.

The eval harness calls `run(text, budget=budget)`. Keep that signature stable so evals keep
working. Thread `budget` through to every `app.llm.complete(...)` call you add, so real cost and
step counts flow into eval reports instead of the zeroed-out numbers you'd get from a call that
never touches `budget` - see `app.llm.Budget`.
"""

from __future__ import annotations

from app.config import Config
from app.llm import Budget
from app.prompts import load_prompt
from app.tracing import span


def run(text: str, *, config: Config | None = None, budget: Budget | None = None) -> str:
    """Run one input through the pipeline. Raises `BudgetExceededError` if a cap is hit.

    Pass a `budget` to read the steps and cost spent afterwards (the eval harness does).
    """
    cfg = config or (budget.config if budget else Config.from_env())
    budget = budget or Budget(cfg)
    prompt = load_prompt("example")
    with span(
        "pipeline.run", config=cfg, prompt_version=prompt.version, input_chars=len(text)
    ) as rec:
        # TODO: replace this with a real `app.llm.complete(...)` call using `prompt.text` and
        # `budget`. For now, echo the input so the harness has something to score structurally.
        output = text
        rec["output_chars"] = len(output)
    return output
