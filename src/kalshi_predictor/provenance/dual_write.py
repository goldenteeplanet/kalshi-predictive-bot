from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import Forecast, MarketRanking, RuntimeProvenanceEvent
from kalshi_predictor.utils.time import utc_now


MODEL_VERSIONS = {"crypto_v2": "2.0.0", "weather_v2": "2.0.0", "sports_v1": "1.0.0"}


def capture_forecast_provenance(
    session: Session, forecast: Forecast, payload: Mapping[str, Any], *,
    market_snapshot_id: int | None, enabled: bool,
) -> RuntimeProvenanceEvent | None:
    if not enabled:
        return None
    feature = payload.get("feature_json") if isinstance(payload.get("feature_json"), Mapping) else {}
    table, feature_id = _feature_reference(forecast.model_name, feature)
    observation = _observation_reference(feature)
    return _append_event(
        session, stage="FORECAST_CREATED", forecast_id=forecast.id, ranking_id=None,
        market_snapshot_id=market_snapshot_id, ticker=forecast.ticker,
        model_name=forecast.model_name, model_version=MODEL_VERSIONS.get(forecast.model_name, "1.0.0"),
        observation=observation, feature_table=table, feature_id=feature_id,
        event_at=forecast.forecasted_at,
    )


def capture_ranking_provenance(
    session: Session, ranking: MarketRanking, payload: Mapping[str, Any], *, enabled: bool,
) -> RuntimeProvenanceEvent | None:
    if not enabled or payload.get("forecast_id") is None:
        return None
    forecast = session.get(Forecast, int(payload["forecast_id"]))
    if forecast is None:
        raise ValueError("ranking provenance forecast_id does not exist")
    feature = json.loads(forecast.feature_json or "{}")
    table, feature_id = _feature_reference(forecast.model_name, feature)
    return _append_event(
        session, stage="RANKING_CREATED", forecast_id=forecast.id, ranking_id=ranking.id,
        market_snapshot_id=_optional_int(payload.get("market_snapshot_id")),
        ticker=ranking.ticker, model_name=forecast.model_name,
        model_version=MODEL_VERSIONS.get(forecast.model_name, "1.0.0"),
        observation=_observation_reference(feature), feature_table=table,
        feature_id=feature_id, event_at=ranking.ranked_at,
    )


def _append_event(session: Session, *, stage: str, forecast_id: int,
                  ranking_id: int | None, market_snapshot_id: int | None, ticker: str,
                  model_name: str, model_version: str, observation: dict[str, Any] | None,
                  feature_table: str | None, feature_id: int | None,
                  event_at: datetime) -> RuntimeProvenanceEvent:
    previous = session.scalar(select(RuntimeProvenanceEvent.provenance_digest).where(
        RuntimeProvenanceEvent.forecast_id == forecast_id
    ).order_by(desc(RuntimeProvenanceEvent.id)).limit(1)) or "GENESIS"
    raw = {"stage": stage, "forecast_id": forecast_id, "ranking_id": ranking_id,
           "market_snapshot_id": market_snapshot_id, "ticker": ticker,
           "model_name": model_name, "model_version": model_version,
           "source_observation_ref": observation, "feature_source_table": feature_table,
           "feature_source_id": feature_id, "event_at": event_at.isoformat(),
           "previous_digest": previous}
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    event = RuntimeProvenanceEvent(
        event_key=f"{stage}:{forecast_id}:{ranking_id if ranking_id is not None else '-'}",
        stage=stage, forecast_id=forecast_id, ranking_id=ranking_id,
        market_snapshot_id=market_snapshot_id, ticker=ticker, model_name=model_name,
        model_version=model_version,
        source_observation_ref_json=(json.dumps(observation, sort_keys=True) if observation else None),
        feature_source_table=feature_table, feature_source_id=feature_id,
        event_at=event_at or utc_now(), previous_digest=previous,
        provenance_digest=digest, raw_json=json.dumps(raw, sort_keys=True),
    )
    session.add(event)
    session.flush()
    return event


def _feature_reference(model: str, payload: Mapping[str, Any]) -> tuple[str | None, int | None]:
    if model == "crypto_v2":
        return "crypto_features", _optional_int(payload.get("crypto_feature_id"))
    if model == "sports_v1":
        return "sports_features", _optional_int(payload.get("sports_feature_id"))
    if model == "weather_v2":
        return "weather_features", _optional_int(payload.get("weather_feature_id"))
    return None, None


def _observation_reference(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    for key in ("source_observation_ref", "observation_reference"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return None


def _optional_int(value: Any) -> int | None:
    return int(value) if value not in (None, "") else None
