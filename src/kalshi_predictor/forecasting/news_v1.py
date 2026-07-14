import re
from decimal import Decimal

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import MarketSnapshot
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.news.repository import feature_linked_news, latest_news_feature
from kalshi_predictor.utils.decimals import midpoint, to_decimal


class NewsV1Forecaster:
    model_name = "news_v1"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def forecast(self, session: Session, snapshot: MarketSnapshot) -> ForecastOutput | None:
        feature = latest_news_feature(session, snapshot.ticker)
        if feature is None or feature.news_count <= 0:
            return None

        market_mid = _market_midpoint(snapshot)
        if market_mid is None:
            return None

        sentiment = to_decimal(feature.avg_sentiment) or Decimal("0")
        importance = to_decimal(feature.max_importance) or Decimal("0")
        freshness = to_decimal(feature.freshness_score) or Decimal("0")
        direction = _market_direction(snapshot)
        directional_sentiment = _directional_sentiment(sentiment, direction)
        adjustment = _bounded_adjustment(
            directional_sentiment=directional_sentiment,
            importance=importance,
            freshness=freshness,
            max_adjustment=self.settings.news_v1_max_adjustment,
        )
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
                "news_feature_id": feature.id,
                "feature_window_minutes": feature.feature_window_minutes,
                "news_count": feature.news_count,
                "high_importance_count": feature.high_importance_count,
                "avg_sentiment": feature.avg_sentiment,
                "max_importance": feature.max_importance,
                "freshness_score": feature.freshness_score,
                "category_counts": decode_json(feature.category_counts_json),
                "entity_counts": decode_json(feature.entity_counts_json),
                "linked_news": feature_linked_news(feature),
                "market_direction": direction,
                "market_mid": str(market_mid),
                "adjustment": str(adjustment),
                "final_probability": str(final_probability),
            },
            notes="news_v1 midpoint plus bounded local news sentiment adjustment.",
        )


def _market_midpoint(snapshot: MarketSnapshot) -> Decimal | None:
    yes_bid = to_decimal(snapshot.best_yes_bid)
    yes_ask = to_decimal(snapshot.best_yes_ask)
    if yes_bid is not None and yes_ask is not None:
        return midpoint(yes_bid, yes_ask)
    return to_decimal(snapshot.last_price_dollars)


def _market_direction(snapshot: MarketSnapshot) -> str:
    raw = decode_json(snapshot.raw_market_json)
    text = " ".join(
        str(part or "")
        for part in (
            snapshot.ticker,
            raw.get("title"),
            raw.get("subtitle"),
            raw.get("rules"),
            raw.get("rules_primary"),
        )
    ).lower()
    if re.search(r"\b(above|over|greater than|exceed|at or above|higher)\b", text):
        return "above"
    if re.search(r"\b(below|under|less than|at or below|lower)\b", text):
        return "below"
    return "unknown"


def _directional_sentiment(sentiment: Decimal, direction: str) -> Decimal:
    if direction == "below":
        return -sentiment
    if direction == "above":
        return sentiment
    return sentiment / Decimal("2")


def _bounded_adjustment(
    *,
    directional_sentiment: Decimal,
    importance: Decimal,
    freshness: Decimal,
    max_adjustment: Decimal,
) -> Decimal:
    weight = _clamp_unit(importance) * _clamp_unit(freshness)
    raw = _clamp_unit_signed(directional_sentiment) * weight * max_adjustment
    if raw > max_adjustment:
        return max_adjustment
    if raw < -max_adjustment:
        return -max_adjustment
    return raw


def _clamp_probability(value: Decimal) -> Decimal:
    if value < Decimal("0.01"):
        return Decimal("0.01")
    if value > Decimal("0.99"):
        return Decimal("0.99")
    return value


def _clamp_unit(value: Decimal) -> Decimal:
    if value < Decimal("0"):
        return Decimal("0")
    if value > Decimal("1"):
        return Decimal("1")
    return value


def _clamp_unit_signed(value: Decimal) -> Decimal:
    if value < Decimal("-1"):
        return Decimal("-1")
    if value > Decimal("1"):
        return Decimal("1")
    return value
