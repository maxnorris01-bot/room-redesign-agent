"""Clarifying questions for a search: an OFFLINE SPIKE (ADR 0015).

Nothing in the API, the service or the search pipeline imports this module, and it changes no
behaviour of theirs. The idea: after one quick first fetch for the base query, ONE small model call
reads the first listings and proposes up to five questions about the defining traits of the product
that the request has not decided, each with two to four options. Pictures for the options are chosen
by code (a listing whose TITLE carries the option's value). Answers become a modified `ShopQuery`
that the pipeline already accepts (`apply_answers`): typed colour, material and shape quotas, style
words, and must-haves.

Three public functions, in the order they are used:

    analyze_facets(request, parsed, candidates) -> Questions     one model call (forced tool call)
    pick_option_images(questions, candidates) -> ImagePicks      no model call: title matching
    apply_answers(parsed, answers) -> ShopQuery                  no model call: existing mechanisms

plus `quality_flags` (code-computed checks on a set of questions) and the size/prompt helpers.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.config import Config
from app.llm import Budget, Completion, LLMOutputError, complete
from app.prompts import load_prompt
from app.schemas import to_output_schema
from app.shop.models import MAX_SEARCH_TEXT_CHARS, MAX_TERM_CHARS, MAX_TERMS, ShopQuery
from app.shop.parse import color_ids
from app.shop.vocab import typed_shape_words, typed_values
from app.shopping import SearchCandidate
from app.sizes import parse_title_sizes

CLARIFY_TOOL = "record_clarifying_questions"
CLARIFY_PROMPT = "shop_clarify"
MAX_QUESTIONS = 5
MIN_OPTIONS, MAX_OPTIONS = 2, 4
CANDIDATES_SHOWN = 40
CLARIFY_MAX_TOKENS = 1100
EVIDENCE_MAX = 3
MAX_TEXT_CHARS = 120
MAX_LABEL_CHARS = 40
DIMENSIONS = ("color", "material", "shape", "style", "size", "feature")
Dimension = Literal["color", "material", "shape", "style", "size", "feature"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- the model's output (the tool's input schema) ---------------------------------------------


class OptionOut(_Strict):
    label: str
    value: str
    evidence_product_ids: list[str]


class QuestionOut(_Strict):
    id: str
    text: str
    kind: Literal["choice", "size_range"]
    dimension: Dimension
    options: list[OptionOut]


class QuestionsOut(_Strict):
    questions: list[QuestionOut]


# ---- the validated questions ------------------------------------------------------------------


class Option(_Strict):
    label: str
    value: str
    evidence_product_ids: list[str] = []  # product ids of listings whose title shows the value


class Question(_Strict):
    id: str
    text: str
    kind: Literal["choice", "size_range"]
    dimension: Dimension
    options: list[Option]


class Questions(_Strict):
    """What `analyze_facets` returns: the questions, and how the call went."""

    questions: list[Question] = []
    notes: list[str] = []  # what the validation changed or dropped
    seconds: float = 0.0  # the model call
    cost_usd: float = 0.0
    model: str = ""
    shown: int = 0  # listings the model was shown


def tool_definition() -> dict[str, Any]:
    schema = to_output_schema(QuestionsOut)
    schema.pop("title", None)
    return {
        "name": CLARIFY_TOOL,
        "description": "Record the clarifying questions for this search.",
        "strict": True,
        "input_schema": schema,
    }


# ---- text helpers -----------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    {
        "in",
        "inch",
        "inches",
        "to",
        "the",
        "and",
        "or",
        "with",
        "a",
        "an",
        "of",
        "for",
        "under",
        "over",
    }
)


STOP_WORDS = _STOP


def clean_text(text: str, limit: int) -> str:
    """Printable, no angle brackets, whitespace collapsed, bounded."""
    printable = "".join(c if c.isprintable() else " " for c in text)
    return " ".join(printable.replace("<", " ").replace(">", " ").replace("|", "/").split())[:limit]


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("-", " "))


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("es"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s"):
        return word[:-1]
    return word


def stems(text: str) -> set[str]:
    return {_stem(w) for w in words(text)}


def size_text(title: str, category: str | None = None) -> str:
    """The size a title states, in inches, as a few words ("w 60 in, h 30 in"), or "" when none."""
    dims = parse_title_sizes(title, category or None)
    parts = []
    for key, tag in (("width_m", "w"), ("length_m", "l"), ("depth_m", "d"), ("height_m", "h")):
        if key in dims:
            parts.append(f"{tag} {dims[key] / 0.0254:.0f} in")
    return ", ".join(parts)


def candidate_rows(
    candidates: Sequence[SearchCandidate], category: str = "", limit: int = CANDIDATES_SHOWN
) -> tuple[list[str], dict[str, str]]:
    """The numbered listing rows the model sees (`n | title | price | seller | size`) and the map
    from each number to the product id. Never a link or a thumbnail."""
    rows: list[str] = []
    ids: dict[str, str] = {}
    for n, c in enumerate(candidates[:limit], start=1):
        price = f"${c.price:,.0f}" if c.price is not None else "no price"
        size = size_text(c.title, category) or "no size"
        rows.append(
            f"{n} | {clean_text(c.title, 150)} | {price} | "
            f"{clean_text(c.seller, 40) or 'unknown'} | {size}"
        )
        ids[str(n)] = c.product_id
    return rows, ids


def stated_attributes(parsed: ShopQuery) -> str:
    """What the request already decided, as the prompt lists it."""
    parts = [
        f"product: {parsed.category or parsed.search_text}",
        f"style: {', '.join(parsed.style_terms) or 'none'}",
        f"material: {', '.join(parsed.material_terms) or 'none'}",
        f"colour: {', '.join(parsed.color_terms) or 'none'}",
        f"shape: {', '.join(parsed.shape_terms) or 'none'}",
        f"must have: {', '.join(parsed.must_have) or 'none'}",
        f"avoid: {', '.join(parsed.avoid) or 'none'}",
    ]
    return "; ".join(parts)


def user_text(request: str, parsed: ShopQuery, rows: list[str]) -> str:
    return "\n".join(
        [
            f"<request>{clean_text(request, 300)}</request>",
            f"Stated attributes: {stated_attributes(parsed)}",
            "Listings (number | title | price | seller | size):",
            *rows,
        ]
    )


# ---- 1. analyze_facets ------------------------------------------------------------------------

Caller = Callable[..., Completion]


def analyze_facets(
    request: str,
    parsed: ShopQuery,
    candidates: Sequence[SearchCandidate],
    *,
    budget: Budget | None = None,
    caller: Caller = complete,
    limit: int = CANDIDATES_SHOWN,
    clock: Callable[[], float] = time.monotonic,
) -> Questions:
    """Up to five clarifying questions about `request`, from the first `limit` candidates.

    One model call (the retrieval model, a forced tool call, thinking disabled), no retry: this is
    on the visitor's wait. An unusable answer gives no questions and a note, never an exception
    (a budget error still propagates). The questions are validated in code (`sanitize`)."""
    budget = budget or Budget(Config.from_env())
    config = budget.config
    shown = list(candidates[:limit])
    if not shown:
        return Questions(notes=["no candidates to read"], model=config.model_retrieval)
    rows, ids = candidate_rows(shown, parsed.category, limit)
    before = budget.cost_usd
    started = clock()
    out: QuestionsOut | None = None
    note = ""
    try:
        completion = caller(
            span_name="llm.shop_clarify",
            prompt=load_prompt(CLARIFY_PROMPT),
            model=config.model_retrieval,
            user_text=user_text(request, parsed, rows),
            budget=budget,
            max_tokens=CLARIFY_MAX_TOKENS,
            tools=[tool_definition()],
            tool_choice={"type": "tool", "name": CLARIFY_TOOL},
            effort=config.effort_retrieval,
            disable_thinking=True,
        )
        out = QuestionsOut.model_validate(completion.tool_input or {})
    except LLMOutputError as exc:
        note = f"the model call failed: {str(exc)[:80]}"
    except ValidationError:
        note = "the model's output did not match the schema"
    seconds = clock() - started
    result = Questions(
        seconds=round(seconds, 3),
        cost_usd=round(budget.cost_usd - before, 6),
        model=config.model_retrieval,
        shown=len(shown),
    )
    if out is None:
        result.notes.append(note)
        return result
    questions, notes = sanitize(out, ids)
    result.questions, result.notes = questions, notes
    return result


def sanitize(out: QuestionsOut, ids: dict[str, str]) -> tuple[list[Question], list[str]]:
    """The model's questions as kept: text cleaned and bounded, ids unique, a size question tagged
    as one, evidence numbers turned into product ids (unknown numbers dropped), at most five
    questions. Option counts are NOT forced into 2 to 4: a question outside that range is kept and
    flagged by `quality_flags` so the harness can see it."""
    notes: list[str] = []
    questions: list[Question] = []
    used: set[str] = set()
    for q in out.questions:
        text = clean_text(q.text, MAX_TEXT_CHARS)
        if not text:
            notes.append("dropped a question with no text")
            continue
        qid = re.sub(r"[^a-z0-9_]+", "_", q.id.lower()).strip("_") or f"q{len(questions) + 1}"
        while qid in used:
            qid += "_"
        used.add(qid)
        kind: Literal["choice", "size_range"] = (
            "size_range" if q.kind == "size_range" or q.dimension == "size" else "choice"
        )
        dimension: Dimension = "size" if kind == "size_range" else q.dimension
        options: list[Option] = []
        for o in q.options:
            label = clean_text(o.label, MAX_LABEL_CHARS)
            value = " ".join(words(clean_text(o.value, MAX_LABEL_CHARS))) or " ".join(words(label))
            if not label or not value:
                notes.append(f"{qid}: dropped an empty option")
                continue
            if any(x.value == value for x in options):
                notes.append(f"{qid}: dropped a repeated option '{value}'")
                continue
            evidence = [ids[n.strip()] for n in o.evidence_product_ids if n.strip() in ids]
            if len(evidence) < len(o.evidence_product_ids):
                notes.append(f"{qid}: ignored evidence numbers that are not listings")
            options.append(
                Option(
                    label=label,
                    value=value,
                    evidence_product_ids=list(dict.fromkeys(evidence))[:EVIDENCE_MAX],
                )
            )
        questions.append(
            Question(id=qid, text=text, kind=kind, dimension=dimension, options=options)
        )
    if len(questions) > MAX_QUESTIONS:
        notes.append(f"kept the first {MAX_QUESTIONS} of {len(questions)} questions")
        questions = questions[:MAX_QUESTIONS]
    return questions, notes


# ---- 2. pick_option_images --------------------------------------------------------------------

# Round 2: a picture must show the product's MAIN body in the option's value, so the title match is
# strict. A value word does not count where the title says it describes something secondary: after a
# lead word ("with Metal Handle", "and Gold"), next to hardware ("Black Metal Legs"), or, for a
# colour question, a top ("Oak Dresser, Black Top"). All the word lists are here.
SECONDARY_LEADS = ("with", "and", "w/", "&", "plus", "including", "featuring")
HARDWARE_WORDS = (
    "handle", "handles", "pull", "pulls", "knob", "knobs", "hardware", "leg", "legs", "base",
    "trim", "accent", "accents", "feet", "foot", "hinge", "hinges",
)  # fmt: skip
TOP_WORDS = ("top",)  # secondary only when the question is about the body's colour
LEAD_WINDOW = 4  # a lead word this many tokens or fewer before the value makes it secondary
AFTER_WINDOW = 2  # a hardware (or top) word this many tokens or fewer after it does too
EARLY_WORDS = 6  # a match in the first six words of the title is preferred
NOUN_WINDOW = 3  # so is one directly before the product noun ("black dresser")
MAX_PICTURE_CANDIDATES = 3  # per option: the verifier looks at up to this many
_TOKEN = re.compile(r"w/|&|[a-z0-9]+")
_HARDWARE_STEMS = frozenset(_stem(w) for w in HARDWARE_WORDS)
_TOP_STEMS = frozenset(_stem(w) for w in TOP_WORDS)
_VARIANT_SPLIT = re.compile(r"\s+or\s+|\s+and\s+|/|,")


def title_tokens(title: str) -> list[str]:
    """A title as lowercase tokens: a hyphen is a space, `w/` and `&` are tokens of their own."""
    return _TOKEN.findall(title.lower().replace("-", " "))


def option_variants(option: Option) -> list[str]:
    """The value words an option can be shown by: its `value`, and each part of its label split on
    "or", "and", "/" or a comma ("Black or gray" gives black and gray). A part longer than three
    words, or only small words, is left out."""
    out = [" ".join(words(option.value))]
    for part in _VARIANT_SPLIT.split(option.label.lower()):
        ws = words(part)
        if 1 <= len(ws) <= 3 and not all(w in _STOP for w in ws):
            text = " ".join(ws)
            if text not in out:
                out.append(text)
    return [v for v in out if v]


def _spans(tokens: list[str], value_words: list[str]) -> list[tuple[int, int]]:
    """Where the value's words sit in the tokens: each word in order, within a window as wide as the
    value (plus one), so "6 drawer" is "6-Drawer" and "light wood" is "Light Oak Wood" but not a
    "light" at the start and a "wood" at the end."""
    want = [_stem(w) for w in value_words if w not in _STOP]
    if not want:
        return []
    stemmed = [_stem(t) for t in tokens]
    found: list[tuple[int, int]] = []
    for i, tok in enumerate(stemmed):
        if tok != want[0]:
            continue
        end, ok = i, True
        for w in want[1:]:
            nxt = next(
                (j for j in range(end + 1, min(len(tokens), end + 3)) if stemmed[j] == w), None
            )
            if nxt is None:
                ok = False
                break
            end = nxt
        if ok:
            found.append((i, end))
    return found


def is_secondary(tokens: list[str], span: tuple[int, int], dimension: str = "") -> bool:
    """Whether a match describes a secondary part of the product: a lead word shortly before it,
    hardware shortly after it, or (for a colour question) a top."""
    a, b = span
    if any(t in SECONDARY_LEADS for t in tokens[max(0, a - LEAD_WINDOW) : a]):
        return True
    after = [_stem(t) for t in tokens[b + 1 : b + 1 + AFTER_WINDOW]]
    if any(t in _HARDWARE_STEMS for t in after):
        return True
    return dimension == "color" and any(t in _TOP_STEMS for t in after)


def match_rank(
    title: str, option: Option, dimension: str = "", category: str = ""
) -> tuple[int, int] | None:
    """How well a title shows an option's value as the product's main body: None when it does not
    (no match, or only a secondary one), else `(0, position)` for a preferred match (in the first
    six words or directly before the product noun) and `(1, position)` for an acceptable one. Lower
    is better."""
    tokens = title_tokens(title)
    nouns = {_stem(w) for w in words(category)}
    best: tuple[int, int] | None = None
    for variant in option_variants(option):
        for span in _spans(tokens, words(variant)):
            if is_secondary(tokens, span, dimension):
                continue
            a, b = span
            before_noun = any(_stem(t) in nouns for t in tokens[b + 1 : b + 1 + NOUN_WINDOW])
            rank = (0 if a < EARLY_WORDS or before_noun else 1, a)
            if best is None or rank < best:
                best = rank
    return best


def title_has_value(title: str, value: str, dimension: str = "", category: str = "") -> bool:
    """Whether a title shows `value` as the product's main body (the strict match above; the value
    is the whole option, so a label of "A or B" is not split here)."""
    return match_rank(title, Option(label=value, value=value), dimension, category) is not None


@dataclass(frozen=True)
class Pick:
    """The listing chosen to picture one option."""

    product_id: str
    title: str
    thumbnail_url: str
    how: str  # "evidence" (the model's own pointer, confirmed by the strict match) or "scan"


@dataclass
class ImagePicks:
    by_option: dict[tuple[str, int], Pick | None] = field(default_factory=dict)
    # up to three title-matching listings with a picture per option, best first (what the
    # verifier tries)
    candidates: dict[tuple[str, int], list[Pick]] = field(default_factory=dict)
    # listings whose title strictly shows the value, with or without a picture
    title_evidence: dict[tuple[str, int], int] = field(default_factory=dict)
    total: int = 0
    with_image: int = 0

    @property
    def coverage(self) -> float:
        return self.with_image / self.total if self.total else 0.0

    def for_option(self, question_id: str, index: int) -> Pick | None:
        return self.by_option.get((question_id, index))


_RANGE = re.compile(r"(\d+(?:\.\d+)?)")


def size_in_range(title: str, value: str, category: str = "") -> bool:
    """A size option written as a range of inches ("40 to 60 in", "under 40 in", "over 60 in"):
    whether the longest side the title states is inside it. False when the title states no size
    or the value is not a numeric range."""
    nums = [float(n) for n in _RANGE.findall(value)]
    if not nums:
        return False
    dims = parse_title_sizes(title, category or None)
    if not dims:
        return False
    longest = max(dims.values()) / 0.0254
    low = value.lower()
    if low.startswith(("under", "less", "below", "up to")) and len(nums) == 1:
        return longest < nums[0]
    if low.startswith(("over", "more", "above")) and len(nums) == 1:
        return longest > nums[0]
    if len(nums) >= 2:
        return min(nums[:2]) <= longest <= max(nums[:2])
    return False


def _rank_for(
    c: SearchCandidate, option: Option, q: Question, category: str
) -> tuple[int, int] | None:
    """The title match rank of listing `c` for option `option` of question `q` (None: no match)."""
    if q.kind == "size_range" and size_in_range(c.title, option.value, category):
        return (0, 0)
    return match_rank(c.title, option, q.dimension, category)


def has_picture(c: SearchCandidate) -> bool:
    return bool(c.thumbnail_url) and (c.thumbnail_url or "").startswith("https://")


def pick_option_images(
    questions: Questions | Sequence[Question],
    candidates: Sequence[SearchCandidate],
    *,
    category: str = "",
) -> ImagePicks:
    """For each option, the listing whose TITLE best shows the value as the product's MAIN body
    (`match_rank`) and that has an https thumbnail; the model's evidence listings rank first, but
    only when the strict match confirms them. Up to three such listings per option are kept for the
    verifier (`candidates`). No two options of one question get the same first pick. No model call.

    Coverage is options with a picture over all options. A "no preference" choice is not an option
    here (the page adds it) and needs no picture. These are title-matched pictures: whether they are
    RIGHT is `clarify_verify`'s job."""
    items = questions.questions if isinstance(questions, Questions) else list(questions)
    picks = ImagePicks()
    for q in items:
        taken: set[str] = set()
        for i, option in enumerate(q.options):
            picks.total += 1
            evidence = set(option.evidence_product_ids)
            ranked: list[tuple[tuple[int, int, int, int], SearchCandidate]] = []
            matched = 0
            for pos, c in enumerate(candidates):
                rank = _rank_for(c, option, q, category)
                if rank is None:
                    continue
                matched += 1
                if has_picture(c):
                    ranked.append(
                        ((0 if c.product_id in evidence else 1, rank[0], rank[1], pos), c)
                    )
            ranked.sort(key=lambda r: r[0])
            picks.title_evidence[(q.id, i)] = matched
            free = [c for _k, c in ranked if c.product_id not in taken]
            top = [
                Pick(
                    c.product_id,
                    c.title,
                    c.thumbnail_url or "",
                    "evidence" if c.product_id in evidence else "scan",
                )
                for c in (free or [c for _k, c in ranked])[:MAX_PICTURE_CANDIDATES]
            ]
            picks.candidates[(q.id, i)] = top
            chosen = top[0] if top else None
            if chosen is not None:
                taken.add(chosen.product_id)
                picks.with_image += 1
            picks.by_option[(q.id, i)] = chosen
    return picks


# ---- 3. apply_answers -------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    """A visitor's choice for one question; `option` is None for "no preference"."""

    question: Question
    option: Option | None


@dataclass
class Applied:
    query: ShopQuery
    mapped: list[dict[str, str]] = field(default_factory=list)  # what each answer became


MAX_ADDED_SEARCH_WORDS = 2


def _add(items: list[str], term: str) -> list[str]:
    term = " ".join(term.lower().split())[:MAX_TERM_CHARS].strip(" .,;:")
    if not term or term in items:
        return items
    return [*items, term][:MAX_TERMS]


def map_answers(parsed: ShopQuery, answers: Sequence[Answer]) -> Applied:
    """The `ShopQuery` the pipeline already accepts, with the answers applied, and a record of what
    each answer became. Mechanisms used, all existing ones (see `app.shop.parse.build_query`):

    * colour:    `color_terms` (colour ids; a typed quota, 70% of a batch), else a must-have;
    * material:  `material_terms`, a typed quota when the vocabulary knows the word, else a soft
                 preference the ranker reads;
    * shape:     `shape_terms` (a typed quota for round, oval, rectangular, square), else a
                 must-have;
    * style:     `style_terms` (soft, read by the ranker);
    * size and feature: `must_have` (soft, read by the ranker); a size range is written as words,
                 e.g. "40 to 60 in wide".

    Like the parse step, the words of a colour, material, shape or style answer are also added to
    `search_text` (at most two words in all, within the length limit), because that is how a typed
    "walnut dresser" reaches Serper. Price, category and everything the request stated are
    untouched.
    A question answered "no preference" (option None) changes nothing."""
    q = parsed.model_copy(deep=True)
    mapped: list[dict[str, str]] = []
    extra_words: list[str] = []
    for a in answers:
        if a.option is None:
            continue
        value, label, dim = a.option.value, a.option.label, a.question.dimension
        how = ""
        if dim == "color":
            ids, left = color_ids([value])
            for cid in ids:
                if cid not in q.color_terms:
                    q.color_terms = [*q.color_terms, cid]
            if ids:
                how = f"color_terms += {', '.join(ids)}"
                extra_words.append(value)
            else:
                q.must_have = _add(q.must_have, f"{left[0] if left else value} color")
                how = f"must_have += {value} color"
        elif dim == "material":
            if typed_values("material", value):
                q.material_terms = _add(q.material_terms, value)
                how = f"material_terms += {value} (typed quota)"
            else:
                q.material_terms = _add(q.material_terms, value)
                how = f"material_terms += {value} (soft: the vocabulary has no such value)"
            extra_words.append(value)
        elif dim == "shape":
            shapes = typed_shape_words(value)
            if shapes:
                q.shape_terms = [*q.shape_terms, *[s for s in shapes if s not in q.shape_terms]][
                    :MAX_TERMS
                ]
                how = f"shape_terms += {', '.join(shapes)} (typed quota)"
                extra_words.append(shapes[0])
            else:
                q.must_have = _add(q.must_have, f"{value} shape")
                how = f"must_have += {value} shape"
        elif dim == "style":
            q.style_terms = _add(q.style_terms, value)
            how = f"style_terms += {value}"
            extra_words.append(value)
        elif dim == "size":
            phrase = value if re.search(r"\d", value) else f"{value} size"
            if re.search(r"\d", value) and not re.search(r"\b(in|inch|inches|ft|cm)\b", value):
                phrase += " in"
            q.must_have = _add(q.must_have, f"{phrase} wide" if re.search(r"\d", value) else phrase)
            how = f"must_have += {q.must_have[-1] if q.must_have else phrase}"
        else:
            q.must_have = _add(q.must_have, value)
            how = f"must_have += {value}"
        mapped.append({"question": a.question.id, "dimension": dim, "answer": label, "became": how})
    text_words = set(words(q.search_text))
    added: list[str] = []
    for phrase in extra_words:
        for w in words(phrase):
            if w in _STOP or w in text_words or w in added:
                continue
            if len(added) < MAX_ADDED_SEARCH_WORDS:
                added.append(w)
    if added:
        text = " ".join([*added, q.search_text])[:MAX_SEARCH_TEXT_CHARS].strip()
        q.search_text = text
    return Applied(query=q, mapped=mapped)


def apply_answers(parsed: ShopQuery, answers: Sequence[Answer]) -> ShopQuery:
    """The parsed request with the answers applied (see `map_answers`)."""
    return map_answers(parsed, answers).query


# ---- code-computed quality flags --------------------------------------------------------------

PRICE_WORDS = re.compile(r"\b(price|budget|cost|cheap|afford|expensive|spend|\$)", re.IGNORECASE)
BRAND_WORDS = re.compile(r"\b(brand|store|seller|retailer|maker|manufacturer)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Flag:
    question_id: str  # "" for a flag about the whole set
    kind: str  # restates_request | duplicate_or_overlap | option_count | asks_price | asks_brand
    detail: str


def _stated_words(parsed: ShopQuery, request: str) -> set[str]:
    terms = [
        *parsed.style_terms,
        *parsed.material_terms,
        *parsed.must_have,
        *parsed.shape_terms,
        request,
    ]
    return {_stem(w) for t in terms for w in words(t)} | {
        _stem(w) for c in parsed.color_terms for w in words(c)
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def quality_flags(
    request: str, parsed: ShopQuery, questions: Questions | Sequence[Question]
) -> list[Flag]:
    """Checks computed in code: a question that restates the request (its trait is already
    stated, or every option's words are already in the request), duplicate or overlapping
    questions (the same trait twice, or options that share half their words), a question with
    fewer than 2 or more than 4 options, and questions about price or brand (which the prompt
    forbids)."""
    items = questions.questions if isinstance(questions, Questions) else list(questions)
    flags: list[Flag] = []
    stated_dims = {
        "color": bool(parsed.color_terms),
        "material": bool(parsed.material_terms),
        "shape": bool(parsed.shape_terms),
        "style": bool(parsed.style_terms),
    }
    request_words = _stated_words(parsed, request)
    for q in items:
        if not MIN_OPTIONS <= len(q.options) <= MAX_OPTIONS:
            flags.append(Flag(q.id, "option_count", f"{len(q.options)} options"))
        if stated_dims.get(q.dimension):
            flags.append(
                Flag(q.id, "restates_request", f"the request already states the {q.dimension}")
            )
        elif q.options and all(stems(o.value) <= request_words for o in q.options):
            flags.append(Flag(q.id, "restates_request", "every option's words are in the request"))
        if PRICE_WORDS.search(q.text):
            flags.append(Flag(q.id, "asks_price", q.text))
        if BRAND_WORDS.search(q.text):
            flags.append(Flag(q.id, "asks_brand", q.text))
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            why = ""
            if a.dimension == b.dimension and a.dimension != "feature":
                why = f"both ask about {a.dimension}"
            elif _jaccard(stems(a.text), stems(b.text)) >= 0.7:
                why = "nearly the same wording"
            else:
                wa = {s for o in a.options for s in stems(o.value)}
                wb = {s for o in b.options for s in stems(o.value)}
                if _jaccard(wa, wb) >= 0.5:
                    why = "their options share half their words"
            if why:
                flags.append(Flag(b.id, "duplicate_or_overlap", f"{a.id} and {b.id}: {why}"))
    return flags
