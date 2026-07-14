from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import SignalEvent
from kalshi_predictor.microstructure.repository import insert_microstructure_signal
from kalshi_predictor.signals.registry import ensure_builtin_signals
from kalshi_predictor.signals.signal_types import (
    LATE_MOVE_SIGNAL,
    LIQUIDITY_IMPROVEMENT_SIGNAL,
    MICROSTRUCTURE_SIGNAL,
    ORDERBOOK_IMBALANCE_SIGNAL,
    PRICE_DISLOCATION_SIGNAL,
    SMART_MONEY_HEURISTIC_SIGNAL,
    SPREAD_TIGHTENING_SIGNAL,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal
from kalshi_predictor.utils.time import utc_now

EVENT_SIGNAL_MAP = {
    "SPREAD_TIGHTENING": SPREAD_TIGHTENING_SIGNAL,
    "SPREAD_NORMALIZED": SPREAD_TIGHTENING_SIGNAL,
    "LIQUIDITY_IMPROVING": LIQUIDITY_IMPROVEMENT_SIGNAL,
    "LIQUIDITY_SPIKE": LIQUIDITY_IMPROVEMENT_SIGNAL,
    "YES_PRESSURE": ORDERBOOK_IMBALANCE_SIGNAL,
    "NO_PRESSURE": ORDERBOOK_IMBALANCE_SIGNAL,
    "IMBALANCE_FLIP": ORDERBOOK_IMBALANCE_SIGNAL,
    "PRICE_DISLOCATION_YES": PRICE_DISLOCATION_SIGNAL,
    "PRICE_DISLOCATION_NO": PRICE_DISLOCATION_SIGNAL,
    "MODEL_MARKET_DIVERGENCE": PRICE_DISLOCATION_SIGNAL,
    "LATE_YES_MOVE": LATE_MOVE_SIGNAL,
    "LATE_NO_MOVE": LATE_MOVE_SIGNAL,
    "LATE_VOLATILITY_SPIKE": LATE_MOVE_SIGNAL,
    "LATE_LIQUIDITY_SURGE": LATE_MOVE_SIGNAL,
    "SMART_MONEY_YES_PRESSURE": SMART_MONEY_HEURISTIC_SIGNAL,
    "SMART_MONEY_NO_PRESSURE": SMART_MONEY_HEURISTIC_SIGNAL,
    "POSSIBLE_INFORMED_FLOW": SMART_MONEY_HEURISTIC_SIGNAL,
}


def generate_microstructure_signals(
    session: Session,
    feature: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    resolved_settings = settings or get_settings()
    ensure_builtin_signals(session)
    generated: list[dict[str, Any]] = []
    if not resolved_settings.microstructure_enabled:
        return generated

    confidence = to_decimal(feature.get("microstructure_confidence")) or Decimal("0")
    if events:
        generated.append(
            _signal_payload(
                feature,
                signal_name=MICROSTRUCTURE_SIGNAL,
                strength=confidence,
                direction=_direction(feature),
                confidence=confidence,
                explanation="Market microstructure changed enough to create diagnostic signals.",
            )
        )
    for event in events:
        signal_name = EVENT_SIGNAL_MAP.get(str(event.get("event_type")))
        if signal_name is None:
            continue
        score = to_decimal(event.get("score")) or Decimal("0")
        generated.append(
            _signal_payload(
                feature,
                signal_name=signal_name,
                strength=score,
                direction=_direction(feature, event),
                confidence=min(confidence + Decimal("10"), Decimal("100")),
                explanation=str(event.get("description") or signal_name),
            )
        )

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for signal in generated:
        key = (signal["signal_name"], signal.get("signal_direction") or "neutral")
        existing = unique.get(key)
        if existing is None or to_decimal(signal["signal_strength"]) > to_decimal(
            existing["signal_strength"]
        ):
            unique[key] = signal

    rows: list[dict[str, Any]] = []
    for signal in unique.values():
        insert_microstructure_signal(session, signal)
        session.add(
            SignalEvent(
                created_at=signal["created_at"],
                ticker=signal["ticker"],
                signal_name=signal["signal_name"],
                model_name="microstructure_v1",
                signal_strength=decimal_to_str(signal["signal_strength"]) or "0",
                signal_value=signal["explanation"][:200],
                signal_direction=signal.get("signal_direction"),
                confidence=decimal_to_str(signal["confidence"]) or "0",
                raw_json=encode_json(signal),
            )
        )
        rows.append(signal)
    session.flush()
    return rows


def _signal_payload(
    feature: dict[str, Any],
    *,
    signal_name: str,
    strength: Decimal,
    direction: str | None,
    confidence: Decimal,
    explanation: str,
) -> dict[str, Any]:
    return {
        "created_at": feature.get("created_at") or utc_now(),
        "ticker": feature["ticker"],
        "signal_name": signal_name,
        "signal_strength": min(strength, Decimal("100")),
        "signal_direction": direction,
        "confidence": min(confidence, Decimal("100")),
        "explanation": explanation,
        "raw_json": {
            "feature": feature,
            "source": "microstructure_engine",
        },
    }


def _direction(feature: dict[str, Any], event: dict[str, Any] | None = None) -> str | None:
    event_type = str((event or {}).get("event_type") or "")
    if event_type.endswith("_YES") or "YES" in event_type:
        return "BUY_YES"
    if event_type.endswith("_NO") or "NO" in event_type:
        return "BUY_NO"
    imbalance = to_decimal(feature.get("orderbook_imbalance")) or Decimal("0")
    velocity = to_decimal(feature.get("price_velocity")) or Decimal("0")
    if imbalance > Decimal("0.25") or velocity > Decimal("0.03"):
        return "BUY_YES"
    if imbalance < Decimal("-0.25") or velocity < Decimal("-0.03"):
        return "BUY_NO"
    return "neutral"

