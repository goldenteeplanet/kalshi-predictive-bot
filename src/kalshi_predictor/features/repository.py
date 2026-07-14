from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.repositories import decode_json, encode_json
from kalshi_predictor.data.schema import Feature, FeatureSnapshot
from kalshi_predictor.utils.time import utc_now


def insert_features(
    session: Session,
    *,
    ticker: str,
    feature_set_name: str,
    features: Mapping[str, Any],
    raw_source: Mapping[str, Any] | None = None,
    source_timestamp: datetime | None = None,
    generated_at: datetime | None = None,
) -> Feature:
    now = utc_now()
    record = Feature(
        ticker=ticker,
        feature_set_name=feature_set_name,
        generated_at=generated_at or now,
        source_timestamp=source_timestamp,
        features_json=encode_json(dict(features)),
        raw_source_json=encode_json(dict(raw_source)) if raw_source is not None else None,
        created_at=now,
    )
    session.add(record)
    session.flush()
    return record


def get_latest_features_for_ticker(
    session: Session,
    ticker: str,
    *,
    feature_set_name: str | None = None,
    include_global: bool = True,
) -> Feature | None:
    tickers = [ticker]
    if include_global and ticker != "*":
        tickers.append("*")
    statement = select(Feature).where(Feature.ticker.in_(tickers))
    if feature_set_name is not None:
        statement = statement.where(Feature.feature_set_name == feature_set_name)
    return session.scalar(
        statement.order_by(desc(Feature.generated_at), desc(Feature.id)).limit(1)
    )


def get_features_for_backtest_window(
    session: Session,
    ticker: str,
    start_time: datetime,
    end_time: datetime,
    *,
    feature_set_name: str | None = None,
    include_global: bool = True,
) -> list[Feature]:
    tickers = [ticker]
    if include_global and ticker != "*":
        tickers.append("*")
    statement = select(Feature).where(
        Feature.ticker.in_(tickers),
        Feature.generated_at >= start_time,
        Feature.generated_at <= end_time,
    )
    if feature_set_name is not None:
        statement = statement.where(Feature.feature_set_name == feature_set_name)
    return list(session.scalars(statement.order_by(Feature.generated_at, Feature.id)))


def insert_feature_snapshot(
    session: Session,
    *,
    ticker: str,
    captured_at: datetime,
    market_features: Mapping[str, Any],
    external_features: Mapping[str, Any],
) -> FeatureSnapshot:
    combined = {
        "market": dict(market_features),
        "external": dict(external_features),
    }
    snapshot = FeatureSnapshot(
        ticker=ticker,
        captured_at=captured_at,
        market_features_json=encode_json(dict(market_features)),
        external_features_json=encode_json(dict(external_features)),
        combined_features_json=encode_json(combined),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def latest_feature_snapshot_for_ticker(
    session: Session,
    ticker: str,
) -> FeatureSnapshot | None:
    return session.scalar(
        select(FeatureSnapshot)
        .where(FeatureSnapshot.ticker == ticker)
        .order_by(desc(FeatureSnapshot.captured_at), desc(FeatureSnapshot.id))
        .limit(1)
    )


def feature_payload(record: Feature | None) -> dict[str, Any]:
    return decode_json(record.features_json if record is not None else None)


def snapshot_external_payload(snapshot: FeatureSnapshot | None) -> dict[str, Any]:
    return decode_json(snapshot.external_features_json if snapshot is not None else None)

