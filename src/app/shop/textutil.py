"""Small text helpers shared by the preference code: phrase normalizing and matching."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.shop.models import MAX_TERM_CHARS

STOP_WORDS = frozenset({"with", "and", "the", "for", "from", "that", "this", "piece", "pieces"})
MIN_CONTAIN_CHARS = 4  # a shorter phrase only matches a removed entry exactly


def normalize_phrase(text: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in " -'" else " " for c in text.lower())
    return " ".join(cleaned.split())[:MAX_TERM_CHARS].strip(" -'")


def phrases_match(a: str, b: str) -> bool:
    """Same phrase, or one contains the other (both at least `MIN_CONTAIN_CHARS` long)."""
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= MIN_CONTAIN_CHARS and shorter in longer


def sig_words(text: str) -> list[str]:
    """The words of a phrase that carry meaning: three letters or more, not filler."""
    return [
        w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3 and w not in STOP_WORDS
    ]


def text_matches(phrase: str, text: str) -> bool:
    """Whether `text` (a title) shows `phrase`: all its significant words, or both when it has more
    than two. "brushed brass" needs both words; "walnut" needs the one."""
    words = sig_words(phrase)
    if not words:
        return False
    lowered = text.lower()
    return sum(1 for w in words if w in lowered) >= min(2, len(words))


def mentions(phrase: str, comment: str) -> bool:
    """Whether a comment names (any significant word of) the phrase."""
    lowered = comment.lower()
    return any(w in lowered for w in sig_words(phrase))


@dataclass(frozen=True)
class Rated:
    """A rated result as the inference rules see it."""

    id: str
    rating: str  # love | like | dislike
    comment: str
    title: str
    price: float | None
    seller: str
    thumbnail_url: str
