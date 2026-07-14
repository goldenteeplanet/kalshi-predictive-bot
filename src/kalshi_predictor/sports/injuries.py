from decimal import Decimal

from kalshi_predictor.data.schema import SportsInjury
from kalshi_predictor.utils.decimals import to_decimal

STATUS_WEIGHTS = {
    "out": Decimal("1.00"),
    "injured reserve": Decimal("1.00"),
    "doubtful": Decimal("0.70"),
    "questionable": Decimal("0.40"),
    "probable": Decimal("0.10"),
    "day-to-day": Decimal("0.20"),
}


def injury_edge(
    home_injuries: list[SportsInjury],
    away_injuries: list[SportsInjury],
) -> Decimal:
    home_impact = injury_impact(home_injuries)
    away_impact = injury_impact(away_injuries)
    return _clamp((away_impact - home_impact) * Decimal("0.04"), Decimal("0.08"))


def injury_impact(rows: list[SportsInjury]) -> Decimal:
    total = Decimal("0")
    for row in rows:
        explicit = to_decimal(row.impact_score)
        if explicit is not None:
            total += explicit
            continue
        status = row.status.strip().lower()
        total += STATUS_WEIGHTS.get(status, Decimal("0.20"))
    return total


def _clamp(value: Decimal, limit: Decimal) -> Decimal:
    if value > limit:
        return limit
    if value < -limit:
        return -limit
    return value.quantize(Decimal("0.0001"))

