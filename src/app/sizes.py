"""Size hints for products: where a size can come from and how far to trust it (ADR 0009).

Order of trust: the lookup's feature text (parsed in `app.shopping`), then the sizes written in
the titles (the store page's own title first, then the search title), then a number in the URL
slug (`leif-54-media-unit`). Each source yields partial sizes (a width only is fine) in metres.

A parsed value outside the category's plausible range (`categories.SIZE_RANGE_IN`) is discarded
with a note, and the next source is tried. Sizes here are only ever used for the fit check and the
replacement-size note; they are approximate by nature, and slug sizes are flagged low-confidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.categories import category_words, size_range_in
from app.schemas import DimensionsM

IN_M = 0.0254
_TO_M = {
    "cm": 0.01, "mm": 0.001, "in": IN_M, "inch": IN_M, "inches": IN_M,
    '"': IN_M, "”": IN_M, "″": IN_M, "": IN_M,
}  # fmt: skip
_NUM = r"\d+(?:\.\d+)?(?:\s+\d+/\d+)?"
_UNIT = r"\"|”|″|inches|inch|in(?!\s*\d)\b|cm\b|mm\b"
# 79"W, 63"L, 12 in W, 214 cm D (a unit is optional when an axis letter follows)
_LABELED = re.compile(rf"(?P<num>{_NUM})\s*(?P<unit>{_UNIT})?\s*(?P<ax>[WDHL])(?![A-Za-z])")
# 79 x 18 x 30 in, 58 x 39 cm (W x D x H; a unit after the last number is required)
_GROUP = re.compile(
    rf"(?P<a>{_NUM})\s*(?:{_UNIT})?\s*[x×]\s*(?P<b>{_NUM})\s*(?:{_UNIT})?"
    rf"(?:\s*[x×]\s*(?P<c>{_NUM}))?\s*(?P<unit>{_UNIT})"
)
_SINGLE = re.compile(rf"(?P<num>{_NUM})\s*(?P<unit>{_UNIT})(?![A-Za-z])")
_AXIS = {"W": "width_m", "D": "depth_m", "H": "height_m", "L": "length_m"}
SLUG_SIZE_WORDS = {"wide", "w", "in", "inch", "inches", "long", "l", "size", "width", "length"}
PLAUSIBLE_PAD = 1.0


@dataclass
class SizeResult:
    dims: DimensionsM | None
    source: str  # "feature" | "title" | "slug" | "none"
    confidence: str  # "high" (feature, or a title with units), "low" (slug), "none"
    notes: list[str] = field(default_factory=list)


def _number(text: str) -> float:
    total = 0.0
    for part in text.split():
        if "/" in part:
            n, d = part.split("/")
            total += float(n) / float(d)
        else:
            total += float(part)
    return total


def _metres(num: str, unit: str | None) -> float:
    return round(_number(num) * _TO_M[(unit or "").lower()], 4)


def parse_title_sizes(text: str) -> dict[str, float]:
    """Sizes written in a title, in metres, keyed width_m / depth_m / height_m / length_m.

    Handles 79"W, 63"L x 12"W x 16"H, W x D x H (79 x 18 x 30 in), 58", 85.5 in and 214 cm. An
    unlabeled size ("58 in") is recorded as `length_m`, the product's longest side. Numbers with
    no unit and no axis letter are ignored.
    """
    found: dict[str, float] = {}
    used: list[tuple[int, int]] = []

    def overlaps(span: tuple[int, int]) -> bool:
        return any(span[0] < u[1] and u[0] < span[1] for u in used)

    for m in _LABELED.finditer(text):
        key = _AXIS[m.group("ax")]
        found.setdefault(key, _metres(m.group("num"), m.group("unit")))
        used.append(m.span())
    for m in _GROUP.finditer(text):
        if overlaps(m.span()):
            continue
        unit = m.group("unit")
        for key, grp in zip(("width_m", "depth_m", "height_m"), "abc", strict=True):
            if m.group(grp):
                found.setdefault(key, _metres(m.group(grp), unit))
        used.append(m.span())
    if not found:
        for m in _SINGLE.finditer(text):
            if not overlaps(m.span()):
                value = _metres(m.group("num"), m.group("unit"))
                found["length_m"] = max(found.get("length_m", 0.0), value)
    return found


def parse_slug_size(url: str, category: str) -> tuple[float | None, str]:
    """A size in inches-as-metres from the URL slug, only when a number sits next to a category or
    size word (`leif-54-media-unit`), and is plausible for the category. Returns (metres, note).

    A pair like `85-5` is read as 85.5 when that is plausible for the category; otherwise the first
    number is tried alone (the 5 is then a stray digit, never a size).
    """
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if not segments:
        return None, ""
    slug = re.sub(r"\.(html?|php|aspx?)$", "", segments[-1], flags=re.I).lower()
    tokens = re.findall(r"[a-z]+|\d+", slug.replace("-", " ").replace("_", " "))
    words = category_words(category) | SLUG_SIZE_WORDS
    lo, hi = size_range_in(category)
    notes: list[str] = []
    i = 0
    while i < len(tokens):
        if not tokens[i].isdigit():
            i += 1
            continue
        j = i
        while j < len(tokens) and tokens[j].isdigit():
            j += 1
        group = tokens[i:j]
        near = any(t in words for t in tokens[max(0, i - 1) : i]) or any(
            t in words for t in tokens[j : j + 3]
        )
        i = j
        if not near or len(group) > 2 or any(len(t) > 3 for t in group):
            continue
        candidates = []
        if len(group) == 2 and len(group[1]) == 1:
            candidates.append(float(group[0]) + int(group[1]) / 10)
        candidates.append(float(group[0]))
        for value in candidates:
            if lo <= value <= hi:
                return round(value * IN_M, 4), ""
        notes.append(f"slug number {'-'.join(group)} outside {lo:.0f}-{hi:.0f} in for {category}")
    return None, "; ".join(notes)


def _plausible(dims: DimensionsM, category: str) -> bool:
    lo, hi = size_range_in(category)
    biggest = max(
        (v for v in (dims.width_m, dims.depth_m, dims.length_m, dims.height_m) if v is not None),
        default=None,
    )
    return biggest is None or lo - PLAUSIBLE_PAD <= biggest / IN_M <= hi + PLAUSIBLE_PAD


def resolve_size(
    category: str, features: DimensionsM | None, titles: list[str], url: str
) -> SizeResult:
    """The best available size for a product: feature text, then titles, then the URL slug."""
    notes: list[str] = []
    if features is not None and any(
        v is not None
        for v in (features.width_m, features.depth_m, features.length_m, features.height_m)
    ):
        if _plausible(features, category):
            return SizeResult(features, "feature", "high", notes)
        notes.append("feature size outside the plausible range; discarded")
    for text in titles:
        parsed = parse_title_sizes(text)
        if not parsed:
            continue
        dims = DimensionsM(**parsed)
        if _plausible(dims, category):
            return SizeResult(dims, "title", "high", notes)
        notes.append("title size outside the plausible range; discarded")
    value, note = parse_slug_size(url, category)
    if note:
        notes.append(note)
    if value is not None:
        return SizeResult(DimensionsM(length_m=value), "slug", "low", notes)
    return SizeResult(None, "none", "none", notes)
