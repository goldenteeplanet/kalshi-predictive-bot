from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import SportsGame, SportsMarketLink
from kalshi_predictor.sports.injuries import injury_edge
from kalshi_predictor.sports.odds import odds_edge
from kalshi_predictor.sports.repository import (
    insert_sports_feature,
    latest_odds,
    latest_team_stat,
    normalize_league,
    recent_injuries,
    sports_games,
    sports_market_links,
)
from kalshi_predictor.sports.rest import rest_edge
from kalshi_predictor.sports.team_strength import team_strength_edge
from kalshi_predictor.sports.travel import travel_edge
from kalshi_predictor.sports.weather import weather_edge
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal


@dataclass(frozen=True)
class SportsFeatureBuildSummary:
    league: str
    games_processed: int
    links_scanned: int
    features_inserted: int


def build_sports_features(
    session: Session,
    *,
    league: str = "ALL",
    settings: Settings | None = None,
) -> SportsFeatureBuildSummary:
    resolved = settings or get_settings()
    normalized = normalize_league(league)
    games = sports_games(session, league=normalized)
    links = sports_market_links(session, league=normalized)
    links_by_game: dict[str, list[SportsMarketLink]] = {}
    for link in links:
        links_by_game.setdefault(link.game_key, []).append(link)

    inserted = 0
    for game in games:
        payload = calculate_sports_feature(session, game, settings=resolved)
        targets: list[SportsMarketLink | None] = links_by_game.get(game.game_key) or [None]
        for link in targets:
            insert_sports_feature(
                session,
                league=game.league,
                game_key=game.game_key,
                ticker=link.ticker if link else None,
                home_team_key=game.home_team_key,
                away_team_key=game.away_team_key,
                team_strength_edge=payload["team_strength_edge"],
                injury_edge=payload["injury_edge"],
                rest_edge=payload["rest_edge"],
                travel_edge=payload["travel_edge"],
                odds_edge=payload["odds_edge"],
                weather_edge=payload["weather_edge"],
                total_edge=payload["total_edge"],
                home_win_probability=payload["home_win_probability"],
                away_win_probability=payload["away_win_probability"],
                projected_total=payload["projected_total"],
                confidence_score=payload["confidence_score"],
                raw_json={**payload, "link_id": link.id if link else None},
            )
            inserted += 1

    return SportsFeatureBuildSummary(
        league=normalized,
        games_processed=len(games),
        links_scanned=len(links),
        features_inserted=inserted,
    )


def calculate_sports_feature(
    session: Session,
    game: SportsGame,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    all_games = sports_games(session, league=game.league)
    home_stat = latest_team_stat(session, league=game.league, team_key=game.home_team_key)
    away_stat = latest_team_stat(session, league=game.league, team_key=game.away_team_key)
    home_injuries = recent_injuries(
        session,
        league=game.league,
        team_key=game.home_team_key,
        lookback_days=resolved.sports_default_lookback_days,
    )
    away_injuries = recent_injuries(
        session,
        league=game.league,
        team_key=game.away_team_key,
        lookback_days=resolved.sports_default_lookback_days,
    )
    odds = latest_odds(session, league=game.league, game_key=game.game_key)

    strength = team_strength_edge(home_stat, away_stat)
    injury = injury_edge(home_injuries, away_injuries)
    rest = rest_edge(
        all_games,
        home_team_key=game.home_team_key,
        away_team_key=game.away_team_key,
        scheduled_at=game.scheduled_at,
    )
    travel = travel_edge(
        all_games,
        home_team_key=game.home_team_key,
        away_team_key=game.away_team_key,
    )
    odds_value = odds_edge(odds)
    weather = weather_edge(game, settings=resolved)
    total = _clamp_probability_delta(strength + injury + rest + travel + odds_value + weather)
    home_probability = _clamp_probability(Decimal("0.5") + total)
    confidence = _confidence_score(
        has_stats=home_stat is not None and away_stat is not None,
        has_injuries=bool(home_injuries or away_injuries),
        has_odds=odds is not None,
        has_rest=rest != 0,
    )
    projected_total = to_decimal(odds.total if odds else None)
    return {
        "league": game.league,
        "game_key": game.game_key,
        "home_team_key": game.home_team_key,
        "away_team_key": game.away_team_key,
        "team_strength_edge": strength,
        "injury_edge": injury,
        "rest_edge": rest,
        "travel_edge": travel,
        "odds_edge": odds_value,
        "weather_edge": weather,
        "total_edge": total,
        "home_win_probability": home_probability,
        "away_win_probability": Decimal("1") - home_probability,
        "projected_total": projected_total,
        "confidence_score": confidence,
        "inputs": {
            "home_stat_id": home_stat.id if home_stat else None,
            "away_stat_id": away_stat.id if away_stat else None,
            "home_injuries": len(home_injuries),
            "away_injuries": len(away_injuries),
            "odds_id": odds.id if odds else None,
            "scheduled_at": game.scheduled_at.isoformat() if game.scheduled_at else None,
        },
    }


def feature_components_for_display(row: Any) -> dict[str, str]:
    return {
        "team_strength_edge": row.team_strength_edge,
        "injury_edge": row.injury_edge,
        "rest_edge": row.rest_edge,
        "travel_edge": row.travel_edge,
        "odds_edge": row.odds_edge,
        "weather_edge": row.weather_edge,
        "total_edge": row.total_edge,
        "confidence_score": row.confidence_score,
    }


def _confidence_score(
    *,
    has_stats: bool,
    has_injuries: bool,
    has_odds: bool,
    has_rest: bool,
) -> Decimal:
    score = Decimal("20")
    if has_stats:
        score += Decimal("30")
    if has_injuries:
        score += Decimal("15")
    if has_odds:
        score += Decimal("25")
    if has_rest:
        score += Decimal("10")
    return min(score, Decimal("100")).quantize(Decimal("0.0001"))


def _clamp_probability(value: Decimal) -> Decimal:
    if value < Decimal("0.01"):
        return Decimal("0.01")
    if value > Decimal("0.99"):
        return Decimal("0.99")
    return value.quantize(Decimal("0.0001"))


def _clamp_probability_delta(value: Decimal) -> Decimal:
    if value < Decimal("-0.20"):
        return Decimal("-0.20")
    if value > Decimal("0.20"):
        return Decimal("0.20")
    return value.quantize(Decimal("0.0001"))


def serialize_feature_value(value: Any) -> str:
    return decimal_to_str(value) or "0"

