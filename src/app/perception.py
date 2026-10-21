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


def output_stats(p: RoomPerception) -> dict[str, int]:
    """Size of the answer, to see whether a prompt's length instructions were followed."""
    notes = [
        p.dimensions.derivation,
        *(o.note for o in p.openings),
        *(f.note for f in p.fixed_items),
        *(f.note for f in p.furniture),
        *(g.reason for g in p.furniture_gaps),
        *(d.note for d in p.art_and_decor),
    ]
    return {
        "art_and_decor": len(p.art_and_decor),
        "uncertainties": len(p.uncertainties),
        "style_descriptors": len(p.style_descriptors),
        "furniture_gaps": len(p.furniture_gaps),
        "longest_note_words": max((len(n.split()) for n in notes), default=0),
    }


@dataclass
class PerceptionResult:
    perception: RoomPerception
    completion: Completion
    prompt_version: str
    output_stats: dict[str, int]


def perceive(
    image: PreparedImage, *, budget: Budget, run_id: str | None = None
) -> PerceptionResult:
    """Describe the room in `image`.

    Raises `BudgetExceededError`, `SchemaError` or `LLMOutputError`.
    """
    config = budget.config
    prompt = load_prompt("perception", config.perception_prompt_version)
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
        stats = output_stats(perception)
        rec.update(
            output_stats=stats,
            time_to_first_token_s=completion.time_to_first_token_s,
            cost_usd=round(completion.cost_usd, 5),
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            furniture=len(perception.furniture),
            openings=len(perception.openings),
            living_things=len(perception.living_things),
            dimensions_confidence=perception.dimensions.confidence,
        )
    return PerceptionResult(perception, completion, prompt.version, stats)
