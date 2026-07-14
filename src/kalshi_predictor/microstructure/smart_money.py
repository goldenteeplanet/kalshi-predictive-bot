from decimal import Decimal
from typing import Any

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.utils.decimals import to_decimal


def smart_money_score(feature: dict[str, Any]) -> Decimal:
    velocity = abs(to_decimal(feature.get("price_velocity")) or Decimal("0"))
    spread_change = to_decimal(feature.get("spread_change")) or Decimal("0")
    liquidity_pct = to_decimal(feature.get("liquidity_change_pct")) or Decimal("0")
    imbalance = abs(to_decimal(feature.get("orderbook_imbalance")) or Decimal("0"))
    late_score = to_decimal(feature.get("late_move_score")) or Decimal("0")
    dislocation = to_decimal(feature.get("dislocation_score")) or Decimal("0")
    score = (
        min(velocity * Decimal("4"), Decimal("0.25"))
        + (Decimal("0.15") if spread_change < 0 else Decimal("0"))
        + min(max(liquidity_pct, Decimal("0")) * Decimal("0.4"), Decimal("0.15"))
        + min(imbalance * Decimal("0.2"), Decimal("0.20"))
        + late_score * Decimal("0.15")
        + dislocation * Decimal("0.10")
    )
    return min(score, Decimal("1"))


def detect_smart_money_events(
    feature: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    resolved_settings = settings or get_settings()
    score = smart_money_score(feature)
    liquidity = to_decimal(feature.get("current_liquidity")) or Decimal("0")
    if liquidity <= 0 and score >= Decimal("0.25"):
        return [
            _event(
                feature,
                "FALSE_ALARM_LOW_LIQUIDITY",
                score,
                "Possible informed-flow pattern is unreliable because liquidity is low.",
                severity="LOW",
            )
        ]
    if score < resolved_settings.microstructure_smart_money_threshold:
        return []
    imbalance = to_decimal(feature.get("orderbook_imbalance")) or Decimal("0")
    event_type = "SMART_MONEY_YES_PRESSURE" if imbalance >= 0 else "SMART_MONEY_NO_PRESSURE"
    return [
        _event(
            feature,
            event_type,
            score,
            "Possible informed flow heuristic triggered; this is not proof of smart money.",
            severity="HIGH",
        ),
        _event(
            feature,
            "POSSIBLE_INFORMED_FLOW",
            score,
            "Possible informed flow, based on price movement, spread, liquidity, and pressure.",
            severity="MEDIUM",
        ),
    ]


def _event(
    feature: dict[str, Any],
    event_type: str,
    score: Decimal,
    description: str,
    *,
    severity: str,
) -> dict[str, Any]:
    return {
        "ticker": feature["ticker"],
        "event_type": event_type,
        "severity": severity,
        "score": score * Decimal("100"),
        "title": event_type.replace("_", " ").title(),
        "description": description,
        "evidence": {
            "smart_money_score": str(score),
            "price_velocity": str(feature.get("price_velocity")),
            "liquidity_change_pct": str(feature.get("liquidity_change_pct")),
            "orderbook_imbalance": str(feature.get("orderbook_imbalance")),
        },
    }

