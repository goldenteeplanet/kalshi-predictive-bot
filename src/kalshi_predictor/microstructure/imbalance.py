from decimal import Decimal
from typing import Any

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.utils.decimals import to_decimal


def calculate_imbalance(
    yes_bid_depth: Decimal | None,
    no_bid_depth: Decimal | None,
) -> Decimal | None:
    yes_depth = yes_bid_depth or Decimal("0")
    no_depth = no_bid_depth or Decimal("0")
    total = yes_depth + no_depth
    if total <= 0:
        return None
    return (yes_depth - no_depth) / total


def detect_imbalance_events(
    feature: dict[str, Any],
    *,
    previous_feature: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    resolved_settings = settings or get_settings()
    imbalance = to_decimal(feature.get("orderbook_imbalance"))
    if imbalance is None:
        return []
    threshold = resolved_settings.microstructure_imbalance_threshold
    events: list[dict[str, Any]] = []
    if imbalance >= threshold:
        events.append(
            _event(
                feature,
                event_type="YES_PRESSURE",
                score=abs(imbalance) * Decimal("100"),
                description=(
                    "Orderbook pressure leans toward YES; this is not proof of informed flow."
                ),
            )
        )
    elif imbalance <= -threshold:
        events.append(
            _event(
                feature,
                event_type="NO_PRESSURE",
                score=abs(imbalance) * Decimal("100"),
                description=(
                    "Orderbook pressure leans toward NO; this is not proof of informed flow."
                ),
            )
        )
    else:
        events.append(
            _event(
                feature,
                event_type="BALANCED_BOOK",
                score=Decimal("35"),
                severity="INFO",
                description="YES and NO top-of-book depth are relatively balanced.",
            )
        )

    previous = to_decimal((previous_feature or {}).get("orderbook_imbalance"))
    if previous is not None and previous * imbalance < 0 and abs(imbalance - previous) >= threshold:
        events.append(
            _event(
                feature,
                event_type="IMBALANCE_FLIP",
                score=min(abs(imbalance - previous) * Decimal("100"), Decimal("100")),
                description="Orderbook pressure flipped direction over the lookback window.",
            )
        )
    return events


def imbalance_direction(imbalance: Decimal | None, threshold: Decimal) -> str:
    if imbalance is None:
        return "neutral"
    if imbalance >= threshold:
        return "BUY_YES"
    if imbalance <= -threshold:
        return "BUY_NO"
    return "neutral"


def _event(
    feature: dict[str, Any],
    *,
    event_type: str,
    score: Decimal,
    description: str,
    severity: str = "MEDIUM",
) -> dict[str, Any]:
    return {
        "ticker": feature["ticker"],
        "event_type": event_type,
        "severity": severity,
        "score": min(score, Decimal("100")),
        "title": event_type.replace("_", " ").title(),
        "description": description,
        "evidence": {
            "orderbook_imbalance": str(feature.get("orderbook_imbalance")),
            "yes_bid_depth": str(feature.get("yes_bid_depth")),
            "no_bid_depth": str(feature.get("no_bid_depth")),
        },
    }
