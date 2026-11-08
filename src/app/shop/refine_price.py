"""Typed price range for the NEXT Shop Again (a refine chip of kind `price`, id `price`).

The page sends `price: {"min": number|null, "max": number|null}` on `POST /refine`. It REPLACES the
stored price; `{}` or both null clears it. While it is set, a Shop Again:

  * uses it instead of any budget parsed from the request or a text refinement ("under $400");
  * drops every candidate outside [min, max] (inclusive), and any with no usable price, before
    ranking (the rule is `search.apply_filters`, fed this range through the query);
  * adds price words to the targeted search when there is one (typed filters), and makes at most
    ONE extra page fetch when the strict rule leaves fewer than a full batch.

Nothing here runs for a first search or for a Shop Again without a typed price.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from app.shop.models import RefineChip

CHIP_KIND: Literal["price"] = "price"
CHIP_ID = "price"
MAX_PRICE = 1_000_000.0

Price = tuple[float | None, float | None]  # (min, max); at least one is set


class PriceError(ValueError):
    """The price payload is not valid (the message is safe to show)."""


def _number(raw: Any, name: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise PriceError(f"price {name} must be a number")
    value = float(raw)
    if not math.isfinite(value) or value < 0 or value > MAX_PRICE:
        raise PriceError(f"price {name} must be between 0 and {MAX_PRICE:.0f}")
    return value


def parse_price(raw: Any) -> Price | None:
    """Validate the payload. None means no price (`{}` or both null). 400-worthy errors raise."""
    if not isinstance(raw, dict):
        raise PriceError("price must be an object with min and max")
    low, high = _number(raw.get("min"), "min"), _number(raw.get("max"), "max")
    if low is None and high is None:
        return None
    if low is not None and high is not None and low > high:
        raise PriceError("price min must not be above max")
    return (low, high)


def _num(value: float | None) -> str:
    """Whole dollars without cents (a fractional bound reads with its cents)."""
    if value is None:
        return ""
    return str(int(value)) if value == int(value) else f"{value:.2f}"


def target_of(price: Price) -> str:
    return f"price:{_num(price[0])}-{_num(price[1])}"


def label_of(price: Price) -> str:
    low, high = price
    if low is None:
        return f"Under ${_num(high)}"
    if high is None:
        return f"Over ${_num(low)}"
    return f"Price: ${_num(low)}-${_num(high)}"


def chip_of(price: Price) -> RefineChip:
    return RefineChip(
        id=CHIP_ID, kind=CHIP_KIND, target=target_of(price), strength="focus", label=label_of(price)
    )


def with_price(chips: list[RefineChip], price: Price | None) -> list[RefineChip]:
    """`chips` with the price chip replaced by the one for `price` (none: removed)."""
    kept = [c for c in chips if c.kind != CHIP_KIND]
    return [*kept, chip_of(price)] if price is not None else kept


def price_of(chips: list[RefineChip]) -> Price | None:
    """The typed price the chips hold, if any."""
    for c in chips:
        if c.kind != CHIP_KIND:
            continue
        _, _, span = c.target.partition(":")
        low, _, high = span.partition("-")
        try:
            got: Price = (float(low) if low else None, float(high) if high else None)
        except ValueError:
            return None
        return got if got != (None, None) else None
    return None


def price_words(price: Price) -> list[str]:
    """Words for a targeted search: "under $200", "over $50" or "$50 to $200"."""
    low, high = price
    if low is None:
        return ["under", f"${_num(high)}"]
    if high is None:
        return ["over", f"${_num(low)}"]
    return [f"${_num(low)}", "to", f"${_num(high)}"]
