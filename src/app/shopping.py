"""Shopping data providers: structured Google Shopping results instead of model-read web pages.

Two roles, deliberately separate:

  * `SearchProvider.search` (Serper or SerpApi) turns a query into normalized `SearchCandidate`s:
    product id, title, seller, price, rating, and the Google product-panel URL (not a retailer URL).
  * `ProductLookup.lookup` (SerpApi `google_product`, by product id) turns one product id into
    `ProductDetails`: the stores that sell it (direct retailer URL, price, shipping, total,
    availability) and any dimensions the listing states.

Prices and URLs shown to a user come from these providers, never from a model (ADR 0005).

Secrets: API keys are read from the environment when a provider is built and held only in the
private header/param passed to the HTTP client. They are never put in an exception message, a
trace, a table or a result: every error names the provider and the HTTP status only.

Mock mode (`APP_LLM_MODE=mock`) uses the `Fixture*` classes at the bottom, backed by trimmed real
responses in `tests/fixtures/shopping/`; no network, no key.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx2 as httpx

from app.config import Config

REQUEST_TIMEOUT_S = 15.0
DEFAULT_GL = "us"
DEFAULT_HL = "en"
FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "shopping"
FIXTURE_TITLE_PREFIX = "[FIXTURE] "


class ProviderError(RuntimeError):
    """A shopping provider call failed. The message never contains a URL or a key."""

    def __init__(self, provider: str, message: str, *, status: int | None = None) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.status = status


@dataclass(frozen=True)
class SearchCandidate:
    product_id: str
    title: str
    seller: str
    price: float | None
    currency: str
    rating: float | None
    rating_count: int | None
    google_panel_url: str | None


@dataclass(frozen=True)
class Store:
    seller: str
    url: str
    price: float | None
    shipping: float | None
    total: float | None
    availability: str | None


@dataclass(frozen=True)
class Dimensions:
    """Metres. Each field is None when the listing did not state it (or it was unparseable)."""

    width_m: float | None = None
    depth_m: float | None = None
    height_m: float | None = None
    length_m: float | None = None

    def is_empty(self) -> bool:
        return all(v is None for v in (self.width_m, self.depth_m, self.height_m, self.length_m))


@dataclass(frozen=True)
class ProductDetails:
    stores: list[Store] = field(default_factory=list)
    attributes: list[dict[str, str]] = field(default_factory=list)
    dimensions: Dimensions | None = None


class SearchProvider(Protocol):
    name: str

    def search(
        self, query: str, gl: str = DEFAULT_GL, hl: str = DEFAULT_HL
    ) -> list[SearchCandidate]:
        """Return normalized results, best first. Raises `ProviderError`."""
        ...


class ProductLookup(Protocol):
    name: str

    def lookup(self, product_id: str, gl: str = DEFAULT_GL, hl: str = DEFAULT_HL) -> ProductDetails:
        """Return stores and attributes for one product id. Raises `ProviderError`."""
        ...


# ---- parsing helpers -----------------------------------------------------------------------

_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "C$": "CAD", "A$": "AUD"}
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_price(text: Any) -> tuple[float, str] | None:
    """Parse a display price like "$1,299.00" into (1299.0, "USD"); None if there is no number.

    Takes the first number, so a range like "$249-$249" gives the low end. Unknown or missing
    currency symbol defaults to USD (every query here is gl=us).
    """
    if isinstance(text, bool):
        return None
    if isinstance(text, int | float):
        return float(text), "USD"
    if not isinstance(text, str):
        return None
    match = _NUMBER.search(text)
    if not match:
        return None
    try:
        amount = float(match.group().replace(",", ""))
    except ValueError:
        return None
    prefix = text[: match.start()].strip()
    currency = next((c for sym, c in _CURRENCY_SYMBOLS.items() if prefix.endswith(sym)), "USD")
    return amount, currency


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _usable_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


# ---- dimensions ----------------------------------------------------------------------------

_UNIT_TO_M = {
    "mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
    '"': 0.0254, "”": 0.0254, "″": 0.0254,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048, "'": 0.3048,
}  # fmt: skip
_DIM_VALUE = re.compile(
    r"""^\s*(?P<num>\d+(?:\.\d+)?(?:[\s-]+\d+/\d+)?|\d+/\d+)\s*(?P<unit>[a-zA-Z"”″']+)\s*\.?\s*$"""
)
_DIM_KEYS = {"width": "width_m", "depth": "depth_m", "height": "height_m", "length": "length_m"}


def _number_with_fraction(text: str) -> float | None:
    """ "12 5/8" -> 12.625, "5/8" -> 0.625, "58" -> 58.0, "12.5" -> 12.5."""
    try:
        total = Fraction(0)
        for part in re.split(r"[\s-]+", text.strip()):
            total += Fraction(part)
        return float(total)
    except (ValueError, ZeroDivisionError):
        return None


def parse_length_m(value: str) -> float | None:
    """Parse one length such as "58 cm", '12 5/8 "', "1.2 m" or "610 mm" into metres.

    Returns None for anything else, including unitless numbers (ambiguous) and compound
    "2 x 3" style strings (out of scope).
    """
    match = _DIM_VALUE.match(value)
    if not match:
        return None
    factor = _UNIT_TO_M.get(match.group("unit").lower())
    number = _number_with_fraction(match.group("num"))
    if factor is None or number is None or number <= 0:
        return None
    return round(number * factor, 4)


def parse_dimensions(
    features: list[dict[str, Any]] | None, attributes: list[dict[str, Any]] | None
) -> Dimensions | None:
    """Width, depth, height and length from `about_the_product.features` and `product_attributes`.

    Both are lists of {title|name: ..., value: ...}. Only exact keys (case-insensitive) "width",
    "depth", "height" and "length" count; "seat height" and the like are ignored on purpose. The
    first parseable value per dimension wins, features before attributes.
    """
    found: dict[str, float] = {}
    for entry in [*(features or []), *(attributes or [])]:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("title") or entry.get("name") or "").strip().lower()
        target = _DIM_KEYS.get(key)
        value = entry.get("value")
        if target is None or target in found or not isinstance(value, str):
            continue
        metres = parse_length_m(value)
        if metres is not None:
            found[target] = metres
    return Dimensions(**found) if found else None


# ---- URLs and store choice -----------------------------------------------------------------

# A containment match ("Walmart" in "Walmart - Oubayajia") needs a name this long, so one-letter
# or very short seller names cannot match by accident.
MIN_CONTAINMENT_CHARS = 4
_TRACKING_PREFIXES = ("utm_", "gad_")
_TRACKING_KEYS = {"srsltid", "gclid", "fbclid"}


def strip_tracking(url: str) -> str:
    """Remove tracking query parameters (utm_*, srsltid, gclid, gad_*, fbclid); keep the rest."""
    parts = urlsplit(url.strip())
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower() in _TRACKING_KEYS or k.lower().startswith(_TRACKING_PREFIXES))
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def _seller_key(name: str) -> str:
    lowered = name.lower().strip()
    lowered = re.sub(r"\.(com|net|org|co)$", "", lowered)
    return re.sub(r"[^a-z0-9]+", "", lowered)


def choose_store(stores: list[Store], seller: str) -> Store | None:
    """The store whose name matches `seller`, else the one with the lowest total.

    Match is case-insensitive and ignores punctuation and a trailing ".com"; an exact match wins,
    then a containment match either way ("Walmart - Oubayajia" vs "Walmart"). Stores without a
    URL are never chosen. "Lowest total" falls back to price, then to listing order.
    """
    usable = [s for s in stores if s.url]
    if not usable:
        return None
    key = _seller_key(seller)
    if key:
        for s in usable:
            if _seller_key(s.seller) == key:
                return s
        for s in usable:
            other = _seller_key(s.seller)
            shorter = min(key, other, key=len)
            if len(shorter) >= MIN_CONTAINMENT_CHARS and (key in other or other in key):
                return s

    def cost(s: Store) -> float:
        for v in (s.total, s.price):
            if v is not None:
                return v
        return float("inf")

    return min(usable, key=cost)  # min is stable: ties keep listing order


# ---- response parsers (pure; used by the HTTP providers and the fixtures) ---------------------


def parse_serper_search(payload: dict[str, Any]) -> list[SearchCandidate]:
    out: list[SearchCandidate] = []
    for item in payload.get("shopping", []) or []:
        pid = _usable_str(item.get("productId")) or (
            str(item["productId"]) if item.get("productId") is not None else None
        )
        title = _usable_str(item.get("title"))
        if not pid or not title:
            continue
        parsed = parse_price(item.get("price"))
        out.append(
            SearchCandidate(
                product_id=pid,
                title=title,
                seller=_usable_str(item.get("source")) or "",
                price=parsed[0] if parsed else None,
                currency=parsed[1] if parsed else "USD",
                rating=_as_float(item.get("rating")),
                rating_count=_as_int(item.get("ratingCount")),
                google_panel_url=_usable_str(item.get("link")),  # absent on some items
            )
        )
    return out


def parse_serpapi_search(payload: dict[str, Any]) -> list[SearchCandidate]:
    out: list[SearchCandidate] = []
    for item in payload.get("shopping_results", []) or []:
        pid = item.get("product_id")
        title = _usable_str(item.get("title"))
        if pid in (None, "") or not title:
            continue
        parsed = parse_price(item.get("extracted_price")) or parse_price(item.get("price"))
        display = parse_price(item.get("price"))
        out.append(
            SearchCandidate(
                product_id=str(pid),
                title=title,
                seller=_usable_str(item.get("source")) or "",
                price=parsed[0] if parsed else None,
                currency=display[1] if display else "USD",
                rating=_as_float(item.get("rating")),
                rating_count=_as_int(item.get("reviews")),
                google_panel_url=_usable_str(item.get("product_link")),
            )
        )
    return out


def parse_product_details(payload: dict[str, Any]) -> ProductDetails:
    """Parse a SerpApi `google_product` (or `google_immersive_product`) response."""
    product = payload.get("product_results") or {}
    stores: list[Store] = []
    for s in product.get("stores", []) or []:
        url = _usable_str(s.get("link"))
        if not url:
            continue
        offers = s.get("details_and_offers")
        availability = next(
            (o for o in offers if isinstance(o, str) and "stock" in o.lower()), None
        ) if isinstance(offers, list) else None  # fmt: skip
        price = _as_float(s.get("extracted_price"))
        if price is None:
            parsed = parse_price(s.get("price"))
            price = parsed[0] if parsed else None
        stores.append(
            Store(
                seller=_usable_str(s.get("name")) or "",
                url=url,
                price=price,
                shipping=_as_float(s.get("shipping_extracted")),
                total=_as_float(s.get("extracted_total")),
                availability=availability,
            )
        )
    attributes = [
        {"name": str(a.get("name", "")), "value": str(a.get("value", ""))}
        for a in product.get("product_attributes", []) or []
        if isinstance(a, dict)
    ]
    about = product.get("about_the_product") or {}
    features = about.get("features") if isinstance(about, dict) else None
    return ProductDetails(
        stores=stores,
        attributes=attributes,
        dimensions=parse_dimensions(features, product.get("product_attributes")),
    )


# ---- HTTP providers --------------------------------------------------------------------------

_STATUS_HELP = {
    401: "authentication failed (check the API key)",
    402: "payment required (credits or plan exhausted)",
    403: "forbidden (check the API key and plan)",
    429: "rate limited or out of searches for this plan",
}


class _HttpProvider:
    name = "provider"

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        if not api_key:
            raise ProviderError(self.name, "API key is not set")
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT_S)

    def __repr__(self) -> str:  # never show the key, even in a debugger or a stray f-string
        return f"{type(self).__name__}()"

    def _request(self, send: Any) -> dict[str, Any]:
        """Run `send()` with one immediate retry on timeout or 5xx; map errors without leaking."""
        last_status: int | None = None
        for attempt in (1, 2):
            try:
                response = send()
            except httpx.TimeoutException:
                if attempt == 2:
                    raise ProviderError(self.name, "request timed out after retry") from None
                continue
            except httpx.HTTPError as exc:
                raise ProviderError(self.name, f"request failed ({type(exc).__name__})") from None
            status = response.status_code
            if status >= 500 and attempt == 1:
                last_status = status
                continue
            if status in _STATUS_HELP:
                raise ProviderError(
                    self.name, f"HTTP {status} {_STATUS_HELP[status]}", status=status
                )
            if status >= 400:
                raise ProviderError(self.name, f"HTTP {status}", status=status)
            try:
                data = response.json()
            except ValueError:
                raise ProviderError(
                    self.name, "response was not valid JSON", status=status
                ) from None
            if not isinstance(data, dict):
                raise ProviderError(self.name, "unexpected response shape", status=status)
            return data
        raise ProviderError(self.name, f"HTTP {last_status}", status=last_status)


class SerperSearch(_HttpProvider):
    """Serper.dev Google Shopping search (POST, key in the X-API-KEY header)."""

    name = "serper"
    URL = "https://google.serper.dev/shopping"

    def search(
        self, query: str, gl: str = DEFAULT_GL, hl: str = DEFAULT_HL
    ) -> list[SearchCandidate]:
        body = {"q": query, "gl": gl, "hl": hl}
        headers = {"X-API-KEY": self._api_key, "Content-Type": "application/json"}
        data = self._request(lambda: self._client.post(self.URL, json=body, headers=headers))
        return parse_serper_search(data)


class SerpApiSearch(_HttpProvider):
    """SerpApi `google_shopping` search (GET, key as a query parameter)."""

    name = "serpapi"
    URL = "https://serpapi.com/search.json"

    def search(
        self, query: str, gl: str = DEFAULT_GL, hl: str = DEFAULT_HL
    ) -> list[SearchCandidate]:
        params = {
            "engine": "google_shopping",
            "q": query,
            "gl": gl,
            "hl": hl,
            "api_key": self._api_key,
        }
        data = self._request(lambda: self._client.get(self.URL, params=params))
        return parse_serpapi_search(data)


class SerpApiProductLookup(_HttpProvider):
    """SerpApi `google_product` by product id (works with Serper product ids too)."""

    name = "serpapi"
    URL = "https://serpapi.com/search.json"

    def lookup(self, product_id: str, gl: str = DEFAULT_GL, hl: str = DEFAULT_HL) -> ProductDetails:
        params = {
            "engine": "google_product", "product_id": product_id, "gl": gl, "hl": hl,
            "api_key": self._api_key,
        }  # fmt: skip
        data = self._request(lambda: self._client.get(self.URL, params=params))
        return parse_product_details(data)


# ---- fixture-backed fakes for mock mode ---------------------------------------------------------


def _load_fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURE_DIR / name).read_text())
    assert isinstance(data, dict)
    return data


def _tag(candidates: list[SearchCandidate]) -> list[SearchCandidate]:
    """Label fixture results so nothing in a mock run can be mistaken for live data."""
    return [
        SearchCandidate(
            c.product_id, FIXTURE_TITLE_PREFIX + c.title, c.seller, c.price, c.currency,
            c.rating, c.rating_count, c.google_panel_url,
        )
        for c in candidates
    ]  # fmt: skip


class FixtureSearch:
    """Fake search provider, keyed by query. Any query not in `by_query` gets `default`."""

    def __init__(self, name: str, default: str, by_query: dict[str, str] | None = None) -> None:
        self.name = name
        self._default = default
        self._by_query = {q.lower(): f for q, f in (by_query or {}).items()}
        self.queries: list[str] = []
        parser = parse_serper_search if name == "serper" else parse_serpapi_search
        self._parser = parser

    def search(
        self, query: str, gl: str = DEFAULT_GL, hl: str = DEFAULT_HL
    ) -> list[SearchCandidate]:
        self.queries.append(query)
        fixture = self._by_query.get(query.lower(), self._default)
        return _tag(self._parser(_load_fixture(fixture)))


class FixtureLookup:
    """Fake product lookup, keyed by product id. Unknown ids have no stores (exercises fallback)."""

    name = "serpapi"

    def __init__(self, by_product_id: dict[str, str]) -> None:
        self._by_id = dict(by_product_id)
        self.product_ids: list[str] = []

    def lookup(self, product_id: str, gl: str = DEFAULT_GL, hl: str = DEFAULT_HL) -> ProductDetails:
        self.product_ids.append(product_id)
        fixture = self._by_id.get(product_id)
        return parse_product_details(_load_fixture(fixture)) if fixture else ProductDetails()


# Product ids that have a lookup fixture. The first has dimensions, the second does not.
FIXTURE_LOOKUPS = {
    "7810752583849448260": "serpapi_product_ikea_guttane.json",
    "6965038339607673363": "serpapi_product_article.json",
}


def build_providers(config: Config) -> tuple[SearchProvider, ProductLookup]:
    """The (search, lookup) pair for `config`: fixture fakes in mock mode, HTTP in live mode."""
    if config.llm_mode == "mock":
        default = (
            "serper_search_oak_side_table.json"
            if config.search_provider == "serper"
            else "serpapi_search_oak_side_table.json"
        )
        return FixtureSearch(config.search_provider, default), FixtureLookup(FIXTURE_LOOKUPS)
    lookup = SerpApiProductLookup(os.environ.get("SERPAPI_API_KEY", ""))
    if config.search_provider == "serper":
        return SerperSearch(os.environ.get("SERPER_API_KEY", "")), lookup
    return SerpApiSearch(os.environ.get("SERPAPI_API_KEY", "")), lookup
