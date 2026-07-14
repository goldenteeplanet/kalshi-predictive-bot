import re
from decimal import Decimal

from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json
from kalshi_predictor.data.schema import EconomicFeature, MarketSnapshot
from kalshi_predictor.economic.repository import (
    get_latest_economic_feature,
    get_latest_economic_link_for_ticker,
)
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.forecasting.skip_log import log_forecast_skip
from kalshi_predictor.utils.decimals import clamp_probability, midpoint, to_decimal

ECONOMIC_TERMS = (
    "cpi",
    "inflation",
    "fed",
    "fomc",
    "rate",
    "rates",
    "interest rate",
    "unemployment",
    "jobs",
    "payrolls",
    "gdp",
    "recession",
)


class EconomicV1Forecaster:
    model_name = "economic_v1"

    def forecast(self, session: Session, snapshot: MarketSnapshot) -> ForecastOutput | None:
        if not _is_economic_market(snapshot):
            _skip(session, snapshot, "not an economic market", available={"snapshot": True})
            return None

        link = get_latest_economic_link_for_ticker(session, snapshot.ticker)
        if link is None:
            _skip(session, snapshot, "no economic market link", available={"snapshot": True})
            return None

        feature = get_latest_economic_feature(session, link.event_key)
        if feature is None:
            _skip(
                session,
                snapshot,
                "no economic features",
                available={"link": True, "event_key": link.event_key},
            )
            return None

        market_mid = _market_midpoint(snapshot)
        if market_mid is None:
            _skip(
                session,
                snapshot,
                "no market midpoint",
                available={
                    "best_yes_bid": snapshot.best_yes_bid,
                    "best_yes_ask": snapshot.best_yes_ask,
                    "last_price": snapshot.last_price_dollars,
                },
            )
            return None

        adjustment = _economic_adjustment(snapshot, feature)
        final_probability = clamp_probability(market_mid + adjustment)
        return ForecastOutput(
            ticker=snapshot.ticker,
            forecasted_at=snapshot.captured_at,
            model_name=self.model_name,
            yes_probability=final_probability,
            market_mid_probability=market_mid,
            best_yes_bid=to_decimal(snapshot.best_yes_bid),
            best_yes_ask=to_decimal(snapshot.best_yes_ask),
            feature_json={
                "event_key": link.event_key,
                "category": link.category,
                "link_confidence": link.confidence,
                "economic_feature_id": feature.id,
                "surprise_score": feature.surprise_score,
                "direction": feature.direction,
                "confidence_score": feature.confidence_score,
                "market_mid": str(market_mid),
                "adjustment": str(adjustment),
                "final_probability": str(final_probability),
            },
            notes="economic_v1 midpoint plus bounded economic surprise adjustment.",
        )


def _is_economic_market(snapshot: MarketSnapshot) -> bool:
    raw_market = decode_json(snapshot.raw_market_json)
    text = " ".join(
        str(raw_market.get(key) or "")
        for key in ("title", "subtitle", "series_ticker", "event_ticker", "ticker")
    ).lower()
    return any(term in text for term in ECONOMIC_TERMS)


def _economic_adjustment(snapshot: MarketSnapshot, feature: EconomicFeature) -> Decimal:
    surprise = to_decimal(feature.surprise_score) or Decimal("0")
    confidence = to_decimal(feature.confidence_score) or Decimal("35")
    direction = _market_direction(snapshot)
    if feature.direction == "DOWN":
        surprise = -abs(surprise)
    elif feature.direction == "UP":
        surprise = abs(surprise)
    if direction == "BELOW":
        surprise = -surprise
    confidence_multiplier = max(Decimal("0"), min(confidence / Decimal("100"), Decimal("1")))
    adjustment = surprise * Decimal("0.10") * confidence_multiplier
    if adjustment > Decimal("0.06"):
        return Decimal("0.06")
    if adjustment < Decimal("-0.06"):
        return Decimal("-0.06")
    return adjustment


def _market_direction(snapshot: MarketSnapshot) -> str:
    raw_market = decode_json(snapshot.raw_market_json)
    text = str(raw_market.get("title") or snapshot.ticker).lower()
    if re.search(r"\b(below|less than|under|at or below)\b", text):
        return "BELOW"
    if re.search(r"\b(above|greater than|over|at or above|exceed)\b", text):
        return "ABOVE"
    return "ABOVE"


def _market_midpoint(snapshot: MarketSnapshot) -> Decimal | None:
    yes_bid = to_decimal(snapshot.best_yes_bid)
    yes_ask = to_decimal(snapshot.best_yes_ask)
    if yes_bid is not None and yes_ask is not None:
        return midpoint(yes_bid, yes_ask)
    return to_decimal(snapshot.last_price_dollars)


def _skip(
    session: Session,
    snapshot: MarketSnapshot,
    reason: str,
    *,
    available: dict[str, object],
) -> None:
    log_forecast_skip(
        session,
        model_name=EconomicV1Forecaster.model_name,
        ticker=snapshot.ticker,
        reason=reason,
        required_data=["economic market link", "economic features", "market midpoint"],
        available_data=available,
    )
