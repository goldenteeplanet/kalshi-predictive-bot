from decimal import Decimal
from typing import Any

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.utils.decimals import to_decimal


def late_move_score(feature: dict[str, Any], *, minutes_to_close: Decimal | None) -> Decimal:
    velocity = abs(to_decimal(feature.get("price_velocity")) or Decimal("0"))
    acceleration = abs(to_decimal(feature.get("price_acceleration")) or Decimal("0"))
    liquidity_pct = abs(to_decimal(feature.get("liquidity_change_pct")) or Decimal("0"))
    spread_change = abs(to_decimal(feature.get("spread_change")) or Decimal("0"))
    timing_multiplier = Decimal("0.25")
    if minutes_to_close is not None:
        if minutes_to_close <= Decimal("60"):
            timing_multiplier = Decimal("1.0")
        elif minutes_to_close <= Decimal("360"):
            timing_multiplier = Decimal("0.75")
        elif minutes_to_close <= Decimal("1440"):
            timing_multiplier = Decimal("0.50")
    raw = (
        velocity * Decimal("5")
        + acceleration * Decimal("3")
        + liquidity_pct * Decimal("0.8")
        + spread_change * Decimal("2")
    )
    return min(raw * timing_multiplier, Decimal("1"))


def detect_late_move_events(
    feature: dict[str, Any],
    *,
    minutes_to_close: Decimal | None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    resolved_settings = settings or get_settings()
    score = late_move_score(feature, minutes_to_close=minutes_to_close)
    if score < resolved_settings.microstructure_late_move_threshold:
        return []
    velocity = to_decimal(feature.get("price_velocity")) or Decimal("0")
    events = [
        {
            "ticker": feature["ticker"],
            "event_type": "LATE_YES_MOVE" if velocity >= 0 else "LATE_NO_MOVE",
            "severity": "HIGH" if score >= Decimal("0.50") else "MEDIUM",
            "score": score * Decimal("100"),
            "title": "Late YES Move" if velocity >= 0 else "Late NO Move",
            "description": "Late price movement is visible near close; treat as a caution flag.",
            "evidence": {
                "price_velocity": str(feature.get("price_velocity")),
                "price_acceleration": str(feature.get("price_acceleration")),
                "minutes_to_close": str(minutes_to_close),
            },
        }
    ]
    if abs(to_decimal(feature.get("price_acceleration")) or Decimal("0")) >= Decimal("0.05"):
        events.append(
            {
                **events[0],
                "event_type": "LATE_VOLATILITY_SPIKE",
                "title": "Late Volatility Spike",
            }
        )
    if abs(to_decimal(feature.get("liquidity_change_pct")) or Decimal("0")) >= Decimal("0.50"):
        events.append(
            {
                **events[0],
                "event_type": "LATE_LIQUIDITY_SURGE",
                "title": "Late Liquidity Surge",
            }
        )
    return events
