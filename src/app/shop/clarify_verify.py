"""Picture verification for the clarifying-questions spike (round 2, ADR 0016). Not imported by the API
or the pipeline.

Round 1 measured picture COVERAGE, not picture CORRECTNESS: a title that says "Black Metal Handle"
can carry a light oak nightstand. Now every chosen picture is looked at:

1. `pick_option_images` ranks listings whose title strictly shows the value as the product's MAIN body
   (see `app.shop.clarify`) and keeps up to three per option.
2. `verify_and_prune` fetches each option's best candidate (the guarded thumbnail fetcher), asks the
   vision model one forced-tool question per picture ("is the product's main body <value>?": yes, no
   or unclear; prompt `shop_clarify_verify` v1), keeps the pictures answered yes, tries the next
   candidate for a no or unclear (up to three), and falls back to a text-only option. Calls run in
   parallel: batched (up to 12 images a call, as the photo tagger does) or one image a call.
3. Options are pruned: one with no title evidence at all, or whose every picture was contradicted
   ("no"), is dropped; a question left with fewer than two options is dropped. "No preference" is the
   page's, never an option here.
4. `audit_pictures` is the independent check the harness uses to measure accuracy: a second, differently
   worded prompt (`shop_clarify_audit` v1) is told nothing about the option and names the main colour,
   material and shape of every shown picture; code compares those words with the option. (Same model
   family as the verifier, so it is a second opinion, not ground truth.)
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.images import PreparedImage
from app.llm import Budget, Completion, LLMOutputError, complete
from app.prompts import load_prompt
from app.schemas import to_output_schema
from app.shop.clarify import (
    ImagePicks,
    Option,
    Pick,
    Question,
    Questions,
    clean_text,
    option_variants,
    pick_option_images,
    stems,
    words,
)
from app.shop.textutil import Rated
from app.shop.vocab import OTHER, normalize
from app.shopping import SearchCandidate
from app.thumbnails import Thumbnailer

VERIFY_PROMPT = "shop_clarify_verify"
AUDIT_PROMPT = "shop_clarify_audit"
VERIFY_TOOL = "record_picture_verdicts"
AUDIT_TOOL = "record_picture_audit"
BATCH = 12  # images per call, as the photo tagger
WORKERS = 8
MAX_ROUNDS = 3  # candidates tried per option
VERIFY_MAX_TOKENS = 400
AUDIT_MAX_TOKENS = 900
MODES = ("batched", "single")
Verdict = Literal["yes", "no", "unclear"]
AUDITABLE = ("color", "material", "shape")  # dimensions a picture's main body can be named for
Caller = Callable[..., Completion]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerdictOut(_Strict):
    index: int
    answer: Verdict


class VerdictsOut(_Strict):
    items: list[VerdictOut]


class AuditItemOut(_Strict):
    index: int
    main_color: str
    main_material: str
    shape: str


class AuditOut(_Strict):
    items: list[AuditItemOut]


def _tool(name: str, model: type[BaseModel], what: str) -> dict[str, Any]:
    schema = to_output_schema(model)
    schema.pop("title", None)
    return {"name": name, "description": what, "strict": True, "input_schema": schema}


def verify_tool() -> dict[str, Any]:
    return _tool(VERIFY_TOOL, VerdictsOut, "Record yes, no or unclear for every image number.")


def audit_tool() -> dict[str, Any]:
    return _tool(AUDIT_TOOL, AuditOut, "Record the main colour, material and shape of every image.")


# ---- the records ------------------------------------------------------------------------------


@dataclass
class Tried:
    product_id: str
    how: str
    verdict: str  # yes | no | unclear | no_image


@dataclass
class OptionRecord:
    question_id: str
    label: str
    value: str
    status: Literal["picture", "text_only", "dropped"]
    reason: str = ""
    title_evidence: int = 0
    tried: list[Tried] = field(default_factory=list)


@dataclass
class Verified:
    """The questions after verification and pruning, with the verified pictures and what happened."""

    questions: Questions
    picks: ImagePicks  # only verified pictures; `total` counts the options that remain
    options: list[OptionRecord] = field(default_factory=list)
    dropped_questions: list[dict[str, Any]] = field(default_factory=list)
    unverified: ImagePicks | None = None  # the title-matched picks before verification
    calls: int = 0
    images_offered: int = 0
    images_failed: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0  # added wall time: image fetches and vision calls, all rounds
    rounds: int = 0
    mode: str = "batched"
    images: dict[str, PreparedImage] = field(
        default_factory=dict
    )  # by thumbnail URL, for the audit
    notes: list[str] = field(default_factory=list)
    call_seconds: list[float] = field(default_factory=list)


# ---- the vision calls -------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    key: tuple[str, int, int]  # question id, option index, candidate rank
    question: Question
    option: Option
    pick: Pick


def item_line(n: int, question: Question, option: Option) -> str:
    return (
        f"Image {n}. Question: {clean_text(question.text, 120)} "
        f"Option: {clean_text(option.label, 40)} ({clean_text(option.value, 40)})."
    )


def fetch_pictures(
    items: Sequence[Item], thumbnailer: Thumbnailer | None, budget: Budget
) -> tuple[dict[tuple[str, int, int], PreparedImage], int]:
    """The thumbnails of `items` through the guarded fetcher (https, public hosts, size and time limits,
    EXIF stripped, downscaled). Returns the images by item key and how many failed."""
    from app.shop.prefs import fetch_images

    rows = [
        Rated(
            f"{i.key[0]}|{i.key[1]}|{i.key[2]}",
            "like",
            "",
            i.pick.title,
            None,
            "",
            i.pick.thumbnail_url,
        )
        for i in items
    ]
    got, report = fetch_images(rows, thumbnailer, budget)
    out: dict[tuple[str, int, int], PreparedImage] = {}
    for item in items:
        image = got.get(f"{item.key[0]}|{item.key[1]}|{item.key[2]}")
        if image is not None:
            out[item.key] = image
    return out, report.failed


def _ask(
    chunk: Sequence[tuple[str, PreparedImage]],
    prompt_name: str,
    tool: dict[str, Any],
    tool_name: str,
    max_tokens: int,
    model: type[BaseModel],
    caller: Caller,
    budget: Budget,
) -> BaseModel | None:
    """One batched vision call (`chunk` is (the user's line, the image) in order). None when the call
    or its output is unusable; no retry, since this is on the visitor's wait."""
    n = len(chunk)
    try:
        completion = caller(
            span_name=f"llm.{prompt_name}",
            prompt=load_prompt(prompt_name),
            model=budget.config.model_retrieval,
            user_text="\n".join(line for line, _ in chunk),
            budget=budget,
            max_tokens=max_tokens,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool_name},
            images=[image for _, image in chunk],
            effort=budget.config.effort_retrieval,
            disable_thinking=True,
        )
        parsed = model.model_validate(completion.tool_input or {})
    except (LLMOutputError, ValidationError):
        return None
    got = sorted(i.index for i in parsed.items)  # type: ignore[attr-defined]
    return parsed if got == list(range(1, n + 1)) else None


def _chunks(items: Sequence[Any], size: int) -> list[list[Any]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def verify_items(
    items: Sequence[Item],
    images: dict[tuple[str, int, int], PreparedImage],
    caller: Caller,
    budget: Budget,
    mode: str = "batched",
    workers: int = WORKERS,
) -> tuple[dict[tuple[str, int, int], str], int, list[float]]:
    """The verdict (yes, no, unclear) for each item that has an image, the number of calls made and
    each call's seconds. A call that fails gives `unclear` for its images, never `no`."""
    todo = [i for i in items if i.key in images]
    size = 1 if mode == "single" else BATCH
    groups = _chunks(todo, size)
    verdicts: dict[tuple[str, int, int], str] = {}
    seconds: list[float] = []

    def run(group: list[Item]) -> tuple[list[Item], BaseModel | None, float]:
        started = time.monotonic()
        chunk = [
            (item_line(n, g.question, g.option), images[g.key])
            for n, g in enumerate(group, start=1)
        ]
        out = _ask(
            chunk,
            VERIFY_PROMPT,
            verify_tool(),
            VERIFY_TOOL,
            VERIFY_MAX_TOKENS,
            VerdictsOut,
            caller,
            budget,
        )
        return group, out, time.monotonic() - started

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(groups) or 1))) as pool:
        for group, out, took in pool.map(run, groups):
            seconds.append(round(took, 3))
            by_index = {v.index: v.answer for v in out.items} if out is not None else {}  # type: ignore[attr-defined]
            for n, g in enumerate(group, start=1):
                verdicts[g.key] = by_index.get(n, "unclear")
    return verdicts, len(groups), seconds


# ---- verify and prune -------------------------------------------------------------------------


def _surviving(
    q: Question, records: list[OptionRecord], keep: list[int]
) -> tuple[Question | None, str]:
    options = [q.options[i] for i in keep]
    if len(options) < 2:
        return None, "fewer than two options left"
    return q.model_copy(update={"options": options}), ""


def verify_and_prune(
    questions: Questions,
    candidates: Sequence[SearchCandidate],
    *,
    category: str = "",
    thumbnailer: Thumbnailer | None,
    budget: Budget,
    caller: Caller = complete,
    mode: str = "batched",
    workers: int = WORKERS,
    clock: Callable[[], float] = time.monotonic,
) -> Verified:
    """Verify every option's picture and prune what cannot be shown (see the module docstring).

    An option is kept as a picture when a title-matched candidate is answered yes; as text only when it
    has title evidence but no verified picture and no picture was contradicted (unclear, no image,
    a failed fetch); dropped when no listing's title strictly shows it, or when every picture tried
    was a clear no and no other title evidence is left. A question left with fewer than two options is
    dropped."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    started = clock()
    cost0 = budget.cost_usd
    title = pick_option_images(questions, candidates, category=category)
    result = Verified(
        questions=questions, picks=ImagePicks(), unverified=title, mode=mode, images={}
    )
    qmap = {q.id: q for q in questions.questions}
    state: dict[tuple[str, int], dict[str, Any]] = {}
    for q in questions.questions:
        for i, option in enumerate(q.options):
            cands = title.candidates.get((q.id, i), [])
            state[(q.id, i)] = {
                "tried": [], "verified": None, "cands": cands, "evidence": title.title_evidence.get((q.id, i), 0),
                "label": option.label, "value": option.value,
            }  # fmt: skip
    for rank in range(MAX_ROUNDS):
        items = [
            Item((qid, i, rank), qmap[qid], qmap[qid].options[i], st["cands"][rank])
            for (qid, i), st in state.items()
            if st["verified"] is None and len(st["cands"]) > rank
        ]
        if not items:
            break
        result.rounds += 1
        result.images_offered += len(items)
        images, failed = fetch_pictures(items, thumbnailer, budget)
        result.images_failed += failed
        for k, img in images.items():
            result.images[state[(k[0], k[1])]["cands"][k[2]].thumbnail_url] = img
        verdicts, calls, seconds = verify_items(items, images, caller, budget, mode, workers)
        result.calls += calls
        result.call_seconds += seconds
        for item in items:
            st = state[(item.key[0], item.key[1])]
            verdict = verdicts.get(item.key, "no_image")
            st["tried"].append(Tried(item.pick.product_id, item.pick.how, verdict))
            if verdict == "yes":
                st["verified"] = item.pick
    # classify, prune, rebuild
    kept_questions: list[Question] = []
    picks = ImagePicks()
    for q in questions.questions:
        keep: list[int] = []
        verified_for: dict[int, Pick] = {}
        for i, option in enumerate(q.options):
            st = state[(q.id, i)]
            tried: list[Tried] = st["tried"]
            record = OptionRecord(
                q.id,
                option.label,
                option.value,
                "dropped",
                title_evidence=st["evidence"],
                tried=tried,
            )
            n_with_picture = len(st["cands"])
            all_no = bool(tried) and all(t.verdict == "no" for t in tried)
            if st["verified"] is not None:
                record.status, record.reason = "picture", "verified"
                verified_for[i] = st["verified"]
            elif st["evidence"] == 0:
                record.reason = "no listing's title shows it as the product's main part"
            elif all_no and len(tried) >= n_with_picture and st["evidence"] <= n_with_picture:
                record.reason = "every picture tried showed something else"
            else:
                record.status = "text_only"
                record.reason = (
                    "no picture to check"
                    if not tried
                    else "no picture was confirmed (unclear or unusable)"
                )
            if record.status != "dropped":
                keep.append(i)
            result.options.append(record)
        survivor, why = _surviving(q, [], keep)
        if survivor is None:
            result.dropped_questions.append(
                {"id": q.id, "text": q.text, "reason": why, "options_left": len(keep)}
            )
            continue
        kept_questions.append(survivor)
        for new_i, old_i in enumerate(keep):
            picks.total += 1
            pick = verified_for.get(old_i)
            picks.by_option[(q.id, new_i)] = pick
            picks.candidates[(q.id, new_i)] = [pick] if pick else []
            picks.title_evidence[(q.id, new_i)] = state[(q.id, old_i)]["evidence"]
            if pick is not None:
                picks.with_image += 1
    result.questions = questions.model_copy(update={"questions": kept_questions})
    result.picks = picks
    result.cost_usd = round(budget.cost_usd - cost0, 6)
    result.seconds = round(clock() - started, 3)
    return result


# ---- the independent audit --------------------------------------------------------------------


@dataclass
class AuditRow:
    question_id: str
    option_index: int
    dimension: str
    label: str
    named: str  # what the audit saw ("" when it was not auditable)
    agree: (
        bool | None
    )  # None: not auditable (a style, size or feature, or the picture was unusable)


@dataclass
class Audit:
    rows: list[AuditRow] = field(default_factory=list)
    calls: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0

    @property
    def auditable(self) -> int:
        return sum(1 for r in self.rows if r.agree is not None)

    @property
    def agreed(self) -> int:
        return sum(1 for r in self.rows if r.agree)

    @property
    def accuracy(self) -> float | None:
        return self.agreed / self.auditable if self.auditable else None


def named_agrees(dimension: str, option: Option, named: str) -> bool:
    """Whether what the audit named matches the option: after the vocabulary's own normalization
    (gray is grey, walnut is a wood) or by a shared word (stems), for any part of the option's label
    ("Black or gray" accepts either)."""
    said = " ".join(words(named))
    if not said or said == "unclear":
        return False
    dim = {"color": "color", "material": "material", "shape": "shape"}.get(dimension, "")
    norm_said = normalize(dim, said) if dim else None
    for variant in option_variants(option):
        if dim:
            norm = normalize(dim, variant)
            if norm and norm != OTHER and norm == norm_said:
                return True
        if stems(variant) & stems(said):
            return True
    return False


def audit_pictures(
    questions: Questions,
    picks: ImagePicks,
    images: dict[str, PreparedImage],
    *,
    budget: Budget,
    caller: Caller = complete,
    workers: int = WORKERS,
) -> Audit:
    """The independent accuracy check: every SHOWN picture of a colour, material or shape option is
    given to the audit prompt, which is not told the option; code compares what it names with the
    option. Other options (style, size, feature) are listed as not auditable."""
    audit = Audit()
    started = time.monotonic()
    cost0 = budget.cost_usd
    todo: list[tuple[str, int, Question, Option, PreparedImage]] = []
    for q in questions.questions:
        for i, option in enumerate(q.options):
            pick = picks.for_option(q.id, i)
            if pick is None:
                continue
            image = images.get(pick.thumbnail_url)
            if q.dimension in AUDITABLE and image is not None:
                todo.append((q.id, i, q, option, image))
            else:
                audit.rows.append(AuditRow(q.id, i, q.dimension, option.label, "", None))
    groups = _chunks(todo, BATCH)

    def run(group: list[tuple[str, int, Question, Option, PreparedImage]]) -> BaseModel | None:
        chunk = [(f"Image {n}.", g[4]) for n, g in enumerate(group, start=1)]
        return _ask(
            chunk,
            AUDIT_PROMPT,
            audit_tool(),
            AUDIT_TOOL,
            AUDIT_MAX_TOKENS,
            AuditOut,
            caller,
            budget,
        )

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(groups) or 1))) as pool:
        for group, out in zip(groups, pool.map(run, groups), strict=True):
            audit.calls += 1
            by_index = {v.index: v for v in out.items} if out is not None else {}  # type: ignore[attr-defined]
            for n, (qid, i, q, option, _img) in enumerate(group, start=1):
                row = by_index.get(n)
                if row is None:
                    audit.rows.append(AuditRow(qid, i, q.dimension, option.label, "", None))
                    continue
                said = {"color": row.main_color, "material": row.main_material, "shape": row.shape}[
                    q.dimension
                ]
                audit.rows.append(
                    AuditRow(
                        qid,
                        i,
                        q.dimension,
                        option.label,
                        said,
                        named_agrees(q.dimension, option, said),
                    )
                )
    audit.cost_usd = round(budget.cost_usd - cost0, 6)
    audit.seconds = round(time.monotonic() - started, 3)
    return audit
