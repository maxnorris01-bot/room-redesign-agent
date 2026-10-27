"""Model judges for the Shop eval (prompts `eval_shop_category_judge`, `eval_shop_persona_judge`).

Both run on the retrieval model (Haiku) through the shared `complete` wrapper: thinking disabled, a
forced tool call with a strict schema. A judge sees only one request or persona and a numbered list
of listings (title, price, seller, thumbnail): never the pipeline's tags, ranker output, chips or
any other listing's score. Output is validated against the fixed schema (every index exactly once,
fixed answer values); a call that fails validation is retried once and then recorded as
"judge failed" (None), never silently scored. At most `BATCH` listings go in one call. Thumbnails go
through the guarded fetch (`app.shop.prefs.fetch_images`: https, public hosts, size and time limits,
EXIF stripped, downscaled).

Limits, stated plainly: the judges are the same model family as the pipeline's tagger and ranker, so
blind spots may be shared; the persona is a text description, not a person; neither judge is
calibrated against a human yet (docs/todo.md).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.config import Config
from app.images import PreparedImage
from app.llm import Budget, Completion, LLMOutputError, complete
from app.prompts import Prompt, load_prompt
from app.schemas import to_output_schema
from app.shop.prefs import fetch_images
from app.shop.textutil import Rated
from app.thumbnails import Thumbnailer
from evals.shop.spec import Case

BATCH = 12  # listings per judge call
CATEGORY_TOOL = "record_category_judgements"
PERSONA_TOOL = "record_persona_judgements"
JUDGE_MAX_TOKENS = 700
MAX_TITLE_CHARS = 160
CATEGORY_VALUES = ("yes", "no", "accessory_or_part")
ATTRIBUTE_VALUES = ("yes", "no", "cannot_tell")
PERSONA_VALUES = ("love", "like", "dislike")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CategoryVerdict(_Strict):
    index: int
    category: Literal["yes", "no", "accessory_or_part"]
    attributes: Literal["yes", "no", "cannot_tell"]


class CategoryJudgements(_Strict):
    items: list[CategoryVerdict]


class PersonaVerdict(_Strict):
    index: int
    answer: Literal["love", "like", "dislike"]


class PersonaJudgements(_Strict):
    items: list[PersonaVerdict]


@dataclass
class JudgeItem:
    """One listing as a judge sees it. `index` is the only handle; nothing else is shared."""

    index: int
    title: str
    price: float | None
    seller: str
    thumbnail_url: str


@dataclass
class JudgeStats:
    calls: int = 0
    retries: int = 0
    failed_items: int = 0
    cost_usd: float = 0.0
    images_offered: int = 0
    images_used: int = 0


def clean_title(title: str) -> str:
    """A listing title as sent: printable, no angle brackets or line breaks, bounded."""
    printable = "".join(c if c.isprintable() else " " for c in title)
    return " ".join(printable.replace("<", " ").replace(">", " ").replace("|", "/").split())[
        :MAX_TITLE_CHARS
    ]


def _row(item: JudgeItem, image_no: int | None) -> str:
    price = f"${item.price:,.2f}" if item.price is not None else "no price"
    seller = clean_title(item.seller)[:60] or "unknown"
    image = f"image {image_no}" if image_no else "image none"
    return f"{item.index} | {clean_title(item.title)} | {price} | {seller} | {image}"


def tool_def(name: str, model: type[BaseModel], what: str) -> dict[str, Any]:
    schema = to_output_schema(model)
    schema.pop("title", None)
    return {"name": name, "description": what, "strict": True, "input_schema": schema}


CATEGORY_TOOL_DEF = tool_def(
    CATEGORY_TOOL, CategoryJudgements, "Record one category and attribute verdict per listing."
)
PERSONA_TOOL_DEF = tool_def(
    PERSONA_TOOL, PersonaJudgements, "Record one love, like or dislike answer per listing."
)


def category_user_text(case: Case, rows: list[str]) -> str:
    attrs = [*case.styles, *case.materials, *case.must_have]
    return "\n".join(
        [
            f"<request>{clean_title(case.request)}</request>",
            f"Product type: {case.category_noun}",
            f"Stated attributes: {'; '.join(attrs) if attrs else 'none'}",
            "Listings (index | title | price | seller | image):",
            *rows,
        ]
    )


def persona_user_text(case: Case, rows: list[str]) -> str:
    return "\n".join(
        [
            f"<persona>{case.persona.text}</persona>",
            f"You are shopping for: {case.category_noun}",
            "Listings (index | title | price | seller | image):",
            *rows,
        ]
    )


def check_indexes(got: list[int], wanted: list[int]) -> bool:
    """Every wanted index exactly once and nothing else."""
    return sorted(got) == sorted(wanted)


Caller = Callable[..., Completion]


class ModelJudge:
    """Runs the two judges for one run. `caller` is `complete` (the tests pass a fake)."""

    def __init__(
        self,
        config: Config,
        thumbnailer: Thumbnailer | None,
        caller: Caller = complete,
    ) -> None:
        self.config = config
        self.thumbnailer = thumbnailer
        self.caller = caller
        self.stats = JudgeStats()
        self.versions = {
            "category": load_prompt("eval_shop_category_judge").version,
            "persona": load_prompt("eval_shop_persona_judge").version,
        }

    # ---- public ---------------------------------------------------------------------------

    def category(self, case: Case, items: list[JudgeItem]) -> dict[int, CategoryVerdict | None]:
        out: dict[int, CategoryVerdict | None] = {}
        for chunk in _chunks(items):
            got = self._ask(
                chunk, load_prompt("eval_shop_category_judge"), CATEGORY_TOOL, CATEGORY_TOOL_DEF,
                lambda rows: category_user_text(case, rows), CategoryJudgements,
            )  # fmt: skip
            for item in chunk:
                verdict = got.get(item.index) if got is not None else None
                out[item.index] = verdict if isinstance(verdict, CategoryVerdict) else None
        return out

    def persona(self, case: Case, items: list[JudgeItem]) -> dict[int, PersonaVerdict | None]:
        out: dict[int, PersonaVerdict | None] = {}
        for chunk in _chunks(items):
            got = self._ask(
                chunk, load_prompt("eval_shop_persona_judge"), PERSONA_TOOL, PERSONA_TOOL_DEF,
                lambda rows: persona_user_text(case, rows), PersonaJudgements,
            )  # fmt: skip
            for item in chunk:
                verdict = got.get(item.index) if got is not None else None
                out[item.index] = verdict if isinstance(verdict, PersonaVerdict) else None
        return out

    # ---- one batched call, validated, retried once ------------------------------------------

    def _ask(
        self,
        chunk: list[JudgeItem],
        prompt: Prompt,
        tool: str,
        tool_def_: dict[str, Any],
        make_text: Callable[[list[str]], str],
        model: type[BaseModel],
    ) -> dict[int, Any] | None:
        budget = Budget(self.config)
        images, report = fetch_images(
            [
                Rated(str(i.index), "like", "", i.title, i.price, i.seller, i.thumbnail_url)
                for i in chunk
                if i.thumbnail_url.startswith("https://")
            ],
            self.thumbnailer,
            budget,
        )
        self.stats.images_offered += report.offered
        self.stats.images_used += report.used
        ordered = [i for i in chunk if str(i.index) in images]
        numbers = {i.index: n for n, i in enumerate(ordered, start=1)}
        rows = [_row(i, numbers.get(i.index)) for i in chunk]
        pictures: list[PreparedImage] = [images[str(i.index)] for i in ordered]
        wanted = [i.index for i in chunk]
        for attempt in (1, 2):
            self.stats.calls += 1
            if attempt == 2:
                self.stats.retries += 1
            try:
                completion = self.caller(
                    span_name="llm.eval_shop_judge",
                    prompt=prompt,
                    model=self.config.model_retrieval,
                    user_text=make_text(rows),
                    budget=budget,
                    max_tokens=JUDGE_MAX_TOKENS,
                    tools=[tool_def_],
                    tool_choice={"type": "tool", "name": tool},
                    images=pictures or None,
                    effort=self.config.effort_retrieval,
                    disable_thinking=True,
                )
            except LLMOutputError:
                continue
            finally:
                self.stats.cost_usd = round(self.stats.cost_usd + budget.cost_usd, 6)
                budget.cost_usd = 0.0
            try:
                parsed = model.model_validate(completion.tool_input or {})
            except ValidationError:
                continue
            verdicts = {v.index: v for v in parsed.items}  # type: ignore[attr-defined]
            if check_indexes([v.index for v in parsed.items], wanted):  # type: ignore[attr-defined]
                return verdicts
        self.stats.failed_items += len(chunk)
        return None


def _chunks(items: list[JudgeItem]) -> list[list[JudgeItem]]:
    return [items[i : i + BATCH] for i in range(0, len(items), BATCH)]


_WORD = re.compile(r"[a-z0-9]+")
