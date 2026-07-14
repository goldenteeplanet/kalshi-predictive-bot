from decimal import Decimal
from typing import Any

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.utils.decimals import to_decimal


def detect_spread_events(
    feature: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    resolved_settings = settings or get_settings()
    spread_change = to_decimal(feature.get("spread_change"))
    current = to_decimal(feature.get("current_spread"))
    max_spread = to_decimal(feature.get("max_spread"))
    avg_spread = to_decimal(feature.get("avg_spread"))
    if spread_change is None or current is None:
        return []
    events: list[dict[str, Any]] = []
    if spread_change <= -resolved_settings.microstructure_spread_tighten_threshold:
        events.append(
            _event(
                feature,
                "SPREAD_TIGHTENING",
                min(abs(spread_change) * Decimal("1000"), Decimal("100")),
                f"Spread tightened by {abs(spread_change)} over the lookback window.",
                severity="MEDIUM",
            )
        )
    if spread_change >= resolved_settings.microstructure_spread_widen_threshold:
        events.append(
            _event(
                feature,
                "SPREAD_WIDENING",
                min(spread_change * Decimal("1000"), Decimal("100")),
                "Spread widened, increasing paper execution risk.",
                severity="HIGH",
            )
        )
    if max_spread is not None and avg_spread is not None and max_spread >= avg_spread * 2:
        events.append(
            _event(
                feature,
                "SPREAD_SPIKE",
                Decimal("75"),
                "Spread spiked relative to its recent average.",
                severity="HIGH",
            )
        )
    if avg_spread is not None and current <= avg_spread and max_spread and max_spread > avg_spread:
        events.append(
            _event(
                feature,
                "SPREAD_NORMALIZED",
                Decimal("50"),
                "Spread has normalized versus the recent spike.",
                severity="INFO",
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
            "current_spread": str(feature.get("current_spread")),
            "avg_spread": str(feature.get("avg_spread")),
            "spread_change": str(feature.get("spread_change")),
        },
    }

