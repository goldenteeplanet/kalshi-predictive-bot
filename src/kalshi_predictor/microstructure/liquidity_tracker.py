from decimal import Decimal
from typing import Any

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.utils.decimals import to_decimal


def detect_liquidity_events(
    feature: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    resolved_settings = settings or get_settings()
    change_pct = to_decimal(feature.get("liquidity_change_pct"))
    if change_pct is None:
        return []
    threshold = resolved_settings.microstructure_liquidity_change_threshold
    events: list[dict[str, Any]] = []
    if change_pct >= threshold:
        events.append(
            _event(
                feature,
                "LIQUIDITY_IMPROVING",
                min(change_pct * Decimal("100"), Decimal("100")),
                f"Liquidity increased {change_pct:.2%}, making paper entry/exit cleaner.",
                severity="MEDIUM",
            )
        )
    if change_pct <= -threshold:
        events.append(
            _event(
                feature,
                "LIQUIDITY_DRYING_UP",
                min(abs(change_pct) * Decimal("100"), Decimal("100")),
                "Liquidity dropped sharply, increasing fill and slippage risk.",
                severity="HIGH",
            )
        )
    if change_pct >= threshold * 2:
        events.append(
            _event(
                feature,
                "LIQUIDITY_SPIKE",
                min(change_pct * Decimal("100"), Decimal("100")),
                "Liquidity spiked relative to the recent average.",
                severity="MEDIUM",
            )
        )
    return events


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
        "score": score,
        "title": event_type.replace("_", " ").title(),
        "description": description,
        "evidence": {
            "current_liquidity": str(feature.get("current_liquidity")),
            "avg_liquidity": str(feature.get("avg_liquidity")),
            "liquidity_change_pct": str(feature.get("liquidity_change_pct")),
        },
    }

