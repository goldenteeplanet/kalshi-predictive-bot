from decimal import Decimal
from typing import Any

from kalshi_predictor.utils.decimals import to_decimal

ZERO = Decimal("0")
HUNDRED = Decimal("100")


def score_liquidity(
    *,
    volume: Any = None,
    open_interest: Any = None,
    liquidity: Any = None,
) -> Decimal:
    volume_value = max(to_decimal(volume) or ZERO, ZERO)
    open_interest_value = max(to_decimal(open_interest) or ZERO, ZERO)
    liquidity_value = max(to_decimal(liquidity) or ZERO, ZERO)
    raw = (
        _scaled(volume_value, Decimal("1000")) * Decimal("0.35")
        + _scaled(open_interest_value, Decimal("500")) * Decimal("0.25")
        + _scaled(liquidity_value, Decimal("10000")) * Decimal("0.40")
    )
    return _clamp_score(raw)


def score_spread(spread: Any, *, max_spread: Decimal = Decimal("0.10")) -> Decimal:
    spread_value = to_decimal(spread)
    if spread_value is None or spread_value < ZERO:
        return Decimal("35")
    if spread_value >= max_spread:
        return ZERO
    return _clamp_score((Decimal("1") - (spread_value / max_spread)) * HUNDRED)


def score_time_to_close(
    time_to_close_minutes: Any,
    *,
    min_minutes: Decimal = Decimal("30"),
) -> Decimal:
    minutes = to_decimal(time_to_close_minutes)
    if minutes is None:
        return Decimal("45")
    if minutes < min_minutes:
        return Decimal("10")
    if minutes <= Decimal("1440"):
        return _clamp_score(Decimal("60") + (minutes / Decimal("1440")) * Decimal("40"))
    if minutes <= Decimal("10080"):
        return _clamp_score(
            Decimal("100") - ((minutes - Decimal("1440")) / Decimal("8640")) * Decimal("35")
        )
    return Decimal("45")


def score_model_confidence(probability: Any) -> Decimal:
    probability_value = to_decimal(probability)
    if probability_value is None:
        return Decimal("35")
    confidence = abs(probability_value - Decimal("0.50")) * Decimal("2") * HUNDRED
    return _clamp_score(confidence)


def score_edge(edge: Any) -> Decimal:
    edge_value = to_decimal(edge)
    if edge_value is None or edge_value <= ZERO:
        return ZERO
    return _clamp_score((edge_value / Decimal("0.20")) * HUNDRED)


def calculate_opportunity_score(
    *,
    estimated_edge: Any,
    liquidity_score: Any,
    spread_score: Any,
    time_score: Any,
    model_confidence_score: Any,
) -> Decimal:
    edge_component = score_edge(estimated_edge)
    return _clamp_score(
        edge_component * Decimal("0.35")
        + (to_decimal(liquidity_score) or ZERO) * Decimal("0.20")
        + (to_decimal(spread_score) or ZERO) * Decimal("0.20")
        + (to_decimal(time_score) or ZERO) * Decimal("0.10")
        + (to_decimal(model_confidence_score) or ZERO) * Decimal("0.15")
    )


def _scaled(value: Decimal, full_score_value: Decimal) -> Decimal:
    if full_score_value <= ZERO:
        return ZERO
    return min(HUNDRED, (value / full_score_value) * HUNDRED)


def _clamp_score(value: Decimal) -> Decimal:
    if value < ZERO:
        return ZERO
    if value > HUNDRED:
        return HUNDRED
    return value
