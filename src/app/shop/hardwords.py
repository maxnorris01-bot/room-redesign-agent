"""The hard words of a request, and the check that a search variant keeps them (ADR 0012, 2q).

A request names things a result must be: its category and every typed style, type, size, colour
and material ("mid-century", "mesh", "8x10", "navy velvet"). A search variant written to find
more results may reword everything else, but it must keep every one of these words, and it must not
add a style, material or colour word the request did not contain: on the post-2p live re-run a
variant "affordable midcentury upholstered dining seat chairs" pulled the whole later batch toward
upholstered espresso chairs and the match to a mid-century request fell from 83% to 21%.

A hard word may be replaced only by a listed synonym from the existing tables (`vocab.SYNONYMS`,
`vocab.COLOR_KEYWORDS`, `categories`), and only from a small group (`SYNONYM_GROUP_MAX` keywords):
a big group is a bucket of related values for tagging ("upholstered" holds velvet, linen and
leather), not a set of synonyms. Code decides; the model only writes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.categories import category_synonyms, singular
from app.shop.models import ShopQuery
from app.shop.vocab import COLOR_KEYWORDS, COLOR_LABELS, SYNONYMS

SYNONYM_GROUP_MAX = 3  # keywords in a synonym group small enough to count as synonyms
# attribute words outside the vocabulary tables that still change what is found
EXTRA_ATTRIBUTE_WORDS = ("retro", "vintage", "antique", "tufted", "faux", "scandinavian")
SIZE_WORDS = ("queen", "king", "twin")  # bed sizes: a variant must keep one the request names
ATTRIBUTE_TABLES = ("style", "material")  # vocabulary tables whose keywords are attribute words

_NUM = r"(\d+(?:\.\d+)?)"
_DIMS = re.compile(rf"{_NUM}\s*(?:'|ft\.?|feet|foot|\")?\s*(?:x|×|by)\s*{_NUM}", re.IGNORECASE)
_LENGTH = re.compile(rf"{_NUM}\s*-?\s*(?:inch(?:es)?|in\b|\")", re.IGNORECASE)


def tokens(text: str) -> list[str]:
    """Lowercase alphanumeric words with a plural made singular ("chairs" -> "chair"); hyphens and
    apostrophes split words ("mid-century" -> mid, century)."""
    return [singular(w) for w in re.findall(r"[a-z0-9]+", text.lower())]


@dataclass(frozen=True)
class HardWord:
    """One thing the request requires. `forms` are the word sets that satisfy it (the typed words
    first, then listed synonyms): a variant has the hard word when it holds every word of one."""

    kind: str  # category | style | material | color | must_have | size
    label: str  # as the shopper typed it
    forms: tuple[frozenset[str], ...]

    def present(self, words: set[str]) -> bool:
        return any(form <= words for form in self.forms)

    def missing_word(self, words: set[str]) -> str:
        """The word of the typed form a variant lacks (the last one: the head of a category)."""
        lacking = [w for w in tokens(self.label) if w not in words]
        return lacking[-1] if lacking else self.label


def _synonym_forms(table: list[tuple[str, tuple[str, ...]]], label: str) -> list[frozenset[str]]:
    """Forms from the small synonym group in `table` that lists `label` as a keyword."""
    mine = tokens(label)
    for value, keywords in table:
        group = (value, *keywords)
        if any(tokens(k) == mine for k in group) and len(keywords) <= SYNONYM_GROUP_MAX:
            return [frozenset(tokens(k)) for k in group if tokens(k)]
    return []


def _compact(label: str) -> list[frozenset[str]]:
    toks = tokens(label)
    return [frozenset({"".join(toks)})] if len(toks) > 1 and "".join(toks).isalnum() else []


def _sizes(request: str) -> list[HardWord]:
    out: list[HardWord] = []
    for m in _DIMS.finditer(request):
        a, b = m.group(1), m.group(2)
        out.append(HardWord("size", f"{a}x{b}", (frozenset({f"{a}x{b}"}), frozenset({a, "x", b}),
                                                 frozenset({a, "by", b}))))  # fmt: skip
    for m in _LENGTH.finditer(request):
        n = m.group(1)
        out.append(HardWord("size", f"{n} inch", (frozenset({n, "inch"}), frozenset({n, "in"}))))
    for word in SIZE_WORDS:
        if word in tokens(request):
            out.append(HardWord("size", word, (frozenset({word}),)))
    return out


def hard_words(base: ShopQuery, adjusted: ShopQuery) -> list[HardWord]:
    """The request's hard words: the category, the parsed styles, materials, colours and must-haves
    (the shopper's refine chips included, through `adjusted`), and the sizes the request writes."""
    out: list[HardWord] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()

    def add(kind: str, label: str, extra: list[frozenset[str]]) -> None:
        toks = tokens(label)
        if not toks or (kind, tuple(toks)) in seen:
            return
        seen.add((kind, tuple(toks)))
        forms = [frozenset(toks), *_compact(label), *extra]
        out.append(HardWord(kind, label, tuple(dict.fromkeys(forms))))

    for category in dict.fromkeys(c for c in (base.category, adjusted.category) if c):
        extra = [frozenset(tokens(k)) for k in category_synonyms(category)]
        add("category", category, extra)
    for term in [*base.style_terms]:
        add("style", term, _synonym_forms(SYNONYMS["style"], term))
    for term in [*base.material_terms, *adjusted.material_terms]:
        add("material", term, _synonym_forms(SYNONYMS["material"], term))
    for color in [*base.color_terms, *adjusted.color_terms]:
        label = COLOR_LABELS.get(color, color.replace("_", " "))
        add("color", label, _synonym_forms(COLOR_KEYWORDS, label))
    for term in [*base.must_have, *adjusted.must_have]:
        add("must_have", term, [])
    for size in _sizes(base.request):
        key = ("size", tuple(tokens(size.label)))
        if key not in seen:
            seen.add(key)
            out.append(size)
    return out


def missing_hard_word(text: str, words: list[HardWord]) -> HardWord | None:
    """The first hard word the variant `text` lacks, or None when it keeps them all."""
    present = set(tokens(text))
    return next((w for w in words if not w.present(present)), None)


def _attribute_phrases() -> list[tuple[str, ...]]:
    phrases: list[tuple[str, ...]] = []
    for dim in ATTRIBUTE_TABLES:
        for value, keywords in SYNONYMS[dim]:
            phrases += [tuple(tokens(k)) for k in (value, *keywords)]
    for value, keywords in COLOR_KEYWORDS:
        phrases += [tuple(tokens(k)) for k in (value.replace("_", " "), *keywords)]
    phrases += [tuple(tokens(w)) for w in EXTRA_ATTRIBUTE_WORDS]
    return [p for p in dict.fromkeys(phrases) if p]


ATTRIBUTE_PHRASES = _attribute_phrases()


def allowed_words(base: ShopQuery, adjusted: ShopQuery, words: list[HardWord]) -> set[str]:
    """Every word a variant may carry although it is an attribute word: the request's own words,
    the parsed fields, the refine chips, and each hard word's listed synonyms."""
    texts = [
        base.request, base.search_text, *base.style_terms, *base.material_terms, *base.must_have,
        *adjusted.must_have, *adjusted.material_terms, *base.color_terms, *adjusted.color_terms,
        *(COLOR_LABELS.get(c, c) for c in (*base.color_terms, *adjusted.color_terms)),
    ]  # fmt: skip
    allowed = {t for text in texts for t in tokens(text.replace("_", " "))}
    for w in words:
        for form in w.forms:
            allowed |= form
    return allowed


def added_attribute_word(text: str, allowed: set[str]) -> str:
    """A style, material or colour word in the variant `text` that the request did not contain
    ("" when there is none). Multi-word keywords count whole ("dark blue")."""
    present = tokens(text)
    for phrase in ATTRIBUTE_PHRASES:
        n = len(phrase)
        for i in range(len(present) - n + 1):
            if tuple(present[i : i + n]) == phrase and not set(phrase) <= allowed:
                return " ".join(phrase)
    return ""
