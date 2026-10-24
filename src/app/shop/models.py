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


def jsonable(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
