"""Question 1 from the request text alone (stage B2).

The questions exist to hide the search wait, so the first one must not wait for the parse, the
listings or any picture. Two ways to get it, both off the raw request text:

1. `template_question`: a small table keyed by a category word (rug, sofa, dresser ...). Each
   category has a first and a second question; the first one whose dimension the request does not
   already state is used. Free and instant. Every option label is in `clarify_vocab.json`, so the
   page draws an icon or swatch for it.
2. `ask_first_question`: no template matches (or all its dimensions are stated): one short model
   call on the raw text returning exactly one question.

Either returns a `Question` with id `q1`, or None.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.config import Config
from app.llm import Budget, Completion, LLMOutputError, complete
from app.prompts import load_prompt
from app.schemas import to_output_schema
from app.shop.clarify import (
    DIMENSIONS,
    MAX_LABEL_CHARS,
    MAX_OPTIONS,
    MAX_TEXT_CHARS,
    Dimension,
    Option,
    Question,
    clean_text,
    words,
)
from app.shop.clarify_visual import icon_in, is_feature_label, norm, vocabulary

FIRST_TOOL = "record_first_question"
FIRST_PROMPT = "shop_clarify_first"
FIRST_MAX_TOKENS = 250
FIRST_ID = "q1"

TEXT = {
    "material": "What should it be made of?",
    "shape": "Which shape do you want?",
    "style": "Which look do you prefer?",
    "size": "How big should it be?",
    "color": "Which colour do you like?",
}

_STYLES = ("style", ["Modern", "Mid-century", "Rustic", "Farmhouse"])
_COLORS = ("color", ["White", "Beige", "Gray", "Black"])
_SHAPES = ("shape", ["Round", "Rectangular", "Oval", "Square"])
_MATERIAL_WOOD_METAL = ("material", ["Wood", "Metal", "Glass"])
_UPHOLSTERY = ("material", ["Velvet", "Leather", "Linen", "Boucle"])

# keywords (matched as whole words, plural tolerant) -> [first question, second question]
Spec = tuple[str, list[str]]
TEMPLATES: dict[tuple[str, ...], list[Spec]] = {
    ("rug", "area rug", "runner rug"): [
        ("material", ["Wool", "Cotton", "Jute", "Polyester"]),
        ("shape", ["Round", "Rectangular", "Runner", "Oval"]),
    ],
    ("couch", "sofa", "sectional"): [_UPHOLSTERY, _STYLES],
    ("loveseat",): [_UPHOLSTERY, _STYLES],
    ("chair", "armchair"): [("material", ["Leather", "Velvet", "Fabric", "Wood"]), _STYLES],
    ("dresser",): [("material", ["Wood", "Metal", "Rattan"]), _STYLES],
    ("nightstand",): [("material", ["Wood", "Metal", "Glass", "Marble"]), _STYLES],
    ("bed", "bed frame"): [("material", ["Wood", "Metal", "Upholstered", "Leather"]), _STYLES],
    ("headboard",): [("material", ["Upholstered", "Wood", "Velvet", "Leather"]), _STYLES],
    ("desk",): [_MATERIAL_WOOD_METAL, ("size", ["Small", "Large"])],
    ("coffee table",): [_SHAPES, ("material", ["Wood", "Glass", "Marble", "Metal"])],
    ("dining table",): [_SHAPES, ("material", ["Wood", "Glass", "Marble", "Metal"])],
    ("side table", "end table"): [
        ("shape", ["Round", "Square", "Oval"]),
        ("material", ["Wood", "Metal", "Glass", "Marble"]),
    ],
    ("tv stand",): [_MATERIAL_WOOD_METAL, _STYLES],
    ("bookshelf", "bookcase", "shelf"): [_MATERIAL_WOOD_METAL, _STYLES],
    ("cabinet",): [_MATERIAL_WOOD_METAL, _STYLES],
    ("mirror",): [
        ("shape", ["Round", "Rectangular", "Oval", "Curved"]),
        ("style", ["Modern", "Vintage", "Minimalist", "Glam"]),
    ],
    ("lamp",): [
        ("style", ["Modern", "Industrial", "Vintage", "Minimalist"]),
        ("material", ["Metal", "Brass", "Glass", "Ceramic"]),
    ],
    ("curtain", "curtains"): [("material", ["Linen", "Velvet", "Cotton", "Silk"]), _COLORS],
    ("pillow",): [("material", ["Velvet", "Linen", "Cotton", "Wool"]), _COLORS],
    ("blanket",): [("material", ["Wool", "Cotton", "Velvet", "Microfiber"]), _COLORS],
    ("vase",): [("material", ["Ceramic", "Glass", "Metal", "Bamboo"]), _COLORS],
    ("planter",): [
        ("material", ["Ceramic", "Plastic", "Metal", "Rattan"]),
        ("shape", ["Round", "Square", "Rectangular"]),
    ],
}  # fmt: skip
_KEYWORDS = sorted(
    ((kw, spec) for kws, spec in TEMPLATES.items() for kw in kws), key=lambda p: -len(p[0])
)
_SIZE_STATED = re.compile(
    r"\b\d+(\.\d+)?\s*(x|by)\s*\d+|\b\d+(\.\d+)?\s*(ft|feet|foot|in|inch|inches|cm)\b|\b\d+'"
)


def _phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(es|s)?(?!\w)", text) is not None


def stated_dimensions(request: str) -> set[str]:
    """The dimensions the raw request text already decides (read from the shared vocabulary)."""
    text = norm(request)
    stated: set[str] = set()
    for dim in ("material", "shape", "style", "size", "feature"):
        if icon_in(dim, text) is not None:
            stated.add(dim)
    if any(_phrase(text, c) for c in vocabulary()["colors"]) or icon_in("color-extra", text):
        stated.add("color")
    if _SIZE_STATED.search(request.lower()):
        stated.add("size")
    return stated


def _question(dimension: str, labels: list[str], text: str | None = None) -> Question:
    dim: Dimension = dimension if dimension in DIMENSIONS else "feature"  # type: ignore[assignment]
    return Question(
        id=FIRST_ID,
        text=text or TEXT.get(dimension, "Which do you prefer?"),
        kind="size_range" if dim == "size" else "choice",
        dimension=dim,
        options=[Option(label=lb, value=" ".join(words(lb))) for lb in labels],
    )


def template_question(request: str) -> Question | None:
    """The first template question the request does not already answer, or None (no category word
    matches, or both of its questions are stated)."""
    text = norm(request)
    stated = stated_dimensions(request)
    for kw, spec in _KEYWORDS:
        if _phrase(text, norm(kw)):
            for dimension, labels in spec:
                if dimension not in stated:
                    return _question(dimension, labels)
            return None
    return None


# ---- the model path ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FirstOut(_Strict):
    dimension: Literal["color", "material", "shape", "style", "size", "feature"]
    text: str
    options: list[str]


def first_tool() -> dict[str, Any]:
    schema = to_output_schema(FirstOut)
    schema.pop("title", None)
    return {
        "name": FIRST_TOOL,
        "description": "Record the one clarifying question for this request.",
        "strict": True,
        "input_schema": schema,
    }


def sanitize_first(out: FirstOut) -> Question | None:
    """The model's question as kept: cleaned and bounded, options deduplicated and cut to four (the
    schema cannot bound the list), at least two. Mixed-dimension labels are dropped."""
    text = clean_text(out.text, MAX_TEXT_CHARS)
    options: list[Option] = []
    for raw in out.options:
        label = clean_text(raw, MAX_LABEL_CHARS)
        value = " ".join(words(label))
        if not label or not value or any(o.value == value for o in options):
            continue
        options.append(Option(label=label, value=value))
    if out.dimension != "feature":
        options = [o for o in options if not is_feature_label(o.label)]
    options = options[:MAX_OPTIONS]
    if not text or len(options) < 2:
        return None
    return Question(
        id=FIRST_ID,
        text=text,
        kind="size_range" if out.dimension == "size" else "choice",
        dimension=out.dimension,
        options=options,
    )


@dataclass
class FirstResult:
    question: Question | None = None
    source: str = "none"  # template | model | none
    cost_usd: float = 0.0
    seconds: float = 0.0


Caller = Callable[..., Completion]


def ask_first_question(
    request: str, budget: Budget, *, caller: Caller = complete, config: Config | None = None
) -> FirstResult:
    """One short model call on the raw request. Never raises except `BudgetExceededError`."""
    config = config or budget.config
    before, started = budget.cost_usd, time.monotonic()
    result = FirstResult()
    try:
        done = caller(
            span_name="llm.shop_clarify_first",
            prompt=load_prompt(FIRST_PROMPT),
            model=config.model_retrieval,
            user_text=f"<request>{clean_text(request, 300)}</request>",
            budget=budget,
            max_tokens=FIRST_MAX_TOKENS,
            tools=[first_tool()],
            tool_choice={"type": "tool", "name": FIRST_TOOL},
            effort=config.effort_retrieval,
            disable_thinking=True,
            timeout_s=8.0,
        )
        result.question = sanitize_first(FirstOut.model_validate(done.tool_input or {}))
    except (LLMOutputError, ValidationError):
        result.question = None
    result.cost_usd = round(budget.cost_usd - before, 6)
    result.seconds = round(time.monotonic() - started, 3)
    if result.question is not None:
        result.source = "model"
    return result


def first_question(request: str, budget: Budget, *, caller: Caller = complete) -> FirstResult:
    """Question 1: a template if one fits, else the model; `source` says which (or "none")."""
    found = template_question(request)
    if found is not None:
        return FirstResult(question=found, source="template")
    return ask_first_question(request, budget, caller=caller)
