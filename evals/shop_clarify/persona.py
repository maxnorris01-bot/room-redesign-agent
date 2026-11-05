"""The scripted persona's answers to generated questions.

The persona has one line per trait (`cases.json`, key = dimension). A question is matched to a trait by
its dimension first, then by words (a trait's key or answer appearing in the question text or an
option). The option is the one whose words overlap the persona's answer most; with no overlap the
closest option by spelling (a size range is matched by number); below that, the question is marked
"unanswerable by persona". The persona never sees the pipeline and the question writer never sees the
persona.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.shop.clarify import STOP_WORDS, Answer, Option, Question, size_in_range, stems, words

CLOSE_ENOUGH = 0.6  # spelling similarity that counts as "the closest option"


def content(text: str) -> set[str]:
    """The stems of a text's words, without the small words ("in", "to", "under") every size label shares."""
    return {s for s in stems(text) if s not in STOP_WORDS}


@dataclass(frozen=True)
class PersonaPick:
    question_id: str
    trait: str  # "" when no trait matched
    wanted: str
    option: Option | None
    how: str  # "words" | "closest" | "range" | "unanswerable: ..."


def _trait_for(question: Question, answers: Mapping[str, str]) -> str:
    if question.dimension in answers:
        return question.dimension
    text = " ".join([question.text, *[f"{o.label} {o.value}" for o in question.options]])
    have = content(text)
    for trait, wanted in answers.items():
        if content(trait) & have or content(wanted) & have:
            return trait
    return ""


def _range_pick(question: Question, wanted: str) -> Option | None:
    """A numeric answer ("8x10", "60 in") against ranges ("40 to 60 in"): the range it falls in."""
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", wanted)]
    if not nums:
        return None
    if re.search(r"\d\s*x\s*\d", wanted.lower()):  # "8x10": a rug size, in feet
        nums = [n * 12 for n in nums]
    for option in question.options:
        if size_in_range(f"{max(nums):g} in", option.value):
            return option
    return None


def pick_for(question: Question, answers: Mapping[str, str]) -> PersonaPick:
    trait = _trait_for(question, answers)
    if not trait:
        return PersonaPick(question.id, "", "", None, "unanswerable: no persona trait matches")
    wanted = answers[trait]
    want = content(wanted)
    scored = [(len(want & content(f"{o.label} {o.value}")), o) for o in question.options]
    best = max(scored, key=lambda s: s[0], default=(0, None))
    if best[1] is not None and best[0] > 0:
        return PersonaPick(question.id, trait, wanted, best[1], "words")
    if question.kind == "size_range":
        ranged = _range_pick(question, wanted)
        if ranged is not None:
            return PersonaPick(question.id, trait, wanted, ranged, "range")

    def spelling(o: Option) -> float:
        return max(
            difflib.SequenceMatcher(None, wanted.lower(), t.lower()).ratio()
            for t in (o.label, o.value)
        )

    close = max(
        ((spelling(o), o) for o in question.options), key=lambda s: s[0], default=(0.0, None)
    )
    if close[1] is not None and close[0] >= CLOSE_ENOUGH:
        return PersonaPick(question.id, trait, wanted, close[1], "closest")
    return PersonaPick(
        question.id, trait, wanted, None, "unanswerable: no option is close to the answer"
    )


def answer_questions(
    questions: Sequence[Question], answers: Mapping[str, str]
) -> tuple[list[Answer], list[PersonaPick]]:
    """The persona's pick for every question; the `Answer`s are only the answerable ones."""
    picks = [pick_for(q, answers) for q in questions]
    by_id = {q.id: q for q in questions}
    return [Answer(by_id[p.question_id], p.option) for p in picks if p.option is not None], picks


__all__ = ["PersonaPick", "answer_questions", "pick_for", "words"]
