from decimal import Decimal

from kalshi_predictor.data.schema import SportsTeamStat
from kalshi_predictor.utils.decimals import to_decimal


def team_strength_edge(
    home_stat: SportsTeamStat | None,
    away_stat: SportsTeamStat | None,
) -> Decimal:
    home_strength = _team_strength(home_stat)
    away_strength = _team_strength(away_stat)
    if home_strength is None or away_strength is None:
        return Decimal("0")
    return _clamp((home_strength - away_strength) * Decimal("0.12"), Decimal("0.08"))


def _team_strength(row: SportsTeamStat | None) -> Decimal | None:
    if row is None:
        return None
    explicit = to_decimal(row.rating)
    if explicit is not None:
        if explicit > Decimal("1"):
            return explicit / Decimal("100")
        return explicit
    wins = to_decimal(row.wins)
    losses = to_decimal(row.losses)
    if wins is not None and losses is not None and wins + losses > 0:
        return wins / (wins + losses)
    offense = to_decimal(row.offense_rating)
    defense = to_decimal(row.defense_rating)
    if offense is not None and defense is not None:
        return Decimal("0.5") + ((offense - defense) / Decimal("200"))
    recent = to_decimal(row.recent_form)
    return recent


def _clamp(value: Decimal, limit: Decimal) -> Decimal:
    if value > limit:
        return limit
    if value < -limit:
        return -limit
    return value.quantize(Decimal("0.0001"))

