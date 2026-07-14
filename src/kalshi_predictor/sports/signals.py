from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import SignalEvent, SportsFeature
from kalshi_predictor.signals.registry import ensure_builtin_signals
from kalshi_predictor.signals.signal_types import (
    INJURY_SIGNAL,
    MLB_SIGNAL,
    NBA_SIGNAL,
    NFL_SIGNAL,
    NHL_SIGNAL,
    ODDS_SIGNAL,
    REST_SIGNAL,
    SPORTS_SIGNAL,
    TEAM_STRENGTH_SIGNAL,
    TRAVEL_SIGNAL,
    WEATHER_SPORTS_SIGNAL,
)
from kalshi_predictor.sports.repository import insert_sports_signal, latest_sports_features
from kalshi_predictor.utils.decimals import decimal_to_str, to_decimal

LEAGUE_SIGNAL_NAMES = {
    "MLB": MLB_SIGNAL,
    "NBA": NBA_SIGNAL,
    "NFL": NFL_SIGNAL,
    "NHL": NHL_SIGNAL,
}

EDGE_SIGNAL_NAMES = {
    "team_strength_edge": TEAM_STRENGTH_SIGNAL,
    "injury_edge": INJURY_SIGNAL,
    "rest_edge": REST_SIGNAL,
    "odds_edge": ODDS_SIGNAL,
    "weather_edge": WEATHER_SPORTS_SIGNAL,
    "travel_edge": TRAVEL_SIGNAL,
}


@dataclass(frozen=True)
class SportsSignalSummary:
    features_scanned: int
    signals_created: int
    signal_events_created: int


def generate_sports_signals(
    session: Session,
    *,
    league: str = "ALL",
    settings: Settings | None = None,
) -> SportsSignalSummary:
    resolved = settings or get_settings()
    ensure_builtin_signals(session)
    features = [
        feature
        for feature in latest_sports_features(session, league=league)
        if feature.ticker is not None
    ]
    threshold = _threshold_score(resolved.sports_min_signal_confidence)
    signals_created = 0
    events_created = 0

    for feature in features:
        for payload in sports_signals_from_feature(feature):
            confidence = to_decimal(payload["confidence"]) or Decimal("0")
            if confidence < threshold:
                continue
            signal = insert_sports_signal(
                session,
                ticker=str(feature.ticker),
                league=feature.league,
                game_key=feature.game_key,
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
                    model_name=_model_name_for_league(signal.league),
                    signal_strength=signal.signal_strength,
                    signal_value=payload["signal_value"],
                    signal_direction=signal.signal_direction,
                    confidence=signal.confidence,
                    raw_json=encode_json({**payload, "source": "sports_signals"}),
                )
            )
            events_created += 1

    return SportsSignalSummary(
        features_scanned=len(features),
        signals_created=signals_created,
        signal_events_created=events_created,
    )


def sports_signals_from_feature(feature: SportsFeature) -> list[dict[str, Any]]:
    total_edge = to_decimal(feature.total_edge) or Decimal("0")
    confidence = _bounded_score(to_decimal(feature.confidence_score) or Decimal("0"))
    strength = _strength_from_edge(total_edge, fallback=confidence)
    direction = _direction_from_edge(total_edge)
    signals = [
        _payload(
            feature,
            signal_name=SPORTS_SIGNAL,
            strength=strength,
            confidence=confidence,
            direction=direction,
            signal_value=feature.total_edge,
            explanation=(
                f"Sports feature edge {feature.total_edge} with confidence "
                f"{feature.confidence_score}."
            ),
        ),
        _payload(
            feature,
            signal_name=LEAGUE_SIGNAL_NAMES.get(feature.league, SPORTS_SIGNAL),
            strength=strength,
            confidence=confidence,
            direction=direction,
            signal_value=feature.league,
            explanation=f"{feature.league} game intelligence is linked to this market.",
        ),
    ]
    for attr, signal_name in EDGE_SIGNAL_NAMES.items():
        edge = to_decimal(getattr(feature, attr)) or Decimal("0")
        if abs(edge) < Decimal("0.005"):
            continue
        signals.append(
            _payload(
                feature,
                signal_name=signal_name,
                strength=_strength_from_edge(edge, fallback=confidence),
                confidence=confidence,
                direction=_direction_from_edge(edge),
                signal_value=getattr(feature, attr),
                explanation=f"{signal_name} contributed edge {getattr(feature, attr)}.",
            )
        )
    return signals


def _payload(
    feature: SportsFeature,
    *,
    signal_name: str,
    strength: Decimal,
    confidence: Decimal,
    direction: str,
    signal_value: str,
    explanation: str,
) -> dict[str, Any]:
    return {
        "ticker": feature.ticker,
        "feature_id": feature.id,
        "league": feature.league,
        "game_key": feature.game_key,
        "signal_name": signal_name,
        "signal_strength": decimal_to_str(strength) or "0",
        "signal_direction": direction,
        "confidence": decimal_to_str(confidence) or "0",
        "signal_value": signal_value,
        "explanation": explanation,
    }


def _strength_from_edge(edge: Decimal, *, fallback: Decimal) -> Decimal:
    if edge == 0:
        return fallback
    return max(min(abs(edge) * Decimal("1000"), Decimal("100")), Decimal("10")).quantize(
        Decimal("0.0001")
    )


def _direction_from_edge(edge: Decimal) -> str:
    if edge > Decimal("0.005"):
        return "yes"
    if edge < Decimal("-0.005"):
        return "no"
    return "neutral"


def _threshold_score(value: Decimal) -> Decimal:
    if value <= Decimal("1"):
        return value * Decimal("100")
    return value


def _bounded_score(value: Decimal) -> Decimal:
    if value < Decimal("0"):
        return Decimal("0")
    if value > Decimal("100"):
        return Decimal("100")
    return value.quantize(Decimal("0.0001"))


def _model_name_for_league(league: str) -> str:
    return {
        "MLB": "mlb_v1",
        "NBA": "nba_v1",
        "NFL": "nfl_v1",
        "NHL": "nhl_v1",
    }.get(league, "sports_v1")

