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

from app.categories import axis_rule, canonical_category, category_words, size_range_in
from app.schemas import DimensionsM

IN_M = 0.0254
_TO_M = {
    "cm": 0.01, "mm": 0.001, "in": IN_M, "inch": IN_M, "inches": IN_M,
    '"': IN_M, "”": IN_M, "″": IN_M, "": IN_M,
}  # fmt: skip
_NUM = r"\d+(?:\.\d+)?(?:\s+\d+/\d+)?"
_UNIT = r"\"|”|″|inches|inch|in(?!\s*\d)\b|cm\b|mm\b"
# 79"W, 63"L, 12 in W, 214 cm D (a unit is optional when an axis letter follows)
_LABELED = re.compile(
    rf"(?P<num>{_NUM})\s*(?P<unit>{_UNIT})?\s*(?P<ax>[WDHL])(?![A-Za-z0-9])", re.IGNORECASE
)
# W80 D34 H30 (axis letter first); only trusted when at least two axes appear
_PREFIXED = re.compile(
    rf"(?<![A-Za-z0-9])(?P<ax>[WDHL])\s*(?P<num>{_NUM})\s*(?P<unit>{_UNIT})?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_TRAILING_UNIT = re.compile(r"\s*(?P<unit>inches|inch|in|cm|mm)\b", re.IGNORECASE)
_RUN_GAP = re.compile(r"^[\s,x×/|]*$", re.IGNORECASE)
THREE_NUMBER_HEIGHT_RATIO = 2.5  # untuned: a W x D x H height over this x the footprint's long side
# 79 x 18 x 30 in, 58 x 39 cm (W x D x H; a unit after the last number is required)
_GROUP = re.compile(
    rf"(?P<a>{_NUM})\s*(?:{_UNIT})?\s*[x×]\s*(?P<b>{_NUM})\s*(?:{_UNIT})?"
    rf"(?:\s*[x×]\s*(?P<c>{_NUM}))?\s*(?P<unit>{_UNIT})",
    re.IGNORECASE,
)
_SINGLE = re.compile(rf"(?P<num>{_NUM})[\s-]*(?P<unit>{_UNIT})(?![A-Za-z])", re.IGNORECASE)
FT_M = 0.3048
# 8x10, 8' x 10', 8 ft x 10 ft, 5x7 ft: a rug size in FEET (a unit is optional: nobody writes a
# 8-inch by 10-inch rug). Only read for rugs, and only when no inch or cm size was found.
_RUG_FEET = re.compile(
    r"(?<![\d.\w])(?P<a>\d{1,2}(?:\.\d)?)\s*(?:'|′|ft\.?|feet|foot)?\s*[x×]\s*"
    r"(?P<b>\d{1,2}(?:\.\d)?)\s*(?:'|′|ft\.?|feet|foot)?(?![\d.]|\s*(?:\"|”|″|in\b|inch|cm|mm))",
    re.IGNORECASE,
)
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


def _labeled_matches(text: str) -> list[tuple[int, int, str, str, str | None]]:
    """(start, end, axis, number, unit) for labeled sizes, trailing unit words applied to a run.

    "80w 34d 30h", "80W x 34D x 30H", '80"W 34"D 30"H' and "W80 D34 H30" all land here. A run of
    labels with no unit of its own takes a trailing unit word ("... 30h cm"); without one, inches.
    """
    hits = [
        (m.start(), m.end(), m.group("ax").upper(), m.group("num"), m.group("unit"))
        for m in _LABELED.finditer(text)
    ]
    taken = [(h[0], h[1]) for h in hits]
    prefixed = [
        (m.start(), m.end(), m.group("ax").upper(), m.group("num"), m.group("unit"))
        for m in _PREFIXED.finditer(text)
        if not any(m.start() < t[1] and t[0] < m.end() for t in taken)
    ]
    if len({h[2] for h in prefixed}) >= 2:
        hits += prefixed
    hits.sort()
    out: list[tuple[int, int, str, str, str | None]] = []
    i = 0
    while i < len(hits):
        j = i + 1
        while j < len(hits) and _RUN_GAP.match(text[hits[j - 1][1] : hits[j][0]]):
            j += 1
        trailing = _TRAILING_UNIT.match(text, hits[j - 1][1])
        for h in hits[i:j]:
            unit = h[4] or (trailing.group("unit") if trailing else hits[j - 1][4])
            end = trailing.end() if trailing and h is hits[j - 1] else h[1]
            out.append((h[0], end, h[2], h[3], unit))
        i = j
    return out


def _three_number_ok(category: str, a: float, b: float, c: float) -> bool:
    """Whether "a x b x c" can be W x D x H for the category (inches): in range, sane height."""
    lo, hi = size_range_in(category)
    if not lo - PLAUSIBLE_PAD <= max(a, b, c) <= hi + PLAUSIBLE_PAD:
        return False
    if axis_rule(category) == "tall":
        return c >= max(a, b)
    return c <= THREE_NUMBER_HEIGHT_RATIO * max(a, b)


def parse_title_detail(
    text: str, category: str | None = None
) -> tuple[dict[str, float], list[str]]:
    """Sizes written in a title (metres, keyed width_m / depth_m / height_m / length_m) and notes.

    Labeled forms: 79"W, 63"L x 12"W x 16"H, 80w 34d 30h, W80 D34 H30, with a trailing unit word.
    Unlabeled groups depend on the category (`categories.axis_rule`): tall products read
    "21x79" as 21 wide, 79 tall (depth unknown); flat ones as width by length; anything else keeps
    only the longest side. An unlabeled three-number group is W x D x H only when plausible for the
    category, else the longest side only. An unlabeled single ("58 in") is `length_m`, the longest
    side. Numbers with no unit and no axis letter are ignored. With `category=None` the older
    reading applies (unlabeled groups are W x D x H).
    """
    found: dict[str, float] = {}
    notes: list[str] = []
    used: list[tuple[int, int]] = []

    def overlaps(span: tuple[int, int]) -> bool:
        return any(span[0] < u[1] and u[0] < span[1] for u in used)

    for start, end, ax, num, unit in _labeled_matches(text):
        found.setdefault(_AXIS[ax], _metres(num, unit))
        used.append((start, end))
    for m in _GROUP.finditer(text):
        if overlaps(m.span()):
            continue
        unit = m.group("unit")
        nums = [m.group(g) for g in "abc" if m.group(g)]
        values = [_metres(n, unit) for n in nums]
        used.append(m.span())
        keys = ("width_m", "depth_m", "height_m")
        if category is None:
            for key, value in zip(keys, values, strict=False):
                found.setdefault(key, value)
            continue
        rule = axis_rule(category)
        raw = ", ".join(nums)
        if len(values) == 3 and _three_number_ok(category, *(v / IN_M for v in values)):
            for key, value in zip(keys, values, strict=True):
                found.setdefault(key, value)
            notes.append(f"{raw} read as W x D x H")
        elif len(values) == 2 and rule == "tall":
            found.setdefault("width_m", min(values))
            found.setdefault("height_m", max(values))
            notes.append(f"{raw} read as width x height (tall category rule; depth unknown)")
        elif len(values) == 2 and rule == "flat":
            found.setdefault("width_m", min(values))
            found.setdefault("length_m", max(values))
            notes.append(f"{raw} read as width x length (flat category rule)")
        else:
            found["length_m"] = max(found.get("length_m", 0.0), max(values))
            why = "implausible as W x D x H" if len(values) == 3 else "no axis rule for category"
            notes.append(f"{raw}: longest side only ({why})")
    if not found and category is not None and canonical_category(category) == "rug":
        rug = _RUG_FEET.search(text)
        if rug and not overlaps(rug.span()):
            a, b = float(rug.group("a")), float(rug.group("b"))
            if 1 <= a <= 20 and 1 <= b <= 20:
                found["width_m"] = round(min(a, b) * FT_M, 4)
                found["length_m"] = round(max(a, b) * FT_M, 4)
                notes.append(f"{rug.group(0).strip()} read as feet, width x length (rug)")
    if not found:
        for m in _SINGLE.finditer(text):
            if not overlaps(m.span()):
                value = _metres(m.group("num"), m.group("unit"))
                found["length_m"] = max(found.get("length_m", 0.0), value)
    return found, notes


def parse_title_sizes(text: str, category: str | None = None) -> dict[str, float]:
    """`parse_title_detail` without the notes."""
    return parse_title_detail(text, category)[0]


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
        parsed, parse_notes = parse_title_detail(text, category)
        if not parsed:
            continue
        dims = DimensionsM(**parsed)
        if _plausible(dims, category):
            return SizeResult(dims, "title", "high", notes + parse_notes)
        notes.append("title size outside the plausible range; discarded")
    value, note = parse_slug_size(url, category)
    if note:
        notes.append(note)
    if value is not None:
        return SizeResult(DimensionsM(length_m=value), "slug", "low", notes)
    return SizeResult(None, "none", "none", notes)
