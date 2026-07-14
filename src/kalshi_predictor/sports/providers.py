from dataclasses import dataclass
from typing import Any

from kalshi_predictor.config import Settings, get_settings

PUBLIC_PROVIDER_MESSAGE = (
    "Phase 3J sports providers are scaffolds for public/free data only. "
    "Use manual JSON/CSV import until a no-key public source is configured."
)


class SportsProviderUnavailable(RuntimeError):
    """Raised when a requested public sports source is not configured."""


@dataclass(frozen=True)
class SportsProviderResult:
    league: str
    source: str
    payload: dict[str, Any]
    message: str = PUBLIC_PROVIDER_MESSAGE


class ManualSportsProvider:
    source = "manual"

    def fetch(
        self,
        *,
        league: str,
        settings: Settings | None = None,
    ) -> SportsProviderResult:
        del settings
        return SportsProviderResult(
            league=league.upper(),
            source=self.source,
            payload={
                "league": league.upper(),
                "teams": [],
                "games": [],
                "team_stats": [],
                "injuries": [],
                "odds": [],
            },
        )


def provider_guidance(settings: Settings | None = None) -> dict[str, Any]:
    resolved = settings or get_settings()
    return {
        "enabled": resolved.sports_enabled,
        "leagues": parse_sports_leagues(resolved.sports_leagues),
        "odds_enabled": resolved.sports_odds_enabled,
        "weather_enabled": resolved.sports_weather_enabled,
        "message": PUBLIC_PROVIDER_MESSAGE,
        "supported_sources": {
            "schedules": "manual JSON/CSV first; public/free source adapter placeholder",
            "standings": "manual JSON/CSV first; public/free source adapter placeholder",
            "scores": "manual JSON/CSV first; public/free source adapter placeholder",
            "injuries": "manual JSON/CSV first; public/free source adapter placeholder",
            "odds": "manual JSON/CSV first; paid odds APIs intentionally not used",
            "weather": "local/manual weather context or existing weather features only",
        },
    }


def parse_sports_leagues(raw: str) -> tuple[str, ...]:
    leagues = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    return leagues or ("MLB", "NBA", "NFL", "NHL")


def fetch_public_sports_data(
    *,
    league: str,
    source: str,
    settings: Settings | None = None,
) -> SportsProviderResult:
    resolved = settings or get_settings()
    normalized_source = source.strip().lower()
    if normalized_source in {"manual", "file"}:
        return ManualSportsProvider().fetch(league=league, settings=resolved)
    raise SportsProviderUnavailable(
        f"{source} is not configured. {PUBLIC_PROVIDER_MESSAGE}"
    )

