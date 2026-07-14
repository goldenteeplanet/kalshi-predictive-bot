from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import ForecastMemory
from kalshi_predictor.memory.contracts import DATA_MODE_AS_OBSERVED
from kalshi_predictor.utils.time import parse_datetime, utc_now


@dataclass(frozen=True)
class MemoryDataset:
    manifest: dict[str, Any]
    rows: list[dict[str, Any]]


def build_forecast_learning_dataset(
    session: Session,
    *,
    training_as_of: datetime,
    data_mode: str = DATA_MODE_AS_OBSERVED,
    include_no_trade: bool = True,
    include_risk_blocked: bool = True,
) -> MemoryDataset:
    cutoff = _require_as_of(training_as_of)
    rows = list(
        session.scalars(
            select(ForecastMemory)
            .where(ForecastMemory.event_type == "FORECAST_OUTCOME_FINALIZED")
            .where(ForecastMemory.forecast_outcome_status == "FINAL")
            .where(ForecastMemory.label_available_at.is_not(None))
            .where(ForecastMemory.label_available_at <= cutoff)
            .order_by(ForecastMemory.event_time, ForecastMemory.forecast_id)
        )
    )
    output = []
    for row in rows:
        timeline = _timeline(session, row.forecast_id, cutoff=cutoff)
        statuses = {event.decision_status for event in timeline if event.decision_status}
        if not include_no_trade and "NO_TRADE" in statuses:
            continue
        if not include_risk_blocked and "RISK_BLOCKED" in statuses:
            continue
        created = next(
            (event for event in timeline if event.event_type == "FORECAST_CREATED"), None
        )
        output.append(
            {
                "forecast_id": row.forecast_id,
                "instrument_id": row.instrument_id,
                "primary_model_id": row.primary_model_id,
                "primary_model_version": row.primary_model_version,
                "forecast_generated_at": (
                    created.forecast_generated_at.isoformat()
                    if created and created.forecast_generated_at
                    else None
                ),
                "predicted_probability": (
                    created.predicted_probability if created else row.predicted_probability
                ),
                "actual_value": row.actual_value,
                "brier_component": row.brier_component,
                "direction_correct": bool(row.direction_correct)
                if row.direction_correct is not None
                else None,
                "label_available_at": row.label_available_at.isoformat()
                if row.label_available_at
                else None,
                "decision_statuses": sorted(status for status in statuses if status),
                "data_quality_flags": _json_value(row.data_quality_flags_json, []),
                "feature_lineage": _json_value(created.feature_lineage_json, {}) if created else {},
            }
        )
    manifest = {
        "dataset_type": "forecast_learning",
        "generated_at": utc_now().isoformat(),
        "training_as_of": cutoff.isoformat(),
        "data_mode": data_mode,
        "include_no_trade_forecasts": include_no_trade,
        "include_risk_blocked_forecasts": include_risk_blocked,
        "exclude_preliminary_outcomes": True,
        "exclude_open_trades": True,
        "row_count": len(output),
    }
    return MemoryDataset(manifest=manifest, rows=output)


def _timeline(session: Session, forecast_id: str, *, cutoff: datetime) -> list[ForecastMemory]:
    return list(
        session.scalars(
            select(ForecastMemory)
            .where(ForecastMemory.forecast_id == forecast_id)
            .where(ForecastMemory.recorded_at <= cutoff)
            .order_by(ForecastMemory.event_sequence, ForecastMemory.recorded_at)
        )
    )


def _require_as_of(value: datetime) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError("training_as_of is required for Phase 3O learning datasets.")
    return parsed


def _json_value(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
