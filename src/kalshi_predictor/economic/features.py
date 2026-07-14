from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import EconomicEvent
from kalshi_predictor.economic.repository import (
    get_economic_events,
    insert_economic_feature,
)
from kalshi_predictor.utils.decimals import to_decimal
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class EconomicFeatureBuildSummary:
    events_processed: int
    features_inserted: int


def build_economic_features(session: Session) -> EconomicFeatureBuildSummary:
    events = get_economic_events(session)
    generated_at = utc_now()
    inserted = 0
    for event in events:
        features = calculate_economic_features(event)
        insert_economic_feature(
            session,
            event_key=event.event_key,
            generated_at=generated_at,
            category=event.category,
            surprise_score=features["surprise_score"],
            direction=features["direction"],
            confidence_score=features["confidence_score"],
            raw_json=features,
        )
        inserted += 1
    return EconomicFeatureBuildSummary(
        events_processed=len(events),
        features_inserted=inserted,
    )


def calculate_economic_features(event: EconomicEvent) -> dict[str, Any]:
    actual = to_decimal(event.actual_value)
    forecast = to_decimal(event.forecast_value)
    previous = to_decimal(event.previous_value)
    surprise = _surprise_score(actual=actual, forecast=forecast, previous=previous)
    direction = _direction(surprise)
    confidence = Decimal("70") if surprise is not None else Decimal("35")
    return {
        "event_key": event.event_key,
        "category": event.category,
        "actual_value": event.actual_value,
        "forecast_value": event.forecast_value,
        "previous_value": event.previous_value,
        "surprise_score": surprise,
        "direction": direction,
        "confidence_score": confidence,
    }


def _surprise_score(
    *,
    actual: Decimal | None,
    forecast: Decimal | None,
    previous: Decimal | None,
) -> Decimal | None:
    if actual is not None and forecast is not None:
        denominator = max(abs(forecast), Decimal("1"))
        return _clamp((actual - forecast) / denominator)
    if actual is not None and previous is not None:
        denominator = max(abs(previous), Decimal("1"))
        return _clamp((actual - previous) / denominator)
    return None


def _direction(surprise: Decimal | None) -> str:
    if surprise is None:
        return "NEUTRAL"
    if surprise > Decimal("0.02"):
        return "UP"
    if surprise < Decimal("-0.02"):
        return "DOWN"
    return "NEUTRAL"


def _clamp(value: Decimal) -> Decimal:
    if value > Decimal("1"):
        return Decimal("1")
    if value < Decimal("-1"):
        return Decimal("-1")
    return value
