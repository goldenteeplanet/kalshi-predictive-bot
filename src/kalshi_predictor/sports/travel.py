from decimal import Decimal

from kalshi_predictor.data.schema import SportsGame


def travel_edge(
    _games: list[SportsGame],
    *,
    home_team_key: str,
    away_team_key: str,
) -> Decimal:
    del home_team_key, away_team_key
    return Decimal("0")

