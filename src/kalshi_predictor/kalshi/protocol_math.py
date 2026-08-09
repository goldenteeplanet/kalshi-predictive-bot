from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

from kalshi_predictor.utils.decimals import ONE_DOLLAR, to_decimal

ZERO = Decimal("0")
CENTICENT = Decimal("0.0001")
DEFAULT_TAKER_RATE = Decimal("0.07")
DEFAULT_MAKER_RATE = Decimal("0.0175")


@dataclass(frozen=True)
class PriceRange:
    start: Decimal
    end: Decimal
    step: Decimal

    def contains(self, price: Decimal) -> bool:
        return self.start <= price <= self.end


def reciprocal_price(price: Any) -> Decimal | None:
    value = to_decimal(price)
    if value is None or value < ZERO or value > ONE_DOLLAR:
        return None
    return ONE_DOLLAR - value


def price_ranges_from_market(market: Mapping[str, Any]) -> tuple[PriceRange, ...]:
    raw_ranges = market.get("price_ranges")
    parsed: list[PriceRange] = []
    if isinstance(raw_ranges, Sequence) and not isinstance(raw_ranges, (str, bytes)):
        for item in raw_ranges:
            if not isinstance(item, Mapping):
                continue
            start = to_decimal(_first_defined(item, "start", "min", "min_price"))
            end = to_decimal(_first_defined(item, "end", "max", "max_price"))
            step = to_decimal(_first_defined(item, "step", "tick_size"))
            if start is not None and end is not None and step is not None and step > ZERO:
                parsed.append(PriceRange(start=start, end=end, step=step))
    if parsed:
        return tuple(parsed)

    structure = str(market.get("price_level_structure") or "linear_cent")
    if structure == "deci_cent":
        return (PriceRange(ZERO, ONE_DOLLAR, Decimal("0.001")),)
    if structure == "tapered_deci_cent":
        return (
            PriceRange(ZERO, Decimal("0.10"), Decimal("0.001")),
            PriceRange(Decimal("0.10"), Decimal("0.90"), Decimal("0.01")),
            PriceRange(Decimal("0.90"), ONE_DOLLAR, Decimal("0.001")),
        )
    return (PriceRange(ZERO, ONE_DOLLAR, Decimal("0.01")),)


def tick_size_for_price(market: Mapping[str, Any], price: Any) -> Decimal | None:
    value = to_decimal(price)
    if value is None:
        return None
    for price_range in price_ranges_from_market(market):
        if price_range.contains(value):
            return price_range.step
    return None


def is_valid_market_price(market: Mapping[str, Any], price: Any) -> bool:
    value = to_decimal(price)
    tick = tick_size_for_price(market, value)
    if value is None or tick is None or value < ZERO or value > ONE_DOLLAR:
        return False
    price_range = next(
        item for item in price_ranges_from_market(market) if item.contains(value)
    )
    return (value - price_range.start) % tick == ZERO


def trading_fee(
    *,
    price: Any,
    contracts: Any = Decimal("1"),
    maker: bool = False,
    multiplier: Any = Decimal("1"),
) -> Decimal | None:
    price_value = to_decimal(price)
    count_value = to_decimal(contracts)
    multiplier_value = to_decimal(multiplier)
    if (
        price_value is None
        or count_value is None
        or multiplier_value is None
        or not ZERO <= price_value <= ONE_DOLLAR
        or count_value < ZERO
        or multiplier_value < ZERO
    ):
        return None
    rate = DEFAULT_MAKER_RATE if maker else DEFAULT_TAKER_RATE
    raw_fee = multiplier_value * rate * count_value * price_value * (ONE_DOLLAR - price_value)
    return raw_fee.quantize(CENTICENT, rounding=ROUND_CEILING)


def fee_adjusted_expected_value(
    *, probability: Any, price: Any, maker: bool = False, multiplier: Any = Decimal("1")
) -> Decimal | None:
    probability_value = to_decimal(probability)
    price_value = to_decimal(price)
    fee = trading_fee(price=price_value, maker=maker, multiplier=multiplier)
    if probability_value is None or price_value is None or fee is None:
        return None
    if not ZERO <= probability_value <= ONE_DOLLAR:
        return None
    return probability_value - price_value - fee


def _first_defined(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None
