from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import MarketSnapshot, SportsGame
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.sports.repository import latest_sports_feature, latest_sports_link
from kalshi_predictor.utils.decimals import midpoint, to_decimal


class SportsV1Forecaster:
    model_name = "sports_v1"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        league: str | None = None,
        model_name: str | None = None,
        max_adjustment_field: str = "sports_v1_max_adjustment",
    ) -> None:
        self.settings = settings or get_settings()
        self.league = league
        self.model_name = model_name or self.model_name
        self.max_adjustment_field = max_adjustment_field

    def forecast(self, session: Session, snapshot: MarketSnapshot) -> ForecastOutput | None:
        link = latest_sports_link(session, snapshot.ticker, league=self.league)
        if link is None:
            return None
        feature = latest_sports_feature(
            session,
            ticker=snapshot.ticker,
            league=link.league,
            game_key=link.game_key,
        )
        if feature is None:
            return None
        market_mid = _market_midpoint(snapshot)
        if market_mid is None:
            return None
        game = session.scalar(select(SportsGame).where(SportsGame.game_key == link.game_key))
        total_edge = to_decimal(feature.total_edge) or Decimal("0")
        adjustment = _bounded_adjustment(
            total_edge,
            getattr(self.settings, self.max_adjustment_field),
        )
        if _market_yes_side(snapshot, game=game) == "away":
            adjustment = -adjustment
        final_probability = _clamp_probability(market_mid + adjustment)
        return ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at,
            model_name=self.model_name,
            yes_probability=final_probability,
            market_mid_probability=market_mid,
            best_yes_bid=to_decimal(snapshot.best_yes_bid),
            best_yes_ask=to_decimal(snapshot.best_yes_ask),
            feature_json={
                "sports_feature_id": feature.id,
                "sports_market_link_id": link.id,
                "league": link.league,
                "game_key": link.game_key,
                "market_type": link.market_type,
                "link_confidence": link.link_confidence,
                "home_team_key": feature.home_team_key,
                "away_team_key": feature.away_team_key,
                "team_strength_edge": feature.team_strength_edge,
                "injury_edge": feature.injury_edge,
                "rest_edge": feature.rest_edge,
                "travel_edge": feature.travel_edge,
                "odds_edge": feature.odds_edge,
                "weather_edge": feature.weather_edge,
                "total_edge": feature.total_edge,
                "home_win_probability": feature.home_win_probability,
                "away_win_probability": feature.away_win_probability,
                "projected_total": feature.projected_total,
                "confidence_score": feature.confidence_score,
                "market_yes_side": _market_yes_side(snapshot, game=game),
                "market_mid": str(market_mid),
                "adjustment": str(adjustment),
                "final_probability": str(final_probability),
            },
            notes=f"{self.model_name} midpoint plus bounded local sports feature adjustment.",
        )


def _market_midpoint(snapshot: MarketSnapshot) -> Decimal | None:
    yes_bid = to_decimal(snapshot.best_yes_bid)
    yes_ask = to_decimal(snapshot.best_yes_ask)
    if yes_bid is not None and yes_ask is not None:
        return midpoint(yes_bid, yes_ask)
    return to_decimal(snapshot.last_price_dollars)


def _market_yes_side(snapshot: MarketSnapshot, *, game: SportsGame | None) -> str:
    if game is None:
        return "home"
    raw = decode_json(snapshot.raw_market_json)
    text = " ".join(
        str(raw.get(key) or "")
        for key in ("title", "subtitle", "rules", "rules_primary", "ticker")
    ).lower()
    home_alias = game.home_team_key.split(":", 1)[-1].replace("-", " ")
    away_alias = game.away_team_key.split(":", 1)[-1].replace("-", " ")
    if away_alias in text and home_alias not in text:
        return "away"
    return "home"


def _bounded_adjustment(value: Decimal, max_adjustment: Decimal) -> Decimal:
    if value > max_adjustment:
        return max_adjustment
    if value < -max_adjustment:
        return -max_adjustment
    return value


def _clamp_probability(value: Decimal) -> Decimal:
    if value < Decimal("0.01"):
        return Decimal("0.01")
    if value > Decimal("0.99"):
        return Decimal("0.99")
    return value.quantize(Decimal("0.0001"))

