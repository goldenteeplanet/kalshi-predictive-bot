from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import (
    MetaModelDecision,
    MetaModelFeature,
    MetaModelPerformance,
    MetaModelTrainingExample,
)
from kalshi_predictor.utils.decimals import decimal_to_str
from kalshi_predictor.utils.time import utc_now


def insert_meta_model_feature(
    session: Session,
    row: Mapping[str, Any],
) -> MetaModelFeature:
    record = MetaModelFeature(
        created_at=row.get("created_at") or utc_now(),
        ticker=str(row["ticker"]),
        category=str(row.get("category") or "general"),
        market_type=_optional_str(row.get("market_type")),
        time_to_close_minutes=decimal_to_str(row.get("time_to_close_minutes")),
        liquidity_score=decimal_to_str(row.get("liquidity_score")),
        spread_score=decimal_to_str(row.get("spread_score")),
        data_freshness_score=decimal_to_str(row.get("data_freshness_score")),
        signal_count=int(row.get("signal_count") or 0),
        active_signals_json=encode_json(row.get("active_signals") or []),
        model_probabilities_json=encode_json(row.get("model_probabilities") or {}),
        model_disagreement_score=decimal_to_str(row.get("model_disagreement_score")),
        model_agreement_score=decimal_to_str(row.get("model_agreement_score")),
        model_recent_performance_json=encode_json(
            row.get("model_recent_performance") or {}
        ),
        category_performance_json=encode_json(row.get("category_performance") or {}),
        microstructure_features_json=encode_json(row.get("microstructure_features") or {}),
        news_features_json=encode_json(row.get("news_features") or {}),
        economic_features_json=encode_json(row.get("economic_features") or {}),
        sports_features_json=encode_json(row.get("sports_features") or {}),
        crypto_features_json=encode_json(row.get("crypto_features") or {}),
        weather_features_json=encode_json(row.get("weather_features") or {}),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def insert_meta_model_decision(
    session: Session,
    row: Mapping[str, Any],
) -> MetaModelDecision:
    record = MetaModelDecision(
        created_at=row.get("created_at") or utc_now(),
        ticker=str(row["ticker"]),
        selected_model_name=str(row["selected_model_name"]),
        selected_probability=decimal_to_str(row.get("selected_probability")),
        selected_confidence=decimal_to_str(row.get("selected_confidence")),
        fallback_model_name=_optional_str(row.get("fallback_model_name")),
        decision_reason=str(row.get("decision_reason") or ""),
        competing_models_json=encode_json(row.get("competing_models") or {}),
        trust_scores_json=encode_json(row.get("trust_scores") or {}),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def insert_meta_training_example(
    session: Session,
    row: Mapping[str, Any],
) -> MetaModelTrainingExample | None:
    existing = _existing_training_example(session, int(row["forecast_id"]))
    if existing is not None:
        return None
    record = MetaModelTrainingExample(
        created_at=row.get("created_at") or utc_now(),
        ticker=str(row["ticker"]),
        forecast_id=int(row["forecast_id"]),
        model_name=str(row["model_name"]),
        category=str(row.get("category") or "general"),
        market_type=_optional_str(row.get("market_type")),
        predicted_probability=decimal_to_str(row.get("predicted_probability")) or "0",
        settlement_result=str(row["settlement_result"]),
        absolute_error=decimal_to_str(row.get("absolute_error")) or "0",
        brier_loss=decimal_to_str(row.get("brier_loss")) or "0",
        was_best_model=1 if row.get("was_best_model") else 0,
        features_json=encode_json(row.get("features") or {}),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def insert_meta_performance(
    session: Session,
    row: Mapping[str, Any],
) -> MetaModelPerformance:
    record = MetaModelPerformance(
        generated_at=row.get("generated_at") or utc_now(),
        lookback_days=int(row.get("lookback_days") or 0),
        evaluated_count=int(row.get("evaluated_count") or 0),
        meta_brier_score=decimal_to_str(row.get("meta_brier_score")),
        ensemble_brier_score=decimal_to_str(row.get("ensemble_brier_score")),
        market_implied_brier_score=decimal_to_str(row.get("market_implied_brier_score")),
        meta_log_loss=decimal_to_str(row.get("meta_log_loss")),
        ensemble_log_loss=decimal_to_str(row.get("ensemble_log_loss")),
        market_implied_log_loss=decimal_to_str(row.get("market_implied_log_loss")),
        meta_roi=decimal_to_str(row.get("meta_roi")),
        ensemble_roi=decimal_to_str(row.get("ensemble_roi")),
        market_implied_roi=decimal_to_str(row.get("market_implied_roi")),
        notes=str(row.get("notes") or ""),
        raw_json=encode_json(dict(row.get("raw_json") or row)),
    )
    session.add(record)
    session.flush()
    return record


def latest_meta_feature(session: Session, ticker: str) -> MetaModelFeature | None:
    return session.scalar(
        select(MetaModelFeature)
        .where(MetaModelFeature.ticker == ticker)
        .order_by(desc(MetaModelFeature.created_at), desc(MetaModelFeature.id))
        .limit(1)
    )


def recent_meta_features(
    session: Session,
    *,
    limit: int = 100,
) -> list[MetaModelFeature]:
    return list(
        session.scalars(
            select(MetaModelFeature)
            .order_by(desc(MetaModelFeature.created_at), desc(MetaModelFeature.id))
            .limit(limit)
        )
    )


def latest_meta_decision(session: Session, ticker: str) -> MetaModelDecision | None:
    return session.scalar(
        select(MetaModelDecision)
        .where(MetaModelDecision.ticker == ticker)
        .order_by(desc(MetaModelDecision.created_at), desc(MetaModelDecision.id))
        .limit(1)
    )


def recent_meta_decisions(
    session: Session,
    *,
    limit: int = 100,
) -> list[MetaModelDecision]:
    return list(
        session.scalars(
            select(MetaModelDecision)
            .order_by(desc(MetaModelDecision.created_at), desc(MetaModelDecision.id))
            .limit(limit)
        )
    )


def recent_meta_training_examples(
    session: Session,
    *,
    limit: int = 100,
) -> list[MetaModelTrainingExample]:
    return list(
        session.scalars(
            select(MetaModelTrainingExample)
            .order_by(
                desc(MetaModelTrainingExample.created_at),
                desc(MetaModelTrainingExample.id),
            )
            .limit(limit)
        )
    )


def latest_meta_performance(session: Session) -> MetaModelPerformance | None:
    return session.scalar(
        select(MetaModelPerformance)
        .order_by(desc(MetaModelPerformance.generated_at), desc(MetaModelPerformance.id))
        .limit(1)
    )


def row_to_dict(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data: dict[str, Any] = {}
    for key in row.__mapper__.columns.keys():
        value = getattr(row, key)
        if isinstance(value, datetime):
            data[key] = value.isoformat()
        elif key.endswith("_json"):
            data[key] = decode_json(value) if isinstance(value, str) else value
        else:
            data[key] = value
    return data


def _existing_training_example(
    session: Session,
    forecast_id: int,
) -> MetaModelTrainingExample | None:
    for item in session.new:
        if (
            isinstance(item, MetaModelTrainingExample)
            and item.forecast_id == forecast_id
        ):
            return item
    return session.scalar(
        select(MetaModelTrainingExample)
        .where(MetaModelTrainingExample.forecast_id == forecast_id)
        .limit(1)
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
