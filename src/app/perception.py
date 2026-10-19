"""Perception stage: one multimodal, schema-constrained call (prompts/perception.md)."""

from __future__ import annotations

from dataclasses import dataclass

from app.images import PreparedImage
from app.llm import Budget, Completion, complete
from app.prompts import load_prompt
from app.schemas import RoomPerception, parse_model, to_output_schema
from app.tracing import span

PERCEPTION_MAX_TOKENS = 6000
PERCEPTION_USER_TEXT = "Analyse the attached room photo and return the structured description."


@dataclass
class PerceptionResult:
    perception: RoomPerception
    completion: Completion
    prompt_version: str


def perceive(
    image: PreparedImage, *, budget: Budget, run_id: str | None = None
) -> PerceptionResult:
    """Describe the room in `image`.

    Raises `BudgetExceededError`, `SchemaError` or `LLMOutputError`.
    """
    config = budget.config
    prompt = load_prompt("perception")
    with span(
        "stage.perception",
        config=config,
        prompt_version=prompt.version,
        prompt_name=prompt.name,
        model=config.model_perception,
        run_id=run_id,
        image=image.summary(),
    ) as rec:
        completion = complete(
            span_name="llm.perception",
            prompt=prompt,
            model=config.model_perception,
            user_text=PERCEPTION_USER_TEXT,
            budget=budget,
            max_tokens=PERCEPTION_MAX_TOKENS,
            schema=to_output_schema(RoomPerception),
            images=[image],
            effort=config.effort_perception,
            stream=True,
        )
        perception = parse_model(RoomPerception, completion.text, what="perception")
        rec.update(
            cost_usd=round(completion.cost_usd, 5),
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            furniture=len(perception.furniture),
            openings=len(perception.openings),
            living_things=len(perception.living_things),
            dimensions_confidence=perception.dimensions.confidence,
        )
    return PerceptionResult(perception, completion, prompt.version)
