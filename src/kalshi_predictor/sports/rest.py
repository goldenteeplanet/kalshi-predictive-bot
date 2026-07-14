from datetime import datetime
from decimal import Decimal

from kalshi_predictor.data.schema import SportsGame
from kalshi_predictor.utils.time import parse_datetime


def rest_days_for_team(
    games: list[SportsGame],
    *,
    team_key: str,
    before: datetime | None,
) -> Decimal | None:
    if before is None:
        return None
    normalized_before = parse_datetime(before)
    if normalized_before is None:
        return None
    previous = [
        scheduled
        for game in games
        if (scheduled := parse_datetime(game.scheduled_at)) is not None
        and scheduled < normalized_before
        and team_key in {game.home_team_key, game.away_team_key}
    ]
    if not previous:
        return None
    last_game = max(previous)
    return Decimal(str((normalized_before - last_game).total_seconds())) / Decimal("86400")


def rest_edge(
    games: list[SportsGame],
    *,
    home_team_key: str,
    away_team_key: str,
    scheduled_at: datetime | None,
) -> Decimal:
    home_rest = rest_days_for_team(games, team_key=home_team_key, before=scheduled_at)
    away_rest = rest_days_for_team(games, team_key=away_team_key, before=scheduled_at)
    if home_rest is None or away_rest is None:
        return Decimal("0")
    return _clamp((home_rest - away_rest) * Decimal("0.015"), Decimal("0.06"))


def _clamp(value: Decimal, limit: Decimal) -> Decimal:
    if value > limit:
        return limit
    if value < -limit:
        return -limit
    return value.quantize(Decimal("0.0001"))
