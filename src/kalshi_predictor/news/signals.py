from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import NewsFeature, SignalEvent
from kalshi_predictor.news.repository import insert_news_signal, latest_news_features
from kalshi_predictor.signals.registry import ensure_builtin_signals
from kalshi_predictor.signals.signal_types import (
    BREAKING_NEWS_SIGNAL,
    CRYPTO_NEWS_SIGNAL,
    ECONOMIC_NEWS_SIGNAL,
    NEWS_SIGNAL,
    SPORTS_NEWS_SIGNAL,
    WEATHER_NEWS_SIGNAL,
)
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal

CATEGORY_SIGNAL_NAMES = {
    "economic": ECONOMIC_NEWS_SIGNAL,
    "crypto": CRYPTO_NEWS_SIGNAL,
    "weather": WEATHER_NEWS_SIGNAL,
    "sports": SPORTS_NEWS_SIGNAL,
}


@dataclass(frozen=True)
class NewsSignalSummary:
    features_scanned: int
    signals_created: int
    signal_events_created: int


def generate_news_signals(session: Session) -> NewsSignalSummary:
    ensure_builtin_signals(session)
    features = latest_news_features(session)
    signals_created = 0
    events_created = 0
    for feature in features:
        for payload in news_signals_from_feature(feature):
            signal = insert_news_signal(
                session,
                ticker=feature.ticker,
                signal_name=payload["signal_name"],
                signal_strength=payload["signal_strength"],
                signal_direction=payload["signal_direction"],
                confidence=payload["confidence"],
                explanation=payload["explanation"],
                raw_json=payload,
            )
            signals_created += 1
            session.add(
                SignalEvent(
                    created_at=signal.created_at,
                    ticker=signal.ticker,
                    signal_name=signal.signal_name,
                    model_name="news_v1",
                    signal_strength=signal.signal_strength,
                    signal_value=payload["signal_value"],
                    signal_direction=signal.signal_direction,
                    confidence=signal.confidence,
                    raw_json=encode_json({**payload, "source": "news_signals"}),
                )
            )
            events_created += 1
    return NewsSignalSummary(
        features_scanned=len(features),
        signals_created=signals_created,
        signal_events_created=events_created,
    )


def news_signals_from_feature(feature: NewsFeature) -> list[dict[str, Any]]:
    category_counts = decode_json(feature.category_counts_json)
    sentiment = to_decimal(feature.avg_sentiment) or Decimal("0")
    max_importance = to_decimal(feature.max_importance) or Decimal("0")
    freshness = to_decimal(feature.freshness_score) or Decimal("0")
    strength = _signal_strength(
        news_count=feature.news_count,
        max_importance=max_importance,
        freshness=freshness,
    )
    confidence = _signal_confidence(
        news_count=feature.news_count,
        max_importance=max_importance,
        freshness=freshness,
    )
    direction = _direction_from_sentiment(sentiment)
    base_payload = _payload(
        feature,
        signal_name=NEWS_SIGNAL,
        strength=strength,
        confidence=confidence,
        direction=direction,
        explanation=(
            f"{feature.news_count} recent news item(s) are linked to this market; "
            f"average sentiment {feature.avg_sentiment or '0'}."
        ),
    )
    signals = [base_payload]
    if feature.high_importance_count > 0 or max_importance >= Decimal("0.75"):
        signals.append(
            _payload(
                feature,
                signal_name=BREAKING_NEWS_SIGNAL,
                strength=max(strength, Decimal("75")),
                confidence=confidence,
                direction=direction,
                explanation=(
                    "High-importance or breaking news is linked to this market "
                    f"within the {feature.feature_window_minutes} minute window."
                ),
            )
        )
    for category, signal_name in CATEGORY_SIGNAL_NAMES.items():
        count = int(category_counts.get(category) or 0)
        if count <= 0:
            continue
        signals.append(
            _payload(
                feature,
                signal_name=signal_name,
                strength=strength,
                confidence=confidence,
                direction=direction,
                explanation=(
                    f"{count} {category} news item(s) are linked to this market."
                ),
            )
        )
    return signals


def _payload(
    feature: NewsFeature,
    *,
    signal_name: str,
    strength: Decimal,
    confidence: Decimal,
    direction: str,
    explanation: str,
) -> dict[str, Any]:
    return {
        "ticker": feature.ticker,
        "feature_id": feature.id,
        "signal_name": signal_name,
        "signal_strength": decimal_to_str(strength) or "0",
        "signal_direction": direction,
        "confidence": decimal_to_str(confidence) or "0",
        "signal_value": f"{feature.news_count} news items",
        "explanation": explanation,
    }


def _signal_strength(
    *,
    news_count: int,
    max_importance: Decimal,
    freshness: Decimal,
) -> Decimal:
    count_factor = min(Decimal(news_count) / Decimal("5"), Decimal("1"))
    value = (
        max_importance * Decimal("45")
        + freshness * Decimal("35")
        + count_factor * Decimal("20")
    )
    return _clamp_score(value)


def _signal_confidence(
    *,
    news_count: int,
    max_importance: Decimal,
    freshness: Decimal,
) -> Decimal:
    sample = min(Decimal(news_count) / Decimal("3"), Decimal("1"))
    value = (
        freshness * Decimal("45")
        + max_importance * Decimal("35")
        + sample * Decimal("20")
    )
    return _clamp_score(value)


def _direction_from_sentiment(sentiment: Decimal) -> str:
    if sentiment >= Decimal("0.05"):
        return "positive"
    if sentiment <= Decimal("-0.05"):
        return "negative"
    return "neutral"


def _clamp_score(value: Decimal) -> Decimal:
    if value < Decimal("0"):
        return Decimal("0")
    if value > Decimal("100"):
        return Decimal("100")
    return value.quantize(Decimal("0.0001"))
