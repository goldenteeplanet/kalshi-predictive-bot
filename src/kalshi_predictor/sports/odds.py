from decimal import Decimal
from typing import Any

from kalshi_predictor.data.schema import SportsOdds
from kalshi_predictor.utils.decimals import to_decimal


def moneyline_to_implied_probability(value: Any) -> Decimal | None:
    odds = to_decimal(value)
    if odds is None:
        return None
    if Decimal("0") < odds < Decimal("1"):
        return odds
    if odds == 0:
        return None
    if odds > 0:
        return (Decimal("100") / (odds + Decimal("100"))).quantize(Decimal("0.0001"))
    absolute = abs(odds)
    return (absolute / (absolute + Decimal("100"))).quantize(Decimal("0.0001"))


def remove_vig(home_probability: Any, away_probability: Any) -> tuple[Decimal, Decimal] | None:
    home = to_decimal(home_probability)
    away = to_decimal(away_probability)
    if home is None or away is None:
        return None
    total = home + away
    if total <= 0:
        return None
    return (
        (home / total).quantize(Decimal("0.0001")),
        (away / total).quantize(Decimal("0.0001")),
    )


def odds_home_probability(row: SportsOdds | None) -> Decimal | None:
    if row is None:
        return None
    home = moneyline_to_implied_probability(row.home_moneyline)
    away = moneyline_to_implied_probability(row.away_moneyline)
    if home is None or away is None:
        return None
    normalized = remove_vig(home, away)
    return normalized[0] if normalized else None


def odds_edge(row: SportsOdds | None) -> Decimal:
    home_probability = odds_home_probability(row)
    if home_probability is None:
        return Decimal("0")
    return _clamp(home_probability - Decimal("0.5"), Decimal("0.08"))


def _clamp(value: Decimal, limit: Decimal) -> Decimal:
    if value > limit:
        return limit
    if value < -limit:
        return -limit
    return value.quantize(Decimal("0.0001"))

