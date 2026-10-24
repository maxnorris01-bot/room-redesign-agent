"""Typed data for Shop Mode: the parsed query, a result card, and the pieces of a search outcome."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_REQUEST_CHARS = 300
MAX_TERMS = 8  # per list
MAX_TERM_CHARS = 40
MAX_SEARCH_TEXT_CHARS = 100
MAX_COMMENT_CHARS = 200
RATINGS = ("love", "like", "dislike")
Rating = Literal["love", "like", "dislike"]
LinkKind = Literal["retailer_product", "google_panel", "none"]
_ID_OK = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShopParse(_Strict):
    """What the model returns for a request (sent as the API schema)."""

    search_text: str = Field(description="Short query for Google Shopping, no price words")
    category: str = Field(description="The kind of product, singular and lowercase, e.g. 'sofa'")
    price_min: float | None = Field(description="USD lower bound, or null")
    price_max: float | None = Field(description="USD upper bound, or null")
    style_terms: list[str] = Field(description="Style words, e.g. 'modern', 'boho'")
    material_terms: list[str] = Field(description="Materials, e.g. 'wood', 'velvet'")
    must_have: list[str] = Field(description="Features the shopper wants, e.g. 'storage'")
    avoid: list[str] = Field(description="Things the shopper does not want")


class ShopQuery(_Strict):
    """A request as the rest of Shop Mode sees it. Prices and category are hard filters; style,
    materials and must-haves are soft preferences used when ranking."""

    request: str
    search_text: str
    category: str
    category_key: str | None = None  # a key from categories.py when one matches, else None
    price_min: float | None = None
    price_max: float | None = None
    style_terms: list[str] = []
    material_terms: list[str] = []
    must_have: list[str] = []
    avoid: list[str] = []


class ShopResult(_Strict):
    id: str
    title: str
    price: float | None
    currency: str
    seller: str
    thumbnail_url: str  # https only; empty when the search gave none (or a non-https one)
    link: str  # the Google product link until resolved, then the retailer link
    link_kind: LinkKind
    size_hint: str | None
    product_id: str
    rank: int  # 1-based position in the returned list
    pool_position: int  # 1-based position in the filtered pool the ranker saw
    rank_reason: str = ""  # the ranker's own words; empty when it did not rank this one


class FilterCounts(BaseModel):
    """What each hard filter removed, in the order applied (for the footer and the trace)."""

    model_config = ConfigDict(extra="forbid")

    returned: int = 0
    no_price: int = 0
    out_of_range: int = 0
    wrong_category: int = 0
    duplicates: int = 0
    kept: int = 0  # pool size after the filters and dedupe
    seller_capped: int = 0  # removed from the ranked list by the per-seller cap
    seen_excluded: int = 0  # Shop Again: already shown earlier in the session


def result_id(product_id: str) -> str:
    """A result id safe for JSON keys, file names and DOM data: the provider's id when it is plain
    (letters, digits, `_.:-`, up to 64), otherwise a short hash of it."""
    if _ID_OK.match(product_id):
        return product_id
    return "h" + hashlib.sha1(product_id.encode()).hexdigest()[:16]


def is_https_url(url: str) -> bool:
    return url.startswith("https://") and len(url) <= 2000 and not any(c.isspace() for c in url)


def clean_comment(text: str) -> str:
    """A comment as stored: control characters dropped, whitespace collapsed, ends trimmed."""
    return " ".join("".join(c for c in text if c.isprintable() or c in "\n\t").split())


def clean_request(text: str) -> str:
    """A request as sent to the model: printable characters only, whitespace collapsed, angle
    brackets dropped (they delimit the request in the prompt), bounded in length."""
    printable = "".join(c if c.isprintable() else " " for c in text).replace("<", " ")
    return " ".join(printable.replace(">", " ").split())[:MAX_REQUEST_CHARS]


# ---- preference profile (Shop Again) ----------------------------------------------------------


class QueryAdjustments(_Strict):
    """How the profile would change the next search. Price and category are never changed here
    except `price_max`, and only downward by a limited amount when a comment asks for it."""

    search_text: str = Field(description="A revised short search phrase, or an empty string")
    style_terms: list[str] = Field(description="Extra style words to prefer")
    material_terms: list[str] = Field(description="Extra materials to prefer")
    must_have: list[str] = Field(description="Extra features the shopper wants")
    avoid: list[str] = Field(description="Extra things to avoid")
    price_max: float | None = Field(description="A lower USD cap only if a comment asks, or null")
    price_note: str = Field(description="Why the cap changed, or an empty string")


class PreferenceProfile(_Strict):
    """What the model returns: attribute phrases the shopper likes and dislikes, and how to adjust
    the query. It is the complete updated profile, not a delta."""

    likes: list[str] = Field(description="Short attribute phrases, e.g. 'boucle fabric'")
    dislikes: list[str] = Field(description="Short attribute phrases, e.g. 'flimsy legs'")
    query_adjustments: QueryAdjustments


ATTR_KINDS = (
    "shape",
    "material",
    "color_finish",
    "frame_or_legs",
    "size",
    "style",
    "quality_cue",
    "seller",
    "price_band",
)
AttrKind = Literal[
    "shape",
    "material",
    "color_finish",
    "frame_or_legs",
    "size",
    "style",
    "quality_cue",
    "seller",
    "price_band",
]


class InferredAttribute(_Strict):
    """One attribute the model infers from the rated items themselves (prompt shop_prefs v2)."""

    kind: AttrKind = Field(description="What sort of attribute this is")
    text: str = Field(description="Short phrase, e.g. 'brushed brass'")
    polarity: Literal["like", "dislike"]
    evidence: list[str] = Field(description="Ids of the rated results that show it")
    confidence: float = Field(description="0 to 1")


class ContradictedGuess(_Strict):
    text: str = Field(description="A parser-guessed style term a loved or liked item contradicts")
    evidence: list[str] = Field(description="Ids of the loved or liked results that contradict it")


class PreferenceProfileV2(_Strict):
    """What the v2 preference call returns: stated likes and dislikes (from the shopper's own
    comments), attributes inferred from the items, contradicted guesses, and query adjustments."""

    likes: list[str] = Field(description="Things the shopper said they like, in their own words")
    dislikes: list[str] = Field(description="Things the shopper said they dislike")
    inferred: list[InferredAttribute]
    contradicted_guesses: list[ContradictedGuess]
    query_adjustments: QueryAdjustments


Shape = Literal["round", "oval", "rectangular", "square", "other"]
SizeClass = Literal["small", "medium", "large"]


class ItemTags(_Strict):
    """What the model sees in one rated item (prompt shop_prefs v3). It describes; it never judges
    preferences. A dimension the title, photo or price do not show is null."""

    id: str = Field(description="The rated result's id, copied exactly")
    shape: Shape | None
    material: str | None = Field(description="Main material, e.g. walnut, steel, marble")
    color_finish: str | None = Field(description="Colour or finish of the main surface")
    frame_or_legs: str | None = Field(description="Leg or base type, e.g. hairpin legs, pedestal")
    frame_color: str | None = Field(
        description="Frame colour, e.g. black metal, brass, natural wood"
    )
    style: str | None = Field(description="Style, e.g. modern, mid-century")
    size_class: SizeClass | None
    quality_cues: list[str] = Field(
        description="Short phrases for build or finish visible in the title, photo or price"
    )


class PreferenceTags(_Strict):
    """What the v3 preference call returns: per-item tags, plus the shopper's own words from their
    comments (stated likes and dislikes, price asks) and any parser guess a loved item contradicts.
    """

    items: list[ItemTags]
    likes: list[str] = Field(description="Things the shopper said they like, in their own words")
    dislikes: list[str] = Field(description="Things the shopper said they dislike")
    contradicted_guesses: list[ContradictedGuess]
    query_adjustments: QueryAdjustments


class ProfileEntry(BaseModel):
    """An inferred chip as stored: sanitized, with the evidence that earned it."""

    model_config = ConfigDict(extra="forbid")

    text: str
    attr_kind: str
    polarity: Literal["like", "dislike"]
    evidence: list[str] = []  # result ids
    evidence_titles: list[str] = []  # plain text for the tooltip
    confidence: float = 0.0
    weight: int = 0  # loved 3, liked 1 (likes); disliked count (dislikes)
    low: float | None = None  # price_band only: the band's bounds in USD
    high: float | None = None
    band: str = ""  # price_band only: "upper" or "lower"
    # stage 2c: entries decided in code from per-item tags (see app.shop.decide)
    dimension: str = (
        ""  # shape, material, color_finish, frame_or_legs, frame_color, style, size_class
    )
    status: str = "converged"  # converged | open | mixed (open and mixed chips exert no pressure)
    values: list[str] = []  # converged: the one value; open: the values liked
    detail: str = ""  # shown after the text, e.g. "liked round and rectangular"
    share: float | None = None  # the value's share of the like (or dislike) mass
    exposure: float | None = None  # its share of everything shown (None: not measurable)
    lift: float | None = None  # share over exposure (None: not measurable)
    pinned: bool = False  # seller chips only


class ImageReport(BaseModel):
    """What happened to the thumbnails offered to the preference call."""

    model_config = ConfigDict(extra="forbid")

    offered: int = 0  # rated items that had a thumbnail url and were picked
    used: int = 0  # fetched and sent
    failed: int = 0  # could not be fetched; the item went by title only
    reasons: list[str] = []  # short failure reasons (hosts and reasons, never urls)


class StoredProfile(BaseModel):
    """The profile as kept in the session: sanitized, with the entries the visitor deleted."""

    model_config = ConfigDict(extra="forbid")

    likes: list[str] = []
    dislikes: list[str] = []
    adjustments: QueryAdjustments = QueryAdjustments(
        search_text="", style_terms=[], material_terms=[], must_have=[], avoid=[],
        price_max=None, price_note="",
    )  # fmt: skip
    removed: list[str] = []  # normalized phrases the visitor deleted; never added back
    updated_at: str = ""
    rated_count: int = 0  # ratings the profile was built from
    inferred: list[ProfileEntry] = []  # learned from the rated items (v2)
    guesses: list[str] = []  # style terms the parser guessed that nothing has confirmed or dropped
    dropped_guesses: list[str] = []  # guesses a loved or liked item contradicted
    images: ImageReport = ImageReport()
    pinned_sellers: list[str] = []  # seller keys the visitor pinned (stage 2c)
    trace: dict[str, Any] = {}  # how every entry was decided: numbers and reasons (stage 2d)
    tagging: dict[str, Any] = {}  # items tagged N of M, images, retry, fallback (stage 2d)


def jsonable(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
