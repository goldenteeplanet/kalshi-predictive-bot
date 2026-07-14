from decimal import Decimal

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import SportsGame


def weather_edge(
    game: SportsGame,
    *,
    settings: Settings | None = None,
) -> Decimal:
    resolved = settings or get_settings()
    if not resolved.sports_weather_enabled:
        return Decimal("0")
    if game.neutral_site:
        return Decimal("0")
    return Decimal("0")

